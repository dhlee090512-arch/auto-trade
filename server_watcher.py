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
import websockets
from datetime import datetime, timedelta, timezone
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

# ==========================================
# 0. 전역 설정 및 퀀트 파라미터 (Dual-Track + OB 2.0)
# ==========================================
BUILD_VERSION = "2026.10.12-v20-dual-alpha-pro-test-run"
MAX_HOLDING_COINS = 2                          
MIN_BUY_KRW = 6000                             
MIN_COIN_PRICE_KRW = 50.0                      
MAX_TICK_RATIO_PCT = 0.08                      
MIN_24H_ACC_TRADE_VALUE = 1_000_000_000        
PRIMARY_1H_TRADE_VAL = 300_000_000             
FALLBACK_1H_TRADE_VAL = 180_000_000            
MIN_15M_ATR_PCT = 1.10                         
MARKET_DROP_RATIO_LIMIT = 0.80                 
DAILY_LOSS_LIMIT_PCT = -3.0                    
COOLDOWN_MINUTES = 120                         # 🛑 손절 종목 2시간 쿨다운

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

PAPER_TRADING = True
PENDING_PAPER_DRAIN = False
UPDATE_BASELINE_TIME = None
EMERGENCY_STOP = False
CIRCUIT_BREAKER_ACTIVE = False
LAST_TELEGRAM_UPDATE_ID = 0
NO_CANDIDATE_CYCLE_COUNT = 0
LAST_NO_CANDIDATE_REASON = "시스템 초기화 완료"

# 🚀 1회성 동작 테스트용 플래그
IS_FIRST_RUN = True

WS_TICKER_CACHE = {}
WS_ORDERBOOK_CACHE = {}

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
# 1. 영구 상태 로드
# ==========================================
def load_json_file(file_path, default_value):
    if os.path.exists(file_path):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception: pass
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
        logging.info(f"🔄 [버전 업데이트] 성과 기준 시각 갱신: {now_kst_iso} (Build: {BUILD_VERSION})")
    
    UPDATE_BASELINE_TIME = state["update_baseline_time"]
    state["last_started_at"] = now_kst_iso
    save_json_file(STATE_FILE, state)

init_server_state()

# ==========================================
# 2. 텔레그램 리포트 & GitHub 동기화
# ==========================================
def send_telegram_msg(msg: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        HTTP_SESSION.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=5)
    except Exception as e: logging.error(f"텔레그램 오류: {e}")

def notify_startup_once():
    now_ts = time.time()
    last_notify_ts = 0.0
    if os.path.exists(RESTART_FLAG_FILE):
        try:
            with open(RESTART_FLAG_FILE, "r") as f:
                last_notify_ts = float(f.read().strip())
        except Exception: pass

    if now_ts - last_notify_ts > 600:
        with open(RESTART_FLAG_FILE, "w") as f:
            f.write(str(now_ts))
        mode_str = "🧪 모의투자" if PAPER_TRADING else "🔥 실전매매"
        base_str = parse_dt_safe(UPDATE_BASELINE_TIME).strftime("%m/%d %H:%M KST") if UPDATE_BASELINE_TIME else "-"
        send_telegram_msg(
            f"🎯 [오라클 서버] OB 2.0 Dual-Track 엔진 가동 ({BUILD_VERSION})\n"
            f"• 모드: {mode_str}\n"
            f"• 비대칭 배분: Track C(30%) 집중 / Track A(15%) 방어\n"
            f"• 호가창 2.0: 스프레드/허수벽 차단, 1~3호가 심도 기반 0초 칼손절\n"
            f"• 쿨다운: 손절 종목 120분(2시간) 진입 전면 차단\n"
            f"• 특이사항: 최초 1회 동작 테스트 모드 가동\n"
            f"• 기준시각: {base_str}\n\n"
            f"📱 명령어: /status, /track, /log, /paper, /real, /stop, /start, /update"
        )

def sync_file_to_github(file_path, content_data):
    if not GH_TOKEN: return
    url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/contents/{file_path}"
    headers = {"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    sha = None
    try:
        res = HTTP_SESSION.get(url, headers=headers, timeout=5)
        if res.status_code == 200: sha = res.json().get('sha')
    except Exception: pass
        
    json_str = json.dumps(content_data, indent=2, ensure_ascii=False)
    encoded = base64.b64encode(json_str.encode('utf-8')).decode('utf-8')
    payload = {"message": f"update: {file_path}", "content": encoded}
    if sha: payload["sha"] = sha
    try: HTTP_SESSION.put(url, headers=headers, json=payload, timeout=8)
    except Exception: pass

def format_portfolio_status_msg(active_positions, closed_trades):
    current_mode_is_paper = PAPER_TRADING
    mode_tag_name = "🧪 모의투자" if current_mode_is_paper else "🔥 실전매매"
    mode_active = {k: v for k, v in active_positions.items() if v.get("is_paper", True) == current_mode_is_paper}
    held_symbols = [f"{v['symbol']}[{v.get('track', 'A')}]" for v in mode_active.values()]
    held_str = f"{', '.join(held_symbols)} ({len(held_symbols)}/2개)" if held_symbols else "(없음)"

    mode_closed = [t for t in closed_trades if t.get("is_paper", True) == current_mode_is_paper]
    recent_10 = mode_closed[-10:][::-1] if mode_closed else []
    
    if not recent_10:
        trades_str = f"• [{mode_tag_name}] 매도 이력이 없습니다."
        win_rate_10, profit_10_krw = 0.0, 0
    else:
        trade_lines = []
        wins_10, profit_10_krw = 0, 0
        for idx, t in enumerate(recent_10, 1):
            p_pct, p_krw = t.get('profit_pct', 0.0), t.get('profit_krw', 0)
            symbol, track = t.get('symbol', 'UNKNOWN'), t.get('track', 'A')
            dt_obj = parse_dt_safe(t.get('exit_time', ''))
            time_disp = dt_obj.strftime("%m/%d %H:%M") if dt_obj else "-"
            sign_pct, sign_k = "+" if p_pct > 0 else "", "+" if p_krw > 0 else ""
            trade_lines.append(f"{idx}. {symbol}[{track}]: {sign_k}{p_krw:,}원 ({sign_pct}{p_pct:.2f}%) | {time_disp}")
            if p_pct > 0: wins_10 += 1
            profit_10_krw += p_krw
        trades_str = "\n".join(trade_lines)
        win_rate_10 = round((wins_10 / len(recent_10)) * 100, 1)

    baseline_dt = parse_dt_safe(UPDATE_BASELINE_TIME)
    tot_tr, tot_win, tot_krw = 0, 0, 0
    for t in mode_closed:
        exit_dt = parse_dt_safe(t.get('exit_time', ''))
        if baseline_dt and exit_dt and exit_dt >= baseline_dt:
            tot_tr += 1
            tot_krw += t.get('profit_krw', 0)
            if t.get('profit_pct', 0.0) > 0: tot_win += 1

    if tot_tr > 0:
        cum_stats_str = f"• 거래: {tot_tr}건 ({tot_win}승 | 승률 {round(tot_win/tot_tr*100,1)}%)\n• 누적 손익: {'+' if tot_krw > 0 else ''}{tot_krw:,} KRW"
    else:
        cum_stats_str = f"• 누적 매도 이력 없음"

    return f"💼 [{mode_tag_name} A/C 프로 운용 현황]\n• 보유: {held_str}\n\n📜 [최근 10건]\n{trades_str}\n\n📊 승률: {win_rate_10}%\n💰 수익: {'+' if profit_10_krw>0 else ''}{profit_10_krw:,} KRW\n━━━━━━━━━━━━\n📈 [누적 성과]\n{cum_stats_str}"

def generate_trade_trajectory_summary(pos: dict, highest_profit_pct: float, elapsed_seconds: float, close_reason: str) -> str:
    mins, secs = int(elapsed_seconds // 60), int(elapsed_seconds % 60)
    dur_str = f"{mins}분 {secs}초" if mins > 0 else f"{secs}초"
    track = pos.get("track", "A")
    if "익절" in close_reason: return f"[{track}트랙] 최고 +{highest_profit_pct:.2f}% 도달 후 반락 익절 (보유 {dur_str})"
    if "본절" in close_reason: return f"[{track}트랙] 최고 +{highest_profit_pct:.2f}% 상승 후 힘 빠짐 방어 (보유 {dur_str})"
    if "미반등" in close_reason or "정리" in close_reason: return f"[{track}트랙] 타임아웃(최고 +{highest_profit_pct:.2f}%) 슬롯 회수 (보유 {dur_str})"
    if "손절" in close_reason or "즉시 컷" in close_reason or "붕괴" in close_reason: return f"[{track}트랙] 1차 방어선 도달 및 OB/심도 붕괴로 즉시 컷 (보유 {dur_str})"
    return f"[{track}트랙] 보유 {dur_str} (고점 +{highest_profit_pct:.2f}%) 후 청산"

# ==========================================
# 3. 빗썸 호가창 및 실전 API (OB 2.0 로직 탑재)
# ==========================================
def get_bithumb_jwt_headers(query_params: dict = None):
    payload = {'access_key': BITHUMB_API_KEY, 'nonce': str(uuid.uuid4()), 'timestamp': round(time.time() * 1000)}
    if query_params:
        query_string = urllib.parse.urlencode(query_params).encode()
        payload['query_hash'] = hashlib.sha512(query_string).hexdigest()
        payload['query_hash_alg'] = 'SHA512'
    token = jwt.encode(payload, BITHUMB_SECRET_KEY, algorithm='HS256')
    return {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}

def get_bithumb_tick_size(price: float) -> float:
    if price < 1: return 0.0001
    elif price < 10: return 0.001
    elif price < 100: return 0.01
    elif price < 1000: return 1.0
    elif price < 5000: return 1.0
    elif price < 10000: return 5.0
    elif price < 50000: return 10.0
    elif price < 100000: return 50.0
    elif price < 500000: return 100.0
    elif price < 1000000: return 500.0
    else: return 1000.0

def round_to_bithumb_tick(price: float) -> float:
    if price < 1: return round(price, 4)
    elif price < 10: return round(price, 3)
    elif price < 100: return round(price, 2)
    elif price < 1000: return float(int(price))
    elif price < 5000: return float(int(price))
    elif price < 10000: return float(int(price / 5.0) * 5)
    elif price < 50000: return float(int(price / 10.0) * 10)
    elif price < 100000: return float(int(price / 50.0) * 50)
    elif price < 500000: return float(int(price / 100.0) * 100)
    elif price < 1000000: return float(int(price / 500.0) * 500)
    else: return float(int(price / 1000.0) * 1000)

def get_current_price(coin_code: str):
    cached = WS_TICKER_CACHE.get(coin_code)
    if cached and (time.time() - cached.get("time", 0) <= 2.0): return cached["price"]
    try:
        res = HTTP_SESSION.get(f"https://api.bithumb.com/public/ticker/{coin_code}_KRW", timeout=1.5).json()
        if res.get("status") == "0000":
            WS_TICKER_CACHE[coin_code] = {"price": float(res["data"]["closing_price"]), "time": time.time()}
            return WS_TICKER_CACHE[coin_code]["price"]
    except Exception: pass
    return cached.get("price") if cached else None

def parse_orderbook_data(data, coin_code):
    bids = [{"price": float(b["price"] if "price" in b else b[0]), "quantity": float(b["quantity"] if "quantity" in b else b[1])} for b in data.get("bids", [])[:10]]
    asks = [{"price": float(a["price"] if "price" in a else a[0]), "quantity": float(a["quantity"] if "quantity" in a else a[1])} for a in data.get("asks", [])[:10]]
    
    bid1 = bids[0]["price"] if bids else 0
    ask1 = asks[0]["price"] if asks else 0
    spread_pct = ((ask1 - bid1) / bid1 * 100) if bid1 > 0 else 0.0

    total_bid_qty = sum(b["quantity"] for b in bids)
    total_ask_qty = sum(a["quantity"] for a in asks)
    total_qty = total_bid_qty + total_ask_qty
    bid_ratio = (total_bid_qty / total_qty) if total_qty > 0 else 0.5
    
    # Top 3 호가 심도 (OB 2.0)
    top3_bid_qty = sum(b["quantity"] for b in bids[:3])
    top3_ask_qty = sum(a["quantity"] for a in asks[:3])
    top3_qty = top3_bid_qty + top3_ask_qty
    top3_bid_ratio = (top3_bid_qty / top3_qty) if top3_qty > 0 else 0.5

    # 허수 매수벽 차단 (OB 2.0)
    max_bid_wall = max(bids, key=lambda x: x["quantity"]) if bids else None
    if max_bid_wall:
        wall_krw = max_bid_wall["price"] * max_bid_wall["quantity"]
        if wall_krw < 20_000_000 or (max_bid_wall["quantity"] / total_bid_qty) < 0.20:
            max_bid_wall = None  # 조건 미달 시 허수벽 무시

    parsed = {
        "bids": bids, "asks": asks,
        "bid_ratio": round(bid_ratio, 3),
        "top3_bid_ratio": round(top3_bid_ratio, 3),
        "spread_pct": round(spread_pct, 3),
        "max_bid_wall": max_bid_wall,
        "time": time.time()
    }
    WS_ORDERBOOK_CACHE[coin_code] = parsed
    return parsed

def get_bithumb_orderbook_10(coin_code: str):
    cached = WS_ORDERBOOK_CACHE.get(coin_code)
    if cached and (time.time() - cached.get("time", 0) <= 2.0): return cached
    try:
        res = HTTP_SESSION.get(f"https://api.bithumb.com/public/orderbook/{coin_code}_KRW", timeout=1.5).json()
        if res.get("status") == "0000": return parse_orderbook_data(res["data"], coin_code)
    except Exception: pass
    return cached

async def bithumb_websocket_stream_worker():
    ws_url = "wss://pubwss.bithumb.com/pub/ws"
    while True:
        try:
            async with websockets.connect(ws_url, ping_interval=30, ping_timeout=10) as ws:
                last_sub_symbols = set()
                while True:
                    s_state = load_json_file(STATE_FILE, {})
                    p_db = load_json_file(PAPER_TRADES_FILE, {"active_positions": {}, "closed_trades": []})
                    targets = [f"{c}_KRW" for c in set(p_db.get("active_positions", {}).keys()).union(set(s_state.get("target_queue", {}).keys())) if c]
                    
                    if set(targets) != last_sub_symbols and len(targets) > 0:
                        await ws.send(json.dumps({"type": "transaction", "symbols": targets}))
                        await ws.send(json.dumps({"type": "orderbookdepth", "symbols": targets}))
                        last_sub_symbols = set(targets)

                    try:
                        raw_msg = await asyncio.wait_for(ws.recv(), timeout=2.0)
                        data = json.loads(raw_msg)
                        m_type, content = data.get("type"), data.get("content", {})
                        if m_type == "transaction":
                            for i in content.get("list", []):
                                c = i.get("symbol", "").split('_')[0].upper()
                                p = float(i.get("price", 0.0))
                                if c and p > 0: WS_TICKER_CACHE[c] = {"price": p, "time": time.time()}
                        elif m_type == "orderbookdepth":
                            for i in content.get("list", []):
                                parse_orderbook_data(i, i.get("symbol", "").split('_')[0].upper())
                    except asyncio.TimeoutError: continue
        except Exception: await asyncio.sleep(3.0)

def get_bithumb_account_summary():
    if not BITHUMB_API_KEY: return None, None
    try:
        res = HTTP_SESSION.get("https://api.bithumb.com/v1/accounts", headers=get_bithumb_jwt_headers(), timeout=2.5).json()
        if isinstance(res, list):
            t_krw, a_krw = 0.0, 0.0
            for acc in res:
                curr, bal, locked = acc.get("currency", ""), float(acc.get("balance", 0.0)), float(acc.get("locked", 0.0))
                tot = bal + locked
                if curr == "KRW": a_krw, t_krw = bal, t_krw + tot
                elif curr not in ["P", "POINT"] and tot > 0:
                    p = get_current_price(curr) or float(acc.get("avg_buy_price", 0.0))
                    if p > 0: t_krw += (tot * p)
            return round(t_krw, 2), round(a_krw, 2)
    except Exception: pass
    return None, None

def get_real_account_coin_info(coin_code: str):
    if PAPER_TRADING: return None, None
    try:
        res = HTTP_SESSION.get("https://api.bithumb.com/v1/accounts", headers=get_bithumb_jwt_headers(), timeout=2.5).json()
        for acc in res:
            if acc.get("currency", "").upper() == coin_code.upper():
                return float(acc.get("avg_buy_price", 0.0)), float(acc.get("balance", 0.0)) + float(acc.get("locked", 0.0))
    except Exception: pass
    return None, None

def execute_real_limit_buy_order(coin_code: str, price: float, krw_amount: float):
    if PAPER_TRADING: return True, "mock_order_" + str(uuid.uuid4())[:8], (krw_amount / price)
    try:
        vol = round(krw_amount / price, 4)
        body = {"market": f"KRW-{coin_code}", "side": "bid", "volume": str(vol), "price": str(price), "ord_type": "limit"}
        res = HTTP_SESSION.post("https://api.bithumb.com/v1/orders", json=body, headers=get_bithumb_jwt_headers(body), timeout=3.0).json()
        if "uuid" in res: return True, res["uuid"], vol
        return False, str(res), 0.0
    except Exception as e: return False, str(e), 0.0

def check_bithumb_order_status(order_uuid: str):
    if PAPER_TRADING or not order_uuid or order_uuid.startswith("mock_"): return "wait", 0.0, 0.0
    try:
        p = {"uuid": order_uuid}
        res = HTTP_SESSION.get("https://api.bithumb.com/v1/order", params=p, headers=get_bithumb_jwt_headers(p), timeout=2.5).json()
        return res.get("state", "wait"), float(res.get("executed_volume", 0.0)), float(res.get("paid_fee", 0.0))
    except Exception: return "wait", 0.0, 0.0

def get_bithumb_order_execution_detail(order_uuid: str):
    if PAPER_TRADING or not order_uuid or order_uuid.startswith("mock_"): return None
    try:
        p = {"uuid": order_uuid}
        res = HTTP_SESSION.get("https://api.bithumb.com/v1/order", params=p, headers=get_bithumb_jwt_headers(p), timeout=3.0).json()
        if "uuid" in res:
            ef, ev = float(res.get("executed_funds", 0.0)), float(res.get("executed_volume", 0.0))
            return {"executed_funds": ef, "executed_volume": ev, "paid_fee": float(res.get("paid_fee", 0.0)), "avg_price": round(ef/ev, 4) if ev > 0 else 0.0}
    except Exception: pass
    return None

def cancel_bithumb_order(order_uuid: str):
    if PAPER_TRADING or not order_uuid or order_uuid.startswith("mock_"): return True
    try:
        p = {"uuid": order_uuid}
        return "uuid" in HTTP_SESSION.delete("https://api.bithumb.com/v1/order", params=p, headers=get_bithumb_jwt_headers(p), timeout=2.5).json()
    except Exception: return False

def execute_real_market_sell_order(coin_code: str, units: float):
    if PAPER_TRADING: return True, "mock_sell_" + str(uuid.uuid4())[:8]
    try:
        body = {"market": f"KRW-{coin_code}", "side": "ask", "volume": str(units), "ord_type": "market"}
        res = HTTP_SESSION.post("https://api.bithumb.com/v1/orders", json=body, headers=get_bithumb_jwt_headers(body), timeout=3.0).json()
        if "uuid" in res: return True, res["uuid"]
        return False, str(res)
    except Exception as e: return False, str(e)

def get_candles(coin_code, interval="15m", limit=40):
    try:
        res = HTTP_SESSION.get(f"https://api.bithumb.com/public/candlestick/{coin_code}_KRW/{interval}", timeout=3.0).json()
        if res.get("status") == "0000":
            return [{"timestamp": int(c[0]), "open": float(c[1]), "close": float(c[2]), "high": float(c[3]), "low": float(c[4]), "volume": float(c[5])} for c in res['data'][-limit:]]
    except Exception: pass
    return []

# ==========================================
# 4. 퀀트 & OB 2.0 스캐너 (Track A / C 최적화)
# ==========================================
def calculate_atr(candles, period=14):
    if len(candles) < period + 1: return 0.0
    trs = [max(c['high'] - c['low'], abs(c['high'] - pc['close']), abs(c['low'] - pc['close'])) for c, pc in zip(candles[1:], candles[:-1])]
    return round(sum(trs[-period:]) / period, 4)

def calculate_ema(candles, period=20):
    if len(candles) < period: return 0.0
    closes = [c['close'] for c in candles]
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]: ema = (price - ema) * k + ema
    return ema

def calculate_quant_features(candles_1h, candles_15m):
    c15 = candles_15m[-1]
    curr_p = c15['close']
    lows = [c['low'] for c in candles_15m[-10:]]
    highs = [c['high'] for c in candles_15m[-10:]]
    box_low, box_high = min(lows), max(highs)
    
    vol_avg_15m = sum(c['volume'] for c in candles_15m[-11:-1]) / 10.0 if len(candles_15m) >= 11 else 1.0
    
    return {
        "box_low": box_low,
        "box_high": box_high,
        "box_range_pct": round(((box_high - box_low) / box_low) * 100.0, 2) if box_low > 0 else 0.0,
        "dist_to_low_pct": round(((curr_p - box_low) / box_low) * 100.0, 2) if box_low > 0 else 999.0,
        "atr_15m_pct": round((calculate_atr(candles_15m, 14) / curr_p) * 100.0, 2) if curr_p > 0 else 0.0,
        "curr_price": curr_p,
        "vol_surge_ratio": round(c15['volume'] / vol_avg_15m, 2) if vol_avg_15m > 0 else 1.0,
        "vol_avg_15m": vol_avg_15m,
        "tick_ratio_pct": round((get_bithumb_tick_size(curr_p) / curr_p) * 100.0, 3) if curr_p > 0 else 1.0,
        "recent_1h_trade_val": candles_1h[-1]['volume'] * candles_1h[-1]['close'] if candles_1h else 0.0
    }

def is_cooling_down(code, closed_trades, now_ts):
    # 🛑 2시간 쿨다운 (연속 손절 방지)
    for t in reversed(closed_trades):
        if t.get('symbol', '').startswith(code) and t.get('profit_pct', 0.0) < -0.01:
            exit_dt = parse_dt_safe(t.get('exit_time'))
            if exit_dt and (now_ts - exit_dt.timestamp()) < (COOLDOWN_MINUTES * 60):
                return True
            break
    return False

def scan_ac_candidates(sym, price, val_24h, candles_1h, candles_15m, candles_5m, min_1h_val):
    if len(candles_1h) < 20 or len(candles_15m) < 20 or len(candles_5m) < 5: return None
    if price < MIN_COIN_PRICE_KRW: return None

    q = calculate_quant_features(candles_1h, candles_15m)
    if q["tick_ratio_pct"] > MAX_TICK_RATIO_PCT or q["atr_15m_pct"] < MIN_15M_ATR_PCT: return None
    if q["recent_1h_trade_val"] < min_1h_val: return None

    ob = get_bithumb_orderbook_10(sym)
    if not ob: return None
    
    # 🛡️ 스프레드 갭 0.25% 이상 스킵 (즉발 손실 방지)
    if ob.get("spread_pct", 0) > 0.25: return None
    bid_ratio = ob.get("bid_ratio", 0.5)

    # -------------------------------------------------------------
    # [Track C] 1차 눌림목 (깊은 40~61.8% 피보나치 + 양봉 턴)
    # -------------------------------------------------------------
    if len(candles_15m) >= 12:
        spike_candle = None
        for c in candles_15m[-5:-1]:
            if ((c['close'] - c['open']) / c['open'] * 100.0) >= 3.8 and (c['volume'] / q["vol_avg_15m"] if q["vol_avg_15m"] else 0) >= 2.8:
                spike_candle = c
                break

        if spike_candle:
            curr_c = candles_15m[-1]
            spike_range = spike_candle['high'] - spike_candle['low']
            support_top = spike_candle['high'] - (spike_range * 0.400)
            support_bottom = spike_candle['high'] - (spike_range * 0.618)
            
            is_volume_dry = (curr_c['volume'] <= spike_candle['volume'] * 0.35)
            holds_support = (price >= support_bottom * 0.995 and price <= support_top * 1.005)
            is_5m_rebounding = (candles_5m[-1]['close'] >= candles_5m[-1]['open']) or (candles_5m[-1]['close'] >= candles_5m[-2]['close'])

            if is_volume_dry and holds_support and bid_ratio >= 0.50 and is_5m_rebounding:
                return {
                    "symbol": f"{sym}/KRW", "code": sym, "track": "C", "name": "1차 눌림목",
                    "current_price": price, "target_entry": round_to_bithumb_tick(price),
                    "sl_pct": -1.30, "tp_trigger_pct": 1.50, "tp_floor_lock": 1.10, "timeout_mins": 12,
                    "bid_ratio": bid_ratio, "quant": q,
                    "reason": f"장대양봉 수급 후 거래량 마름 및 황금비 지지 반등(5m 턴 & 호가 {bid_ratio*100:.1f}%)"
                }

    # -------------------------------------------------------------
    # [Track A] 박스 하단 지지 반등 (1H EMA 20 상승 추세 필터)
    # -------------------------------------------------------------
    ema_20_1h = calculate_ema(candles_1h, 20)
    if q["box_range_pct"] >= 2.0 and (-0.50 <= q["dist_to_low_pct"] <= 1.20) and bid_ratio >= 0.45:
        if price > ema_20_1h: # 🛡️ 하락장 휴게소 배제
            return {
                "symbol": f"{sym}/KRW", "code": sym, "track": "A", "name": "바닥 반등",
                "current_price": price, "target_entry": round_to_bithumb_tick(price),
                "sl_pct": -1.00, "tp_trigger_pct": 1.15, "tp_floor_lock": 1.00, "timeout_mins": 15,
                "bid_ratio": bid_ratio, "quant": q,
                "reason": f"1H 정배열 내 15분 박스 하단 지지({q['dist_to_low_pct']}%) 반등 (호가 {bid_ratio*100:.1f}%)"
            }

    return None

# ==========================================
# 5. 리스크 관리 모듈 & 파싱 로직
# ==========================================
def clean_and_parse_json(raw_text):
    if not raw_text: return None
    try:
        cleaned = re.sub(r"^```(?:json)?", "", raw_text.strip(), flags=re.MULTILINE)
        cleaned = re.sub(r"```$", "", cleaned.strip(), flags=re.MULTILINE).strip()
        return json.loads(cleaned)
    except Exception:
        match = re.search(r"\{.*\}", raw_text, re.DOTALL)
        if match:
            try: return json.loads(match.group(0))
            except Exception: pass
    return None

def call_ai_api(sys_prompt, user_prompt):
    providers = [
        {"name": "Gemini", "key": GEMINI_API_KEY, "url": "https://generativelanguage.googleapis.com/v1beta/openai/", "model": "gemini-3.5-flash-lite"},
        {"name": "Samba", "key": SAMBANOVA_API_KEY, "url": "https://api.sambanova.ai/v1", "model": "Meta-Llama-3.3-70B-Instruct"},
        {"name": "Groq", "key": GROQ_API_KEY3 or GROQ_API_KEY2, "url": "https://api.groq.com/openai/v1", "model": "llama-3.3-70b-specdec"}
    ]
    for p in providers:
        if not p["key"]: continue
        try:
            res = OpenAI(base_url=p['url'], api_key=p['key']).chat.completions.create(
                model=p['model'], messages=[{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_prompt}],
                response_format={"type": "json_object"}, temperature=0.1
            )
            return res.choices[0].message.content
        except Exception: pass
    return None

def check_daily_circuit_breaker(closed_trades, total_asset):
    now_kst = get_kst_now()
    today_start = now_kst.replace(hour=0, minute=0, second=0, microsecond=0)
    today_losses_krw = sum(t.get('profit_krw',0) for t in closed_trades if t.get("is_paper", True) == PAPER_TRADING and parse_dt_safe(t.get('exit_time','')) and parse_dt_safe(t.get('exit_time','')) >= today_start)
    loss_ratio_pct = (today_losses_krw / total_asset) * 100.0 if total_asset > 0 else 0.0
    if loss_ratio_pct <= DAILY_LOSS_LIMIT_PCT:
        return False, f"당일 누적 손실({loss_ratio_pct:.2f}%) 일일 한도 초과"
    return True, "정상"

def check_market_drop_ratio(ticker_all_data):
    if not ticker_all_data: return True, 0.0, "정상"
    valid_coins = [(sym, float(i.get('acc_trade_value_24H',0)), float(i.get('fluctate_rate_24H',0))) for sym, i in ticker_all_data.items() if sym != "date" and sym.upper() not in STABLE_COINS]
    top_30 = sorted(valid_coins, key=lambda x: x[1], reverse=True)[:30]
    if len(top_30) < 15: return True, 0.0, "통과"
    drop_count = sum(1 for c in top_30 if c[2] < 0)
    drop_ratio = drop_count / len(top_30)
    if drop_ratio >= MARKET_DROP_RATIO_LIMIT: return False, drop_ratio, f"상위 30개 중 {drop_count}개({drop_ratio*100:.1f}%) 하락"
    return True, drop_ratio, "정상"

# ==========================================
# 6. 스크리닝 (비대칭 비중 배분 적용)
# ==========================================
def execute_server_side_strategy():
    global CIRCUIT_BREAKER_ACTIVE, NO_CANDIDATE_CYCLE_COUNT, LAST_NO_CANDIDATE_REASON, IS_FIRST_RUN
    if PENDING_PAPER_DRAIN: return

    server_state = load_json_file(STATE_FILE, {})
    paper_db = load_json_file(PAPER_TRADES_FILE, {"active_positions": {}, "closed_trades": []})
    active_pos = paper_db.get("active_positions", {})
    closed_trades = paper_db.get("closed_trades", [])
    target_queue = server_state.get("target_queue", {})
    
    if len([v for v in active_pos.values() if v.get("is_paper", True) == PAPER_TRADING]) >= MAX_HOLDING_COINS: return

    tot_asset, av_krw = get_bithumb_account_summary()
    if tot_asset is None:
        if PAPER_TRADING: tot_asset, av_krw = 100000.0, 100000.0
        else: return

    cb_ok, cb_reason = check_daily_circuit_breaker(closed_trades, tot_asset)
    if not cb_ok:
        if not CIRCUIT_BREAKER_ACTIVE:
            CIRCUIT_BREAKER_ACTIVE = True
            send_telegram_msg(f"🛑 [서킷브레이커] {cb_reason}")
        return
    CIRCUIT_BREAKER_ACTIVE = False

    try: res = HTTP_SESSION.get("https://api.bithumb.com/public/ticker/ALL_KRW", timeout=6).json()
    except Exception: return
    if res.get("status") != "0000": return

    m_ok, d_ratio, m_msg = check_market_drop_ratio(res.get("data", {}))
    if not m_ok:
        NO_CANDIDATE_CYCLE_COUNT += 1
        LAST_NO_CANDIDATE_REASON = m_msg
        check_and_notify_idle_briefing(active_pos, target_queue)
        return

    held_codes = set(active_pos.keys()).union(set(target_queue.keys()))
    raw_list = []
    for sym, info in res["data"].items():
        if sym == "date" or sym.upper() in STABLE_COINS or sym in held_codes: continue
        try:
            p, v24 = float(info['closing_price']), float(info['acc_trade_value_24H'])
            if p >= MIN_COIN_PRICE_KRW and v24 >= MIN_24H_ACC_TRADE_VALUE:
                raw_list.append((sym, p, float(info['fluctate_rate_24H']), v24))
        except Exception: pass

    sorted_list = sorted(raw_list, key=lambda x: x[3], reverse=True)[:35]
    now_ts = time.time()
    min_1h_threshold = PRIMARY_1H_TRADE_VAL
    candidates_pool = []
    
    # 🚀 [테스트 모드] 첫 실행 시 퀀트 스캔을 건너뛰고 상위 1개 종목을 가짜 후보로 생성
    if IS_FIRST_RUN:
        logging.info("🚀 [테스트 모드] 최초 1회 강제 종목 선정 실행 (모의투자 전용 안전장치 포함)")
        if sorted_list:
            test_sym, test_p, _, _ = sorted_list[0]
            candidates_pool.append({
                "symbol": f"{test_sym}/KRW", "code": test_sym, "track": "T", "name": "시스템 테스트",
                "current_price": test_p, "target_entry": round_to_bithumb_tick(test_p * 0.99), # 체결 안되게 약간 낮게
                "sl_pct": -1.00, "tp_trigger_pct": 1.15, "tp_floor_lock": 1.00, "timeout_mins": 3,
                "bid_ratio": 0.5, "quant": {}, "reason": "시스템 구동 및 알림 파이프라인 검증용 강제 편입"
            })
        IS_FIRST_RUN = False # 플래그 해제 (다음 턴부터는 정상 로직)
    else:
        # 정상 퀀트 스캔
        for attempt in range(2):
            for sym, price, change, val_24h in sorted_list:
                if is_cooling_down(sym, closed_trades, now_ts): continue # 🛑 쿨다운 검사
                
                c_1h = get_candles(sym, "1h", 25)
                time.sleep(0.02)
                c_15m = get_candles(sym, "15m", 35)
                time.sleep(0.02)
                c_5m = get_candles(sym, "5m", 10)
                time.sleep(0.02)
    
                cand = scan_ac_candidates(sym, price, val_24h, c_1h, c_15m, c_5m, min_1h_threshold)
                if cand: candidates_pool.append(cand)
            if len(candidates_pool) >= 2 or attempt == 1: break
            min_1h_threshold = FALLBACK_1H_TRADE_VAL

    if not candidates_pool:
        NO_CANDIDATE_CYCLE_COUNT += 1
        LAST_NO_CANDIDATE_REASON = f"Dual-Track (A/C) 타점 포착 실패 (시장 하락률: {d_ratio*100:.1f}%)"
        check_and_notify_idle_briefing(active_pos, target_queue)
        return

    # 테스트 모드인 경우 AI 판정 건너뛰기
    if candidates_pool and candidates_pool[0]["track"] == "T":
        decision = {"selected_candidates": [{"symbol": candidates_pool[0]["symbol"], "score": 99, "entry_discount_pct": 0.5, "detailed_reason": "테스트 모드 자동 승인"}]}
    else:
        sys_prompt = "You are a quantitative trader analyzing Dual-Track (A: Box Bottom, C: Pullback). Select UP TO 2 best setups (score >= 65). Output valid JSON ONLY matching schema."
        usr_prompt = f"Candidates:\n{json.dumps(candidates_pool[:6], ensure_ascii=False)}\n\nSchema: {{'selected_candidates': [{{'symbol':'...', 'score':85, 'entry_discount_pct':0.10, 'detailed_reason':'...'}}]}}"
        decision = clean_and_parse_json(call_ai_api(sys_prompt, usr_prompt))
        
    if not decision or not decision.get("selected_candidates"):
        NO_CANDIDATE_CYCLE_COUNT += 1
        LAST_NO_CANDIDATE_REASON = "AI 점수 미달"
        check_and_notify_idle_briefing(active_pos, target_queue)
        return

    added = 0
    for cand in decision["selected_candidates"][:2]:
        code = cand.get("symbol", "").split('/')[0].upper()
        if not code or cand.get("score", 0) < 65 or code in held_codes: continue
        curr_p = get_current_price(code)
        if not curr_p: continue

        setup = next((i for i in candidates_pool if i["code"] == code), candidates_pool[0])
        
        # 🚀 [테스트 모드 안전장치] 테스트 종목은 실전 진입 강제 차단 (모의 상태로 고정)
        is_paper_trade_for_this = PAPER_TRADING
        if setup.get("track") == "T":
            is_paper_trade_for_this = True
            
        disc = max(float(cand.get("entry_discount_pct", 0.10)), 0.0)
        t_entry = round_to_bithumb_tick(curr_p * (1.0 - (disc / 100.0)))
        
        ob = get_bithumb_orderbook_10(code)
        if ob and ob.get("max_bid_wall"):
            wp = ob["max_bid_wall"]["price"]
            adj_p = round_to_bithumb_tick(wp + get_bithumb_tick_size(wp))
            if abs((adj_p - t_entry) / t_entry) <= 0.005 and adj_p <= curr_p: t_entry = adj_p

        # 📊 비대칭 자금 배분 적용 (C: 30%, A: 15%, T: 10%)
        alloc_ratio = 0.30 if setup.get("track") == "C" else (0.15 if setup.get("track") == "A" else 0.10)
        buy_krw = max(MIN_BUY_KRW, int(tot_asset * alloc_ratio))
        if buy_krw > av_krw: buy_krw = int(av_krw)
        if buy_krw < MIN_BUY_KRW: continue

        target_queue[code] = {
            "symbol": f"{code}/KRW", "code": code, "track": setup.get("track"), "track_name": setup.get("name"),
            "is_paper": is_paper_trade_for_this, "score": cand.get("score"), "current_price": curr_p, "target_entry": t_entry,
            "sl_pct": setup.get("sl_pct"), "emergency_sl_pct": -1.50,
            "tp_trigger_pct": setup.get("tp_trigger_pct"), "tp_floor_lock": setup.get("tp_floor_lock"),
            "timeout_mins": setup.get("timeout_mins", 12), "holding_timeout_mins": setup.get("timeout_mins", 12),
            "buy_amount_krw": buy_krw, "ordered_volume": 0.0, "order_uuid": None, "order_placed": False,
            "bid_breach_start_time": None, "detailed_reason": cand.get("detailed_reason"),
            "created_at": get_kst_now().isoformat(), "created_timestamp": time.time()
        }
        added += 1

    if added > 0:
        NO_CANDIDATE_CYCLE_COUNT = 0
        server_state["target_queue"] = target_queue
        save_json_file(STATE_FILE, server_state)
    else:
        NO_CANDIDATE_CYCLE_COUNT += 1
        check_and_notify_idle_briefing(active_pos, target_queue)

def check_and_notify_idle_briefing(active_pos, target_queue):
    global NO_CANDIDATE_CYCLE_COUNT
    if NO_CANDIDATE_CYCLE_COUNT >= 12:
        if len([v for v in active_pos.values() if v.get("is_paper")==PAPER_TRADING]) == 0 and len([v for v in target_queue.values() if v.get("is_paper")==PAPER_TRADING]) == 0:
            send_telegram_msg(f"👀 [시장 관망 브리핑] (1시간 경과)\n• 사유: {LAST_NO_CANDIDATE_REASON}\n• 조치: 원금 보존을 위해 무리한 진입 자제")
        NO_CANDIDATE_CYCLE_COUNT = 0

# ==========================================
# 7. 실시간 감시 엔진 (OB 2.0 심도 컷 & +1.05% 본절 락)
# ==========================================
async def realtime_execution_engine():
    global EMERGENCY_STOP, PAPER_TRADING, PENDING_PAPER_DRAIN
    last_run = 0
    threading.Thread(target=telegram_listener_thread, daemon=True).start()
    asyncio.create_task(bithumb_websocket_stream_worker())
    
    # 🐛 중복 알림 방지용 세션 캐시 (서버 재시작 시 이미 알림 보낸 내역 무시)
    notified_trades = set()

    while True:
        try:
            now = time.time()
            now_dt = get_kst_now()

            if now - last_run >= 300:
                last_run = now
                asyncio.create_task(asyncio.to_thread(execute_server_side_strategy))

            s_state = load_json_file(STATE_FILE, {})
            p_db = load_json_file(PAPER_TRADES_FILE, {"active_positions": {}, "closed_trades": []})
            t_queue = s_state.get("target_queue", {})
            act_pos = p_db.get("active_positions", {})
            closed = p_db.get("closed_trades", [])
            
            # 초기 로딩 시 과거 청산 건수 캐싱 (중복 알림 방지)
            if not notified_trades and closed:
                for idx, t in enumerate(closed):
                    notified_trades.add(f"{t.get('symbol')}_{t.get('exit_time')}_{idx}")

            if PENDING_PAPER_DRAIN and len([v for v in act_pos.values() if not v.get("is_paper", True)]) == 0:
                PENDING_PAPER_DRAIN = False; PAPER_TRADING = True
                s_state.update({"paper_trading": True, "pending_paper_drain": False})
                save_json_file(STATE_FILE, s_state)
                send_telegram_msg("🎉 [모드 전환] 실전 포지션 청산 완료 ➔ 모의투자 전환")

            act_cnt = len([v for v in act_pos.values() if v.get("is_paper", True) == PAPER_TRADING])
            ord_cnt = len([v for v in t_queue.values() if v.get("is_paper", True) == PAPER_TRADING and v.get("order_placed")])

            for code, plan in sorted(list(t_queue.items()), key=lambda x: (x[1].get("score", 0), -x[1].get("created_timestamp", 0)), reverse=True):
                # 🚀 테스트 모드용 안전장치 우회 허용
                is_this_plan_paper = plan.get("is_paper", True)
                if is_this_plan_paper != PAPER_TRADING and plan.get("track") != "T": break 
                
                if ord_cnt >= max(0, MAX_HOLDING_COINS - act_cnt): break
                
                if not plan.get("order_placed"):
                    succ, ouuid, ovol = execute_real_limit_buy_order(code, plan["target_entry"], plan["buy_amount_krw"])
                    if succ:
                        plan.update({"order_placed": True, "order_uuid": ouuid, "ordered_volume": ovol})
                        ord_cnt += 1
                        save_json_file(STATE_FILE, s_state)
                        mode_str = '모의' if is_this_plan_paper else '실전'
                        send_telegram_msg(f"🎯 [{mode_str}] [{plan['track_name']}] 예약\n• {plan['symbol']}({plan['score']}점)\n• 매수: {plan['buy_amount_krw']:,}원 @ {plan['target_entry']:,.4f}")

            for code, plan in list(t_queue.items()):
                elapsed = (now - plan.get("created_timestamp", now)) / 60.0
                ouuid = plan.get("order_uuid")

                if elapsed >= plan.get("timeout_mins", 12):
                    if plan.get("order_placed") and ouuid: cancel_bithumb_order(ouuid)
                    if code in t_queue: del t_queue[code]
                    save_json_file(STATE_FILE, s_state)
                    # 🚀 테스트 모드 타임아웃 메시지
                    if plan.get("track") == "T":
                        send_telegram_msg(f"✅ [테스트 완료] {plan['symbol']} 대기열 타임아웃으로 정상 소멸. (이후부터 정상 퀀트 로직 작동)")
                    continue

                ob = get_bithumb_orderbook_10(code)
                inv_rsn = ""
                if ob and ob.get("bid_ratio", 0.5) < 0.20:
                    if plan.get("bid_breach_start_time") is None: plan["bid_breach_start_time"] = now
                    elif now - plan["bid_breach_start_time"] >= 3.0: inv_rsn = "호가창 20% 미만 붕괴"
                else: plan["bid_breach_start_time"] = None

                if inv_rsn:
                    if plan.get("order_placed") and ouuid: cancel_bithumb_order(ouuid)
                    if code in t_queue: del t_queue[code]
                    save_json_file(STATE_FILE, s_state)
                    continue

                if plan.get("order_placed"):
                    cp = get_current_price(code)
                    is_fill, buy_fee = False, 0.0

                    if plan.get("is_paper", True):
                        if cp and cp <= plan["target_entry"]: is_fill = True
                    else:
                        st, ev, fee = check_bithumb_order_status(ouuid)
                        if st == "done": is_fill = True; buy_fee = fee
                        elif st == "cancel":
                            if code in t_queue: del t_queue[code]
                            save_json_file(STATE_FILE, s_state)
                            continue

                    if is_fill:
                        rp, ru, akrw = plan["target_entry"], plan.get("ordered_volume", 0.0), round(plan["target_entry"] * plan.get("ordered_volume", 0.0))
                        if not plan.get("is_paper", True):
                            dtl = get_bithumb_order_execution_detail(ouuid)
                            if dtl: rp, ru, akrw, buy_fee = dtl["avg_price"], dtl["executed_volume"], dtl["executed_funds"], dtl["paid_fee"]

                        act_pos[code] = {**plan, "entry_price": rp, "highest_price": rp, "buy_amount_krw": akrw, "units": ru, "buy_fee": buy_fee, "entry_timestamp": now, "entry_time": now_dt.isoformat(), "locked_floor_profit_pct": 0.0, "breakeven_locked": False, "price_history": [(now, rp)]}
                        if code in t_queue: del t_queue[code]
                        save_json_file(PAPER_TRADES_FILE, {"active_positions": act_pos, "closed_trades": closed})
                        save_json_file(STATE_FILE, s_state)
                        send_telegram_msg(f"⚡ [체결] {plan['symbol']} ({plan['track']})\n• 진입: {rp:,.4f}\n• 매수: {akrw:,.0f}원\n• 락: +1.05% 도달 시 발동")

            for code, pos in list(act_pos.items()):
                cp = get_current_price(code)
                if not cp: continue

                ep, ets = pos["entry_price"], pos.get("entry_timestamp", now)
                el_sec = now - ets
                
                pos["price_history"] = [(ts, p) for ts, p in pos.get("price_history", []) + [(now, cp)] if now - ts <= 3600]
                if cp > pos.get("highest_price", ep): pos["highest_price"] = cp
                
                cpct = ((cp - ep) / ep) * 100.0
                hpct = ((pos["highest_price"] - ep) / ep) * 100.0

                # 🔒 본절 락 상향 (+1.05%)
                if hpct >= 1.05: pos["breakeven_locked"] = True

                t_act, s_pull, s_flr = False, 0.22, 0.0
                if hpct >= 5.0: t_act, s_pull, s_flr = True, 0.50, 4.20
                elif hpct >= 3.5: t_act, s_pull, s_flr = True, 0.40, 3.00
                elif hpct >= 2.5: t_act, s_pull, s_flr = True, 0.30, 2.10
                elif hpct >= 1.8: t_act, s_pull, s_flr = True, 0.25, 1.45
                elif hpct >= pos.get("tp_trigger_pct", 1.35): t_act, s_pull, s_flr = True, 0.22, pos.get("tp_floor_lock", 1.00)

                pos["locked_floor_profit_pct"] = max(pos.get("locked_floor_profit_pct", 0.0), s_flr)
                
                sc, rsn, xp, apull = False, "", cp, round(hpct - cpct, 2)

                if t_act:
                    tr_p = max(pos["highest_price"] * (1 - s_pull/100), ep * (1 + pos["locked_floor_profit_pct"]/100))
                    obx = get_bithumb_orderbook_10(code)
                    if obx and obx.get("max_bid_wall"):
                        wp = obx["max_bid_wall"]["price"]
                        if (cp - wp) / cp <= 0.003:
                            w2 = round_to_bithumb_tick(wp + 2 * get_bithumb_tick_size(wp))
                            if tr_p < w2 <= cp: tr_p = w2
                    if cp <= tr_p or apull >= s_pull or cpct <= pos["locked_floor_profit_pct"]:
                        sc, rsn = True, f"📈 익절 (고점 +{hpct:.2f}% ➔ 하한 +{pos['locked_floor_profit_pct']:.2f}%)"
                        if pos.get("is_paper", True): xp = round_to_bithumb_tick(tr_p - get_bithumb_tick_size(tr_p))

                elif cpct <= -1.50:
                    sc, rsn = True, f"🚨 비상 하드 손절 (-1.50%)"
                    if pos.get("is_paper", True): xp = round_to_bithumb_tick(ep * 0.985)

                elif pos.get("breakeven_locked") and cpct <= -0.05:
                    sc, rsn = True, f"🛡️ 본절 락 컷 (고점 +{hpct:.2f}%)"
                    if pos.get("is_paper", True): xp = round_to_bithumb_tick(ep * 0.9995)

                elif cpct <= pos["sl_pct"]:
                    obs = get_bithumb_orderbook_10(code)
                    br, t3 = obs.get("bid_ratio", 0.5) if obs else 0.5, obs.get("top3_bid_ratio", 0.5) if obs else 0.5
                    
                    # 🛡️ OB 2.0: 1~3호가 심도 붕괴 시 즉시 컷
                    if br < 0.30 or t3 < 0.15:
                        sc, rsn = True, f"⚡ 심도 붕괴 컷 (1~3호가 {t3*100:.1f}%, 전체 {br*100:.1f}%)"
                    else:
                        if not pos.get("sl_breach_start_time"): pos["sl_breach_start_time"] = now
                        elif now - pos["sl_breach_start_time"] >= 2.5: sc, rsn = True, f"🛡️ 손절선({pos['sl_pct']}%) 이탈"
                else: pos["sl_breach_start_time"] = None

                if not sc and el_sec >= pos.get("holding_timeout_mins", 12) * 60:
                    if hpct < 0.35: sc, rsn = True, f"⌛ {pos.get('holding_timeout_mins')}분 미반등 조기 정리"
                    elif 0.75 <= hpct < 1.20 and el_sec >= 1800: sc, rsn = True, f"⌛ 30분 만료 안전 정리"

                if sc:
                    suuid, sfee, af = None, 0.0, 0.0
                    if not pos.get("is_paper", True):
                        succ, uuid = execute_real_market_sell_order(code, pos.get("units", 0.0))
                        if succ:
                            suuid = uuid
                            for _ in range(3):
                                time.sleep(0.5)
                                dtl = get_bithumb_order_execution_detail(uuid)
                                if dtl and dtl.get("executed_funds", 0) > 0:
                                    xp, af, sfee = dtl["avg_price"], dtl["executed_funds"], dtl["paid_fee"]
                                    break

                    bk, bf = pos.get("buy_amount_krw", MIN_BUY_KRW), pos.get("buy_fee", 0.0)
                    if not pos.get("is_paper", True) and af > 0:
                        pk = round(af - bk - bf - sfee)
                        fpct = round((pk / bk) * 100.0, 2)
                    else:
                        fpct = round(((xp - ep) / ep) * 100.0, 2)
                        pk = round(bk * (fpct / 100.0))
                    
                    exit_iso = now_dt.isoformat()
                    closed.append({
                        "symbol": pos["symbol"], "track": pos["track"], "track_name": pos["track_name"],
                        "is_paper": pos.get("is_paper", True), "entry_price": ep, "exit_price": xp,
                        "buy_amount_krw": bk, "profit_krw": pk, "profit_pct": fpct, "buy_fee": bf, "sell_fee": sfee,
                        "reason": rsn, "trajectory_summary": generate_trade_trajectory_summary(pos, hpct, el_sec, rsn),
                        "entry_time": pos["entry_time"], "exit_time": exit_iso
                    })
                    del act_pos[code]
                    save_json_file(PAPER_TRADES_FILE, {"active_positions": act_pos, "closed_trades": closed})
                    
                    # 🐛 신규 청산 건만 텔레그램 발송 처리
                    trade_key = f"{pos['symbol']}_{exit_iso}_{len(closed)-1}"
                    if trade_key not in notified_trades:
                        notified_trades.add(trade_key)
                        send_telegram_msg(f"{'🎉' if fpct>0 else '🌧️'} [청산] {pos['symbol']}\n• 손익: {pk:,}원 ({fpct:+.2f}%)\n• 사유: {rsn}")
                        send_telegram_msg(format_portfolio_status_msg(act_pos, closed))

            await asyncio.sleep(0.5 if act_pos or t_queue else 2.0)
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
        if init_res.get("ok") and init_res.get("result"): LAST_TELEGRAM_UPDATE_ID = init_res["result"][-1]["update_id"]
    except Exception: pass

    while True:
        try:
            res = HTTP_SESSION.get(url, params={"offset": LAST_TELEGRAM_UPDATE_ID + 1, "timeout": 10}, timeout=15).json()
            if res.get("ok"):
                for upd in res.get("result", []):
                    LAST_TELEGRAM_UPDATE_ID = upd["update_id"]
                    msg = upd.get("message", {})
                    text = msg.get("text", "").strip()
                    if TELEGRAM_CHAT_ID and str(msg.get("chat", {}).get("id", "")) != TELEGRAM_CHAT_ID: continue

                    s_state = load_json_file(STATE_FILE, {})
                    p_db = load_json_file(PAPER_TRADES_FILE, {"active_positions": {}, "closed_trades": []})

                    if text == "/track":
                        base_str = s_state.get('update_baseline_time', '')
                        ts = {'A': {'w':0,'l':0,'p':0}, 'C': {'w':0,'l':0,'p':0}}
                        nm = {'A': '바닥반등(15%)', 'C': '1차눌림목(30%)'}
                        for t in p_db.get("closed_trades", []):
                            if t.get("is_paper", True) != PAPER_TRADING or (base_str and t.get('exit_time', '') < base_str): continue
                            trk = t.get('track', 'A')
                            if trk in ts:
                                if t.get('profit_pct', 0) > 0: ts[trk]['w'] += 1
                                else: ts[trk]['l'] += 1
                                ts[trk]['p'] += t.get('profit_krw', 0)
                        lines = [f"• [{trk}:{nm[trk]}] {ts[trk]['w']+ts[trk]['l']}건 ({ts[trk]['w']}승 {ts[trk]['l']}패 | {(ts[trk]['w']/(ts[trk]['w']+ts[trk]['l'])*100 if ts[trk]['w']+ts[trk]['l']>0 else 0):.1f}% | {ts[trk]['p']:+,}원)" for trk in ['A', 'C']]
                        send_telegram_msg(f"📊 [Dual-Track 리포트]\n" + "\n".join(lines))
                    
                    elif text == "/status":
                        send_telegram_msg(format_portfolio_status_msg(p_db.get("active_positions", {}), p_db.get("closed_trades", [])))
                        
                    elif text == "/stop":
                        EMERGENCY_STOP = True
                        s_state["target_queue"] = {}
                        save_json_file(STATE_FILE, s_state)
                        send_telegram_msg("🛑 [인터락 작동] 대기열 전량 취소.")
                        
                    elif text == "/start":
                        EMERGENCY_STOP = False
                        CIRCUIT_BREAKER_ACTIVE = False
                        send_telegram_msg("▶️ [인터락 해제] 엔진 감시 재개.")
                        
                    elif text == "/update":
                        send_telegram_msg("🔄 [원격 업데이트] 재시작...")
                        def do_restart():
                            time.sleep(1)
                            subprocess.run(["git", "stash"], cwd=PROJECT_DIR)
                            subprocess.run(["git", "pull", "origin", "main"], cwd=PROJECT_DIR)
                            subprocess.run(["sudo", "systemctl", "restart", "autotrade.service"])
                        threading.Thread(target=do_restart, daemon=True).start()
            time.sleep(1)
        except Exception: time.sleep(3)

if __name__ == "__main__":
    notify_startup_once()
    asyncio.run(realtime_execution_engine())
