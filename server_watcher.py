import os
import time
import json
import logging
import asyncio
import requests
import jwt
import uuid
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GH_TOKEN = os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
BITHUMB_API_KEY = os.getenv("BITHUMB_API_KEY")
BITHUMB_SECRET_KEY = os.getenv("BITHUMB_SECRET_KEY")

GITHUB_REPOSITORY = "dhlee090512-arch/auto-trade"
TARGETS_RAW_URL = f"https://raw.githubusercontent.com/{GITHUB_REPOSITORY}/main/targets.json"

STATE_FILE = "server_state.json"
PAPER_TRADES_FILE = "paper_trades.json"

EMERGENCY_STOP = False
LAST_TELEGRAM_UPDATE_ID = 0
TIME_EXIT_HOURS = 3

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.FileHandler("watcher.log"), logging.StreamHandler()]
)

def send_telegram_msg(msg: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=8)
    except Exception as e:
        logging.error(f"텔레그램 발송 에러: {e}")

def format_recent_trades_summary(closed_trades):
    if not closed_trades:
        return "[최근 매도 이력]\n매도 이력이 없습니다.\n최근 10건 승률 : 0%\n최근 10건 손익 : +0KRW (+0.0%)"
    
    recent_10 = closed_trades[-10:]
    lines = ["[최근 매도 이력]"]
    wins = 0
    total_profit_krw = 0
    total_profit_pct = 0.0
    
    for idx, t in enumerate(recent_10, 1):
        p_pct = t.get('profit_pct', 0.0)
        p_krw = t.get('profit_krw', 0)
        symbol = t.get('symbol', 'UNKNOWN')
        exit_time_str = t.get('exit_time', '')
        try:
            dt = datetime.fromisoformat(exit_time_str)
            formatted_time = dt.strftime("%y/%m/%d %H:%M")
        except:
            formatted_time = "-"
            
        sign = "+" if p_pct > 0 else ""
        lines.append(f"{idx}. {symbol} ({sign}{p_pct:.1f}%) {formatted_time}")
        if p_pct > 0:
            wins += 1
        total_profit_krw += p_krw
        total_profit_pct += p_pct

    win_rate = round((wins / len(recent_10)) * 100) if recent_10 else 0
    sign_krw = "+" if total_profit_krw > 0 else ""
    sign_pct = "+" if total_profit_pct > 0 else ""
    lines.append(f"최근 10건 승률 : {win_rate}%")
    lines.append(f"최근 10건 손익 : {sign_krw}{total_profit_krw:,}KRW ({sign_pct}{total_profit_pct:.1f}%)")
    return "\n".join(lines)

def load_json_file(file_path, default_value):
    if os.path.exists(file_path):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return default_value
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
    headers = {}
    if GH_TOKEN:
        headers["Authorization"] = f"token {GH_TOKEN}"
    try:
        url = f"{TARGETS_RAW_URL}?_={int(time.time())}"
        res = requests.get(url, headers=headers, timeout=5)
        if res.status_code == 200:
            return res.json()
    except Exception as e:
        logging.error(f"targets.json 동기화 에러: {e}")
    return None

async def telegram_command_listener():
    """텔레그램 인터락 리스너 (/status, /stop, /start, /panic)"""
    global EMERGENCY_STOP, LAST_TELEGRAM_UPDATE_ID
    if not TELEGRAM_BOT_TOKEN:
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    logging.info("📱 텔레그램 원격 명령어 리스너 활성화")

    while True:
        try:
            params = {"offset": LAST_TELEGRAM_UPDATE_ID + 1, "timeout": 5}
            res = requests.get(url, params=params, timeout=10).json()
            if res.get("ok"):
                for update in res.get("result", []):
                    LAST_TELEGRAM_UPDATE_ID = update["update_id"]
                    msg = update.get("message", {})
                    text = msg.get("text", "").strip()
                    chat_id = str(msg.get("chat", {}).get("id", ""))

                    if chat_id != TELEGRAM_CHAT_ID:
                        continue

                    if text == "/status":
                        server_state = load_json_file(STATE_FILE, {"pending_targets": {}, "active_positions": {}})
                        paper_db = load_json_file(PAPER_TRADES_FILE, {"active_positions": {}, "closed_trades": []})
                        
                        held_list = [v['symbol'] for v in paper_db['active_positions'].values()]
                        pending_list = list(server_state.get('pending_targets', {}).keys())
                        status_str = "🛑 일시정지 (STOP)" if EMERGENCY_STOP else "🟢 정상 감시 중 (RUNNING)"

                        res_msg = f"""[시스템 상태 보고]
• 인터락 상태: {status_str}
• 진입 대기 종목: {', '.join(pending_list) if pending_list else '(없음)'}
• 현재 보유 종목: {', '.join(held_list) if held_list else '(없음)'}
• 누적 복기 거래수: {len(paper_db.get('closed_trades', []))}건"""
                        send_telegram_msg(res_msg)

                    elif text == "/stop":
                        EMERGENCY_STOP = True
                        send_telegram_msg("🛑 [인터락 작동] 신규 매수 감시가 일시 중단되었습니다. (보유 포지션 익/손절 감시는 유지)")

                    elif text == "/start":
                        EMERGENCY_STOP = False
                        send_telegram_msg("▶️ [인터락 해제] 신규 매수 감시가 정상 재개되었습니다.")

                    elif text == "/panic":
                        EMERGENCY_STOP = True
                        send_telegram_msg("🚨 [PANIC] 신규 매수 중단 및 보유 종목 전량 긴급 청산 실행")
                        # 보유 종목 전량 청산 로직
                        paper_db = load_json_file(PAPER_TRADES_FILE, {"active_positions": {}, "closed_trades": []})
                        paper_db["active_positions"] = {}
                        save_json_file(PAPER_TRADES_FILE, paper_db)
                        send_telegram_msg("✅ 보유 포지션이 모두 초기화되었습니다.")

            await asyncio.sleep(1)
        except Exception as e:
            logging.error(f"텔레그램 리스너 오류: {e}")
            await asyncio.sleep(3)

async def realtime_execution_engine():
    global EMERGENCY_STOP
    logging.info("⚡ 오라클 서버 실시간 가격 감시 엔진 가동")
    last_sync_time = 0

    asyncio.create_task(telegram_command_listener())

    while True:
        try:
            now = time.time()
            now_dt = datetime.now()
            server_state = load_json_file(STATE_FILE, {"pending_targets": {}, "last_updated": ""})
            paper_db = load_json_file(PAPER_TRADES_FILE, {"active_positions": {}, "closed_trades": []})

            # 1. 30초마다 GitHub targets.json 동기화
            if now - last_sync_time >= 30:
                data = fetch_latest_targets()
                if data and data.get("updated_at") != server_state.get("last_updated"):
                    server_state["last_updated"] = data.get("updated_at")
                    for coin_code, plan in data.get("targets", {}).items():
                        if coin_code not in paper_db.get("active_positions", {}):
                            server_state["pending_targets"][coin_code] = plan
                    save_json_file(STATE_FILE, server_state)
                    logging.info(f"🔄 최신 targets.json 동기화 완료 ({server_state['last_updated']})")
                last_sync_time = now

            pending = server_state.get("pending_targets", {})
            active_positions = paper_db.get("active_positions", {})
            closed_trades = paper_db.get("closed_trades", [])

            # 2. [진입 대기 감시] 지정 진입가 도달 시 매수 체결
            if not EMERGENCY_STOP:
                for coin_code, plan in list(pending.items()):
                    curr_p = get_current_price(coin_code)
                    if not curr_p:
                        continue

                    # 진입가 도달 확인 (현재가 <= 목표 진입가)
                    if curr_p <= plan["target_entry"]:
                        logging.info(f"🎯 [{plan['symbol']}] 진입 타점 도달! 매수 체결 진행")
                        
                        # 포지션 등록
                        active_positions[coin_code] = {
                            "symbol": plan["symbol"],
                            "entry_price": curr_p,
                            "stop_loss": plan["stop_loss"],
                            "take_profit": plan["take_profit"],
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

                        # 기존 텔레그램 체결 메시지 포맷 그대로 전송
                        held_str = "\n".join([f"- {v['symbol']}" for v in active_positions.values()])
                        recent_summary = format_recent_trades_summary(closed_trades)
                        tp_sign = "+" if plan['tp_pct'] > 0 else ""

                        buy_msg = f"""[체결 완료] - 모의투자
종목 : {plan['symbol']}
진입가 : {curr_p:,.0f}KRW

익절 목표 : {plan['take_profit']:,.0f}KRW({tp_sign}{plan['tp_pct']}%) / 손절 목표 : {plan['stop_loss']:,.0f}KRW({plan['sl_pct']}%)

매수 근거 : {plan['detailed_reason']}
=================================
[현재 보유종목]
{held_str}
=================================
{recent_summary}"""
                        send_telegram_msg(buy_msg)

            # 3. [보유 포지션 실시간 감시] 익절 / 손절 / 시간 손절
            closed_keys = []
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

                # 익절 도달
                if curr_p >= tp_p:
                    status = "CLOSED_TAKE_PROFIT"
                    exit_price = curr_p
                    exit_reason = "익절 목표가 달성"
                # 손절 도달
                elif curr_p <= sl_p:
                    status = "CLOSED_STOP_LOSS"
                    exit_price = curr_p
                    exit_reason = "스탑로스 도달 (손절)"
                # 3시간 시간 손절
                elif now_dt - entry_time >= timedelta(hours=TIME_EXIT_HOURS):
                    status = "CLOSED_TIME_EXIT"
                    exit_price = curr_p
                    exit_reason = f"{TIME_EXIT_HOURS}시간 이상 횡보로 시간 손절(시장가 청산)"

                if status != "HOLDING":
                    closed_keys.append(coin_code)
                    profit_pct = round(((exit_price - entry_p) / entry_p) * 100, 2)
                    profit_krw = round(pos["buy_amount_krw"] * (profit_pct / 100.0))

                    trade_record = {
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
                    }
                    closed_trades.append(trade_record)

                    del active_positions[coin_code]
                    paper_db["active_positions"] = active_positions
                    paper_db["closed_trades"] = closed_trades
                    save_json_file(PAPER_TRADES_FILE, paper_db)

                    # 기존 텔레그램 청산 메시지 포맷 그대로 전송
                    remaining_held = [v['symbol'] for v in active_positions.values()]
                    held_str = "\n".join([f"- {s}" for s in remaining_held]) if remaining_held else "- (없음)"
                    recent_summary = format_recent_trades_summary(closed_trades)
                    sign_pct = "+" if profit_pct > 0 else ""
                    sign_krw = "+" if profit_krw > 0 else ""

                    exit_msg = f"""[청산 완료] - 모의투자
종목 : {pos['symbol']}
진입가 : {entry_p:,.0f}KRW ➔ 청산가 : {exit_price:,.0f}KRW
손익 : {sign_krw}{profit_krw:,}KRW ({sign_pct}{profit_pct:.1f}%)

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

if __name__ == "__main__":
    send_telegram_msg("🚀 [오라클 서버] 실시간 감시 & 텔레그램 원격 제어 엔진 가동 시작\n(명령어: /status, /stop, /start, /panic)")
    asyncio.run(realtime_execution_engine())
