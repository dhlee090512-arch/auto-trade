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
# 0. 전역 설정 및 퀀트 파라미터 (3-Track 멀티 엔진)
# ==========================================
BUILD_VERSION = "2026.09.30-v16-fast-cut"     # 🔖 호가 붕괴 즉시 컷 강화 버전
MAX_HOLDING_COINS = 2                          # 🛡️ 최대 동시 운용 종목 수
MIN_BUY_KRW = 6000                             # 💵 최소 매수 금액 (원)
DEFAULT_BUY_RATIO = 0.20                       # 📊 기본 1회 투입 비중 (총 자산의 20%)
MIN_COIN_PRICE_KRW = 50.0                      # 🛡️ 최소 진입 가격 (50원 미만 동전주 차단)
MAX_TICK_RATIO_PCT = 0.08                      # 🛡️ 1틱 변동률 상한선 (0.08% 이하 우량/중형 선별)
MIN_24H_ACC_TRADE_VALUE = 1_000_000_000        # 🛡️ 24시간 누적 거래대금 하한선 (10억 원)
PRIMARY_1H_TRADE_VAL = 300_000_000             # 🛡️ 1차 1시간 거래대금 필터 (3.0억 원)
FALLBACK_1H_TRADE_VAL = 180_000_000            # 🛡️ 후보 부족 시 2차 폴백 (1.8억 원)
MIN_15M_ATR_PCT = 1.10                         # ⚡ 15분봉 최소 변동폭 (1.1% 미만 배제)
MARKET_DROP_RATIO_LIMIT = 0.80                 # 🛑 상위 30개 중 80% 이상 하락 시 신규 진입 전면 차단
DAILY_LOSS_LIMIT_PCT = -3.0                    # 🛑 당일 누적 손실 한도 (-3.0%)

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
PENDING_PAPER_DRAIN = False
UPDATE_BASELINE_TIME = None
EMERGENCY_STOP = False
CIRCUIT_BREAKER_ACTIVE = False
LAST_TELEGRAM_UPDATE_ID = 0
NO_CANDIDATE_CYCLE_COUNT = 0
LAST_NO_CANDIDATE_REASON = "시스템 초기화 완료"

# 실시간 웹소켓 캐시 데이터
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
        logging.info(f"🔄 [버전 업데이트] 성과 기준 시각 갱신: {now_kst_iso} (Build: {BUILD_VERSION})")
    
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
            f"🎯 [오라클 서버] 3-Track 알파 + 호가붕괴 칼손절 엔진 가동 ({BUILD_VERSION})\n"
            f"• 모드: {mode_str}\n"
            f"• 리스크 방어: 1차 손절선 도달 시 호가 30% 미만이면 0초 즉시 탈출 (슬리피지 원천 차단)\n"
            f"• 브리핑: 12회(1시간) 주기 관망 알림 / 미체결 12분 대기\n"
            f"• 기준시각: {base_str}\n\n"
            f"📱 명령어: /status, /log, /paper, /real, /reset_stats, /stop, /start, /update"
        )

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
    held_symbols = [f"{v['symbol']}[{v.get('track', 'A')}]" for v in mode_active.values()]
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
            track = t.get('track', 'A')
            dt_obj = parse_dt_safe(t.get('exit_time', ''))
            time_display = dt_obj.strftime("%m/%d %H:%M KST") if dt_obj else "-"
                
            sign_pct = "+" if p_pct > 0 else ""
            sign_k = "+" if p_krw > 0 else ""
            trade_lines.append(f"{idx}. {symbol}[{track}]: {sign_k}{p_krw:,}원 ({sign_pct}{p_pct:.2f}%) | {time_display}")
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
    return f"""💼 [{mode_tag_name} 3-Track 알파 운용 현황]
• 보유 종목 : {held_str}

📜 [최근 10건 매도 이력 (KST)]
{trades_str}

📊 최근 10건 승률 : {win_rate_10}%
💰 최근 10건 실현 손익 : {sign_10}{profit_10_krw:,} KRW
━━━━━━━━━━━━━━━━━━━━
📈 [업데이트 이후 누적 성과] ({baseline_display} KST 이후)
{cum_stats_str}"""

def generate_trade_trajectory_summary(pos: dict, exit_p: float, curr_profit_pct: float, highest_profit_pct: float, elapsed_seconds: float, close_reason: str) -> str:
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

    track = pos.get("track", "A")
    if "트레일링 익절" in close_reason:
        return f"[{track}전략] 진입 {peak_time_str} 최고 +{highest_profit_pct:.2f}%까지 분출 후, 반락 시 실현 기준선({curr_profit_pct:+.2f}%)에서 확정 청산함. (보유: {dur_str})"
    elif "본절 락" in close_reason:
        return f"[{track}전략] 진입 {peak_time_str} 최고 +{highest_profit_pct:.2f}% 상승 후 후속 매수세 부재로 본절선에서 안전 탈출함. (보유: {dur_str})"
    elif "미반등" in close_reason or "정리" in close_reason:
        return f"[{track}전략] 정해진 타임아웃 동안 최고 +{highest_profit_pct:.2f}%에 그쳐 슬롯 회수 및 기회비용 방어를 위해 조기 정리함. (보유: {dur_str})"
    elif "스마트 손절" in close_reason or "즉시 컷" in close_reason or "호가 붕괴" in close_reason:
        return f"[{track}전략] 1차 손절선 도달과 동시에 호가 매수벽 붕괴가 감지되어 슬리피지 방지를 위해 0초 칼손절함. (보유: {dur_str})"
    else:
        return f"[{track}전략] 보유 시간 {dur_str} 동안 최저 {lowest_profit_pct:.2f}% ~ 최고 +{highest_profit_pct:.2f}% 흐름을 보인 후 기준선에 맞춰 정리함."

# ==========================================
# 3. 빗썸 호가창 및 실전 체결 상세 API
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
    logging.info("🌐 [WebSocket] 빗썸 실시간 스트림 연결 시작")
    ws_url = "wss://pubwss.bithumb.com/pub/ws"
    
    while True:
        try:
            async with websockets.connect(ws_url, ping_interval=30, ping_timeout=10) as ws:
                logging.info("🟢 [WebSocket] 빗썸 스트림 서버 연결 성공")
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
                        logging.info(f"📡 [WebSocket] 구독 갱신: {', '.join(target_symbols)}")

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
            logging.warning(f"⚠️ [WebSocket] 연결 해제: {e} ➔ 3초 후 재접속")
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

def get_bithumb_order_execution_detail(order_uuid: str):
    if PAPER_TRADING or not order_uuid or order_uuid.startswith("mock_"):
        return None
    try:
        url = "https://api.bithumb.com/v1/order"
        params = {"uuid": order_uuid}
        headers = get_bithumb_jwt_headers(params)
        res = HTTP_SESSION.get(url, params=params, headers=headers, timeout=3.0).json()
        if isinstance(res, dict) and "uuid" in res:
            executed_funds = float(res.get("executed_funds", 0.0))
            executed_vol = float(res.get("executed_volume", 0.0))
            paid_fee = float(res.get("paid_fee", 0.0))
            avg_price = (executed_funds / executed_vol) if executed_vol > 0 else 0.0
            return {
                "executed_funds": executed_funds,
                "executed_volume": executed_vol,
                "paid_fee": paid_fee,
                "avg_price": round(avg_price, 4)
            }
    except Exception as e:
        logging.error(f"주문 상세 조회 실패 ({order_uuid}): {e}")
    return None

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
        return True, "mock_sell_" + str(uuid.uuid4())[:8]
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
# 4. 퀀트 특징값 및 3-Track 스캐너
# ==========================================
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

def calculate_quant_features(candles_1h, candles_15m):
    closes_15m = [c['close'] for c in candles_15m]
    curr_p = closes_15m[-1]
    
    atr_15m = calculate_atr(candles_15m, 14)
    recent_10_lows = [c['low'] for c in candles_15m[-10:]]
    recent_10_highs = [c['high'] for c in candles_15m[-10:]]
    box_low = min(recent_10_lows)
    box_high = max(recent_10_highs)
    box_range_pct = ((box_high - box_low) / box_low) * 100.0 if box_low > 0 else 0.0
    dist_to_low_pct = ((curr_p - box_low) / box_low) * 100.0 if box_low > 0 else 999.0
    
    vol_avg_15m = sum(c['volume'] for c in candles_15m[-11:-1]) / 10.0 if len(candles_15m) >= 11 else 1.0
    vol_surge_ratio = round(candles_15m[-1]['volume'] / vol_avg_15m, 2) if vol_avg_15m > 0 else 1.0

    tick_size = get_bithumb_tick_size(curr_p)
    tick_ratio_pct = round((tick_size / curr_p) * 100.0, 3) if curr_p > 0 else 1.0
    atr_15m_pct = round((atr_15m / curr_p) * 100.0, 2) if curr_p > 0 else 0.0
    recent_1h_trade_val = candles_1h[-1]['volume'] * candles_1h[-1]['close'] if candles_1h else 0.0

    return {
        "box_low": box_low,
        "box_high": box_high,
        "box_range_pct": round(box_range_pct, 2),
        "dist_to_low_pct": round(dist_to_low_pct, 2),
        "atr_15m_pct": atr_15m_pct,
        "curr_price": curr_p,
        "vol_surge_ratio": vol_surge_ratio,
        "vol_avg_15m": vol_avg_15m,
        "tick_ratio_pct": tick_ratio_pct,
        "recent_1h_trade_val": recent_1h_trade_val
    }

def scan_3track_candidates(sym, price, val_24h, candles_1h, candles_15m, candles_5m, min_1h_val):
    if len(candles_1h) < 20 or len(candles_15m) < 20 or len(candles_5m) < 5:
        return None
    if price < MIN_COIN_PRICE_KRW:
        return None

    q = calculate_quant_features(candles_1h, candles_15m)
    if q["tick_ratio_pct"] > MAX_TICK_RATIO_PCT or q["atr_15m_pct"] < MIN_15M_ATR_PCT:
        return None
    if q["recent_1h_trade_val"] < min_1h_val:
        return None

    ob = get_bithumb_orderbook_10(sym)
    bid_ratio = ob.get("bid_ratio", 0.5) if ob else 0.5

    # -------------------------------------------------------------
    # [Track B] 모멘텀 돌파 (전고점 코앞 & 공격적 거래량 폭증)
    # -------------------------------------------------------------
    day_high = max(c['high'] for c in candles_15m[-32:]) if len(candles_15m) >= 32 else q["box_high"]
    dist_to_high_pct = ((day_high - price) / price) * 100.0 if price > 0 else 999.0
    
    if dist_to_high_pct <= 0.35 and q["vol_surge_ratio"] >= 2.5 and bid_ratio >= 0.55:
        target_entry = round_to_bithumb_tick(price)
        return {
            "symbol": f"{sym}/KRW", "code": sym, "track": "B",
            "name": "모멘텀 돌파", "slot": "SLOT_RANGE_ALPHA",
            "current_price": price, "target_entry": target_entry,
            "sl_pct": -1.20, "tp_trigger_pct": 1.50, "tp_floor_lock": 1.10, "timeout_mins": 8,
            "bid_ratio": bid_ratio, "quant": q,
            "reason": f"당일 고점 돌파 직전({dist_to_high_pct:.2f}%) + 15분 거래량 {q['vol_surge_ratio']}배 + 매수호가 {bid_ratio*100:.1f}% 쇄도"
        }

    # -------------------------------------------------------------
    # [Track C] 거래량 폭발 1차 눌림목 (주도 알트 안전 2차 파동)
    # -------------------------------------------------------------
    if len(candles_15m) >= 12:
        recent_4 = candles_15m[-5:-1]
        spike_candle = None
        for c in recent_4:
            c_ret = ((c['close'] - c['open']) / c['open']) * 100.0
            vol_ratio = c['volume'] / q["vol_avg_15m"] if q["vol_avg_15m"] > 0 else 0
            if c_ret >= 3.8 and vol_ratio >= 2.8:
                spike_candle = c
                break

        if spike_candle:
            curr_c = candles_15m[-1]
            spike_mid = (spike_candle['low'] + spike_candle['high']) / 2.0
            is_volume_dry = (curr_c['volume'] <= spike_candle['volume'] * 0.38)
            holds_support = (price >= spike_mid * 0.995 and price <= spike_candle['high'])

            if is_volume_dry and holds_support and bid_ratio >= 0.48:
                target_entry = round_to_bithumb_tick(price)
                return {
                    "symbol": f"{sym}/KRW", "code": sym, "track": "C",
                    "name": "1차 눌림목", "slot": "SLOT_RANGE_ALPHA",
                    "current_price": price, "target_entry": target_entry,
                    "sl_pct": -1.10, "tp_trigger_pct": 1.50, "tp_floor_lock": 1.10, "timeout_mins": 12,
                    "bid_ratio": bid_ratio, "quant": q,
                    "reason": f"장대양봉(거래량폭증) 후 거래량 마름({curr_c['volume']:.0f}) 확인 및 허리선({spike_mid:,.2f}) 지지 반등 타점"
                }

    # -------------------------------------------------------------
    # [Track A] 박스 하단 지지 반등 (호가 균형 45% 완화)
    # -------------------------------------------------------------
    if q["box_range_pct"] >= 2.0 and (-0.50 <= q["dist_to_low_pct"] <= 1.20) and bid_ratio >= 0.45:
        target_entry = round_to_bithumb_tick(price)
        return {
            "symbol": f"{sym}/KRW", "code": sym, "track": "A",
            "name": "바닥 반등", "slot": "SLOT_RANGE_ALPHA",
            "current_price": price, "target_entry": target_entry,
            "sl_pct": -1.00, "tp_trigger_pct": 1.35, "tp_floor_lock": 1.00, "timeout_mins": 10,
            "bid_ratio": bid_ratio, "quant": q,
            "reason": f"15분봉 박스 하단 지지({q['dist_to_low_pct']}%) + 호가 매수비율({bid_ratio*100:.1f}%) 흡수 턴 타점"
        }

    return None

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

def check_market_drop_ratio(ticker_all_data):
    if not ticker_all_data or not isinstance(ticker_all_data, dict):
        return True, 0.0, "시세 데이터 수신 정상"

    valid_coins = []
    for sym, info in ticker_all_data.items():
        if sym == "date" or sym.upper() in STABLE_COINS:
            continue
        try:
            val_24h = float(info.get('acc_trade_value_24H', 0.0))
            fluc_rate = float(info.get('fluctate_rate_24H', 0.0))
            valid_coins.append((sym, val_24h, fluc_rate))
        except Exception:
            continue

    top_30 = sorted(valid_coins, key=lambda x: x[1], reverse=True)[:30]
    if len(top_30) < 15:
        return True, 0.0, "표본 종목 수 부족 (통과)"

    drop_count = sum(1 for item in top_30 if item[2] < 0.0)
    drop_ratio = drop_count / len(top_30)

    if drop_ratio >= MARKET_DROP_RATIO_LIMIT:
        return False, drop_ratio, f"상위 30개 중 {drop_count}개({drop_ratio*100:.1f}%) 하락 중 (시장 전반 투매장 감지)"

    return True, drop_ratio, f"시장 하락 비율 {drop_ratio*100:.1f}% (정상 범위)"

def build_reflection_prompt():
    return (
        "Core Quantitative Directives (3-Track Alpha):\n"
        "1. Prioritize setups with genuine liquidity and clear structural edge (A: Box Reversion, C: Pullback, B: Breakout).\n"
        "2. Score >= 65 required for candidate approval.\n"
        "3. Output valid JSON ONLY matching schema."
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
# 6. 스크리닝 및 주기적 관망 브리핑 (1시간 주기)
# ==========================================
def execute_server_side_strategy():
    global CIRCUIT_BREAKER_ACTIVE, NO_CANDIDATE_CYCLE_COUNT, LAST_NO_CANDIDATE_REASON
    
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

    url = "https://api.bithumb.com/public/ticker/ALL_KRW"
    try:
        res = HTTP_SESSION.get(url, timeout=6).json()
    except Exception as e:
        logging.error(f"빗썸 전체 시세 조회 실패: {e}")
        return

    if res.get("status") != "0000": return

    market_ok, drop_ratio, market_msg = check_market_drop_ratio(res.get("data", {}))
    if not market_ok:
        NO_CANDIDATE_CYCLE_COUNT += 1
        LAST_NO_CANDIDATE_REASON = market_msg
        logging.info(f"🛑 [시장 방어 발동] {market_msg} ➔ 신규 매수 차단")
        check_and_notify_idle_briefing(active_positions, target_queue)
        return

    dynamic_ratio = calculate_dynamic_buy_ratio(closed_trades)
    target_buy_krw = round(total_asset * dynamic_ratio)

    if available_krw < MIN_BUY_KRW:
        logging.info(f"⏸️ 가용 원화 부족 (가용: {available_krw:,.0f} KRW < 최소 주문: {MIN_BUY_KRW:,} KRW) ➔ 관망")
        return

    actual_buy_krw = min(target_buy_krw, int(available_krw))
    actual_buy_krw = max(actual_buy_krw, MIN_BUY_KRW)

    held_or_queued_codes = set(mode_active.keys()).union(set(mode_queue.keys()))
    logging.info(f"🧠 [3-Track 알파 스크리닝] (총자산: {total_asset:,.0f}원 | 배정: {actual_buy_krw:,}원 | 시장하락률: {drop_ratio*100:.1f}%)")

    raw_list = []
    for sym, info in res["data"].items():
        if sym == "date" or sym.upper() in STABLE_COINS: continue
        if sym in held_or_queued_codes: continue
        try:
            close_p = float(info['closing_price'])
            if close_p < MIN_COIN_PRICE_KRW: continue
            val_24h = float(info['acc_trade_value_24H'])
            if val_24h < MIN_24H_ACC_TRADE_VALUE: continue
            raw_list.append((sym, close_p, float(info['fluctate_rate_24H']), val_24h))
        except Exception: pass

    sorted_list = sorted(raw_list, key=lambda x: x[3], reverse=True)[:35]

    min_1h_threshold = PRIMARY_1H_TRADE_VAL
    for attempt in range(2):
        candidates_pool = []

        for sym, price, change, val_24h in sorted_list:
            c_1h = get_candles(sym, interval="1h", limit=25)
            time.sleep(0.02)
            c_15m = get_candles(sym, interval="15m", limit=35)
            time.sleep(0.02)
            c_5m = get_candles(sym, interval="5m", limit=10)
            time.sleep(0.02)

            cand = scan_3track_candidates(sym, price, val_24h, c_1h, c_15m, c_5m, min_1h_threshold)
            if cand:
                candidates_pool.append(cand)

        if len(candidates_pool) >= 2 or attempt == 1:
            break
        min_1h_threshold = FALLBACK_1H_TRADE_VAL
        logging.info(f"🔄 1시간 거래대금 폴백 가동: 3.0억 ➔ 1.8억 원")

    if not candidates_pool:
        NO_CANDIDATE_CYCLE_COUNT += 1
        LAST_NO_CANDIDATE_REASON = f"3-Track(A/B/C) 조건 부합 종목 부재 (시장 하락률: {drop_ratio*100:.1f}%)"
        logging.info("⏸️ 조건 충족 후보가 없어 관망합니다.")
        check_and_notify_idle_briefing(active_positions, target_queue)
        return

    target_pool = sorted(candidates_pool, key=lambda x: x['bid_ratio'], reverse=True)[:6]
    reflection_text = build_reflection_prompt()

    sys_prompt = (
        "You are an elite crypto prop trader orchestrating a 3-track scalping strategy (A: Box Reversion, B: Breakout, C: Pullback).\n"
        f"Context:\n{reflection_text}\n\n"
        "Rules:\n"
        "1. Select UP TO 2 best setups from candidate pool (score >= 65).\n"
        "2. Output valid JSON ONLY strictly matching schema."
    )
    user_prompt = (
        f"Candidate 3-Track Setups:\n{json.dumps(target_pool, ensure_ascii=False)}\n\n"
        "Schema: {\n"
        '  "selected_candidates": [\n'
        '    {\n'
        '      "symbol": "SYMBOL/KRW",\n'
        '      "score": 78,\n'
        '      "entry_discount_pct": 0.10,\n'
        '      "detailed_reason": "전략 적합성 및 진입 근거"\n'
        '    }\n'
        '  ]\n'
        "}"
    )

    res_raw = call_ai_api(sys_prompt, user_prompt)
    decision = clean_and_parse_json(res_raw)

    if not decision or "selected_candidates" not in decision:
        NO_CANDIDATE_CYCLE_COUNT += 1
        LAST_NO_CANDIDATE_REASON = "AI 점수 65점 이상 후보 미선정"
        check_and_notify_idle_briefing(active_positions, target_queue)
        return

    candidates = decision.get("selected_candidates", [])
    if not candidates:
        NO_CANDIDATE_CYCLE_COUNT += 1
        LAST_NO_CANDIDATE_REASON = "AI 점수 65점 이상 후보 미선정"
        check_and_notify_idle_briefing(active_positions, target_queue)
        return

    now_iso = get_kst_now().isoformat()
    now_ts = time.time()
    added_to_queue = 0

    for cand in candidates[:2]:
        sym = cand.get("symbol", "")
        code = sym.split('/')[0].upper()
        score = int(cand.get("score", 0))

        if not code or score < 65:
            continue
        if code in held_or_queued_codes:
            continue

        curr_p = get_current_price(code)
        if not curr_p or curr_p < MIN_COIN_PRICE_KRW:
            continue

        chosen_setup = next((item for item in target_pool if item["code"] == code), target_pool[0])
        discount = max(float(cand.get("entry_discount_pct", 0.10)), 0.0)
        base_target_entry = round_to_bithumb_tick(curr_p * (1.0 - (discount / 100.0)))
        
        ob = get_bithumb_orderbook_10(code)
        final_target_entry = base_target_entry
        bid_ratio_entry = chosen_setup.get("bid_ratio", 0.5)
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
            "track": chosen_setup.get("track", "A"),
            "track_name": chosen_setup.get("name", "전략"),
            "mode": "SCALPING",
            "is_paper": PAPER_TRADING,
            "slot": "SLOT_RANGE_ALPHA",
            "score": score,
            "current_price": curr_p,
            "target_entry": final_target_entry,
            "sl_pct": chosen_setup.get("sl_pct", -1.00),
            "emergency_sl_pct": -1.50,
            "tp_trigger_pct": chosen_setup.get("tp_trigger_pct", 1.35),
            "tp_floor_lock": chosen_setup.get("tp_floor_lock", 1.00),
            "timeout_mins": 12,
            "holding_timeout_mins": chosen_setup.get("timeout_mins", 10),
            "buy_amount_krw": actual_buy_krw,
            "ordered_volume": 0.0,
            "order_uuid": None,
            "order_placed": False,
            "bid_breach_start_time": None,
            "entry_bid_ratio": bid_ratio_entry,
            "detailed_reason": cand.get("detailed_reason", chosen_setup.get("reason", "사유 미기재")),
            "created_at": now_iso,
            "created_timestamp": now_ts
        }

        target_queue[code] = plan_data
        added_to_queue += 1
        logging.info(f"📥 [대기열 등록] {code}[Track {plan_data['track']}] (점수: {score}점, 타점: {final_target_entry:,.4f})")

    if added_to_queue > 0:
        NO_CANDIDATE_CYCLE_COUNT = 0
        server_state["target_queue"] = target_queue
        server_state["last_updated"] = now_iso
        save_json_file(STATE_FILE, server_state)
        
        targets_payload = {"updated_at": now_iso, "paper_trading": PAPER_TRADING, "queue": target_queue}
        save_json_file(TARGETS_FILE, targets_payload)
        sync_file_to_github(TARGETS_FILE, targets_payload)
    else:
        NO_CANDIDATE_CYCLE_COUNT += 1
        check_and_notify_idle_briefing(active_positions, target_queue)

def check_and_notify_idle_briefing(active_positions, target_queue):
    global NO_CANDIDATE_CYCLE_COUNT
    if NO_CANDIDATE_CYCLE_COUNT >= 12:
        mode_str = "모의투자" if PAPER_TRADING else "실전매매"
        held_count = len([v for v in active_positions.values() if v.get("is_paper", True) == PAPER_TRADING])
        queue_count = len([v for v in target_queue.values() if v.get("is_paper", True) == PAPER_TRADING])
        
        if held_count == 0 and queue_count == 0:
            briefing_msg = f"""👀 [시장 관망 브리핑 (1시간 경과)] - {mode_str}
• 상태: 신규 진입 대기 중 (최근 1시간 동안 조건 미충족)
• 사유: {LAST_NO_CANDIDATE_REASON}
• 조치: 원금 보존을 위해 무리한 진입을 자제하며 3-Track(A/B/C) 호가 감시 지속 중
• 엔진: WebSocket 및 퀀트 엔진 정상 작동 중"""
            send_telegram_msg(briefing_msg)
        
        NO_CANDIDATE_CYCLE_COUNT = 0

# ==========================================
# 7. 실시간 감시 엔진 (호가 붕괴 즉시 컷 적용)
# ==========================================
async def realtime_execution_engine():
    global EMERGENCY_STOP, PAPER_TRADING, PENDING_PAPER_DRAIN
    logging.info(f"⚡ 3-Track 알파 + 호가붕괴 칼손절 엔진 가동 (Build: {BUILD_VERSION})")
    last_strategy_run = 0

    threading.Thread(target=telegram_listener_thread, daemon=True).start()
    asyncio.create_task(bithumb_websocket_stream_worker())

    while True:
        try:
            now = time.time()
            now_dt = get_kst_now()

            # 5분 주기 퀀트 스크리닝
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
                    send_telegram_msg("🎉 [모드 전환 완료] 모든 실전 포지션이 청산되어 모의투자 모드로 자동 전환되었습니다.")

            # [1] 대기열 순환 승격 및 인터락 감시
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
                        send_telegram_msg(
                            f"🎯 {mode_title} [{plan.get('track_name', '전략')}] 지정가 예약 접수\n"
                            f"• 종목 : {plan['symbol']} (트랙 {plan.get('track', 'A')} | 점수: {plan['score']}점)\n"
                            f"• 지정가 : {plan['target_entry']:,.4f} KRW\n"
                            f"• 배정금 : {plan['buy_amount_krw']:,} KRW (수량: {ord_vol:,.4f})\n"
                            f"💡 선정 사유: {plan.get('detailed_reason')}"
                        )

            # 대기열 종목 감시 (12분 대기)
            for code, plan in list(target_queue.items()):
                created_ts = plan.get("created_timestamp", now)
                elapsed_mins = (now - created_ts) / 60.0
                timeout_limit = plan.get("timeout_mins", 12)
                order_uuid = plan.get("order_uuid")

                if elapsed_mins >= timeout_limit:
                    if plan.get("order_placed", False) and order_uuid:
                        cancel_bithumb_order(order_uuid)
                    if code in target_queue:
                        del target_queue[code]
                    server_state["target_queue"] = target_queue
                    save_json_file(STATE_FILE, server_state)
                    send_telegram_msg(f"⌛ [{plan['symbol']}] 12분 대기 만료로 주문을 취소하고 차순위 종목으로 회전합니다.")
                    continue

                # 5분봉 음봉 -2.0% 인터락
                c_5m = get_candles(code, interval="5m", limit=5)
                invalidate_reason = ""
                if len(c_5m) >= 3:
                    last_candle = c_5m[-2]
                    candle_drop_pct = ((last_candle['open'] - last_candle['close']) / last_candle['open']) * 100.0
                    if candle_drop_pct >= 2.0:
                        invalidate_reason = f"직전 5분봉 투매 장대음봉(-{candle_drop_pct:.1f}%) 발생"

                ob = get_bithumb_orderbook_10(code)
                if ob and ob.get("bid_ratio", 0.5) < 0.20:
                    if plan.get("bid_breach_start_time") is None:
                        plan["bid_breach_start_time"] = now
                    elif now - plan["bid_breach_start_time"] >= 3.0:
                        invalidate_reason = f"호가창 매수 잔량 20% 미만 붕괴 3초 지속"
                else:
                    plan["bid_breach_start_time"] = None

                if invalidate_reason:
                    if plan.get("order_placed", False) and order_uuid:
                        cancel_bithumb_order(order_uuid)
                    if code in target_queue:
                        del target_queue[code]
                    server_state["target_queue"] = target_queue
                    save_json_file(STATE_FILE, server_state)
                    send_telegram_msg(f"🛑 [{plan['symbol']}] 타점 무효화 ({invalidate_reason}) ➔ 주문 철회")
                    continue

                # 체결 검사 및 실제 매수 총액 산출
                if plan.get("order_placed", False):
                    curr_p = get_current_price(code)
                    is_filled = False
                    buy_fee_paid = 0.0

                    if plan.get("is_paper", True):
                        if curr_p and curr_p <= plan["target_entry"]:
                            is_filled = True
                    else:
                        order_state, exec_vol, fee = check_bithumb_order_status(order_uuid)
                        if order_state == "done":
                            is_filled = True
                            buy_fee_paid = fee
                        elif order_state == "cancel":
                            if code in target_queue:
                                del target_queue[code]
                            server_state["target_queue"] = target_queue
                            save_json_file(STATE_FILE, server_state)
                            continue

                    if is_filled:
                        real_entry_price = plan["target_entry"]
                        real_units = plan.get("ordered_volume", 0.0)
                        actual_invested_krw = round(real_entry_price * real_units)

                        if not plan.get("is_paper", True):
                            detail = get_bithumb_order_execution_detail(order_uuid)
                            if detail:
                                real_entry_price = detail["avg_price"]
                                real_units = detail["executed_volume"]
                                actual_invested_krw = detail["executed_funds"]
                                buy_fee_paid = detail["paid_fee"]
                            else:
                                account_avg_p, account_units = get_real_account_coin_info(code)
                                if account_avg_p and account_avg_p > 0:
                                    real_entry_price = account_avg_p
                                    real_units = account_units
                                    actual_invested_krw = round(real_entry_price * real_units)

                        logging.info(f"🎯 [{plan['symbol']}] 체결 완료 ➔ 포지션 등록")

                        active_positions[code] = {
                            "symbol": plan["symbol"],
                            "track": plan.get("track", "A"),
                            "track_name": plan.get("track_name", "전략"),
                            "mode": "SCALPING",
                            "is_paper": plan.get("is_paper", True),
                            "slot": "SLOT_RANGE_ALPHA",
                            "entry_price": real_entry_price,
                            "highest_price": real_entry_price,
                            "buy_amount_krw": actual_invested_krw,
                            "units": real_units,
                            "buy_fee": buy_fee_paid,
                            "buy_order_uuid": order_uuid,
                            "sl_pct": plan.get("sl_pct", -1.00),
                            "emergency_sl_pct": -1.50,
                            "tp_trigger_pct": plan.get("tp_trigger_pct", 1.35),
                            "tp_floor_lock": plan.get("tp_floor_lock", 1.00),
                            "holding_timeout_mins": plan.get("holding_timeout_mins", 10),
                            "sl_breach_start_time": None,
                            "entry_timestamp": now,
                            "entry_time": now_dt.isoformat(),
                            "locked_floor_profit_pct": 0.0,
                            "breakeven_locked": False,
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
                        fee_info_str = f" (수수료: {buy_fee_paid:,.1f}원)" if buy_fee_paid > 0 else ""
                        send_telegram_msg(
                            f"⚡ [체결 완료] - {mode_str}\n"
                            f"• 종목 : {plan['symbol']} (트랙 {plan.get('track', 'A')} - {plan.get('track_name', '전략')})\n"
                            f"• 체결가 : {real_entry_price:,.4f} KRW\n"
                            f"• 매수금 : {actual_invested_krw:,.0f} KRW{fee_info_str} (수량: {real_units:,.4f})\n"
                            f"🛡️ 손절: {plan.get('sl_pct', -1.00):.2f}% (호가 30% 미만 시 0초 즉시 컷) | 비상: -1.50%\n"
                            f"📈 익절: +{plan.get('tp_trigger_pct', 1.35):.2f}% 시작 트레일링 (하한 +{plan.get('tp_floor_lock', 1.00):.2f}% 락)\n"
                            f"🔒 안전: +0.80% 도달 시 본절(-0.05%) 락 / {plan.get('holding_timeout_mins', 10)}분 미반등 시 즉시 정리\n"
                            f"⏰ 체결 시각: {now_dt.strftime('%m/%d %H:%M KST')}"
                        )

            # [2] 보유 포지션 실시간 감시 및 동적 청산
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

                # 본절 락 (+0.80% 도달 시 -0.05% 영구 락)
                if highest_profit_pct >= 0.80:
                    pos["breakeven_locked"] = True

                tp_start = pos.get("tp_trigger_pct", 1.35)
                tp_base_floor = pos.get("tp_floor_lock", 1.00)

                trailing_active = False
                static_pullback = 0.22
                stage_floor_lock = 0.0

                if highest_profit_pct >= 5.0:
                    trailing_active = True
                    static_pullback = 0.50
                    stage_floor_lock = 4.20
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
                elif highest_profit_pct >= tp_start:
                    trailing_active = True
                    static_pullback = 0.22
                    stage_floor_lock = tp_base_floor

                pos["locked_floor_profit_pct"] = max(pos.get("locked_floor_profit_pct", 0.0), stage_floor_lock)
                save_json_file(PAPER_TRADES_FILE, paper_db)

                should_close = False
                close_reason = ""
                execution_exit_price = curr_p
                actual_pullback = round(highest_profit_pct - curr_profit_pct, 2)

                # ① 트레일링 익절
                if trailing_active:
                    static_pullback_price = pos["highest_price"] * (1.0 - (static_pullback / 100.0))
                    static_floor_price = entry_p * (1.0 + (pos["locked_floor_profit_pct"] / 100.0))
                    target_trigger_price = max(static_pullback_price, static_floor_price)

                    ob_exit = get_bithumb_orderbook_10(coin_code)
                    if ob_exit and ob_exit.get("max_bid_wall"):
                        wall_price = ob_exit["max_bid_wall"]["price"]
                        if (curr_p - wall_price) / curr_p <= 0.003:
                            tick_size = get_bithumb_tick_size(wall_price)
                            wall_2tick = round_to_bithumb_tick(wall_price + (2 * tick_size))
                            if wall_2tick > target_trigger_price and wall_2tick <= curr_p:
                                target_trigger_price = wall_2tick

                    if curr_p <= target_trigger_price or actual_pullback >= static_pullback or curr_profit_pct <= pos["locked_floor_profit_pct"]:
                        should_close = True
                        close_reason = f"📈 트레일링 익절 (고점 +{highest_profit_pct:.2f}% ➔ 하한선 락 {pos['locked_floor_profit_pct']:.2f}% 확보)"
                        if pos.get("is_paper", True):
                            tick_sz = get_bithumb_tick_size(target_trigger_price)
                            execution_exit_price = round_to_bithumb_tick(target_trigger_price - tick_sz)

                # ② 비상 하드 손절선 (-1.50% 캡)
                elif curr_profit_pct <= pos.get("emergency_sl_pct", -1.50):
                    should_close = True
                    close_reason = f"🚨 비상 하드 손절선 도달 ({curr_profit_pct:.2f}%) 즉시 탈출"
                    if pos.get("is_paper", True):
                        execution_exit_price = round_to_bithumb_tick(entry_p * (1.0 + (pos.get("emergency_sl_pct", -1.50) / 100.0)))

                # ③ 본절 락 (+0.80% 도달 후 -0.05% 복귀 시)
                elif pos.get("breakeven_locked", False) and curr_profit_pct <= -0.05:
                    should_close = True
                    close_reason = f"🛡️ 고점(+{highest_profit_pct:.2f}%) 달성 후 평단가 복귀 본절 락 탈출 (-0.05%)"
                    if pos.get("is_paper", True):
                        execution_exit_price = round_to_bithumb_tick(entry_p * 0.9995)

                # ④ 1차 손절선 도달 시 호가벽 연동 즉시 컷 (슬리피지 방어 핵심)
                elif curr_profit_pct <= pos["sl_pct"]:
                    ob_sl = get_bithumb_orderbook_10(coin_code)
                    bid_ratio = ob_sl.get("bid_ratio", 0.5) if ob_sl else 0.5

                    # 호가벽이 30% 미만으로 비어있으면 버퍼 없이 0초 즉시 탈출
                    if bid_ratio < 0.30:
                        should_close = True
                        close_reason = f"⚡ 스마트 손절 (호가 붕괴 감지 {bid_ratio*100:.1f}%, 0초 즉시 컷 {curr_profit_pct:.2f}%)"
                        if pos.get("is_paper", True):
                            execution_exit_price = curr_p
                    else:
                        if pos.get("sl_breach_start_time") is None:
                            pos["sl_breach_start_time"] = now
                            logging.info(f"⚠️ [{pos['symbol']}] 손절선({pos['sl_pct']}%) 터치 (매수벽 {bid_ratio*100:.1f}%) ➔ 2.5초 버퍼 대기")
                        elif now - pos["sl_breach_start_time"] >= 2.5:
                            should_close = True
                            close_reason = f"🛡️ 손절선({pos['sl_pct']}%) 2.5초 지속 이탈 ({curr_profit_pct:.2f}%)"
                            if pos.get("is_paper", True):
                                execution_exit_price = round_to_bithumb_tick(entry_p * (1.0 + (pos["sl_pct"] / 100.0)))
                else:
                    pos["sl_breach_start_time"] = None

                # ⑤ 전략별 횡보 타임아웃
                if not should_close and elapsed_seconds >= (pos.get("holding_timeout_mins", 10) * 60):
                    if highest_profit_pct >= 1.20:
                        pass
                    elif highest_profit_pct >= 0.75:
                        if elapsed_seconds >= 1800:
                            should_close = True
                            close_reason = f"⌛ 30분 만료 2차 분출 부재 안전 정리 ({curr_profit_pct:.2f}%)"
                    elif highest_profit_pct < 0.30:
                        should_close = True
                        close_reason = f"⌛ {pos.get('holding_timeout_mins', 10)}분 미반등 탄력 소멸 즉시 정리 ({curr_profit_pct:.2f}%)"

                if should_close:
                    sell_order_uuid = None
                    sell_fee_paid = 0.0
                    actual_sell_funds = 0.0

                    if not pos.get("is_paper", True):
                        succ, sell_uuid = execute_real_market_sell_order(coin_code, pos.get("units", 0.0))
                        if succ:
                            sell_order_uuid = sell_uuid
                            for _ in range(3):
                                time.sleep(0.5)
                                detail = get_bithumb_order_execution_detail(sell_uuid)
                                if detail and detail.get("executed_funds", 0.0) > 0:
                                    execution_exit_price = detail["avg_price"]
                                    actual_sell_funds = detail["executed_funds"]
                                    sell_fee_paid = detail["paid_fee"]
                                    break

                    buy_krw = pos.get("buy_amount_krw", MIN_BUY_KRW)
                    buy_fee = pos.get("buy_fee", 0.0)

                    if not pos.get("is_paper", True) and actual_sell_funds > 0:
                        total_fees = buy_fee + sell_fee_paid
                        profit_krw = round(actual_sell_funds - buy_krw - total_fees)
                        final_profit_pct = round((profit_krw / buy_krw) * 100.0, 2)
                    else:
                        final_profit_pct = round(((execution_exit_price - entry_p) / entry_p) * 100.0, 2)
                        profit_krw = round(buy_krw * (final_profit_pct / 100.0))

                    trajectory_summary = generate_trade_trajectory_summary(
                        pos, execution_exit_price, final_profit_pct, highest_profit_pct, elapsed_seconds, close_reason
                    )

                    closed_trades.append({
                        "symbol": pos["symbol"],
                        "track": pos.get("track", "A"),
                        "track_name": pos.get("track_name", "전략"),
                        "is_paper": pos.get("is_paper", True),
                        "slot": "SLOT_RANGE_ALPHA",
                        "entry_price": entry_p,
                        "exit_price": execution_exit_price,
                        "buy_amount_krw": buy_krw,
                        "profit_krw": profit_krw,
                        "profit_pct": final_profit_pct,
                        "buy_fee": buy_fee,
                        "sell_fee": sell_fee_paid,
                        "sell_order_uuid": sell_order_uuid,
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

                    sign_pct = "+" if final_profit_pct > 0 else ""
                    sign_krw = "+" if profit_krw > 0 else ""
                    icon = "🎉" if final_profit_pct > 0 else "🌧️"
                    mode_str = "모의투자" if pos.get("is_paper", True) else "실전매매"

                    fee_summary_str = f"\n• 수수료 정산: 총 {buy_fee + sell_fee_paid:,.1f}원 차감 반영" if (buy_fee + sell_fee_paid) > 0 else ""
                    exit_msg = f"""{icon} [청산 완료] - {mode_str} (Track {pos.get('track', 'A')} - {pos.get('track_name', '전략')})
• 종목 : {pos['symbol']}
• 진입가 : {entry_p:,.4f} KRW ➔ 청산가 : {execution_exit_price:,.4f} KRW
• 확정 손익 : {sign_krw}{profit_krw:,}원 ({sign_pct}{final_profit_pct:.2f}%){fee_summary_str}
• 사유 : {close_reason}

📊 흐름 요약:
{trajectory_summary}"""
                    send_telegram_msg(exit_msg)

                    portfolio_msg = format_portfolio_status_msg(active_positions, closed_trades)
                    send_telegram_msg(portfolio_msg)

            if len(active_positions) > 0 or len(target_queue) > 0:
                await asyncio.sleep(0.5)
            else:
                await asyncio.sleep(2.0)

        except Exception as e:
            logging.error(f"감시 루프 오류: {e}")
            await asyncio.sleep(2)

# ==========================================
# 8. 텔레그램 명령 리스너 (/track 명령어 탑재)
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

                    if text == "/track":
                        base_str = server_state.get('update_baseline_time', '')
                        track_stats = {'A': {'w':0,'l':0,'p':0}, 'B': {'w':0,'l':0,'p':0}, 'C': {'w':0,'l':0,'p':0}}
                        name_map = {'A': '바닥반등', 'B': '모멘텀돌파', 'C': '1차눌림목'}
                        
                        for t in closed_trades:
                            if t.get("is_paper", True) != PAPER_TRADING:
                                continue
                            exit_t = t.get('exit_time', '')
                            if base_str and exit_t < base_str:
                                continue
                            trk = t.get('track', 'A')
                            if trk in track_stats:
                                if t.get('profit_pct', 0.0) > 0:
                                    track_stats[trk]['w'] += 1
                                else:
                                    track_stats[trk]['l'] += 1
                                track_stats[trk]['p'] += t.get('profit_krw', 0)

                        lines = []
                        for trk in ['A', 'B', 'C']:
                            s = track_stats[trk]
                            tot = s['w'] + s['l']
                            rate = (s['w'] / tot * 100) if tot > 0 else 0.0
                            lines.append(f"• [{trk}:{name_map[trk]}] {tot}건 ({s['w']}승 {s['l']}패 | {rate:.1f}% | {s['p']:+,}원)")

                        mode_label = "🧪 모의투자" if PAPER_TRADING else "🔥 실전매매"
                        send_telegram_msg(f"📊 [3-Track 전략별 성과 리포트] - {mode_label}\n" + "\n".join(lines))

                    elif text == "/reset_stats":
                        now_kst_iso = get_kst_now().isoformat()
                        UPDATE_BASELINE_TIME = now_kst_iso
                        server_state["update_baseline_time"] = now_kst_iso
                        save_json_file(STATE_FILE, server_state)
                        send_telegram_msg(f"⏱️ [성과 기준 시각 초기화]\n지금 시각({get_kst_now().strftime('%m/%d %H:%M KST')}) 이후 매매부터 새롭게 집계됩니다.")

                    elif text == "/real":
                        if not PAPER_TRADING and not PENDING_PAPER_DRAIN:
                            send_telegram_msg("ℹ️ 이미 실전매매 모드입니다.")
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
                                "track": pos.get("track", "A"),
                                "track_name": pos.get("track_name", "전략"),
                                "is_paper": pos.get("is_paper", True),
                                "slot": "SLOT_RANGE_ALPHA",
                                "entry_price": pos["entry_price"],
                                "exit_price": curr_p,
                                "buy_amount_krw": buy_krw,
                                "profit_krw": profit_krw,
                                "profit_pct": profit_pct,
                                "reason": "실전 모드 전환 가상 포지션 정리",
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
                            f"🔥 [모드 전환: 실전매매 가동]\n"
                            f"• 가상 포지션({cleared_count}개) 정리 완료\n"
                            f"• 빗썸 계좌 총 자산: {tot_str}\n"
                            f"• 빗썸 가용 잔고: {avail_str}\n"
                            f"• 3-Track 알파 엔진 실전 주문이 거래소에 직접 접수됩니다."
                        )

                    elif text == "/paper":
                        if PAPER_TRADING:
                            send_telegram_msg("ℹ️ 이미 모의투자 모드입니다.")
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
                            held_names = [f"{v['symbol']}[{v.get('track', 'A')}]" for v in real_positions]
                            send_telegram_msg(
                                f"⏳ [모의투자 전환 대기]\n"
                                f"• 신규 실전 주문 차단\n"
                                f"• 보유 실전 종목({', '.join(held_names)}) 청산 후 모의투자로 자동 전환됩니다."
                            )
                        else:
                            PAPER_TRADING = True
                            PENDING_PAPER_DRAIN = False
                            server_state["paper_trading"] = True
                            server_state["pending_paper_drain"] = False
                            save_json_file(STATE_FILE, server_state)
                            send_telegram_msg("🧪 [모드 전환 완료] 모의투자(PAPER) 모드로 전환되었습니다.")

                    elif text == "/status":
                        mode_tag = "🧪 모의투자" if PAPER_TRADING else "🔥 실전매매"
                        mode_active = {k: v for k, v in active_positions.items() if v.get("is_paper", True) == PAPER_TRADING}
                        held = [f"{v['symbol']}[{v.get('track', 'A')}]" for v in mode_active.values()]
                        
                        queue_items = []
                        for code, plan in server_state.get('target_queue', {}).items():
                            sym = plan.get('symbol', f"{code}/KRW")
                            track = plan.get('track', 'A')
                            score = plan.get('score', 0)
                            status_label = "주문중" if plan.get('order_placed') else "대기중"
                            queue_items.append(f"{sym}[{track}]({score}점|{status_label})")
                        
                        if EMERGENCY_STOP:
                            status_str = "🛑 일시정지 (STOP)"
                        elif CIRCUIT_BREAKER_ACTIVE:
                            status_str = "🚨 서킷브레이커 발동 중"
                        elif PENDING_PAPER_DRAIN:
                            status_str = "⏳ 모의투자 전환 대기 중"
                        else:
                            status_str = "🟢 WebSocket 3-Track 엔진 가동 중 (RUNNING)"

                        mode_closed = [t for t in closed_trades if t.get("is_paper", True) == PAPER_TRADING]

                        res_msg = f"""📊 [시스템 상태 보고]
• 모드: {mode_tag}
• 상태: {status_str}
• 대기열: {', '.join(queue_items) if queue_items else '(대기열 없음)'}
• 보유 종목: {', '.join(held) if held else '(없음)'} ({len(held)}/2개)
• 완료 거래: {len(mode_closed)}건
• 최근 관망 사유: {LAST_NO_CANDIDATE_REASON}"""
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
                        send_telegram_msg("🛑 [인터락 작동] 감시 중단 및 대기열 전량 취소.")

                    elif text == "/start":
                        EMERGENCY_STOP = False
                        CIRCUIT_BREAKER_ACTIVE = False
                        send_telegram_msg("▶️ [인터락 해제] 3-Track 엔진 감시 재개.")

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
