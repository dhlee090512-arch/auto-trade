import os
import sys

# ==========================================
# [필수] 시스템 프록시 환경변수 원천 무효화
# ==========================================
for proxy_var in ['http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'all_proxy', 'WEBSHARE_URL']:
    os.environ.pop(proxy_var, None)

import time
import json
import base64
import logging
import asyncio
import threading
import subprocess
import requests
import jwt
import uuid
import hashlib
import urllib.parse
import re
import math
import websockets
from datetime import datetime, timedelta, timezone
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

# ==========================================
# 0. 전역 설정 및 퀀트 파라미터
# ==========================================
BUILD_VERSION = "2026.09.21-v8-refine"     # 🔖 코드 업데이트 감지용 빌드 태그
MAX_HOLDING_COINS = 2                       # 🛡️ 최대 동시 운용 종목 수 (2개 집중)
MIN_BUY_KRW = 6000                          # 💵 최소 매수 금액 (원)
DEFAULT_BUY_RATIO = 0.20                    # 📊 기본 1회 투입 비중 (총 자산의 20%)
MIN_COIN_PRICE_KRW = 50.0                   # 🛡️ 최소 진입 가격 (50원 미만 초저가 동전주 원천 차단)
MAX_TICK_RATIO_PCT = 0.08                   # 🛡️ 1틱 변동률 상한선 (0.08% 이하 우량/중형 코인만 선별)
MIN_24H_ACC_TRADE_VALUE = 1_500_000_000     # 🛡️ 24시간 누적 거래대금 하한선 (15억 원)
PRIMARY_1H_TRADE_VAL = 600_000_000          # 🛡️ 슬롯 1 1차 1시간 거래대금 필터 (6억 원)
FALLBACK_1H_TRADE_VAL = 400_000_000         # 🛡️ 슬롯 1 후보 부족 시 2차 폴백 (4억 원)
DAILY_LOSS_LIMIT_PCT = -3.0                 # 🛑 일일 누적 손실 서킷브레이커 (-3.0% 도달 시 당일 매매 중단)

TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
TELEGRAM_CHAT_ID = str(os.getenv("TELEGRAM_CHAT_ID") or "").strip()
GH_TOKEN = os.getenv("GH_TOKEN2") or os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
GITHUB_REPOSITORY = "dhlee090512-arch/auto-trade"

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SAMBANOVA_API_KEY = os.getenv("SAMBANOVA_API_KEY")
GROQ_API_KEY3 = os.getenv("GROQ_API_KEY3")
GROQ_API_KEY2 = os.getenv("GROQ_API_KEY2")
BITHUMB_API_KEY = os.getenv("BITHUMB_API_KEY")
BITHUMB_SECRET_KEY = os.getenv("BITHUMB_SECRET_KEY")

STABLE_COINS = {"USDT", "USDC", "DAI", "TUSD", "FDUSD", "USDD", "BUSD", "KRW", "BTC", "ETH"}

STATE_FILE = "server_state.json"
PAPER_TRADES_FILE = "paper_trades.json"
TARGETS_FILE = "targets.json"
RESTART_FLAG_FILE = "last_restart_notify.txt"
PROJECT_DIR = "/home/ubuntu/auto-trade"

# 런타임 동적 상태 변수
PAPER_TRADING = True
PENDING_PAPER_DRAIN = False       # 실전 -> 모의 전환 시 잔여 포지션 소진 대기 플래그
UPDATE_BASELINE_TIME = None       # 이번 업데이트 배포 기준 시각 (KST ISO)
EMERGENCY_STOP = False
CIRCUIT_BREAKER_ACTIVE = False
LAST_TELEGRAM_UPDATE_ID = 0

# 실시간 웹소켓 캐시 데이터 (밀리초 단위 업데이트)
WS_TICKER_CACHE = {}     # { "SOL": { "price": 182500.0, "time": 123456789 } }
WS_ORDERBOOK_CACHE = {}  # { "SOL": { "bids": [...], "asks": [...], "bid_ratio": 0.55, "max_bid_wall": {...} } }

HTTP_SESSION = requests.Session()
adapter = requests.adapters.HTTPAdapter(pool_connections=35, pool_maxsize=35, max_retries=1)
HTTP_SESSION.mount('https://', adapter)
HTTP_SESSION.mount('http://', adapter)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler("watcher.log"),
        logging.StreamHandler(sys.stdout)
    ]
)

KST = timezone(timedelta(hours=9))

def get_kst_now():
    return datetime.now(KST)

def parse_dt_safe(dt_str):
    if not dt_str or not isinstance(dt_str, str):
        return None
    try:
        cleaned_str = dt_str.strip()
        if cleaned_str.endswith('Z'):
            cleaned_str = cleaned_str[:-1] + '+00:00'
        dt = datetime.fromisoformat(cleaned_str)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=KST)
        return dt.astimezone(KST)
    except Exception:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d %H:%M:%S"):
            try:
                dt = datetime.strptime(cleaned_str[:19], fmt)
                return dt.replace(tzinfo=KST)
            except Exception:
                continue
    return None

# ==========================================
# 1. 영구 상태 로드 및 업데이트 기준시각 자동 갱신
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
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logging.error(f"JSON 저장 실패 ({file_path}): {e}")

def init_server_state():
    global PAPER_TRADING, UPDATE_BASELINE_TIME, PENDING_PAPER_DRAIN
    state = load_json_file(STATE_FILE, {})
    
    PAPER_TRADING = state.get("paper_trading", True)
    PENDING_PAPER_DRAIN = state.get("pending_paper_drain", False)
    
    last_build = state.get("build_version", "")
    now_kst_iso = get_kst_now().isoformat()
    
    if last_build != BUILD_VERSION or "update_baseline_time" not in state or not state["update_baseline_time"]:
        state["update_baseline_time"] = now_kst_iso
        state["build_version"] = BUILD_VERSION
        logging.info(f"🔄 [버전 업데이트 감지] 누적 성과 기준 시각을 갱신합니다: {now_kst_iso} (Build: {BUILD_VERSION})")
    
    UPDATE_BASELINE_TIME = state["update_baseline_time"]
    state["last_started_at"] = now_kst_iso
    save_json_file(STATE_FILE, state)

init_server_state()

# ==========================================
# 2. 텔레그램 리포트 & GitHub 동기화
# ==========================================
def send_telegram_msg(msg: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg}
    try:
        HTTP_SESSION.post(url, json=payload, timeout=5)
    except Exception as e:
        logging.error(f"텔레그램 발송 오류: {e}")

def notify_startup_once():
    now_ts = time.time()
    last_notify_ts = 0.0
    if os.path.exists(RESTART_FLAG_FILE):
        try:
            with open(RESTART_FLAG_FILE, "r") as f:
                last_notify_ts = float(f.read().strip())
        except Exception:
            pass

    if now_ts - last_notify_ts > 600:
        with open(RESTART_FLAG_FILE, "w") as f:
            f.write(str(now_ts))
        mode_str = "🧪 모의투자" if PAPER_TRADING else "🔥 실전매매"
        baseline_dt = parse_dt_safe(UPDATE_BASELINE_TIME)
        base_str = baseline_dt.strftime("%m/%d %H:%M KST") if baseline_dt else "-"
        send_telegram_msg(
            f"🚀 [오라클 서버] 고도화 퀀트 엔진 가동 ({BUILD_VERSION})\n"
            f"• 모드: {mode_str} (WebSocket 스트림 + 0.5초 감시 / 2종목)\n"
            f"• 필터: 50원 이상 & 1틱 0.08% 이하 & 1시간 수급 6억(폴백 4억)\n"
            f"• 대기열: 최대 2개 선별 스코어링 + 10분 엄수 자동 순환\n"
            f"• 익절: +1.2% 시작 5단계 트레일링(반락 0.20%~) + 10호가벽 2틱 탈출\n"
            f"• 방어: 완화된 스크래치(2~4분, -0.65%) + 본절 락(+0.8%) + 30분 만료\n"
            f"• 알림: 청산 시 궤적 흐름 요약 자동 첨부\n\n"
            f"📱 명령어: /status, /log, /paper, /real, /reset_stats, /stop, /start, /update"
        )
    else:
        logging.info("ℹ️ 최근 재시작 알림 발송 이력으로 시작 메시지 전송 생략")

def sync_file_to_github(file_path, content_data):
    if not GH_TOKEN:
        return
    url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/contents/{file_path}"
    headers = {"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    sha = None
    try:
        res = HTTP_SESSION.get(url, headers=headers, timeout=5)
        if res.status_code == 200:
            sha = res.json().get('sha')
    except Exception:
        pass
        
    json_str = json.dumps(content_data, indent=2, ensure_ascii=False)
    encoded = base64.b64encode(json_str.encode('utf-8')).decode('utf-8')
    payload = {"message": f"update: {file_path} from oracle server", "content": encoded}
    if sha:
        payload["sha"] = sha
    try:
        HTTP_SESSION.put(url, headers=headers, json=payload, timeout=8)
    except Exception as e:
        logging.error(f"GitHub 동기화 실패 ({file_path}): {e}")

def format_portfolio_status_msg(active_positions, closed_trades):
    current_mode_is_paper = PAPER_TRADING
    mode_tag_name = "🧪 모의투자" if current_mode_is_paper else "🔥 실전매매"

    mode_active = {k: v for k, v in active_positions.items() if v.get("is_paper", True) == current_mode_is_paper}
    held_symbols = [f"{v['symbol']}({v.get('slot', 'S1')})" for v in mode_active.values()]
    held_str = f"{', '.join(held_symbols)} ({len(held_symbols)}/2개 보유 중)" if held_symbols else "(현재 보유 종목 없음)"

    mode_closed = [t for t in closed_trades if t.get("is_paper", True) == current_mode_is_paper]
    recent_10 = mode_closed[-10:][::-1] if mode_closed else []
    
    if not recent_10:
        trades_str = f"• [{mode_tag_name}] 매도 이력이 없습니다."
        win_rate_10 = 0.0
        profit_10_krw = 0
    else:
        trade_lines = []
        wins_10 = 0
        profit_10_krw = 0
        for idx, t in enumerate(recent_10, 1):
            p_pct = t.get('profit_pct', 0.0)
            p_krw = t.get('profit_krw', 0)
            symbol = t.get('symbol', 'UNKNOWN')
            dt_obj = parse_dt_safe(t.get('exit_time', ''))
            time_display = dt_obj.strftime("%m/%d %H:%M KST") if dt_obj else "-"
                
            sign_pct = "+" if p_pct > 0 else ""
            sign_k = "+" if p_krw > 0 else ""
            trade_lines.append(f"{idx}. {symbol}: {sign_k}{p_krw:,}원 ({sign_pct}{p_pct:.2f}%) | {time_display}")
            if p_pct > 0:
                wins_10 += 1
            profit_10_krw += p_krw
            
        trades_str = "\n".join(trade_lines)
        win_rate_10 = round((wins_10 / len(recent_10)) * 100, 1)

    baseline_dt = parse_dt_safe(UPDATE_BASELINE_TIME)
    baseline_display = baseline_dt.strftime("%m/%d %H:%M") if baseline_dt else "최근 업데이트"
    
    total_cum_trades = 0
    total_cum_wins = 0
    total_cum_profit_krw = 0

    for t in mode_closed:
        exit_dt = parse_dt_safe(t.get('exit_time', ''))
        if baseline_dt and exit_dt and exit_dt >= baseline_dt:
            total_cum_trades += 1
            p_krw = t.get('profit_krw', 0)
            p_pct = t.get('profit_pct', 0.0)
            total_cum_profit_krw += p_krw
            if p_pct > 0:
                total_cum_wins += 1

    if total_cum_trades > 0:
        cum_win_rate = round((total_cum_wins / total_cum_trades) * 100, 1)
        cum_sign = "+" if total_cum_profit_krw > 0 else ""
        cum_stats_str = f"• 총 거래: {total_cum_trades}건 ({total_cum_wins}승 {total_cum_trades - total_cum_wins}패 | 승률 {cum_win_rate}%)\n• 총 누적 손익: {cum_sign}{total_cum_profit_krw:,} KRW"
    else:
        cum_stats_str = f"• 업데이트 이후 청산 완료된 {mode_tag_name} 거래가 아직 없습니다."

    sign_10 = "+" if profit_10_krw > 0 else ""
    return f"""💼 [{mode_tag_name} 전용 매매 상황]
• 보유 종목 : {held_str}

📜 [최근 10건 매도 이력 (KST)]
{trades_str}

📊 최근 10건 승률 : {win_rate_10}%
💰 최근 10건 실현 손익 : {sign_10}{profit_10_krw:,} KRW
━━━━━━━━━━━━━━━━━━━━
📈 [업데이트 이후 누적 성과] ({baseline_display} KST 이후)
{cum_stats_str}"""

def generate_trade_trajectory_summary(pos: dict, curr_p: float, curr_profit_pct: float, highest_profit_pct: float, elapsed_seconds: float, close_reason: str) -> str:
    mins = int(elapsed_seconds // 60)
    secs = int(elapsed_seconds % 60)
    dur_str = f"{mins}분 {secs}초" if mins > 0 else f"{secs}초"

    hist = pos.get("price_history", [])
    entry_p = pos["entry_price"]
    lowest_profit_pct = 0.0
    if hist:
        min_p = min(p for _, p in hist)
        lowest_profit_pct = round(((min_p - entry_p) / entry_p) * 100.0, 2)

    peak_time_str = "중반"
    if hist and len(hist) > 2:
        high_entry = max(hist, key=lambda x: x[1])
        peak_elapsed = high_entry[0] - pos.get("entry_timestamp", hist[0][0])
        peak_mins = int(peak_elapsed // 60)
        peak_secs = int(peak_elapsed % 60)
        peak_time_str = f"{peak_mins}분 {peak_secs}초경" if peak_mins > 0 else f"{peak_secs}초경"

    if "트레일링 익절" in close_reason:
        return f"진입 {peak_time_str} 최고 +{highest_profit_pct:.2f}%까지 슈팅했으나, 상단 저항에 밀려 반락 시 호가 매수벽 2틱 위에서 실현 손익을 선제 확보함. (보유: {dur_str})"
    elif "본절 락" in close_reason:
        return f"진입 {peak_time_str} 최고 +{highest_profit_pct:.2f}%까지 1차 분출했으나, 후속 수급 부재로 평단가까지 되밀려 원금 보호를 위해 본절 수준에서 청산함. (보유: {dur_str})"
    elif "스크래치" in close_reason:
        return f"진입 후 {dur_str} 동안 최고 +{highest_profit_pct:.2f}%에 그치며 반등 탄력을 전혀 받지 못했고, 호가 매수세가 이탈하여 조기 손절함."
    elif "30분" in close_reason or "타임아웃" in close_reason or "만료" in close_reason:
        return f"진입 후 30분 동안 {lowest_profit_pct:.2f}% ~ +{highest_profit_pct:.2f}% 사이 박스권에 갇혀 거래량이 소멸되었기에 슬롯 회수를 위해 전량 정리함."
    elif "킬스위치" in close_reason:
        return f"약손실 구간에서 매수벽이 순간 붕괴되며 매수잔량 비율이 급감하여, 추가 슬리피지 방지를 위해 선제 탈출함. (보유: {dur_str})"
    elif "비상" in close_reason:
        return f"진입 직후 매도 덤핑 투매가 발생하여 비상 하드 손절선에 닿아 즉각 탈출함. (보유: {dur_str})"
    else:
        return f"보유 시간 {dur_str} 동안 최저 {lowest_profit_pct:.2f}% ~ 최고 +{highest_profit_pct:.2f}% 진폭을 보인 후 손절/익절 조건에 부합하여 정리함."

# ==========================================
# 3. 빗썸 호가창(Orderbook) 및 웹소켓(WebSocket) 엔진
# ==========================================
def get_bithumb_jwt_headers(query_params: dict = None):
    if not BITHUMB_API_KEY or not BITHUMB_SECRET_KEY:
        return {}
    payload = {
        'access_key': BITHUMB_API_KEY,
        'nonce': str(uuid.uuid4()),
        'timestamp': round(time.time() * 1000)
    }
    if query_params:
        query_string = urllib.parse.urlencode(query_params).encode()
        m = hashlib.sha512()
        m.update(query_string)
        payload['query_hash'] = m.hexdigest()
        payload['query_hash_alg'] = 'SHA512'

    token = jwt.encode(payload, BITHUMB_SECRET_KEY, algorithm='HS256')
    return {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json'
    }

def get_bithumb_tick_size(price: float) -> float:
    if price < 1.0: return 0.0001
    elif price < 10.0: return 0.001
    elif price < 100.0: return 0.01
    elif price < 1000.0: return 1.0
    elif price < 5000.0: return 1.0
    elif price < 10000.0: return 5.0
    elif price < 50000.0: return 10.0
    elif price < 100000.0: return 50.0
    elif price < 500000.0: return 100.0
    elif price < 1000000.0: return 500.0
    else: return 1000.0

def round_to_bithumb_tick(price: float) -> float:
    if price < 1.0: return round(price, 4)
    elif price < 10.0: return round(price, 3)
    elif price < 100.0: return round(price, 2)
    elif price < 1000.0: return float(int(price))
    elif price < 5000.0: return float(int(price))
    elif price < 10000.0: return float(int(price / 5.0) * 5)
    elif price < 50000.0: return float(int(price / 10.0) * 10)
    elif price < 100000.0: return float(int(price / 50.0) * 50)
    elif price < 500000.0: return float(int(price / 100.0) * 100)
    elif price < 1000000.0: return float(int(price / 500.0) * 500)
    else: return float(int(price / 1000.0) * 1000)

def get_current_price(coin_code: str):
    cached = WS_TICKER_CACHE.get(coin_code)
    if cached and (time.time() - cached.get("time", 0) <= 2.0):
        return cached["price"]
    try:
        url = f"https://api.bithumb.com/public/ticker/{coin_code}_KRW"
        res = HTTP_SESSION.get(url, timeout=1.5).json()
        if res.get("status") == "0000":
            price = float(res["data"]["closing_price"])
            if price > 0:
                WS_TICKER_CACHE[coin_code] = {"price": price, "time": time.time()}
                return price
    except Exception:
        pass
    return cached.get("price") if cached else None

def get_bithumb_orderbook_10(coin_code: str):
    cached = WS_ORDERBOOK_CACHE.get(coin_code)
    if cached and (time.time() - cached.get("time", 0) <= 2.0):
        return cached

    try:
        url = f"https://api.bithumb.com/public/orderbook/{coin_code}_KRW"
        res = HTTP_SESSION.get(url, timeout=1.5).json()
        if res.get("status") == "0000":
            data = res["data"]
            bids = [{"price": float(b["price"]), "quantity": float(b["quantity"])} for b in data.get("bids", [])[:10]]
            asks = [{"price": float(a["price"]), "quantity": float(a["quantity"])} for a in data.get("asks", [])[:10]]
            
            total_bid_qty = sum(b["quantity"] for b in bids)
            total_ask_qty = sum(a["quantity"] for a in asks)
            total_qty = total_bid_qty + total_ask_qty
            bid_ratio = (total_bid_qty / total_qty) if total_qty > 0 else 0.5
            
            total_bid_krw = sum(b["price"] * b["quantity"] for b in bids)
            max_bid_wall = max(bids, key=lambda x: x["quantity"]) if bids else None
            
            parsed = {
                "bids": bids,
                "asks": asks,
                "total_bid_qty": total_bid_qty,
                "total_ask_qty": total_ask_qty,
                "total_bid_krw": total_bid_krw,
                "bid_ratio": round(bid_ratio, 3),
                "max_bid_wall": max_bid_wall,
                "time": time.time()
            }
            WS_ORDERBOOK_CACHE[coin_code] = parsed
            return parsed
    except Exception:
        pass
    return cached

async def bithumb_websocket_stream_worker():
    logging.info("🌐 [WebSocket] 빗썸 실시간 스트림 연결 프로세스 시작")
    ws_url = "wss://pubwss.bithumb.com/pub/ws"
    
    while True:
        try:
            async with websockets.connect(ws_url, ping_interval=30, ping_timeout=10) as ws:
                logging.info("🟢 [WebSocket] 빗썸 퍼블릭 스트림 서버 연결 성공")
                last_sub_symbols = set()

                while True:
                    server_state = load_json_file(STATE_FILE, {})
                    paper_db = load_json_file(PAPER_TRADES_FILE, {"active_positions": {}, "closed_trades": []})
                    active_coins = set(paper_db.get("active_positions", {}).keys())
                    queue_coins = set(server_state.get("target_queue", {}).keys())
                    
                    target_symbols = [f"{c}_KRW" for c in active_coins.union(queue_coins) if c]
                    target_symbols_set = set(target_symbols)

                    if target_symbols_set != last_sub_symbols and len(target_symbols) > 0:
                        sub_msg_tx = {"type": "transaction", "symbols": target_symbols}
                        sub_msg_ob = {"type": "orderbookdepth", "symbols": target_symbols}
                        await ws.send(json.dumps(sub_msg_tx))
                        await ws.send(json.dumps(sub_msg_ob))
                        last_sub_symbols = target_symbols_set
                        logging.info(f"📡 [WebSocket] 구독 갱신 완료: {', '.join(target_symbols)}")

                    try:
                        raw_msg = await asyncio.wait_for(ws.recv(), timeout=2.0)
                        data = json.loads(raw_msg)
                        msg_type = data.get("type")
                        content = data.get("content", {})

                        if msg_type == "transaction":
                            for item in content.get("list", []):
                                symbol = item.get("symbol", "")
                                code = symbol.split('_')[0].upper()
                                price = float(item.get("price", 0.0))
                                if code and price > 0:
                                    WS_TICKER_CACHE[code] = {"price": price, "time": time.time()}

                        elif msg_type == "orderbookdepth":
                            for item in content.get("list", []):
                                symbol = item.get("symbol", "")
                                code = symbol.split('_')[0].upper()
                                bids_raw = item.get("bids", [])[:10]
                                asks_raw = item.get("asks", [])[:10]
                                
                                bids = [{"price": float(b[0]), "quantity": float(b[1])} for b in bids_raw]
                                asks = [{"price": float(a[0]), "quantity": float(a[1])} for a in asks_raw]
                                
                                total_bid_qty = sum(b["quantity"] for b in bids)
                                total_ask_qty = sum(a["quantity"] for a in asks)
                                total_qty = total_bid_qty + total_ask_qty
                                bid_ratio = (total_bid_qty / total_qty) if total_qty > 0 else 0.5
                                total_bid_krw = sum(b["price"] * b["quantity"] for b in bids)
                                max_bid_wall = max(bids, key=lambda x: x["quantity"]) if bids else None

                                WS_ORDERBOOK_CACHE[code] = {
                                    "bids": bids,
                                    "asks": asks,
                                    "total_bid_qty": total_bid_qty,
                                    "total_ask_qty": total_ask_qty,
                                    "total_bid_krw": total_bid_krw,
                                    "bid_ratio": round(bid_ratio, 3),
                                    "max_bid_wall": max_bid_wall,
                                    "time": time.time()
                                }
                    except asyncio.TimeoutError:
                        continue
        except Exception as e:
            logging.warning(f"⚠️ [WebSocket] 연결 끊김 또는 수신 에러: {e} ➔ 3초 후 재연결")
            await asyncio.sleep(3.0)

def get_bithumb_account_summary():
    if not BITHUMB_API_KEY or not BITHUMB_SECRET_KEY:
        return None, None
    try:
        url = "https://api.bithumb.com/v1/accounts"
        headers = get_bithumb_jwt_headers()
        res = HTTP_SESSION.get(url, headers=headers, timeout=2.5).json()
        if isinstance(res, list):
            total_krw = 0.0
            available_krw = 0.0
            for acc in res:
                curr = acc.get("currency", "")
                bal = float(acc.get("balance", 0.0))
                locked = float(acc.get("locked", 0.0))
                total_units = bal + locked

                if curr == "KRW":
                    available_krw = bal
                    total_krw += total_units
                elif curr in ["P", "POINT"]:
                    continue
                else:
                    if total_units <= 0:
                        continue
                    price = get_current_price(curr)
                    if not price or price <= 0:
                        price = float(acc.get("avg_buy_price", 0.0))
                    
                    if price > 0:
                        total_krw += (total_units * price)
                        
            return round(total_krw, 2), round(available_krw, 2)
    except Exception as e:
        logging.error(f"빗썸 잔고 조회 실패: {e}")
    return None, None

def get_real_account_coin_info(coin_code: str):
    if PAPER_TRADING:
        return None, None
    try:
        url = "https://api.bithumb.com/v1/accounts"
        headers = get_bithumb_jwt_headers()
        res = HTTP_SESSION.get(url, headers=headers, timeout=2.5).json()
        if isinstance(res, list):
            for acc in res:
                if acc.get("currency", "").upper() == coin_code.upper():
                    avg_p = float(acc.get("avg_buy_price", 0.0))
                    bal = float(acc.get("balance", 0.0)) + float(acc.get("locked", 0.0))
                    if avg_p > 0:
                        return avg_p, bal
    except Exception as e:
        logging.error(f"체결 단가 조회 오류 ({coin_code}): {e}")
    return None, None

def execute_real_limit_buy_order(coin_code: str, price: float, krw_amount: float):
    if PAPER_TRADING:
        return True, "mock_order_" + str(uuid.uuid4())[:8], (krw_amount / price)
    try:
        url = "https://api.bithumb.com/v1/orders"
        market = f"KRW-{coin_code.upper()}"
        volume = round(krw_amount / price, 4)
        
        body = {
            "market": market,
            "side": "bid",
            "volume": str(volume),
            "price": str(price),
            "ord_type": "limit"
        }
        headers = get_bithumb_jwt_headers(body)
        res = HTTP_SESSION.post(url, json=body, headers=headers, timeout=3.0).json()
        if "uuid" in res:
            return True, res["uuid"], volume
        return False, str(res), 0.0
    except Exception as e:
        return False, str(e), 0.0

def check_bithumb_order_status(order_uuid: str):
    if PAPER_TRADING or not order_uuid or order_uuid.startswith("mock_"):
        return "wait", 0.0, 0.0
    try:
        url = "https://api.bithumb.com/v1/order"
        params = {"uuid": order_uuid}
        headers = get_bithumb_jwt_headers(params)
        res = HTTP_SESSION.get(url, params=params, headers=headers, timeout=2.5).json()
        state = res.get("state", "wait")
        executed_volume = float(res.get("executed_volume", 0.0))
        paid_fee = float(res.get("paid_fee", 0.0))
        return state, executed_volume, paid_fee
    except Exception as e:
        logging.error(f"주문 상태 조회 실패 ({order_uuid}): {e}")
        return "wait", 0.0, 0.0

def cancel_bithumb_order(order_uuid: str):
    if PAPER_TRADING or not order_uuid or order_uuid.startswith("mock_"):
        return True
    try:
        url = "https://api.bithumb.com/v1/order"
        params = {"uuid": order_uuid}
        headers = get_bithumb_jwt_headers(params)
        res = HTTP_SESSION.delete(url, params=params, headers=headers, timeout=2.5).json()
        return "uuid" in res
    except Exception as e:
        logging.error(f"주문 취소 API 실패 ({order_uuid}): {e}")
        return False

def execute_real_market_sell_order(coin_code: str, units: float):
    if PAPER_TRADING:
        return True, "모의투자 매도 체결"
    try:
        url = "https://api.bithumb.com/v1/orders"
        market = f"KRW-{coin_code.upper()}"
        body = {"market": market, "side": "ask", "volume": str(units), "ord_type": "market"}

        headers = get_bithumb_jwt_headers(body)
        res = HTTP_SESSION.post(url, json=body, headers=headers, timeout=3.0).json()
        if "uuid" in res:
            return True, res["uuid"]
        return False, str(res)
    except Exception as e:
        return False, str(e)

def get_candles(coin_code, interval="15m", limit=40):
    try:
        url = f"https://api.bithumb.com/public/candlestick/{coin_code}_KRW/{interval}"
        res = HTTP_SESSION.get(url, timeout=3.0).json()
        if res.get("status") == "0000":
            return [{
                "timestamp": int(c[0]),
                "open": float(c[1]),
                "close": float(c[2]),
                "high": float(c[3]),
                "low": float(c[4]),
                "volume": round(float(c[5]), 2)
            } for c in res['data'][-limit:]]
    except Exception:
        pass
    return []

# ==========================================
# 4. 정량 퀀트 지표 (ATR 기반 동적 손절 산출)
# ==========================================
def calculate_rsi(closes, period=14):
    if len(closes) < period + 1: return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i-1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0: return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 1)

def calculate_atr(candles, period=14):
    if len(candles) < period + 1: return 0.0
    trs = []
    for i in range(1, len(candles)):
        h = candles[i]['high']
        l = candles[i]['low']
        prev_c = candles[i-1]['close']
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)
    return round(sum(trs[-period:]) / period, 4)

def calculate_bollinger_bands(candles, period=20, num_std=2.0):
    if len(candles) < period:
        return None
    closes = [c['close'] for c in candles[-period:]]
    ma = sum(closes) / period
    variance = sum((x - ma) ** 2 for x in closes) / period
    std = math.sqrt(variance)
    upper = ma + (num_std * std)
    lower = ma - (num_std * std)
    bandwidth = ((upper - lower) / ma) * 100.0 if ma > 0 else 0.0
    return {
        "ma": round(ma, 4),
        "upper": round(upper, 4),
        "lower": round(lower, 4),
        "bandwidth": round(bandwidth, 2),
        "std": round(std, 4)
    }

def calculate_quant_features(candles_1h, candles_15m):
    closes_1h = [c['close'] for c in candles_1h]
    closes_15m = [c['close'] for c in candles_15m]
    
    rsi_1h = calculate_rsi(closes_1h, 14)
    rsi_15m = calculate_rsi(closes_15m, 14)
    atr_1h = calculate_atr(candles_1h, 14)
    ma20_1h = sum(closes_1h[-20:]) / 20.0 if len(closes_1h) >= 20 else closes_1h[-1]
    
    vol_avg_15m = sum(c['volume'] for c in candles_15m[-11:-1]) / 10.0 if len(candles_15m) >= 11 else 1.0
    vol_surge_ratio = round(candles_15m[-1]['volume'] / vol_avg_15m, 2) if vol_avg_15m > 0 else 1.0

    curr_p = closes_15m[-1]
    tick_size = get_bithumb_tick_size(curr_p)
    tick_ratio_pct = round((tick_size / curr_p) * 100.0, 3) if curr_p > 0 else 1.0
    atr_pct = round((atr_1h / curr_p) * 100.0, 2) if curr_p > 0 else 0.0

    dynamic_sl_pct = -round(min(max(atr_pct * 0.9, 1.4), 1.8), 2)
    emergency_sl_pct = -round(abs(dynamic_sl_pct) + 0.8, 2)

    recent_1h_trade_val = candles_1h[-1]['volume'] * candles_1h[-1]['close'] if candles_1h else 0.0

    return {
        "rsi_1h": rsi_1h,
        "rsi_15m": rsi_15m,
        "atr_1h": atr_1h,
        "atr_pct": atr_pct,
        "dynamic_sl_pct": dynamic_sl_pct,
        "emergency_sl_pct": emergency_sl_pct,
        "ma20_1h": round(ma20_1h, 4),
        "curr_price": curr_p,
        "vol_surge_ratio": vol_surge_ratio,
        "tick_ratio_pct": tick_ratio_pct,
        "recent_1h_trade_val": recent_1h_trade_val
    }

# ==========================================
# 5. 리스크 관리 모듈
# ==========================================
def check_daily_circuit_breaker(closed_trades, total_asset):
    now_kst = get_kst_now()
    today_start = now_kst.replace(hour=0, minute=0, second=0, microsecond=0)
    
    today_losses_krw = 0
    for t in closed_trades:
        if t.get("is_paper", True) != PAPER_TRADING:
            continue
        exit_dt = parse_dt_safe(t.get('exit_time', ''))
        if exit_dt and exit_dt >= today_start:
            today_losses_krw += t.get('profit_krw', 0)
            
    loss_ratio_pct = (today_losses_krw / total_asset) * 100.0 if total_asset > 0 else 0.0
    if loss_ratio_pct <= DAILY_LOSS_LIMIT_PCT:
        mode_str = "모의투자" if PAPER_TRADING else "실전매매"
        return False, f"당일 [{mode_str}] 누적 손실({loss_ratio_pct:.2f}%)이 일일 한도({DAILY_LOSS_LIMIT_PCT}%) 초과"
    return True, "당일 손실 허용 범위 내"

def check_btc_trend():
    btc_15m = get_candles("BTC", interval="15m", limit=10)
    if len(btc_15m) >= 2:
        prev_c = btc_15m[-2]['open']
        curr_c = btc_15m[-1]['close']
        btc_15m_change = ((curr_c - prev_c) / prev_c) * 100.0
        if btc_15m_change <= -0.8:
            return False, f"🚨 BTC 15분 급락 발생 ({btc_15m_change:.2f}%)"

    btc_c = get_candles("BTC", interval="1h", limit=25)
    if len(btc_c) < 20:
        return True, "BTC 데이터 정상 진행"
    closes = [c['close'] for c in btc_c]
    ma20 = sum(closes[-20:]) / 20.0
    curr_btc = closes[-1]
    ret_3h = ((curr_btc - closes[-4]) / closes[-4]) * 100.0 if len(closes) >= 4 else 0.0

    if curr_btc < (ma20 * 0.985) or ret_3h < -2.5:
        return False, f"BTC 하락 추세 경보 (현재가 {curr_btc:,.0f} KRW, MA20 대비 {((curr_btc-ma20)/ma20)*100:.2f}%)"
    return True, "BTC 추세 양호"

def build_reflection_prompt():
    return (
        "Core Quantitative Principles:\n"
        "1. Prioritize active, confirmed setups (confidence >= 60) over indefinite NONE.\n"
        "2. Slot 1 focuses on volume surges with first red-candle pullbacks on coins with robust 1h trade volume.\n"
        "3. Slot 2 focuses on range-bound oversold bounces at lower Bollinger Band with minimum 2.0% expected rebound room.\n"
        "4. Output up to 2 qualified setups if multiple setups meet criteria."
    )

def calculate_dynamic_buy_ratio(closed_trades):
    mode_closed = [t for t in closed_trades if t.get("is_paper", True) == PAPER_TRADING]
    if not mode_closed or len(mode_closed) < 3:
        return DEFAULT_BUY_RATIO
    last_3 = mode_closed[-3:]
    if all(t.get('profit_pct', 0.0) < 0 for t in last_3):
        return 0.15
    last_5 = mode_closed[-5:]
    wins = sum(1 for t in last_5 if t.get('profit_pct', 0.0) > 0)
    if (wins / len(last_5)) >= 0.8:
        return 0.25
    return DEFAULT_BUY_RATIO

def call_ai_api(system_instruction, user_prompt):
    providers = []
    if GEMINI_API_KEY:
        providers.append({
            "name": "Gemini 3.5 Flash-Lite",
            "key": GEMINI_API_KEY,
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
            "model": "gemini-3.5-flash-lite"
        })
    if SAMBANOVA_API_KEY:
        providers.append({
            "name": "SambaNova Llama-3.3-70B",
            "key": SAMBANOVA_API_KEY,
            "base_url": "https://api.sambanova.ai/v1",
            "model": "Meta-Llama-3.3-70B-Instruct"
        })
    if GROQ_API_KEY3 or GROQ_API_KEY2:
        providers.append({
            "name": "Groq SpecDec",
            "key": GROQ_API_KEY3 or GROQ_API_KEY2,
            "base_url": "https://api.groq.com/openai/v1",
            "model": "llama-3.3-70b-specdec"
        })

    for prov in providers:
        try:
            client = OpenAI(base_url=prov['base_url'], api_key=prov['key'])
            res = client.chat.completions.create(
                model=prov['model'],
                messages=[
                    {"role": "system", "content": system_instruction + "\nStrictly output valid JSON ONLY."},
                    {"role": "user", "content": user_prompt}
                ],
                response_format={"type": "json_object"},
                temperature=0.1
            )
            return res.choices[0].message.content
        except Exception as e:
            logging.warning(f"AI 호출 실패 ({prov['name']}): {e}")
    return None

def clean_and_parse_json(raw_text):
    if not raw_text: return None
    try:
        cleaned = re.sub(r"^```(?:json)?", "", raw_text.strip(), flags=re.MULTILINE)
        cleaned = re.sub(r"```$", "", cleaned.strip(), flags=re.MULTILINE).strip()
        return json.loads(cleaned)
    except Exception:
        match = re.search(r"\{.*\}", raw_text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except Exception:
                pass
    return None

# ==========================================
# 6. 듀얼 슬롯 퀀트 스크리닝 & 대기열(Queue) 스코어링
# ==========================================
def evaluate_slot_candidates(sym, price, val_24h, candles_1h, candles_15m, candles_5m, min_1h_val):
    if len(candles_1h) < 20 or len(candles_15m) < 20 or len(candles_5m) < 5:
        return None, None

    # 🛡️ 50원 미만 초저가 동전주 원천 차단
    if price < MIN_COIN_PRICE_KRW:
        return None, None

    q = calculate_quant_features(candles_1h, candles_15m)
    # 🛡️ 1틱 변동률 상한선 0.08% 엄격 적용
    if q["tick_ratio_pct"] > MAX_TICK_RATIO_PCT:
        return None, None

    bb_15m = calculate_bollinger_bands(candles_15m, period=20, num_std=2.0)
    
    # [슬롯 1] 초단기 수급 펄스 (1시간 수급 필터 동적 적용: 6억 우선 / 4억 폴백)
    if q["recent_1h_trade_val"] >= min_1h_val and q["vol_surge_ratio"] >= 2.0 and (50.0 <= q["rsi_15m"] <= 75.0) and q["rsi_1h"] <= 75.0:
        target_discount = 0.30
        target_entry = round_to_bithumb_tick(price * (1.0 - (target_discount / 100.0)))
        
        return "SLOT_1_PULSE", {
            "symbol": f"{sym}/KRW",
            "code": sym,
            "mode": "SCALPING",
            "slot": "SLOT_1_PULSE",
            "current_price": price,
            "target_entry": target_entry,
            "sl_pct": q["dynamic_sl_pct"],
            "emergency_sl_pct": q["emergency_sl_pct"],
            "timeout_mins": 10,
            "recent_1h_val": q["recent_1h_trade_val"],
            "vol_surge_ratio": q["vol_surge_ratio"],
            "quant": q,
            "reason": f"15분 거래량 {q['vol_surge_ratio']}배 급증 및 1시간 수급({q['recent_1h_trade_val']/100000000:.1f}억) 첫 음봉 눌림목"
        }

    # [슬롯 2] 박스권 평균회귀 (최소 2.0% 이상 반등 룸 확보 종목만 선별)
    if bb_15m and val_24h >= 1_500_000_000 and q["rsi_15m"] <= 38.0 and q["rsi_1h"] <= 65.0:
        lower_band = bb_15m['lower']
        mid_band = bb_15m['ma']
        if price <= (lower_band * 1.01):
            expected_gain = ((mid_band - price) / price) * 100.0
            if expected_gain >= 2.0:  # 🎯 잔잔바리 방지를 위해 반등 기대폭 최소 2.0% 이상 상향
                target_entry = round_to_bithumb_tick(min(price, lower_band * 1.002))
                return "SLOT_2_RANGE", {
                    "symbol": f"{sym}/KRW",
                    "code": sym,
                    "mode": "SWING",
                    "slot": "SLOT_2_RANGE",
                    "current_price": price,
                    "target_entry": target_entry,
                    "target_exit_ma": mid_band,
                    "sl_pct": q["dynamic_sl_pct"],
                    "emergency_sl_pct": q["emergency_sl_pct"],
                    "timeout_mins": 10,
                    "expected_gain": round(expected_gain, 2),
                    "quant": q,
                    "reason": f"15분봉 RSI({q['rsi_15m']}) 과매도 및 볼린저 하단({lower_band}) 지지 중심선({mid_band}) 반등 기대(이격도 {expected_gain:.1f}%)"
                }

    return None, None

def execute_server_side_strategy():
    global CIRCUIT_BREAKER_ACTIVE
    
    if PENDING_PAPER_DRAIN:
        logging.info("⏳ 모의투자 전환 대기 중으로 신규 진입을 탐색하지 않습니다.")
        return

    server_state = load_json_file(STATE_FILE, {})
    paper_db = load_json_file(PAPER_TRADES_FILE, {"active_positions": {}, "closed_trades": []})
    active_positions = paper_db.get("active_positions", {})
    closed_trades = paper_db.get("closed_trades", [])
    target_queue = server_state.get("target_queue", {})
    
    mode_active = {k: v for k, v in active_positions.items() if v.get("is_paper", True) == PAPER_TRADING}
    mode_queue = {k: v for k, v in target_queue.items() if v.get("is_paper", True) == PAPER_TRADING}

    # 보유 포지션이 이미 최대 2개 꽉 찼으면 스크리닝 생략
    if len(mode_active) >= MAX_HOLDING_COINS:
        logging.info("💼 최대 보유 종목(2개) 도달로 신규 탐색 생략")
        return

    total_asset, available_krw = get_bithumb_account_summary()
    if total_asset is None or available_krw is None:
        if PAPER_TRADING:
            total_asset, available_krw = 100000.0, 100000.0
        else:
            logging.error("❌ 빗썸 잔고 수신 실패로 신규 전략 수립 중단")
            return

    cb_ok, cb_reason = check_daily_circuit_breaker(closed_trades, total_asset)
    if not cb_ok:
        if not CIRCUIT_BREAKER_ACTIVE:
            CIRCUIT_BREAKER_ACTIVE = True
            send_telegram_msg(f"🛑 [서킷브레이커 발동] {cb_reason}\n금일 24:00 KST까지 신규 매수가 중단됩니다.")
        logging.info(f"🛑 [서킷브레이커 작동 중] {cb_reason}")
        return
    else:
        CIRCUIT_BREAKER_ACTIVE = False

    btc_ok, btc_reason = check_btc_trend()
    if not btc_ok:
        logging.info(f"🛑 [매크로 방어] {btc_reason} ➔ 신규 매수 차단")
        return

    dynamic_ratio = calculate_dynamic_buy_ratio(closed_trades)
    target_buy_krw = round(total_asset * dynamic_ratio)

    if available_krw < MIN_BUY_KRW:
        logging.info(f"⏸️ 가용 원화 부족 (가용: {available_krw:,.0f} KRW < 최소 주문: {MIN_BUY_KRW:,} KRW) ➔ 관망")
        return

    actual_buy_krw = min(target_buy_krw, int(available_krw))
    actual_buy_krw = max(actual_buy_krw, MIN_BUY_KRW)

    held_or_queued_codes = set(mode_active.keys()).union(set(mode_queue.keys()))
    logging.info(f"🧠 [듀얼 슬롯 퀀트 & AI 분석 가동] (총자산: {total_asset:,.0f}원 | 1회배정: {actual_buy_krw:,}원)")

    url = "https://api.bithumb.com/public/ticker/ALL_KRW"
    try:
        res = HTTP_SESSION.get(url, timeout=6).json()
    except Exception as e:
        logging.error(f"빗썸 전체 시세 조회 실패: {e}")
        return

    if res.get("status") != "0000": return

    raw_list = []
    for sym, info in res["data"].items():
        if sym == "date" or sym.upper() in STABLE_COINS: continue
        if sym in held_or_queued_codes: continue
        try:
            close_p = float(info['closing_price'])
            if close_p < MIN_COIN_PRICE_KRW: continue  # 🛡️ 50원 미만 배제
            val_24h = float(info['acc_trade_value_24H'])
            if val_24h < MIN_24H_ACC_TRADE_VALUE: continue
            raw_list.append((sym, close_p, float(info['fluctate_rate_24H']), val_24h))
        except Exception: pass

    sorted_list = sorted(raw_list, key=lambda x: x[3], reverse=True)[:35]

    # 🎯 1시간 거래대금 6억 우선 적용 후 후보 부족 시 4억으로 폴백
    min_1h_threshold = PRIMARY_1H_TRADE_VAL
    for attempt in range(2):
        pulse_candidates = []
        range_candidates = []

        for sym, price, change, val_24h in sorted_list:
            c_1h = get_candles(sym, interval="1h", limit=25)
            time.sleep(0.02)
            c_15m = get_candles(sym, interval="15m", limit=30)
            time.sleep(0.02)
            c_5m = get_candles(sym, interval="5m", limit=10)
            time.sleep(0.02)

            slot_type, setup_data = evaluate_slot_candidates(sym, price, val_24h, c_1h, c_15m, c_5m, min_1h_threshold)
            if slot_type == "SLOT_1_PULSE":
                pulse_candidates.append(setup_data)
            elif slot_type == "SLOT_2_RANGE":
                range_candidates.append(setup_data)

        if len(pulse_candidates) + len(range_candidates) >= 2 or attempt == 1:
            break
        # 1차(6억)에서 후보가 2개 미만이면 2차(4억)로 자동 완화
        min_1h_threshold = FALLBACK_1H_TRADE_VAL
        logging.info(f"🔄 1시간 거래대금 기준 완화 폴백 적용: 6억 ➔ 4억 원")

    target_pool = []
    engine_type = ""
    
    if pulse_candidates:
        target_pool = sorted(pulse_candidates, key=lambda x: x['vol_surge_ratio'], reverse=True)[:5]
        engine_type = "SLOT_1_PULSE"
    elif range_candidates:
        target_pool = sorted(range_candidates, key=lambda x: x['quant']['rsi_15m'])[:5]
        engine_type = "SLOT_2_RANGE"

    if not target_pool:
        logging.info("⏸️ 조건 충족 후보가 없어 관망합니다.")
        return

    reflection_text = build_reflection_prompt()

    sys_prompt = (
        "You are an elite quantitative crypto hedge fund trader.\n"
        f"Context:\n{reflection_text}\n\n"
        "Rules:\n"
        "1. Evaluate the provided quant setups. Select UP TO 2 best setups with score >= 60.\n"
        "2. Keep entry_discount_pct reasonable (0.20% to 0.45% for pulse, 0.0% to 0.3% for range).\n"
        "3. Output valid JSON ONLY adhering strictly to the schema."
    )
    user_prompt = (
        f"Active Quant Engine: {engine_type}\n"
        f"Candidate Setups:\n{json.dumps(target_pool, ensure_ascii=False)}\n\n"
        "Schema: {\n"
        '  "selected_candidates": [\n'
        '    {\n'
        '      "symbol": "SYMBOL/KRW",\n'
        '      "score": 85,\n'
        '      "entry_discount_pct": 0.25,\n'
        '      "detailed_reason": "기술적 타점 및 기대 근거"\n'
        '    }\n'
        '  ]\n'
        "}"
    )

    res_raw = call_ai_api(sys_prompt, user_prompt)
    decision = clean_and_parse_json(res_raw)

    if not decision or "selected_candidates" not in decision:
        logging.info("⏸️ AI 응답 파싱 실패 또는 후보 없음")
        return

    candidates = decision.get("selected_candidates", [])
    if not candidates:
        logging.info("⏸️ AI 분석 결과 기준 충족 후보 없음")
        return

    now_iso = get_kst_now().isoformat()
    now_ts = time.time()
    added_to_queue = 0

    for cand in candidates[:2]:
        sym = cand.get("symbol", "")
        code = sym.split('/')[0].upper()
        score = int(cand.get("score", 0))

        if not code or score < 60:
            continue
        if code in held_or_queued_codes:
            continue

        curr_p = get_current_price(code)
        if not curr_p or curr_p < MIN_COIN_PRICE_KRW:
            continue

        chosen_setup = next((item for item in target_pool if item["code"] == code), target_pool[0])
        discount = max(float(cand.get("entry_discount_pct", 0.25)), 0.15)
        base_target_entry = round_to_bithumb_tick(curr_p * (1.0 - (discount / 100.0)))
        
        ob = get_bithumb_orderbook_10(code)
        final_target_entry = base_target_entry
        bid_ratio_entry = 0.5
        if ob and ob.get("max_bid_wall"):
            bid_ratio_entry = ob["bid_ratio"]
            wall_p = ob["max_bid_wall"]["price"]
            tick_sz = get_bithumb_tick_size(wall_p)
            adjusted_p = round_to_bithumb_tick(wall_p + tick_sz)
            if abs((adjusted_p - base_target_entry) / base_target_entry) <= 0.005 and adjusted_p <= curr_p:
                final_target_entry = adjusted_p

        plan_data = {
            "symbol": f"{code}/KRW",
            "code": code,
            "mode": chosen_setup.get("mode", "SCALPING"),
            "is_paper": PAPER_TRADING,
            "slot": chosen_setup.get("slot", engine_type),
            "score": score,
            "current_price": curr_p,
            "target_entry": final_target_entry,
            "sl_pct": chosen_setup.get("sl_pct", -1.5),
            "emergency_sl_pct": chosen_setup.get("emergency_sl_pct", -2.3),
            "timeout_mins": 10,
            "buy_amount_krw": actual_buy_krw,
            "ordered_volume": 0.0,
            "order_uuid": None,
            "order_placed": False,
            "bid_breach_start_time": None,    # 호가창 3초 붕괴 버퍼
            "entry_bid_ratio": bid_ratio_entry,
            "detailed_reason": cand.get("detailed_reason", "사유 미기재"),
            "created_at": now_iso,            # 🎯 AI 분석 완료 시점 (만료시간 엄수 기준)
            "created_timestamp": now_ts
        }

        target_queue[code] = plan_data
        added_to_queue += 1
        logging.info(f"📥 [대기열 등록] {code} (점수: {score}점, 타점: {final_target_entry:,.4f})")

    if added_to_queue > 0:
        server_state["target_queue"] = target_queue
        server_state["last_updated"] = now_iso
        save_json_file(STATE_FILE, server_state)
        
        targets_payload = {"updated_at": now_iso, "paper_trading": PAPER_TRADING, "queue": target_queue}
        save_json_file(TARGETS_FILE, targets_payload)
        sync_file_to_github(TARGETS_FILE, targets_payload)

# ==========================================
# 7. 실시간 감시 엔진 (웹소켓 연동 / 10호가벽 2틱 탈출 / 조건부 본절 락 / 30분 만료)
# ==========================================
async def realtime_execution_engine():
    global EMERGENCY_STOP, PAPER_TRADING, PENDING_PAPER_DRAIN
    logging.info(f"⚡ 웹소켓 기반 실시간 엔진 가동 (Build: {BUILD_VERSION})")
    last_strategy_run = 0

    threading.Thread(target=telegram_listener_thread, daemon=True).start()
    asyncio.create_task(bithumb_websocket_stream_worker())

    while True:
        try:
            now = time.time()
            now_dt = get_kst_now()

            # 5분 주기 퀀트 스크리닝 및 대기열 수집
            if now - last_strategy_run >= 300:
                last_strategy_run = now
                asyncio.create_task(asyncio.to_thread(execute_server_side_strategy))

            server_state = load_json_file(STATE_FILE, {})
            paper_db = load_json_file(PAPER_TRADES_FILE, {"active_positions": {}, "closed_trades": []})

            target_queue = server_state.get("target_queue", {})
            active_positions = paper_db.get("active_positions", {})
            closed_trades = paper_db.get("closed_trades", [])

            if PENDING_PAPER_DRAIN:
                real_active = [v for v in active_positions.values() if not v.get("is_paper", True)]
                if len(real_active) == 0:
                    PENDING_PAPER_DRAIN = False
                    PAPER_TRADING = True
                    server_state["paper_trading"] = True
                    server_state["pending_paper_drain"] = False
                    save_json_file(STATE_FILE, server_state)
                    send_telegram_msg("🎉 [모드 전환 완료] 모든 실전 포지션이 청산되어 모의투자(PAPER) 모드로 자동 전환되었습니다.")

            # [1] 점수 기반 대기열 2슬롯 순환 승격 및 인터락 감시
            mode_active_count = len([v for v in active_positions.values() if v.get("is_paper", True) == PAPER_TRADING])
            available_slots = max(0, MAX_HOLDING_COINS - mode_active_count)

            current_active_orders = len([
                v for v in target_queue.values()
                if v.get("is_paper", True) == PAPER_TRADING and v.get("order_placed", False)
            ])

            sorted_queue_items = sorted(
                list(target_queue.items()),
                key=lambda x: (x[1].get("score", 0), -x[1].get("created_timestamp", 0)),
                reverse=True
            )

            # 빈 슬롯이 있으면 점수 최상위 미주문 종목을 거래소 지정가 매수로 승격
            for code, plan in sorted_queue_items:
                if plan.get("is_paper", True) != PAPER_TRADING:
                    continue
                if current_active_orders >= available_slots:
                    break

                if not plan.get("order_placed", False):
                    succ, order_uuid, ord_vol = execute_real_limit_buy_order(code, plan["target_entry"], plan["buy_amount_krw"])
                    if succ:
                        plan["order_placed"] = True
                        plan["order_uuid"] = order_uuid
                        plan["ordered_volume"] = ord_vol
                        current_active_orders += 1
                        server_state["target_queue"] = target_queue
                        save_json_file(STATE_FILE, server_state)

                        mode_title = "🧪 [모의투자]" if PAPER_TRADING else "🔥 [실전매매]"
                        slot_title = "⚡ [슬롯 1]" if plan.get("slot") == "SLOT_1_PULSE" else "🌊 [슬롯 2]"
                        send_telegram_msg(
                            f"🎯 {mode_title} {slot_title} 대기열 상위 승격 및 지정가 예약 접수\n"
                            f"• 종목 : {plan['symbol']} (점수: {plan['score']}점)\n"
                            f"• 지정가 : {plan['target_entry']:,.4f} KRW (호가벽 1틱 앞)\n"
                            f"• 배정금 : {plan['buy_amount_krw']:,} KRW (수량: {ord_vol:,.4f})\n"
                            f"• 만료 예정: AI 분석 시각 기준 10분 엄수\n"
                            f"💡 선정 사유: {plan.get('detailed_reason')}"
                        )

            # 대기열 등록 종목 감시 (복사본 순회로 런타임 오류 방지)
            for code, plan in list(target_queue.items()):
                created_ts = plan.get("created_timestamp", now)
                elapsed_mins = (now - created_ts) / 60.0
                timeout_limit = plan.get("timeout_mins", 10)
                order_uuid = plan.get("order_uuid")

                # ① 만료 시간 엄수 (AI 분석 시점 기준 10분)
                if elapsed_mins >= timeout_limit:
                    logging.info(f"⌛ [{plan['symbol']}] AI 분석 기준 10분 경과로 대기열 및 거래소 주문 자동 취소")
                    if plan.get("order_placed", False) and order_uuid:
                        cancel_bithumb_order(order_uuid)
                    if code in target_queue:
                        del target_queue[code]
                    server_state["target_queue"] = target_queue
                    save_json_file(STATE_FILE, server_state)
                    send_telegram_msg(f"⌛ [{plan['symbol']}] 유효 시간(10분) 만료로 대기 주문을 철회하고 다음 대기 종목으로 순환합니다.")
                    continue

                # ② 타점 유효성 상실 검사 (5분봉 음봉 -2.0% 기준 유지)
                c_5m = get_candles(code, interval="5m", limit=5)
                invalidate_reason = ""
                if len(c_5m) >= 3:
                    last_candle = c_5m[-2]
                    candle_drop_pct = ((last_candle['open'] - last_candle['close']) / last_candle['open']) * 100.0
                    if candle_drop_pct >= 2.0:
                        invalidate_reason = f"직전 5분봉 투매 장대음봉(-{candle_drop_pct:.1f}%) 발생"

                btc_ok, btc_msg = check_btc_trend()
                if not btc_ok:
                    invalidate_reason = f"BTC 매크로 급락 경보 ({btc_msg})"

                # ③ 🎯 [호가창 3초 붕괴 버퍼 인터락]
                ob = get_bithumb_orderbook_10(code)
                if ob and ob.get("bid_ratio", 0.5) < 0.20:
                    if plan.get("bid_breach_start_time") is None:
                        plan["bid_breach_start_time"] = now
                        logging.info(f"⚠️ [{plan['symbol']}] 매수 잔량 비율 급감({ob['bid_ratio']*100:.1f}%) ➔ 3초 지속 버퍼 계측")
                    elif now - plan["bid_breach_start_time"] >= 3.0:
                        invalidate_reason = f"호가창 매수 잔량 20% 미만 붕괴 3초 지속 ({ob['bid_ratio']*100:.1f}%)"
                else:
                    if plan.get("bid_breach_start_time") is not None:
                        plan["bid_breach_start_time"] = None

                if invalidate_reason:
                    logging.info(f"🛑 [{plan['symbol']}] 타점 무효화 ({invalidate_reason}) ➔ 거래소 주문 철회 및 대기열 제거")
                    if plan.get("order_placed", False) and order_uuid:
                        cancel_bithumb_order(order_uuid)
                    if code in target_queue:
                        del target_queue[code]
                    server_state["target_queue"] = target_queue
                    save_json_file(STATE_FILE, server_state)
                    send_telegram_msg(f"🛑 [{plan['symbol']}] 타점 무효화로 주문을 철회했습니다.\n• 사유: {invalidate_reason}\n➔ 대기열 다음 순위 종목으로 슬롯을 채웁니다.")
                    continue

                # ④ 체결 검사
                if plan.get("order_placed", False):
                    curr_p = get_current_price(code)
                    is_filled = False

                    if plan.get("is_paper", True):
                        if curr_p and curr_p <= plan["target_entry"]:
                            is_filled = True
                    else:
                        order_state, exec_vol, fee = check_bithumb_order_status(order_uuid)
                        if order_state == "done":
                            is_filled = True
                        elif order_state == "cancel":
                            if code in target_queue:
                                del target_queue[code]
                            server_state["target_queue"] = target_queue
                            save_json_file(STATE_FILE, server_state)
                            continue

                    if is_filled:
                        real_entry_price = plan["target_entry"]
                        real_units = plan.get("ordered_volume", 0.0)

                        if not plan.get("is_paper", True):
                            account_avg_p, account_units = get_real_account_coin_info(code)
                            if account_avg_p and account_avg_p > 0:
                                real_entry_price = account_avg_p
                                real_units = account_units

                        actual_invested_krw = round(real_entry_price * real_units) if real_units > 0 else plan.get("buy_amount_krw", MIN_BUY_KRW)

                        logging.info(f"🎯 [{plan['symbol']}] 거래소 완전 체결 완료 ➔ 보유 포지션 등록")

                        active_positions[code] = {
                            "symbol": plan["symbol"],
                            "mode": plan.get("mode", "SCALPING"),
                            "is_paper": plan.get("is_paper", True),
                            "slot": plan.get("slot", "SLOT_1_PULSE"),
                            "entry_price": real_entry_price,
                            "highest_price": real_entry_price,
                            "buy_amount_krw": actual_invested_krw,
                            "units": real_units,
                            "sl_pct": plan["sl_pct"],                      # ATR 기본 손절선
                            "emergency_sl_pct": plan["emergency_sl_pct"],  # 비상 탈출선
                            "sl_breach_start_time": None,
                            "scratch_breach_start": None,
                            "killswitch_breach_start": None,
                            "entry_timestamp": now,
                            "entry_time": now_dt.isoformat(),
                            "locked_floor_profit_pct": 0.0,
                            "breakeven_locked": False,                     # +0.8% 도달 시 본절 락 플래그
                            "price_history": [(now, real_entry_price)]
                        }

                        if code in target_queue:
                            del target_queue[code]
                        paper_db["active_positions"] = active_positions
                        server_state["target_queue"] = target_queue
                        save_json_file(PAPER_TRADES_FILE, paper_db)
                        save_json_file(STATE_FILE, server_state)
                        sync_file_to_github(PAPER_TRADES_FILE, paper_db)

                        mode_str = "모의투자" if plan.get("is_paper", True) else "실전매매"
                        send_telegram_msg(
                            f"⚡ [체결 완료] - {mode_str} ({plan.get('slot')})\n"
                            f"• 종목 : {plan['symbol']} (점수: {plan['score']}점)\n"
                            f"• 체결가 : {real_entry_price:,.4f} KRW\n"
                            f"• 매수금 : {actual_invested_krw:,} KRW (수량: {real_units:,.4f})\n"
                            f"🛡️ 1차 손절선: {plan['sl_pct']}% (3초 버퍼) / 비상선: {plan['emergency_sl_pct']}%\n"
                            f"📈 익절: +1.2%부터 매수벽 2틱 위 선제 탈출 + 5단계 트레일링\n"
                            f"🔒 안전: +0.8% 도달 시 본절(-0.1%) 락 + 30분 만료 타임아웃\n"
                            f"⏰ 체결 시각: {now_dt.strftime('%m/%d %H:%M KST')}"
                        )

            # [2] 보유 포지션 실시간 감시 (호가벽 2틱 선제탈출 + 5단계 트레일링 + 본절락 + 30분 만료)
            for coin_code, pos in list(active_positions.items()):
                curr_p = get_current_price(coin_code)
                if not curr_p: continue

                entry_p = pos["entry_price"]
                entry_time = parse_dt_safe(pos.get("entry_time", ""))
                entry_ts = pos.get("entry_timestamp", now)
                elapsed_seconds = now - entry_ts
                if entry_time is None: continue

                hist = pos.get("price_history", [])
                hist.append((now, curr_p))
                hist = [(ts, p) for ts, p in hist if now - ts <= 3600]
                pos["price_history"] = hist

                curr_profit_pct = ((curr_p - entry_p) / entry_p) * 100.0

                if curr_p > pos.get("highest_price", entry_p):
                    pos["highest_price"] = curr_p

                highest_profit_pct = ((pos["highest_price"] - entry_p) / entry_p) * 100.0

                # 🔒 [조건부 본절 상향: 최고 수익률 +0.8% 도달 시 본절(-0.1%) 영구 락]
                if highest_profit_pct >= 0.8:
                    pos["breakeven_locked"] = True

                # 🎯 [5단계 정적 트레일링 파라미터: +1.2% 시작 & 타이트한 반락 0.20%~]
                trailing_active = False
                static_pullback = 0.20
                stage_floor_lock = 0.0

                if highest_profit_pct >= 5.0:
                    trailing_active = True
                    static_pullback = 0.50
                    stage_floor_lock = 4.30
                elif highest_profit_pct >= 3.5:
                    trailing_active = True
                    static_pullback = 0.40
                    stage_floor_lock = 3.00
                elif highest_profit_pct >= 2.5:
                    trailing_active = True
                    static_pullback = 0.30
                    stage_floor_lock = 2.10
                elif highest_profit_pct >= 1.8:
                    trailing_active = True
                    static_pullback = 0.25
                    stage_floor_lock = 1.45
                elif highest_profit_pct >= 1.2:
                    trailing_active = True
                    static_pullback = 0.20
                    stage_floor_lock = 0.90

                pos["locked_floor_profit_pct"] = max(pos.get("locked_floor_profit_pct", 0.0), stage_floor_lock)
                save_json_file(PAPER_TRADES_FILE, paper_db)

                should_close = False
                close_reason = ""
                actual_pullback = round(highest_profit_pct - curr_profit_pct, 2)

                # ① 🎯 [트레일링 익절: 상위 10호가 매수벽 2틱 위 선제 탈출]
                if trailing_active:
                    static_pullback_price = pos["highest_price"] * (1.0 - (static_pullback / 100.0))
                    static_floor_price = entry_p * (1.0 + (pos["locked_floor_profit_pct"] / 100.0))
                    static_trigger_price = max(static_pullback_price, static_floor_price)
                    
                    final_exit_trigger_price = static_trigger_price
                    exit_type_msg = f"정적 기준선 (고점대비 -{static_pullback}%, 하한 +{pos['locked_floor_profit_pct']}%)"

                    ob_exit = get_bithumb_orderbook_10(coin_code)
                    if ob_exit and ob_exit.get("max_bid_wall"):
                        wall_price = ob_exit["max_bid_wall"]["price"]
                        tick_size = get_bithumb_tick_size(wall_price)
                        wall_2tick_price = round_to_bithumb_tick(wall_price + (2 * tick_size))
                        
                        if wall_2tick_price > final_exit_trigger_price and wall_2tick_price <= curr_p:
                            final_exit_trigger_price = wall_2tick_price
                            exit_type_msg = f"호가벽 2틱 선제 탈출 (매수벽 {wall_price:,.4f} ➔ 2틱 위 {final_exit_trigger_price:,.4f})"

                    if curr_p <= final_exit_trigger_price or actual_pullback >= static_pullback or curr_profit_pct <= pos["locked_floor_profit_pct"]:
                        should_close = True
                        close_reason = f"📈 트레일링 익절 ({exit_type_msg} | 최고 +{highest_profit_pct:.2f}% ➔ 실현 {curr_profit_pct:.2f}%)"

                # ② 🚨 [비상 하드 손절선]
                elif curr_profit_pct <= pos.get("emergency_sl_pct", -2.4):
                    should_close = True
                    close_reason = f"🚨 비상 하드 손절선 도달 ({curr_profit_pct:.2f}%) 즉시 탈출"

                # ③ 🔒 [조건부 본절 락 탈출: +0.8% 이상 찍고 -0.1% 이하로 복귀 시]
                elif pos.get("breakeven_locked", False) and curr_profit_pct <= -0.10:
                    should_close = True
                    close_reason = f"🛡️ 고점(+{highest_profit_pct:.2f}%) 달성 후 평단가 복귀 본절 락 탈출 ({curr_profit_pct:.2f}%)"

                # ④ ⚡ [완화된 모멘텀 스크래치: 2분~4분 구간, -0.65% 이하 & 호가 20% 미만 3초 지속 시에만]
                elif (120.0 <= elapsed_seconds <= 240.0) and (highest_profit_pct <= 0.15) and (curr_profit_pct <= -0.65):
                    ob_scratch = get_bithumb_orderbook_10(coin_code)
                    if ob_scratch and ob_scratch.get("bid_ratio", 0.5) < 0.20:
                        if pos.get("scratch_breach_start") is None:
                            pos["scratch_breach_start"] = now
                        elif now - pos["scratch_breach_start"] >= 3.0:
                            should_close = True
                            close_reason = f"⚡ 수급 이탈 완화 스크래치 탈출 ({curr_profit_pct:.2f}%, 매수비율 {ob_scratch['bid_ratio']*100:.1f}%)"
                    else:
                        pos["scratch_breach_start"] = None

                # ⑤ 🛡️ [호가창 킬스위치]
                elif -0.7 <= curr_profit_pct <= -0.3:
                    ob_pos = get_bithumb_orderbook_10(coin_code)
                    if ob_pos and ob_pos.get("bid_ratio", 0.5) < 0.20:
                        if pos.get("killswitch_breach_start") is None:
                            pos["killswitch_breach_start"] = now
                        elif now - pos["killswitch_breach_start"] >= 3.0:
                            should_close = True
                            close_reason = f"🛡️ 호가창 매수벽 붕괴 킬스위치 선제 탈출 (매수잔량 비율 {ob_pos['bid_ratio']*100:.1f}%)"
                    else:
                        pos["killswitch_breach_start"] = None

                # ⑥ ⌛ [단순 명료한 타임아웃: 30분 경과 시 +1.0% 미만 전량 정리]
                elif elapsed_seconds >= 1800 and curr_profit_pct < 1.0:
                    should_close = True
                    close_reason = f"⌛ 30분 만료 탄력 소멸 시장가 정리 (수익률 {curr_profit_pct:.2f}%)"

                # ⑦ 🛡️ [기본 ATR 손절선 유지 + 3초 지속 버퍼]
                elif curr_profit_pct <= pos["sl_pct"]:
                    if pos.get("sl_breach_start_time") is None:
                        pos["sl_breach_start_time"] = now
                        logging.info(f"⚠️ [{pos['symbol']}] ATR 손절선({pos['sl_pct']}%) 터치 (현재: {curr_profit_pct:.2f}%) ➔ 3초 버퍼 계측")
                    elif now - pos["sl_breach_start_time"] >= 3.0:
                        should_close = True
                        close_reason = f"🛡️ 기본 ATR 손절선({pos['sl_pct']}%) 3초 지속 이탈 ({curr_profit_pct:.2f}%)"
                else:
                    if pos.get("sl_breach_start_time") is not None:
                        pos["sl_breach_start_time"] = None

                if should_close:
                    if not pos.get("is_paper", True):
                        execute_real_market_sell_order(coin_code, pos.get("units", 0.0))

                    profit_pct = round(curr_profit_pct, 2)
                    buy_krw = pos.get("buy_amount_krw", MIN_BUY_KRW)
                    profit_krw = round(buy_krw * (profit_pct / 100.0))

                    trajectory_summary = generate_trade_trajectory_summary(
                        pos, curr_p, curr_profit_pct, highest_profit_pct, elapsed_seconds, close_reason
                    )

                    closed_trades.append({
                        "symbol": pos["symbol"],
                        "is_paper": pos.get("is_paper", True),
                        "slot": pos.get("slot", "UNKNOWN"),
                        "entry_price": entry_p,
                        "exit_price": curr_p,
                        "buy_amount_krw": buy_krw,
                        "profit_krw": profit_krw,
                        "profit_pct": profit_pct,
                        "reason": close_reason,
                        "trajectory_summary": trajectory_summary,
                        "entry_time": pos.get("entry_time", ""),
                        "exit_time": now_dt.isoformat()
                    })

                    del active_positions[coin_code]
                    paper_db["active_positions"] = active_positions
                    paper_db["closed_trades"] = closed_trades
                    save_json_file(PAPER_TRADES_FILE, paper_db)
                    sync_file_to_github(PAPER_TRADES_FILE, paper_db)

                    sign_pct = "+" if profit_pct > 0 else ""
                    sign_krw = "+" if profit_krw > 0 else ""
                    icon = "🎉" if profit_pct > 0 else "🌧️"
                    mode_str = "모의투자" if pos.get("is_paper", True) else "실전매매"

                    exit_msg = f"""{icon} [청산 완료] - {mode_str} ({pos.get('slot')})
• 종목 : {pos['symbol']}
• 진입가 : {entry_p:,.4f} KRW ➔ 청산가 : {curr_p:,.4f} KRW
• 손익률 : {sign_pct}{profit_pct:.2f}% ({sign_krw}{profit_krw:,}원)
• 사유 : {close_reason}

📊 흐름 요약:
{trajectory_summary}"""
                    send_telegram_msg(exit_msg)

                    portfolio_msg = format_portfolio_status_msg(active_positions, closed_trades)
                    send_telegram_msg(portfolio_msg)

            # ⏱️ 0.5초 초정밀 폴링
            if len(active_positions) > 0 or len(target_queue) > 0:
                await asyncio.sleep(0.5)
            else:
                await asyncio.sleep(2.0)

        except Exception as e:
            logging.error(f"감시 루프 오류: {e}")
            await asyncio.sleep(2)

# ==========================================
# 8. 텔레그램 명령 리스너
# ==========================================
def telegram_listener_thread():
    global EMERGENCY_STOP, CIRCUIT_BREAKER_ACTIVE, LAST_TELEGRAM_UPDATE_ID, PAPER_TRADING, PENDING_PAPER_DRAIN, UPDATE_BASELINE_TIME
    if not TELEGRAM_BOT_TOKEN: return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"

    try:
        init_res = HTTP_SESSION.get(url, params={"timeout": 1}, timeout=5).json()
        if init_res.get("ok") and init_res.get("result"):
            LAST_TELEGRAM_UPDATE_ID = init_res["result"][-1]["update_id"]
            HTTP_SESSION.get(url, params={"offset": LAST_TELEGRAM_UPDATE_ID + 1, "timeout": 1}, timeout=5)
    except Exception:
        pass

    while True:
        try:
            params = {"offset": LAST_TELEGRAM_UPDATE_ID + 1, "timeout": 10}
            res = HTTP_SESSION.get(url, params=params, timeout=15).json()
            if res.get("ok"):
                for update in res.get("result", []):
                    LAST_TELEGRAM_UPDATE_ID = update["update_id"]
                    msg = update.get("message", {})
                    text = msg.get("text", "").strip()
                    sender_chat_id = str(msg.get("chat", {}).get("id", "")).strip()

                    if TELEGRAM_CHAT_ID and sender_chat_id != TELEGRAM_CHAT_ID:
                        continue

                    server_state = load_json_file(STATE_FILE, {})
                    paper_db = load_json_file(PAPER_TRADES_FILE, {"active_positions": {}, "closed_trades": []})
                    active_positions = paper_db.get("active_positions", {})
                    closed_trades = paper_db.get("closed_trades", [])
                    target_queue = server_state.get("target_queue", {})

                    if text == "/reset_stats":
                        now_kst_iso = get_kst_now().isoformat()
                        UPDATE_BASELINE_TIME = now_kst_iso
                        server_state["update_baseline_time"] = now_kst_iso
                        save_json_file(STATE_FILE, server_state)
                        send_telegram_msg(f"⏱️ [누적 성과 기준 시각 초기화]\n지금 시각({get_kst_now().strftime('%m/%d %H:%M KST')}) 이후 발생한 매매부터 누적 손익 및 승률로 새롭게 집계됩니다.")

                    elif text == "/real":
                        if not PAPER_TRADING and not PENDING_PAPER_DRAIN:
                            send_telegram_msg("ℹ️ 이미 실전매매(REAL) 모드로 동작 중입니다.")
                            continue

                        for p in target_queue.values():
                            if p.get("order_uuid"):
                                cancel_bithumb_order(p.get("order_uuid"))
                        server_state["target_queue"] = {}

                        PENDING_PAPER_DRAIN = False
                        PAPER_TRADING = False
                        
                        cleared_count = len(active_positions)
                        now_iso = get_kst_now().isoformat()
                        
                        for code, pos in list(active_positions.items()):
                            curr_p = get_current_price(code) or pos["entry_price"]
                            profit_pct = round(((curr_p - pos["entry_price"]) / pos["entry_price"]) * 100.0, 2)
                            buy_krw = pos.get("buy_amount_krw", MIN_BUY_KRW)
                            profit_krw = round(buy_krw * (profit_pct / 100.0))
                            
                            closed_trades.append({
                                "symbol": pos["symbol"],
                                "is_paper": pos.get("is_paper", True),
                                "slot": pos.get("slot", "UNKNOWN"),
                                "entry_price": pos["entry_price"],
                                "exit_price": curr_p,
                                "buy_amount_krw": buy_krw,
                                "profit_krw": profit_krw,
                                "profit_pct": profit_pct,
                                "reason": "실전 모드 전환에 따른 가상 포지션 초기화",
                                "entry_time": pos.get("entry_time", ""),
                                "exit_time": now_iso
                            })
                            del active_positions[code]

                        paper_db["active_positions"] = {}
                        paper_db["closed_trades"] = closed_trades
                        server_state["paper_trading"] = False
                        server_state["pending_paper_drain"] = False
                        save_json_file(PAPER_TRADES_FILE, paper_db)
                        save_json_file(STATE_FILE, server_state)
                        sync_file_to_github(PAPER_TRADES_FILE, paper_db)

                        tot_asset, avail_krw = get_bithumb_account_summary()
                        avail_str = f"{avail_krw:,.0f} KRW" if avail_krw is not None else "조회 실패"
                        tot_str = f"{tot_asset:,.0f} KRW" if tot_asset is not None else "조회 실패"
                        send_telegram_msg(
                            f"🔥 [모드 전환 완료: 실전매매 가동]\n"
                            f"• 기존 가상 포지션({cleared_count}개)을 정리했습니다.\n"
                            f"• 빗썸 실계좌 총 자산: {tot_str}\n"
                            f"• 빗썸 실계좌 가용 잔고: {avail_str}\n"
                            f"• 지금부터 대기열 최상위 종목들이 거래소 호가창에 직접 예약됩니다."
                        )

                    elif text == "/paper":
                        if PAPER_TRADING:
                            send_telegram_msg("ℹ️ 이미 모의투자(PAPER) 모드로 동작 중입니다.")
                            continue

                        for p in target_queue.values():
                            if p.get("order_uuid"):
                                cancel_bithumb_order(p.get("order_uuid"))
                        server_state["target_queue"] = {}
                        save_json_file(STATE_FILE, server_state)

                        real_positions = [v for v in active_positions.values() if not v.get("is_paper", True)]
                        if len(real_positions) > 0:
                            PENDING_PAPER_DRAIN = True
                            server_state["pending_paper_drain"] = True
                            save_json_file(STATE_FILE, server_state)
                            held_names = [v['symbol'] for v in real_positions]
                            send_telegram_msg(
                                f"⏳ [모드 전환 예약: 모의투자 대기]\n"
                                f"• 신규 실전 주문 접수를 즉시 차단했습니다.\n"
                                f"• 현재 실전 보유 종목: {', '.join(held_names)} ({len(held_names)}개)\n"
                                f"• 보유 포지션이 정상 청산되는 즉시 모의투자 모드로 자동 전환됩니다."
                            )
                        else:
                            PAPER_TRADING = True
                            PENDING_PAPER_DRAIN = False
                            server_state["paper_trading"] = True
                            server_state["pending_paper_drain"] = False
                            save_json_file(STATE_FILE, server_state)
                            send_telegram_msg("🧪 [모드 전환 완료] 실전 보유 종목이 없어 즉시 모의투자(PAPER) 모드로 전환되었습니다.")

                    elif text == "/status":
                        mode_tag = "🧪 모의투자" if PAPER_TRADING else "🔥 실전매매"
                        mode_active = {k: v for k, v in active_positions.items() if v.get("is_paper", True) == PAPER_TRADING}
                        held = [f"{v['symbol']}({v.get('slot', 'S1')})" for v in mode_active.values()]
                        
                        queue_items = []
                        for code, plan in server_state.get('target_queue', {}).items():
                            sym = plan.get('symbol', f"{code}/KRW")
                            score = plan.get('score', 0)
                            status_label = "주문중" if plan.get('order_placed') else "대기중"
                            queue_items.append(f"{sym}({score}점|{status_label})")
                        
                        if EMERGENCY_STOP:
                            status_str = "🛑 일시정지 (STOP)"
                        elif CIRCUIT_BREAKER_ACTIVE:
                            status_str = "🚨 일일 서킷브레이커 발동 중"
                        elif PENDING_PAPER_DRAIN:
                            status_str = "⏳ 모의투자 전환 대기 중 (실전 포지션 소진 중)"
                        else:
                            status_str = "🟢 WebSocket 스트림 감시 중 (RUNNING)"

                        mode_closed = [t for t in closed_trades if t.get("is_paper", True) == PAPER_TRADING]

                        res_msg = f"""📊 [시스템 상태 보고]
• 현재 모드: {mode_tag}
• 실행 상태: {status_str}
• 점수 기반 대기열: {', '.join(queue_items) if queue_items else '(대기열 비어있음)'}
• 보유 종목: {', '.join(held) if held else '(없음)'} ({len(held)}/2개)
• 전용 복기 기록: {len(mode_closed)}건"""
                        send_telegram_msg(res_msg)

                    elif text == "/log":
                        summary_msg = format_portfolio_status_msg(active_positions, closed_trades)
                        send_telegram_msg(summary_msg)

                    elif text == "/stop":
                        EMERGENCY_STOP = True
                        for p in target_queue.values():
                            if p.get("order_uuid"):
                                cancel_bithumb_order(p.get("order_uuid"))
                        server_state["target_queue"] = {}
                        save_json_file(STATE_FILE, server_state)
                        send_telegram_msg("🛑 [인터락 작동] 감시 일시 중단 및 대기열/주문을 전량 취소했습니다.")

                    elif text == "/start":
                        EMERGENCY_STOP = False
                        CIRCUIT_BREAKER_ACTIVE = False
                        send_telegram_msg("▶️ [인터락 해제] 신규 매수 감시가 정상 재개되었습니다.")

                    elif text == "/update":
                        send_telegram_msg("🔄 [원격 업데이트] 최신 코드를 다운로드하고 서비스를 재시작합니다...")
                        try:
                            HTTP_SESSION.get(url, params={"offset": LAST_TELEGRAM_UPDATE_ID + 1, "timeout": 1}, timeout=3)
                        except Exception:
                            pass

                        def do_restart():
                            time.sleep(2.0)
                            try:
                                subprocess.run(["git", "stash"], cwd=PROJECT_DIR, timeout=10)
                                subprocess.run(["git", "pull", "origin", "main"], cwd=PROJECT_DIR, timeout=20)
                                subprocess.run(["sudo", "systemctl", "restart", "autotrade.service"])
                            except Exception as ex:
                                logging.error(f"재시작 실패: {ex}")

                        threading.Thread(target=do_restart, daemon=True).start()

            time.sleep(1)
        except Exception:
            time.sleep(3)

if __name__ == "__main__":
    notify_startup_once()
    asyncio.run(realtime_execution_engine())
