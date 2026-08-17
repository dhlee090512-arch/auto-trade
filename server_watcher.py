import os
import sys
import time
import json
import base64
import logging
import asyncio
import threading
import subprocess
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

# ==========================================
# 0. 환경 변수 및 전역 설정
# ==========================================
TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
TELEGRAM_CHAT_ID = str(os.getenv("TELEGRAM_CHAT_ID") or "").strip()
GH_TOKEN = os.getenv("GH_TOKEN2") or os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")

GITHUB_REPOSITORY = "dhlee090512-arch/auto-trade"
TARGETS_API_URL = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/contents/targets.json"

STATE_FILE = "server_state.json"
PAPER_TRADES_FILE = "paper_trades.json"
PROJECT_DIR = "/home/ubuntu/auto-trade"

EMERGENCY_STOP = False
LAST_TELEGRAM_UPDATE_ID = 0
ENTRY_TIMEOUT_MINUTES = 20   # ⏰ 20분 내 미체결 시 취소/폐기
TIME_EXIT_HOURS = 3          # ⏰ 3시간 도달 실패 시 시장가 강제 청산

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler("watcher.log"),
        logging.StreamHandler(sys.stdout)
    ]
)

# ==========================================
# 1. 텔레그램 유틸리티 (손익액 단독 표기 개선)
# ==========================================
def send_telegram_msg(msg: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("⚠️ 텔레그램 설정 누락으로 발송 건너뜀")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg}
    try:
        res = requests.post(url, json=payload, timeout=8)
        if res.status_code != 200:
            logging.error(f"텔레그램 전송 실패 ({res.status_code}): {res.text}")
    except Exception as e:
        logging.error(f"텔레그램 발송 오류: {e}")

def format_recent_trades_summary(closed_trades):
    """최근 10건 승률 및 총 실현 손익액 표기 (수익률 합산 왜곡 제거)"""
    if not closed_trades:
        return "[최근 매도 이력]\n매도 이력이 없습니다.\n최근 10건 승률 : 0%\n최근 10건 실현 손익 : +0 KRW"
    
    recent_10 = closed_trades[-10:]
    lines = ["[최근 매도 이력]"]
    wins = 0
    total_profit_krw = 0
    
    for idx, t in enumerate(recent_10, 1):
        p_pct = t.get('profit_pct', 0.0)
        p_krw = t.get('profit_krw', 0)
        symbol = t.get('symbol', 'UNKNOWN')
        exit_time_str = t.get('exit_time', '')
        try:
            formatted_time = datetime.fromisoformat(exit_time_str).strftime("%y/%m/%d %H:%M")
        except:
            formatted_time = "-"
            
        sign_pct = "+" if p_pct > 0 else ""
        lines.append(f"{idx}. {symbol} ({sign_pct}{p_pct:.1f}%) {formatted_time}")
        if p_pct > 0:
            wins += 1
        total_profit_krw += p_krw

    win_rate = round((wins / len(recent_10)) * 100) if recent_10 else 0
    sign_krw = "+" if total_profit_krw > 0 else ""
    lines.append(f"최근 10건 승률 : {win_rate}%")
    lines.append(f"최근 10건 실현 손익 : {sign_krw}{total_profit_krw:,} KRW")
    return "\n".join(lines)

# ==========================================
# 2. 로컬 상태 파일 및 빗썸/GitHub 연동
# ==========================================
def load_json_file(file_path, default_value):
    if os.path.exists(file_path):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default_value

def save_json_file(file_path, data):
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def get_current_price(coin_code: str) -> float:
    try:
        url = f"https://api.bithumb.com/public/ticker/{coin_code}_KRW"
        res = requests.get(url, timeout=3).json()
        if res.get("status") == "0000":
            return float(res["data"]["closing_price"])
    except Exception:
        pass
    return None

def fetch_latest_targets():
    """GitHub API를 통해 캐시 지연 없이 실시간 최신 targets.json 조회"""
    headers = {"Accept": "application/vnd.github.v3+json"}
    if GH_TOKEN:
        headers["Authorization"] = f"Bearer {GH_TOKEN}"
    try:
        res = requests.get(TARGETS_API_URL, headers=headers, timeout=5)
        if res.status_code == 200:
            content_b64 = res.json().get('content', '')
            decoded = base64.b64decode(content_b64).decode('utf-8')
            return json.loads(decoded)
    except Exception as e:
        logging.error(f"targets.json 실시간 API 조회 실패: {e}")
    return None

# ==========================================
# 3. 텔레그램 실시간 리스너 (독립 스레드 구동)
# ==========================================
def telegram_listener_thread():
    global EMERGENCY_STOP, LAST_TELEGRAM_UPDATE_ID
    if not TELEGRAM_BOT_TOKEN:
        logging.warning("⚠️ TELEGRAM_BOT_TOKEN 없음. 리스너 중단.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    logging.info(f"📱 텔레그램 인터락 리스너 가동 (Chat ID: {TELEGRAM_CHAT_ID})")

    try:
        init_res = requests.get(url, params={"timeout": 1}, timeout=5).json()
        if init_res.get("ok") and init_res.get("result"):
            LAST_TELEGRAM_UPDATE_ID = init_res["result"][-1]["update_id"]
    except Exception:
        pass

    while True:
        try:
            params = {"offset": LAST_TELEGRAM_UPDATE_ID + 1, "timeout": 10}
            res = requests.get(url, params=params, timeout=15).json()
            
            if res.get("ok"):
                for update in res.get("result", []):
                    LAST_TELEGRAM_UPDATE_ID = update["update_id"]
                    msg = update.get("message", {})
                    text = msg.get("text", "").strip()
                    sender_chat_id = str(msg.get("chat", {}).get("id", "")).strip()

                    if TELEGRAM_CHAT_ID and sender_chat_id != TELEGRAM_CHAT_ID:
                        logging.warning(f"⚠️ 미인증 접근 무시 (ID: {sender_chat_id})")
                        continue

                    if not text:
                        continue

                    logging.info(f"📩 [텔레그램 수신] 명령어: '{text}'")

                    if text == "/status":
                        server_state = load_json_file(STATE_FILE, {"pending_targets": {}, "last_updated": "-"})
                        paper_db = load_json_file(PAPER_TRADES_FILE, {"active_positions": {}, "closed_trades": []})
                        
                        held = [v['symbol'] for v in paper_db.get('active_positions', {}).values()]
                        pending = list(server_state.get('pending_targets', {}).keys())
                        status_str = "🛑 일시정지 (STOP)" if EMERGENCY_STOP else "🟢 정상 감시 중 (RUNNING)"

                        res_msg = f"""[시스템 상태 보고]
• 인터락 상태: {status_str}
• 마지막 타점 갱신: {server_state.get('last_updated', '-')}
• 진입 대기 종목: {', '.join(pending) if pending else '(없음)'}
• 현재 보유 종목: {', '.join(held) if held else '(없음)'}
• 누적 복기 거래수: {len(paper_db.get('closed_trades', []))}건"""
                        send_telegram_msg(res_msg)

                    elif text == "/stop":
                        EMERGENCY_STOP = True
                        send_telegram_msg("🛑 [인터락 작동] 신규 매수 감시가 일시 중단되었습니다. (보유 포지션 익/손절 방어는 유지)")

                    elif text == "/start":
                        EMERGENCY_STOP = False
                        send_telegram_msg("▶️ [인터락 해제] 신규 매수 감시 및 자동 매매가 정상 재개되었습니다.")

                    elif text == "/panic":
                        EMERGENCY_STOP = True
                        paper_db = load_json_file(PAPER_TRADES_FILE, {"active_positions": {}, "closed_trades": []})
                        paper_db["active_positions"] = {}
                        save_json_file(PAPER_TRADES_FILE, paper_db)
                        send_telegram_msg("🚨 [PANIC] 보유 포지션이 전량 초기화되었습니다.")

                    elif text == "/update":
                        send_telegram_msg("🔄 [원격 업데이트] GitHub에서 최신 코드를 다운로드합니다...")
                        try:
                            result = subprocess.run(
                                ["git", "pull", "origin", "main"],
                                cwd=PROJECT_DIR,
                                capture_output=True,
                                text=True,
                                timeout=20
                            )
                            log_output = result.stdout.strip()
                            err_output = result.stderr.strip()
                            
                            if result.returncode != 0:
                                send_telegram_msg(f"❌ [Git Pull 에러]\n{err_output}")
                                continue

                            send_telegram_msg(f"✅ [Git Pull 완료]\n`{log_output}`\n\n⚡ 봇 서비스를 재시작합니다...")
                            
                            def do_restart():
                                time.sleep(1.5)
                                subprocess.run(["sudo", "systemctl", "restart", "autotrade.service"])

                            threading.Thread(target=do_restart, daemon=True).start()

                        except Exception as e:
                            send_telegram_msg(f"❌ [업데이트 실패] {e}")

                    elif text == "/log":
                        if os.path.exists("watcher.log"):
                            with open("watcher.log", "r", encoding="utf-8") as f:
                                lines = f.readlines()[-10:]
                                send_telegram_msg("📜 [최근 서버 로그]\n" + "".join(lines))
                        else:
                            send_telegram_msg("로그 파일이 아직 없습니다.")

            time.sleep(1)
        except Exception as e:
            logging.error(f"텔레그램 리스너 루프 에러: {e}")
            time.sleep(3)

# ==========================================
# 4. 실시간 감시 및 타임어택 주문 집행 엔진
# ==========================================
async def realtime_execution_engine():
    global EMERGENCY_STOP
    logging.info("⚡ 오라클 실시간 감시 & 체결 엔진 구동 시작")
    last_sync_time = 0

    t = threading.Thread(target=telegram_listener_thread, daemon=True)
    t.start()

    while True:
        try:
            now = time.time()
            now_dt = datetime.now()
            server_state = load_json_file(STATE_FILE, {"pending_targets": {}, "last_updated": ""})
            paper_db = load_json_file(PAPER_TRADES_FILE, {"active_positions": {}, "closed_trades": []})

            # 30초마다 GitHub API로 targets.json 실시간 동기화
            if now - last_sync_time >= 30:
                data = fetch_latest_targets()
                if data and data.get("updated_at") != server_state.get("last_updated"):
                    server_state["last_updated"] = data.get("updated_at")
                    
                    new_pending = {}
                    for coin_code, plan in data.get("targets", {}).items():
                        if coin_code not in paper_db.get("active_positions", {}):
                            if "created_at" not in plan:
                                plan["created_at"] = data.get("updated_at", now_dt.isoformat())
                            new_pending[coin_code] = plan
                    
                    server_state["pending_targets"] = new_pending
                    save_json_file(STATE_FILE, server_state)
                    logging.info(f"🔄 targets.json 동기화 완료: 대기 종목 갱신 ({list(new_pending.keys())})")
                last_sync_time = now

            pending = server_state.get("pending_targets", {})
            active_positions = paper_db.get("active_positions", {})
            closed_trades = paper_db.get("closed_trades", [])

            # [1] 진입 대기 감시 (20분 타임어택 & 실제 체결가 기준 재계산)
            if not EMERGENCY_STOP:
                for coin_code, plan in list(pending.items()):
                    created_time_str = plan.get("created_at", now_dt.isoformat())
                    try:
                        created_dt = datetime.fromisoformat(created_time_str)
                    except:
                        created_dt = now_dt
                        
                    if now_dt - created_dt >= timedelta(minutes=ENTRY_TIMEOUT_MINUTES):
                        logging.info(f"⌛ [{plan['symbol']}] 20분 내 미체결로 대기 주문 취소(폐기)")
                        del pending[coin_code]
                        save_json_file(STATE_FILE, server_state)
                        continue

                    curr_p = get_current_price(coin_code)
                    if not curr_p:
                        continue

                    # 목표 진입가 이하 도달 시 매수 체결
                    if curr_p <= plan["target_entry"]:
                        logging.info(f"🎯 [{plan['symbol']}] 진입 타점 도달! 매수 체결 진행")
                        
                        # 💡 [핵심] 실제 체결가(curr_p)를 기준으로 손절/익절 절대 가격 재계산
                        real_entry_price = curr_p
                        exact_stop_loss = round(real_entry_price * (1.0 + (plan["sl_pct"] / 100.0)), 2 if real_entry_price < 100 else 0)
                        exact_take_profit = round(real_entry_price * (1.0 + (plan["tp_pct"] / 100.0)), 2 if real_entry_price < 100 else 0)

                        active_positions[coin_code] = {
                            "symbol": plan["symbol"],
                            "entry_price": real_entry_price,
                            "stop_loss": exact_stop_loss,
                            "take_profit": exact_take_profit,
                            "buy_amount_krw": plan["buy_amount_krw"],
                            "sl_pct": plan["sl_pct"],
                            "tp_pct": plan["tp_pct"],
                            "detailed_reason": plan["detailed_reason"],
                            "entry_time": now_dt.isoformat()
                        }
                        del pending[coin_code]
                        paper_db["active_positions"] = active_positions
                        save_json_file(PAPER_TRADES_FILE, paper_db)
                        save_json_file(STATE_FILE, server_state)

                        held_str = "\n".join([f"- {v['symbol']}" for v in active_positions.values()])
                        recent_summary = format_recent_trades_summary(closed_trades)
                        tp_sign = "+" if plan['tp_pct'] > 0 else ""

                        buy_msg = f"""[체결 완료] - 모의투자
종목 : {plan['symbol']}
진입가 : {real_entry_price:,.0f} KRW

익절 목표 : {exact_take_profit:,.0f} KRW ({tp_sign}{plan['tp_pct']}%)
손절 목표 : {exact_stop_loss:,.0f} KRW ({plan['sl_pct']}%)

매수 근거 :
{plan['detailed_reason']}
=================================
[현재 보유종목]
{held_str}
=================================
{recent_summary}"""
                        send_telegram_msg(buy_msg)

            # [2] 보유 포지션 실시간 감시 (익절 / 손절 / 3시간 시간손절)
            for coin_code, pos in list(active_positions.items()):
                curr_p = get_current_price(coin_code)
                if not curr_p:
                    continue

                entry_p = pos["entry_price"]
                sl_p = pos["stop_loss"]
                tp_p = pos["take_profit"]
                entry_time = datetime.fromisoformat(pos["entry_time"])

                status = "HOLDING"
                exit_price = 0.0
                exit_reason = ""

                if curr_p >= tp_p:
                    status = "CLOSED_TAKE_PROFIT"
                    exit_price = curr_p
                    exit_reason = "익절 목표가 달성"
                elif curr_p <= sl_p:
                    status = "CLOSED_STOP_LOSS"
                    exit_price = curr_p
                    exit_reason = "스탑로스 도달 (손절)"
                elif now_dt - entry_time >= timedelta(hours=TIME_EXIT_HOURS):
                    status = "CLOSED_TIME_EXIT"
                    exit_price = curr_p
                    exit_reason = f"{TIME_EXIT_HOURS}시간 횡보로 시간 손절(시장가 강제 청산)"

                if status != "HOLDING":
                    profit_pct = round(((exit_price - entry_p) / entry_p) * 100, 2)
                    profit_krw = round(pos["buy_amount_krw"] * (profit_pct / 100.0))

                    closed_trades.append({
                        "symbol": pos["symbol"],
                        "entry_price": entry_p,
                        "exit_price": exit_price,
                        "buy_amount_krw": pos["buy_amount_krw"],
                        "profit_krw": profit_krw,
                        "profit_pct": profit_pct,
                        "status": status,
                        "reason": exit_reason,
                        "entry_time": pos["entry_time"],
                        "exit_time": now_dt.isoformat()
                    })

                    del active_positions[coin_code]
                    paper_db["active_positions"] = active_positions
                    paper_db["closed_trades"] = closed_trades
                    save_json_file(PAPER_TRADES_FILE, paper_db)

                    remaining_held = [v['symbol'] for v in active_positions.values()]
                    held_str = "\n".join([f"- {s}" for s in remaining_held]) if remaining_held else "- (없음)"
                    recent_summary = format_recent_trades_summary(closed_trades)
                    sign_pct = "+" if profit_pct > 0 else ""
                    sign_krw = "+" if profit_krw > 0 else ""

                    exit_msg = f"""[청산 완료] - 모의투자
종목 : {pos['symbol']}
진입가 : {entry_p:,.0f} KRW ➔ 청산가 : {exit_price:,.0f} KRW
손익 : {sign_krw}{profit_krw:,} KRW ({sign_pct}{profit_pct:.1f}%)

청산 사유 : {exit_reason}
=================================
[현재 보유종목]
{held_str}
=================================
{recent_summary}"""
                    send_telegram_msg(exit_msg)

            await asyncio.sleep(2)
        except Exception as e:
            logging.error(f"감시 루프 오류: {e}")
            await asyncio.sleep(5)

# ==========================================
# 메인 시작점
# ==========================================
if __name__ == "__main__":
    send_telegram_msg(
        "🚀 [오라클 서버] 실시간 감시 & 원격 업데이트 엔진 가동\n\n"
        "📱 사용 가능한 명령어:\n"
        "• /status : 시스템 상태 및 포지션 확인\n"
        "• /update : GitHub 최신 코드 동기화 후 재시작\n"
        "• /log : 최근 실행 로그 10줄 확인\n"
        "• /stop : 신규 매수 일시정지\n"
        "• /start : 매매 재개\n"
        "• /panic : 전량 청산 및 비상 정지"
    )
    asyncio.run(realtime_execution_engine())
