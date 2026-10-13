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
# 0. 전역 설정 및 퀀트 파라미터 (공격형 고손익비 알파 엔진)
# ==========================================
BUILD_VERSION = "2026.10.13-v26-alpha-scalper-pro"     
MAX_HOLDING_COINS = 2                          
MIN_BUY_KRW = 6000                             
DEFAULT_BUY_RATIO = 0.22                       
MIN_COIN_PRICE_KRW = 50.0                      
MAX_TICK_RATIO_PCT = 0.08                      
MIN_24H_ACC_TRADE_VALUE = 1_500_000_000        # 🛡️ 거래대금 필터 상향 (15억 원)
PRIMARY_1H_TRADE_VAL = 400_000_000             # 🛡️ 1시간 거래대금 기준 상향 (4억 원)
FALLBACK_1H_TRADE_VAL = 220_000_000            
MIN_15M_ATR_PCT = 1.20                         # ⚡ 최소 변동성 상향 (1.2% 이상만 타격)
MARKET_DROP_RATIO_LIMIT = 0.80                 
DAILY_LOSS_LIMIT_PCT = -3.0                    

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

WS_TICKER_CACHE = {}
WS_ORDERBOOK_CACHE = {}

HTTP_SESSION = requests.Session()
adapter = requests.adapters.HTTPAdapter(pool_connections=35, pool_maxsize=35, max_retries=1)
HTTP_SESSION.mount('https://', adapter)
HTTP_SESSION.mount('http://', adapter)

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s', handlers=[logging.FileHandler("watcher.log"), logging.StreamHandler(sys.stdout)])

KST = timezone(timedelta(hours=9))

def get_kst_now():
    return datetime.now(KST)

def parse_dt_safe(dt_str):
    if not dt_str or not isinstance(dt_str, str): return None
    try:
        cleaned_str = dt_str.strip()
        if cleaned_str.endswith('Z'): cleaned_str = cleaned_str[:-1] + '+00:00'
        dt = datetime.fromisoformat(cleaned_str)
        if dt.tzinfo is None: return dt.replace(tzinfo=KST)
        return dt.astimezone(KST)
    except Exception:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d %H:%M:%S"):
            try: return datetime.strptime(cleaned_str[:19], fmt).replace(tzinfo=KST)
            except Exception: continue
    return None

def load_json_file(file_path, default_value):
    if os.path.exists(file_path):
        try:
            with open(file_path, "r", encoding="utf-8") as f: return json.load(f)
        except Exception: pass
    return default_value

def save_json_file(file_path, data):
    try:
        with open(file_path, "w", encoding="utf-8") as f: json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception: pass

def init_server_state():
    global PAPER_TRADING, UPDATE_BASELINE_TIME, PENDING_PAPER_DRAIN
    state = load_json_file(STATE_FILE, {})
    PAPER_TRADING = state.get("paper_trading", True)
    PENDING_PAPER_DRAIN = state.get("pending_paper_drain", False)
    now_kst_iso = get_kst_now().isoformat()
    if state.get("build_version", "") != BUILD_VERSION or not state.get("update_baseline_time"):
        state["update_baseline_time"] = now_kst_iso
        state["build_version"] = BUILD_VERSION
    UPDATE_BASELINE_TIME = state["update_baseline_time"]
    state["last_started_at"] = now_kst_iso
    save_json_file(STATE_FILE, state)

init_server_state()

def send_telegram_msg(msg: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return
    try: HTTP_SESSION.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=5)
    except Exception: pass

def notify_startup_once():
    now_ts = time.time()
    last_notify_ts = float(open(RESTART_FLAG_FILE, "r").read().strip()) if os.path.exists(RESTART_FLAG_FILE) else 0.0
    if now_ts - last_notify_ts > 600:
        with open(RESTART_FLAG_FILE, "w") as f: f.write(str(now_ts))
        mode_str = "🧪 모의투자" if PAPER_TRADING else "🔥 실전매매"
        base_dt = parse_dt_safe(UPDATE_BASELINE_TIME)
        send_telegram_msg(f"🎯 [오라클 서버] 알파 스캘퍼 프로 엔진 가동 ({BUILD_VERSION})\n• 모드: {mode_str}\n• 성격: 노이즈 필터 강화 및 추세 추종 수익 극대화 모드\n• 기준시각: {base_dt.strftime('%m/%d %H:%M KST') if base_dt else '-'}\n\n📱 명령어: /status, /log, /paper, /real, /reset_stats, /stop, /start, /update")

def sync_file_to_github(file_path, content_data):
    if not GH_TOKEN: return
    url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/contents/{file_path}"
    headers = {"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    try:
        res = HTTP_SESSION.get(url, headers=headers, timeout=5)
        sha = res.json().get('sha') if res.status_code == 200 else None
        encoded = base64.b64encode(json.dumps(content_data, indent=2, ensure_ascii=False).encode('utf-8')).decode('utf-8')
        payload = {"message": f"update: {file_path}", "content": encoded}
        if sha: payload["sha"] = sha
        HTTP_SESSION.put(url, headers=headers, json=payload, timeout=8)
    except Exception: pass

def format_portfolio_status_msg(active_positions, closed_trades):
    current_mode_is_paper = PAPER_TRADING
    mode_tag_name = "🧪 모의투자" if current_mode_is_paper else "🔥 실전매매"

    mode_active = {k: v for k, v in active_positions.items() if v.get("is_paper", True) == current_mode_is_paper}
    held_symbols = [f"{v['symbol']}[{v.get('track', 'A')}]" for v in mode_active.values()]
    held_str = f"{', '.join(held_symbols)} ({len(held_symbols)}/2개 보유 중)" if held_symbols else "(현재 보유 종목 없음)"

    mode_closed = [t for t in closed_trades if t.get("is_paper", True) == current_mode_is_paper]
    recent_10 = mode_closed[-10:][::-1] if mode_closed else []
    
    if not recent_10:
        trades_str, win_rate_10, profit_10_krw = f"• [{mode_tag_name}] 매도 이력이 없습니다.", 0.0, 0
    else:
        trade_lines, wins_10, profit_10_krw = [], 0, 0
        for idx, t in enumerate(recent_10, 1):
            p_pct, p_krw = t.get('profit_pct', 0.0), t.get('profit_krw', 0)
            dt_obj = parse_dt_safe(t.get('exit_time', ''))
            time_display = dt_obj.strftime("%m/%d %H:%M KST") if dt_obj else "-"
            trade_lines.append(f"{idx}. {t.get('symbol', 'UNK')}[{t.get('track', 'A')}]: {'+' if p_krw > 0 else ''}{p_krw:,}원 ({'+' if p_pct > 0 else ''}{p_pct:.2f}%) | {time_display}")
            if p_pct > 0: wins_10 += 1
            profit_10_krw += p_krw
        trades_str = "\n".join(trade_lines)
        win_rate_10 = round((wins_10 / len(recent_10)) * 100, 1)

    baseline_dt = parse_dt_safe(UPDATE_BASELINE_TIME)
    total_cum_trades, total_cum_wins, total_cum_profit_krw = 0, 0, 0
    for t in mode_closed:
        exit_dt = parse_dt_safe(t.get('exit_time', ''))
        if baseline_dt and exit_dt and exit_dt >= baseline_dt:
            total_cum_trades += 1
            total_cum_profit_krw += t.get('profit_krw', 0)
            if t.get('profit_pct', 0.0) > 0: total_cum_wins += 1

    if total_cum_trades > 0:
        cum_stats_str = f"• 총 거래: {total_cum_trades}건 ({total_cum_wins}승 {total_cum_trades - total_cum_wins}패 | 승률 {round((total_cum_wins / total_cum_trades) * 100, 1)}%)\n• 총 누적 손익: {'+' if total_cum_profit_krw > 0 else ''}{total_cum_profit_krw:,} KRW"
    else:
        cum_stats_str = f"• 업데이트 이후 청산 완료된 {mode_tag_name} 거래가 아직 없습니다."

    return f"""💼 [{mode_tag_name} 알파 스캘퍼 운용 현황]
- 보유 종목 : {held_str}

📜 [최근 10건 매도 이력]
{trades_str}

📊 최근 10건 승률 : {win_rate_10}%
💰 최근 10건 수익 : {'+' if profit_10_krw > 0 else ''}{profit_10_krw:,} KRW
━━━━━━━━━━━━━━━━━━━━
📈 [업데이트 이후 누적 성과] ({baseline_dt.strftime('%m/%d %H:%M') if baseline_dt else '-'} 이후)
{cum_stats_str}"""

def generate_trade_trajectory_summary(pos: dict, exit_p: float, curr_profit_pct: float, highest_profit_pct: float, elapsed_seconds: float, close_reason: str) -> str:
    mins, secs = int(elapsed_seconds // 60), int(elapsed_seconds % 60)
    dur_str = f"{mins}분 {secs}초" if mins > 0 else f"{secs}초"
    track = pos.get("track", "A")
    if "익절" in close_reason: return f"[{track}] 고점 +{highest_profit_pct:.2f}% 터치 후 추세 추종 수익 +{curr_profit_pct:.2f}% 확정. (보유: {dur_str})"
    elif "본절" in close_reason: return f"[{track}] 추세 무산으로 본절 락 컷. (보유: {dur_str})"
    elif "미반등" in close_reason or "정리" in close_reason: return f"[{track}] 타임아웃 조기 정리. (보유: {dur_str})"
    elif "손절" in close_reason or "즉시 컷" in close_reason or "붕괴" in close_reason: return f"[{track}] 손절선 및 호가 붕괴 즉시 탈출. (보유: {dur_str})"
    return f"[{track}] 보유 {dur_str} 후 청산완료."

def get_bithumb_jwt_headers(query_params: dict = None):
    if not BITHUMB_API_KEY or not BITHUMB_SECRET_KEY: return {}
    payload = {'access_key': BITHUMB_API_KEY, 'nonce': str(uuid.uuid4()), 'timestamp': round(time.time() * 1000)}
    if query_params:
        payload['query_hash'] = hashlib.sha512(urllib.parse.urlencode(query_params).encode()).hexdigest()
        payload['query_hash_alg'] = 'SHA512'
    return {'Authorization': f"Bearer {jwt.encode(payload, BITHUMB_SECRET_KEY, algorithm='HS256')}", 'Content-Type': 'application/json'}

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
    return 1000.0

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
    return float(int(price / 1000.0) * 1000)

def get_current_price(coin_code: str):
    cached = WS_TICKER_CACHE.get(coin_code)
    if cached and (time.time() - cached.get("time", 0) <= 2.0): return cached["price"]
    try:
        res = HTTP_SESSION.get(f"https://api.bithumb.com/public/ticker/{coin_code}_KRW", timeout=1.5).json()
        if res.get("status") == "0000":
            price = float(res["data"]["closing_price"])
            if price > 0:
                WS_TICKER_CACHE[coin_code] = {"price": price, "time": time.time()}
                return price
    except Exception: pass
    return cached.get("price") if cached else None

def get_bithumb_orderbook_10(coin_code: str):
    cached = WS_ORDERBOOK_CACHE.get(coin_code)
    if cached and (time.time() - cached.get("time", 0) <= 2.0): return cached
    try:
        res = HTTP_SESSION.get(f"https://api.bithumb.com/public/orderbook/{coin_code}_KRW", timeout=1.5).json()
        if res.get("status") == "0000":
            data = res["data"]
            bids = [{"price": float(b["price"]), "quantity": float(b["quantity"])} for b in data.get("bids", [])[:10]]
            asks = [{"price": float(a["price"]), "quantity": float(a["quantity"])} for a in data.get("asks", [])[:10]]
            tb = sum(b["quantity"] for b in bids); ta = sum(a["quantity"] for a in asks)
            tq = tb + ta
            parsed = {
                "bids": bids, "asks": asks, "total_bid_qty": tb, "total_ask_qty": ta,
                "total_bid_krw": sum(b["price"] * b["quantity"] for b in bids),
                "bid_ratio": round(tb / tq, 3) if tq > 0 else 0.5,
                "max_bid_wall": max(bids, key=lambda x: x["quantity"]) if bids else None,
                "time": time.time()
            }
            WS_ORDERBOOK_CACHE[coin_code] = parsed
            return parsed
    except Exception: pass
    return cached

async def bithumb_websocket_stream_worker():
    while True:
        try:
            async with websockets.connect("wss://pubwss.bithumb.com/pub/ws", ping_interval=30) as ws:
                l_sub = set()
                while True:
                    st = load_json_file(STATE_FILE, {})
                    pd = load_json_file(PAPER_TRADES_FILE, {"active_positions": {}})
                    ac, qc = set(pd.get("active_positions", {}).keys()), set(st.get("target_queue", {}).keys())
                    ts = [f"{c}_KRW" for c in ac.union(qc) if c]
                    if set(ts) != l_sub and ts:
                        await ws.send(json.dumps({"type": "transaction", "symbols": ts}))
                        await ws.send(json.dumps({"type": "orderbookdepth", "symbols": ts}))
                        l_sub = set(ts)
                    try:
                        data = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
                        msg_type, content = data.get("type"), data.get("content", {})
                        if msg_type == "transaction":
                            for item in content.get("list", []):
                                c = item.get("symbol", "").split('_')[0].upper()
                                p = float(item.get("price", 0.0))
                                if c and p > 0: WS_TICKER_CACHE[c] = {"price": p, "time": time.time()}
                        elif msg_type == "orderbookdepth":
                            for item in content.get("list", []):
                                c = item.get("symbol", "").split('_')[0].upper()
                                br = item.get("bids", [])[:10]; ar = item.get("asks", [])[:10]
                                bids = [{"price": float(b[0]), "quantity": float(b[1])} for b in br]
                                asks = [{"price": float(a[0]), "quantity": float(a[1])} for a in ar]
                                tb = sum(b["quantity"] for b in bids); ta = sum(a["quantity"] for a in asks)
                                tq = tb + ta
                                WS_ORDERBOOK_CACHE[c] = {
                                    "bids": bids, "asks": asks, "total_bid_qty": tb, "total_ask_qty": ta,
                                    "total_bid_krw": sum(b["price"] * b["quantity"] for b in bids),
                                    "bid_ratio": round(tb / tq, 3) if tq > 0 else 0.5,
                                    "max_bid_wall": max(bids, key=lambda x: x["quantity"]) if bids else None,
                                    "time": time.time()
                                }
                    except asyncio.TimeoutError: continue
        except Exception: await asyncio.sleep(3.0)

def get_bithumb_account_summary():
    if not BITHUMB_API_KEY or not BITHUMB_SECRET_KEY: return None, None
    try:
        res = HTTP_SESSION.get("https://api.bithumb.com/v1/accounts", headers=get_bithumb_jwt_headers(), timeout=2.5).json()
        if isinstance(res, list):
            t_krw, a_krw = 0.0, 0.0
            for acc in res:
                c, b, l = acc.get("currency", ""), float(acc.get("balance", 0.0)), float(acc.get("locked", 0.0))
                tu = b + l
                if c == "KRW": a_krw, t_krw = b, t_krw + tu
                elif c not in ["P", "POINT"] and tu > 0:
                    p = get_current_price(c) or float(acc.get("avg_buy_price", 0.0))
                    if p > 0: t_krw += (tu * p)
            return round(t_krw, 2), round(a_krw, 2)
    except Exception: pass
    return None, None

def execute_real_limit_buy_order(c: str, p: float, krw: float):
    if PAPER_TRADING: return True, "mock_" + str(uuid.uuid4())[:8], (krw / p)
    try:
        v = round(krw / p, 4)
        b = {"market": f"KRW-{c.upper()}", "side": "bid", "volume": str(v), "price": str(p), "ord_type": "limit"}
        res = HTTP_SESSION.post("https://api.bithumb.com/v1/orders", json=b, headers=get_bithumb_jwt_headers(b), timeout=3.0).json()
        if "uuid" in res: return True, res["uuid"], v
    except Exception as e: return False, str(e), 0.0
    return False, "", 0.0

def check_bithumb_order_status(uid: str):
    if PAPER_TRADING or not uid or uid.startswith("mock_"): return "wait", 0.0, 0.0
    try:
        p = {"uuid": uid}
        res = HTTP_SESSION.get("https://api.bithumb.com/v1/order", params=p, headers=get_bithumb_jwt_headers(p), timeout=2.5).json()
        return res.get("state", "wait"), float(res.get("executed_volume", 0.0)), float(res.get("paid_fee", 0.0))
    except Exception: return "wait", 0.0, 0.0

def get_bithumb_order_execution_detail(uid: str):
    if PAPER_TRADING or not uid or uid.startswith("mock_"): return None
    try:
        p = {"uuid": uid}
        res = HTTP_SESSION.get("https://api.bithumb.com/v1/order", params=p, headers=get_bithumb_jwt_headers(p), timeout=3.0).json()
        if isinstance(res, dict) and "uuid" in res:
            ef = float(res.get("executed_funds", 0.0)); ev = float(res.get("executed_volume", 0.0))
            return {"executed_funds": ef, "executed_volume": ev, "paid_fee": float(res.get("paid_fee", 0.0)), "avg_price": round(ef / ev, 4) if ev > 0 else 0.0}
    except Exception: pass
    return None

def cancel_bithumb_order(uid: str):
    if PAPER_TRADING or not uid or uid.startswith("mock_"): return True
    try:
        p = {"uuid": uid}
        res = HTTP_SESSION.delete("https://api.bithumb.com/v1/order", params=p, headers=get_bithumb_jwt_headers(p), timeout=2.5).json()
        return "uuid" in res
    except Exception: pass
    return False

def execute_real_market_sell_order(c: str, u: float):
    if PAPER_TRADING: return True, "mock_sell_" + str(uuid.uuid4())[:8]
    try:
        b = {"market": f"KRW-{c.upper()}", "side": "ask", "volume": str(u), "ord_type": "market"}
        res = HTTP_SESSION.post("https://api.bithumb.com/v1/orders", json=b, headers=get_bithumb_jwt_headers(b), timeout=3.0).json()
        if "uuid" in res: return True, res["uuid"]
    except Exception as e: return False, str(e)
    return False, ""

def get_candles(c, interval="15m", limit=40):
    try:
        res = HTTP_SESSION.get(f"https://api.bithumb.com/public/candlestick/{c}_KRW/{interval}", timeout=3.0).json()
        if res.get("status") == "0000":
            return [{"timestamp": int(x[0]), "open": float(x[1]), "close": float(x[2]), "high": float(x[3]), "low": float(x[4]), "volume": round(float(x[5]), 2)} for x in res['data'][-limit:]]
    except Exception: pass
    return []

def calculate_ema(candles, period=20):
    if len(candles) < period: return 0.0
    closes = [c['close'] for c in candles]
    multiplier = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = (price - ema) * multiplier + ema
    return ema

def calculate_atr(candles, period=14):
    if len(candles) < period + 1: return 0.0
    trs = [max(candles[i]['high'] - candles[i]['low'], abs(candles[i]['high'] - candles[i-1]['close']), abs(candles[i]['low'] - candles[i-1]['close'])) for i in range(1, len(candles))]
    return round(sum(trs[-period:]) / period, 4)

def calculate_quant_features(c_1h, c_15m):
    curr_p = c_15m[-1]['close']
    r_lows = [c['low'] for c in c_15m[-10:]]; r_highs = [c['high'] for c in c_15m[-10:]]
    box_low = min(r_lows); box_high = max(r_highs)
    vol_avg = sum(c['volume'] for c in c_15m[-11:-1]) / 10.0 if len(c_15m) >= 11 else 1.0
    
    return {
        "box_low": box_low, "box_high": box_high,
        "box_range_pct": round(((box_high - box_low) / box_low) * 100.0, 2) if box_low > 0 else 0.0,
        "dist_to_low_pct": round(((curr_p - box_low) / box_low) * 100.0, 2) if box_low > 0 else 999.0,
        "atr_15m_pct": round((calculate_atr(c_15m, 14) / curr_p) * 100.0, 2) if curr_p > 0 else 0.0,
        "curr_price": curr_p,
        "vol_surge_ratio": round(c_15m[-1]['volume'] / vol_avg, 2) if vol_avg > 0 else 1.0,
        "vol_avg_15m": vol_avg,
        "tick_ratio_pct": round((get_bithumb_tick_size(curr_p) / curr_p) * 100.0, 3) if curr_p > 0 else 1.0,
        "recent_1h_trade_val": c_1h[-1]['volume'] * c_1h[-1]['close'] if c_1h else 0.0
    }

def scan_3track_candidates(sym, price, val_24h, c_1h, c_15m, c_5m, min_1h_val):
    if len(c_1h) < 20 or len(c_15m) < 20 or len(c_5m) < 5 or price < MIN_COIN_PRICE_KRW: return None
    q = calculate_quant_features(c_1h, c_15m)
    if q["tick_ratio_pct"] > MAX_TICK_RATIO_PCT or q["atr_15m_pct"] < MIN_15M_ATR_PCT or q["recent_1h_trade_val"] < min_1h_val: return None

    ob = get_bithumb_orderbook_10(sym)
    bid_ratio = ob.get("bid_ratio", 0.5) if ob else 0.5

    # [Track B] 공격적 모멘텀 돌파 (거래량 폭증 조건 강화)
    day_high = max(c['high'] for c in c_15m[-32:]) if len(c_15m) >= 32 else q["box_high"]
    dist_to_high_pct = ((day_high - price) / price) * 100.0 if price > 0 else 999.0
    if dist_to_high_pct <= 0.35 and q["vol_surge_ratio"] >= 3.0 and bid_ratio >= 0.58:
        return {"symbol": f"{sym}/KRW", "code": sym, "track": "B", "name": "모멘텀 돌파", "current_price": price, "target_entry": round_to_bithumb_tick(price), "sl_pct": -1.10, "tp_trigger_pct": 1.50, "tp_floor_lock": 1.10, "timeout_mins": 8, "bid_ratio": bid_ratio, "quant": q, "reason": "거래량 3배 폭증 동반 고점 돌파 타점"}

    # [Track C] 1차 눌림목 (철저한 5분봉 턴어라운드 확인)
    if len(c_15m) >= 12:
        spike = next((c for c in c_15m[-5:-1] if ((c['close'] - c['open']) / c['open']) * 100.0 >= 4.0 and (c['volume'] / q["vol_avg_15m"] if q["vol_avg_15m"] > 0 else 0) >= 3.0), None)
        if spike:
            spike_mid = (spike['low'] + spike['high']) / 2.0
            is_volume_dry = (c_15m[-1]['volume'] <= spike['volume'] * 0.35)
            holds_support = (price >= spike_mid * 0.995 and price <= spike['high'])
            is_5m_turning = (c_5m[-1]['close'] >= c_5m[-1]['open'] and c_5m[-1]['close'] >= c_5m[-2]['close'])

            if is_volume_dry and holds_support and bid_ratio >= 0.50 and is_5m_turning:
                return {"symbol": f"{sym}/KRW", "code": sym, "track": "C", "name": "1차 눌림목", "current_price": price, "target_entry": round_to_bithumb_tick(price), "sl_pct": -1.20, "tp_trigger_pct": 1.50, "tp_floor_lock": 1.10, "timeout_mins": 12, "bid_ratio": bid_ratio, "quant": q, "reason": "수급 주도주 눌림 후 5분봉 확실한 반등 턴 포착"}

    # [Track A] 상방 추세(1H EMA 20) 내 박스 하단 반등
    ema_1h = calculate_ema(c_1h, 20)
    if q["box_range_pct"] >= 2.0 and (-0.50 <= q["dist_to_low_pct"] <= 1.20) and bid_ratio >= 0.48:
        if price > ema_1h:
            return {"symbol": f"{sym}/KRW", "code": sym, "track": "A", "name": "바닥 반등", "current_price": price, "target_entry": round_to_bithumb_tick(price), "sl_pct": -1.00, "tp_trigger_pct": 1.35, "tp_floor_lock": 1.00, "timeout_mins": 10, "bid_ratio": bid_ratio, "quant": q, "reason": "정배열 추세 내 박스 하단 지지 흡수 반등"}

    return None

def check_daily_circuit_breaker(closed_trades, total_asset):
    today_start = get_kst_now().replace(hour=0, minute=0, second=0, microsecond=0)
    loss_krw = sum(t.get('profit_krw', 0) for t in closed_trades if t.get("is_paper", True) == PAPER_TRADING and parse_dt_safe(t.get('exit_time', '')) and parse_dt_safe(t.get('exit_time', '')) >= today_start)
    loss_pct = (loss_krw / total_asset) * 100.0 if total_asset > 0 else 0.0
    if loss_pct <= DAILY_LOSS_LIMIT_PCT: return False, f"당일 손실({loss_pct:.2f}%) 한도 초과"
    return True, "정상"

def check_market_drop_ratio(ticker_all_data):
    if not ticker_all_data: return True, 0.0, "데이터 이상"
    v = [(s, float(i.get('acc_trade_value_24H', 0)), float(i.get('fluctate_rate_24H', 0))) for s, i in ticker_all_data.items() if s != "date" and s.upper() not in STABLE_COINS]
    t30 = sorted(v, key=lambda x: x[1], reverse=True)[:30]
    if len(t30) < 15: return True, 0.0, "표본 부족"
    dr = sum(1 for i in t30 if i[2] < 0.0) / len(t30)
    if dr >= MARKET_DROP_RATIO_LIMIT: return False, dr, f"투매 감지 (상위 30개 중 {dr*100:.0f}% 하락)"
    return True, dr, "정상"

def calculate_dynamic_buy_ratio(closed_trades):
    m_cls = [t for t in closed_trades if t.get("is_paper", True) == PAPER_TRADING]
    if len(m_cls) < 3: return DEFAULT_BUY_RATIO
    if all(t.get('profit_pct', 0.0) < 0 for t in m_cls[-3:]): return 0.15
    if (sum(1 for t in m_cls[-5:] if t.get('profit_pct', 0.0) > 0) / min(len(m_cls), 5)) >= 0.8: return 0.25
    return DEFAULT_BUY_RATIO

def clean_and_parse_json(raw_text):
    if not raw_text: return None
    try: return json.loads(re.sub(r"```$", "", re.sub(r"^```(?:json)?", "", raw_text.strip(), flags=re.MULTILINE), flags=re.MULTILINE).strip())
    except Exception:
        m = re.search(r"\{.*\}", raw_text, re.DOTALL)
        if m:
            try: return json.loads(m.group(0))
            except: pass
    return None

def call_ai_api(sys_prompt, user_prompt):
    providers = [{"k": GEMINI_API_KEY, "u": "https://generativelanguage.googleapis.com/v1beta/openai/", "m": "gemini-3.5-flash-lite"}, {"k": SAMBANOVA_API_KEY, "u": "https://api.sambanova.ai/v1", "m": "Meta-Llama-3.3-70B-Instruct"}, {"k": GROQ_API_KEY3 or GROQ_API_KEY2, "u": "https://api.groq.com/openai/v1", "m": "llama-3.3-70b-specdec"}]
    for p in providers:
        if not p["k"]: continue
        try: return OpenAI(base_url=p['u'], api_key=p['k']).chat.completions.create(model=p['m'], messages=[{"role": "system", "content": sys_prompt + "\nOutput valid JSON ONLY."}, {"role": "user", "content": user_prompt}], response_format={"type": "json_object"}, temperature=0.1).choices[0].message.content
        except Exception: pass
    return None

def execute_server_side_strategy():
    global CIRCUIT_BREAKER_ACTIVE, NO_CANDIDATE_CYCLE_COUNT, LAST_NO_CANDIDATE_REASON
    if PENDING_PAPER_DRAIN: return

    st = load_json_file(STATE_FILE, {})
    pd = load_json_file(PAPER_TRADES_FILE, {"active_positions": {}, "closed_trades": []})
    ap, cl, tq = pd.get("active_positions", {}), pd.get("closed_trades", []), st.get("target_queue", {})
    m_act = {k: v for k, v in ap.items() if v.get("is_paper", True) == PAPER_TRADING}

    if len(m_act) >= MAX_HOLDING_COINS: return
    ta, av = get_bithumb_account_summary()
    if ta is None or av is None:
        if PAPER_TRADING: ta, av = 100000.0, 100000.0
        else: return

    cb_ok, cb_reason = check_daily_circuit_breaker(cl, ta)
    if not cb_ok:
        if not CIRCUIT_BREAKER_ACTIVE: CIRCUIT_BREAKER_ACTIVE = True; send_telegram_msg(f"🛑 [서킷브레이커 발동] {cb_reason}")
        return
    CIRCUIT_BREAKER_ACTIVE = False

    try: res = HTTP_SESSION.get("https://api.bithumb.com/public/ticker/ALL_KRW", timeout=6).json()
    except Exception: return
    if res.get("status") != "0000": return

    m_ok, dr, msg = check_market_drop_ratio(res.get("data", {}))
    if not m_ok:
        NO_CANDIDATE_CYCLE_COUNT += 1; LAST_NO_CANDIDATE_REASON = msg
        if NO_CANDIDATE_CYCLE_COUNT >= 12 and not m_act and not tq: send_telegram_msg(f"👀 [시장 관망 브리핑] {msg}"); NO_CANDIDATE_CYCLE_COUNT = 0
        return

    buy_krw = max(MIN_BUY_KRW, min(round(ta * calculate_dynamic_buy_ratio(cl)), int(av)))
    if av < MIN_BUY_KRW: return

    hc = set(m_act.keys()).union(set({k: v for k, v in tq.items() if v.get("is_paper", True) == PAPER_TRADING}.keys()))
    rl = [(s, float(i['closing_price']), float(i['fluctate_rate_24H']), float(i['acc_trade_value_24H'])) for s, i in res["data"].items() if s != "date" and s.upper() not in STABLE_COINS and s not in hc and float(i['closing_price']) >= MIN_COIN_PRICE_KRW and float(i['acc_trade_value_24H']) >= MIN_24H_ACC_TRADE_VALUE]
    sl = sorted(rl, key=lambda x: x[3], reverse=True)[:35]

    m1 = PRIMARY_1H_TRADE_VAL
    for attempt in range(2):
        cp = []
        for s, p, c, v in sl:
            c1, c15, c5 = get_candles(s, "1h", 25), get_candles(s, "15m", 35), get_candles(s, "5m", 10)
            cnd = scan_3track_candidates(s, p, v, c1, c15, c5, m1)
            if cnd: cp.append(cnd)
        if len(cp) >= 2 or attempt == 1: break
        m1 = FALLBACK_1H_TRADE_VAL

    if not cp:
        NO_CANDIDATE_CYCLE_COUNT += 1; LAST_NO_CANDIDATE_REASON = f"3-Track 조건 미달 (시장 하락률: {dr*100:.1f}%)"
        if NO_CANDIDATE_CYCLE_COUNT >= 12 and not m_act and not tq: send_telegram_msg(f"👀 [시장 관망 브리핑] {LAST_NO_CANDIDATE_REASON}"); NO_CANDIDATE_CYCLE_COUNT = 0
        return

    tp = sorted(cp, key=lambda x: x['bid_ratio'], reverse=True)[:6]
    dc = clean_and_parse_json(call_ai_api("Select UP TO 2 best setups from candidate pool (score >= 65).", f"Candidates:\n{json.dumps(tp, ensure_ascii=False)}\n\nSchema: {{'selected_candidates': [{{'symbol':'...', 'score':78, 'entry_discount_pct':0.10, 'detailed_reason':'...'}}]}}"))
    
    if not dc or "selected_candidates" not in dc or not dc["selected_candidates"]:
        NO_CANDIDATE_CYCLE_COUNT += 1; LAST_NO_CANDIDATE_REASON = "AI 승인 거절"
        return

    ad = 0; niso = get_kst_now().isoformat(); nts = time.time()
    for c in dc["selected_candidates"][:2]:
        co = c.get("symbol", "").split('/')[0].upper(); sc = int(c.get("score", 0))
        if not co or sc < 65 or co in hc: continue
        cp_val = get_current_price(co)
        if not cp_val or cp_val < MIN_COIN_PRICE_KRW: continue

        su = next((i for i in tp if i["code"] == co), tp[0])
        te = round_to_bithumb_tick(cp_val * (1.0 - (max(float(c.get("entry_discount_pct", 0.10)), 0.0) / 100.0)))
        ob = get_bithumb_orderbook_10(co)
        if ob and ob.get("max_bid_wall"):
            wp = ob["max_bid_wall"]["price"]
            ap = round_to_bithumb_tick(wp + get_bithumb_tick_size(wp))
            if abs((ap - te) / te) <= 0.005 and ap <= cp_val: te = ap

        tq[co] = {
            "symbol": f"{co}/KRW", "code": co, "track": su.get("track", "A"), "track_name": su.get("name", "전략"),
            "is_paper": PAPER_TRADING, "score": sc, "current_price": cp_val, "target_entry": te,
            "sl_pct": su.get("sl_pct", -1.00), "emergency_sl_pct": -1.50, "tp_trigger_pct": su.get("tp_trigger_pct", 1.35), "tp_floor_lock": su.get("tp_floor_lock", 1.00),
            "timeout_mins": 12, "holding_timeout_mins": su.get("timeout_mins", 10),
            "buy_amount_krw": buy_krw, "ordered_volume": 0.0, "order_uuid": None, "order_placed": False,
            "bid_breach_start_time": None, "entry_bid_ratio": su.get("bid_ratio", 0.5), "detailed_reason": c.get("detailed_reason", ""), "created_at": niso, "created_timestamp": nts
        }
        ad += 1

    if ad > 0:
        NO_CANDIDATE_CYCLE_COUNT = 0
        st["target_queue"] = tq
        save_json_file(STATE_FILE, st)

async def realtime_execution_engine():
    global EMERGENCY_STOP, PAPER_TRADING, PENDING_PAPER_DRAIN
    lr = 0
    threading.Thread(target=telegram_listener_thread, daemon=True).start()
    asyncio.create_task(bithumb_websocket_stream_worker())

    while True:
        try:
            now = time.time(); ndt = get_kst_now()
            if now - lr >= 300:
                lr = now; asyncio.create_task(asyncio.to_thread(execute_server_side_strategy))

            st = load_json_file(STATE_FILE, {})
            pd = load_json_file(PAPER_TRADES_FILE, {"active_positions": {}, "closed_trades": []})
            tq, ap, cl = st.get("target_queue", {}), pd.get("active_positions", {}), pd.get("closed_trades", [])

            if PENDING_PAPER_DRAIN and not [v for v in ap.values() if not v.get("is_paper", True)]:
                PENDING_PAPER_DRAIN, PAPER_TRADING = False, True
                st.update({"paper_trading": True, "pending_paper_drain": False})
                save_json_file(STATE_FILE, st); send_telegram_msg("🎉 [모드 전환] 실전 청산 완료 ➔ 모의투자 전환")

            ac = len([v for v in ap.values() if v.get("is_paper", True) == PAPER_TRADING])
            oc = len([v for v in tq.values() if v.get("is_paper", True) == PAPER_TRADING and v.get("order_placed")])

            for c, pl in sorted(list(tq.items()), key=lambda x: (x[1].get("score", 0), -x[1].get("created_timestamp", 0)), reverse=True):
                if pl.get("is_paper", True) != PAPER_TRADING or oc >= max(0, MAX_HOLDING_COINS - ac): break
                if not pl.get("order_placed"):
                    sc, ou, ov = execute_real_limit_buy_order(c, pl["target_entry"], pl["buy_amount_krw"])
                    if sc:
                        pl.update({"order_placed": True, "order_uuid": ou, "ordered_volume": ov})
                        oc += 1; save_json_file(STATE_FILE, st)
                        send_telegram_msg(f"🎯 [{'모의투자' if PAPER_TRADING else '실전매매'}] [{pl.get('track_name', '')}] 예약\n• {pl['symbol']}({pl['score']}점)\n• 매수: {pl['buy_amount_krw']:,}원 @ {pl['target_entry']:,.4f}")

            for c, pl in list(tq.items()):
                el = (now - pl.get("created_timestamp", now)) / 60.0
                ou = pl.get("order_uuid")

                if el >= pl.get("timeout_mins", 12):
                    if pl.get("order_placed") and ou: cancel_bithumb_order(ou)
                    if c in tq: del tq[c]
                    save_json_file(STATE_FILE, st); continue

                ob = get_bithumb_orderbook_10(c)
                inv = ""
                if ob and ob.get("bid_ratio", 0.5) < 0.20:
                    if pl.get("bid_breach_start_time") is None: pl["bid_breach_start_time"] = now
                    elif now - pl["bid_breach_start_time"] >= 3.0: inv = "호가창 붕괴"
                else: pl["bid_breach_start_time"] = None

                if inv:
                    if pl.get("order_placed") and ou: cancel_bithumb_order(ou)
                    if c in tq: del tq[c]
                    save_json_file(STATE_FILE, st); continue

                if pl.get("order_placed"):
                    cp = get_current_price(c)
                    fl, bf = False, 0.0
                    if pl.get("is_paper", True):
                        if cp and cp <= pl["target_entry"]: fl = True
                    else:
                        os, ev, fe = check_bithumb_order_status(ou)
                        if os == "done": fl, bf = True, fe
                        elif os == "cancel":
                            if c in tq: del tq[c]
                            save_json_file(STATE_FILE, st); continue

                    if fl:
                        rp, ru, ak = pl["target_entry"], pl.get("ordered_volume", 0.0), round(pl["target_entry"] * pl.get("ordered_volume", 0.0))
                        if not pl.get("is_paper", True):
                            dtl = get_bithumb_order_execution_detail(ou)
                            if dtl: rp, ru, ak, bf = dtl["avg_price"], dtl["executed_volume"], dtl["executed_funds"], dtl["paid_fee"]
                            else:
                                arp, aunits = get_real_account_coin_info(c)
                                if arp and arp > 0: rp, ru, ak = arp, aunits, round(arp * aunits)

                        ap[c] = {**pl, "entry_price": rp, "highest_price": rp, "buy_amount_krw": ak, "units": ru, "buy_fee": bf, "entry_timestamp": now, "entry_time": ndt.isoformat(), "locked_floor_profit_pct": 0.0, "breakeven_locked": False, "price_history": [(now, rp)]}
                        if c in tq: del tq[c]
                        save_json_file(PAPER_TRADES_FILE, {"active_positions": ap, "closed_trades": cl})
                        save_json_file(STATE_FILE, st)
                        send_telegram_msg(f"⚡ [체결 완료] {pl['symbol']} ({pl['track']})\n• 진입가: {rp:,.4f}\n• 매수금: {ak:,}원")

            for c, pos in list(ap.items()):
                cp = get_current_price(c)
                if not cp: continue

                ep, ets = pos["entry_price"], pos.get("entry_timestamp", now)
                el_sec = now - ets
                
                hist = [(ts, p) for ts, p in pos.get("price_history", []) if now - ts <= 3600]
                hist.append((now, cp)); pos["price_history"] = hist
                
                if cp > pos.get("highest_price", ep): pos["highest_price"] = cp
                cpct, hpct = ((cp - ep) / ep) * 100.0, ((pos["highest_price"] - ep) / ep) * 100.0

                if hpct >= 0.80: pos["breakeven_locked"] = True

                # 🚀 공격적 트레일링 익절 로직: 노이즈를 완전히 무시하고 상승폭을 끝까지 발라먹도록 풀백 대폭 확대
                t_act, s_pull, s_flr = False, 0.50, 0.0
                if hpct >= 6.0: t_act, s_pull, s_flr = True, 1.20, 4.50
                elif hpct >= 4.0: t_act, s_pull, s_flr = True, 0.90, 2.80
                elif hpct >= 2.5: t_act, s_pull, s_flr = True, 0.70, 1.50
                elif hpct >= pos.get("tp_trigger_pct", 1.35): t_act, s_pull, s_flr = True, 0.50, pos.get("tp_floor_lock", 1.00)

                pos["locked_floor_profit_pct"] = max(pos.get("locked_floor_profit_pct", 0.0), s_flr)
                sc, rsn, xp, apull = False, "", cp, round(hpct - cpct, 2)

                if t_act:
                    tp = max(pos["highest_price"] * (1 - s_pull/100), ep * (1 + pos["locked_floor_profit_pct"]/100))
                    obx = get_bithumb_orderbook_10(c)
                    if obx and obx.get("max_bid_wall") and (cp - obx["max_bid_wall"]["price"]) / cp <= 0.003:
                        w2 = round_to_bithumb_tick(obx["max_bid_wall"]["price"] + 2 * get_bithumb_tick_size(obx["max_bid_wall"]["price"]))
                        if tp < w2 <= cp: tp = w2
                    if cp <= tp or apull >= s_pull or cpct <= pos["locked_floor_profit_pct"]:
                        sc, rsn = True, f"📈 추세 추종 익절 (고점 +{hpct:.2f}% ➔ +{pos['locked_floor_profit_pct']:.2f}% 락)"
                        if pos.get("is_paper", True): xp = round_to_bithumb_tick(tp - get_bithumb_tick_size(tp))

                elif cpct <= -1.50: sc, rsn = True, "🚨 하드 손절 (-1.50%)"; xp = round_to_bithumb_tick(ep * 0.985) if pos.get("is_paper", True) else xp
                elif pos.get("breakeven_locked") and cpct <= -0.05: sc, rsn = True, "🛡️ 본절 락 컷"; xp = round_to_bithumb_tick(ep * 0.9995) if pos.get("is_paper", True) else xp
                elif cpct <= pos["sl_pct"]:
                    obs = get_bithumb_orderbook_10(c)
                    if obs and obs.get("bid_ratio", 0.5) < 0.30: sc, rsn = True, f"⚡ 호가 붕괴 즉시 컷 ({obs.get('bid_ratio')*100:.1f}%)"
                    else:
                        if not pos.get("sl_breach_start_time"): pos["sl_breach_start_time"] = now
                        elif now - pos["sl_breach_start_time"] >= 2.5: sc, rsn = True, f"🛡️ 손절선({pos['sl_pct']}%) 이탈"
                else: pos["sl_breach_start_time"] = None

                if not sc and el_sec >= pos.get("holding_timeout_mins", 10) * 60:
                    if hpct < 0.30: sc, rsn = True, f"⌛ {pos.get('holding_timeout_mins')}분 조기 정리"
                    elif 0.75 <= hpct < 1.20 and el_sec >= 1800: sc, rsn = True, "⌛ 30분 안전 정리"

                if sc:
                    su, sf, af = None, 0.0, 0.0
                    if not pos.get("is_paper", True):
                        sc_o, uid = execute_real_market_sell_order(c, pos.get("units", 0.0))
                        if sc_o:
                            su = uid
                            for _ in range(3):
                                time.sleep(0.5); dtl = get_bithumb_order_execution_detail(uid)
                                if dtl and dtl.get("executed_funds", 0) > 0:
                                    xp, af, sf = dtl["avg_price"], dtl["executed_funds"], dtl["paid_fee"]; break

                    bk, bf = pos.get("buy_amount_krw", MIN_BUY_KRW), pos.get("buy_fee", 0.0)
                    if not pos.get("is_paper", True) and af > 0: pk, fpct = round(af - bk - bf - sf), round(((af - bk - bf - sf) / bk) * 100.0, 2)
                    else: fpct = round(((xp - ep) / ep) * 100.0, 2); pk = round(bk * (fpct / 100.0))

                    cl.append({
                        "symbol": pos["symbol"], "track": pos["track"], "track_name": pos["track_name"], "is_paper": pos.get("is_paper", True),
                        "entry_price": ep, "exit_price": xp, "buy_amount_krw": bk, "profit_krw": pk, "profit_pct": fpct, "buy_fee": bf, "sell_fee": sf,
                        "reason": rsn, "trajectory_summary": generate_trade_trajectory_summary(pos, xp, fpct, hpct, el_sec, rsn), "entry_time": pos["entry_time"], "exit_time": ndt.isoformat()
                    })
                    del ap[c]
                    save_json_file(PAPER_TRADES_FILE, {"active_positions": ap, "closed_trades": cl})
                    send_telegram_msg(f"{'🎉' if fpct>0 else '🌧️'} [청산] {pos['symbol']}\n• 확정 손익: {pk:,}원 ({fpct:+.2f}%)\n• 사유: {rsn}")
                    send_telegram_msg(format_portfolio_status_msg(ap, cl))

            if ap or tq: await asyncio.sleep(0.5)
            else: await asyncio.sleep(2.0)
        except Exception as e: logging.error(f"감시 루프 오류: {e}"); await asyncio.sleep(2)

def telegram_listener_thread():
    global EMERGENCY_STOP, CIRCUIT_BREAKER_ACTIVE, LAST_TELEGRAM_UPDATE_ID, PAPER_TRADING, PENDING_PAPER_DRAIN, UPDATE_BASELINE_TIME
    if not TELEGRAM_BOT_TOKEN: return
    try:
        r = HTTP_SESSION.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates", params={"timeout": 1}, timeout=5).json()
        if r.get("ok") and r.get("result"): LAST_TELEGRAM_UPDATE_ID = r["result"][-1]["update_id"]
    except Exception: pass

    while True:
        try:
            r = HTTP_SESSION.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates", params={"offset": LAST_TELEGRAM_UPDATE_ID + 1, "timeout": 10}, timeout=15).json()
            if r.get("ok"):
                for upd in r.get("result", []):
                    LAST_TELEGRAM_UPDATE_ID = upd["update_id"]
                    msg = upd.get("message", {}); txt = msg.get("text", "").strip()
                    if TELEGRAM_CHAT_ID and str(msg.get("chat", {}).get("id", "")) != TELEGRAM_CHAT_ID: continue

                    st, pd = load_json_file(STATE_FILE, {}), load_json_file(PAPER_TRADES_FILE, {"active_positions": {}, "closed_trades": []})
                    if txt == "/track":
                        bs = st.get('update_baseline_time', '')
                        ts = {'A': {'w':0,'l':0,'p':0}, 'B': {'w':0,'l':0,'p':0}, 'C': {'w':0,'l':0,'p':0}}
                        for t in pd.get("closed_trades", []):
                            if t.get("is_paper", True) != PAPER_TRADING or (bs and t.get('exit_time', '') < bs): continue
                            tk = t.get('track', 'A')
                            if tk in ts:
                                if t.get('profit_pct', 0) > 0: ts[tk]['w'] += 1
                                else: ts[tk]['l'] += 1
                                ts[tk]['p'] += t.get('profit_krw', 0)
                        send_telegram_msg("📊 [3-Track 리포트]\n" + "\n".join([f"• [{k}] {v['w']+v['l']}건 ({v['w']}승 {v['l']}패 | {v['p']:+,}원)" for k, v in ts.items()]))
                    elif txt == "/status": send_telegram_msg(format_portfolio_status_msg(pd.get("active_positions", {}), pd.get("closed_trades", [])))
                    elif txt == "/log": send_telegram_msg(f"📜 [최근 시스템 로그]\n```\n{subprocess.getoutput('journalctl -u autotrade.service -n 15 --no-pager')}\n```")
                    elif txt == "/stop": EMERGENCY_STOP = True; st["target_queue"] = {}; save_json_file(STATE_FILE, st); send_telegram_msg("🛑 [인터락 작동] 대기열 전량 취소.")
                    elif txt == "/start": EMERGENCY_STOP, CIRCUIT_BREAKER_ACTIVE = False, False; send_telegram_msg("▶️ [인터락 해제] 감시 재개.")
                    elif txt == "/paper": PAPER_TRADING, st["paper_trading"] = True, True; save_json_file(STATE_FILE, st); send_telegram_msg("🧪 모의투자 모드로 전환되었습니다.")
                    elif txt == "/real":
                        if [v for v in pd.get("active_positions", {}).values() if v.get("is_paper", True)]:
                            PENDING_PAPER_DRAIN, st["pending_paper_drain"] = True, True; save_json_file(STATE_FILE, st); send_telegram_msg("⚠️ 모의 포지션 청산 대기 중...")
                        else: PAPER_TRADING, st["paper_trading"] = False, False; save_json_file(STATE_FILE, st); send_telegram_msg("🔥 실전매매 모드로 전환되었습니다.")
                    elif txt == "/update":
                        send_telegram_msg("🔄 [원격 업데이트] 최신 코드 다운로드 중...")
                        def dr():
                            time.sleep(2); subprocess.run(["git", "stash"], cwd=PROJECT_DIR); subprocess.run(["git", "pull", "origin", "main"], cwd=PROJECT_DIR); subprocess.run(["sudo", "systemctl", "restart", "autotrade.service"])
                        threading.Thread(target=dr, daemon=True).start()
            time.sleep(1)
        except Exception: time.sleep(3)

if __name__ == "__main__":
    notify_startup_once()
    asyncio.run(realtime_execution_engine())
