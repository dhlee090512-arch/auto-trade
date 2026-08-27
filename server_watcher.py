import os
import sys

# ==========================================
# [중요] 시스템 프록시 환경변수 원천 무효화 (402 에러 차단)
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
from datetime import datetime, timedelta, timezone
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

# ==========================================
# 0. 전역 설정 및 환경 변수
# ==========================================
PAPER_TRADING = True             # 🧪 True: 모의투자 / False: 빗썸 실전매매
MIN_CONFIDENCE_SCORE = 65        # 🎯 최소 신뢰도
MAX_HOLDING_COINS = 3            # 🛡️ 최대 보유 가능 종목 수
MIN_BUY_KRW = 6000               # 💵 최소 매수 금액 (원)
BUY_RATIO = 0.20                 # 📊 가용 잔고 대비 1회 투입 비중 (20%)
ENTRY_TIMEOUT_MINUTES = 20       # ⏰ 진입 대기 만료 시간

# ⏰ 청산 및 추세 추종 파라미터
EARLY_EXIT_MINUTES = 25          # ⏰ 모멘텀 소멸 조기 탈출 (마이너스 시)
MAX_HOLD_MINUTES = 60            # ⏰ 비추세/횡보 종목 최대 보유 제한
BREAK_EVEN_TRIGGER_PCT = 0.7     # 🛡️ 본절 방어선 발동 기준 (+0.7%)
TRAILING_START_PCT = 1.2         # 📈 트레일링 스탑 활성화 기준 (+1.2%)
TRAILING_GAP_PCT = 0.4           # 📈 고점 대비 하락 반락 허용폭 (-0.4%)

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

STABLE_COINS = {"USDT", "USDC", "DAI", "TUSD", "FDUSD", "USDD", "BUSD"}

STATE_FILE = "server_state.json"
PAPER_TRADES_FILE = "paper_trades.json"
TARGETS_FILE = "targets.json"
PROJECT_DIR = "/home/ubuntu/auto-trade"

EMERGENCY_STOP = False
LAST_TELEGRAM_UPDATE_ID = 0

# 🌟 이번 세션(코드 실행/업데이트 시점) 기준 추적 변수
SESSION_START_TIME = datetime.now(timezone(timedelta(hours=9)))
BTC_DEFENSIVE_MODE = False  # BTC 하락 방어 모드 상태 플래그

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler("watcher.log"),
        logging.StreamHandler(sys.stdout)
    ]
)

# ==========================================
# 1. 텔레그램 및 KST 시간 유틸리티
# ==========================================
def get_kst_now():
    """한국 표준시 (KST, UTC+9) datetime 반환"""
    return datetime.now(timezone(timedelta(hours=9)))

def send_telegram_msg(msg: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg}
    try:
        requests.post(url, json=payload, timeout=6)
    except Exception as e:
        logging.error(f"텔레그램 발송 오류: {e}")

def format_portfolio_status_msg(active_positions, closed_trades):
    """💼 [현재 매매 상황] (현재 가동 세션 누적 손익 포함)"""
    held_symbols = [v['symbol'] for v in active_positions.values()]
    if held_symbols:
        held_str = f"{', '.join(held_symbols)} ({len(held_symbols)}개 보유 중)"
    else:
        held_str = "(현재 보유 종목 없음)"

    recent_10 = closed_trades[-10:] if closed_trades else []
    
    # 1. 최근 10건 통계
    if not recent_10:
        trades_str = "• 매도 이력이 없습니다."
        win_rate = 0
        r10_profit_krw = 0
    else:
        trade_lines = []
        wins = 0
        r10_profit_krw = 0
        for idx, t in enumerate(recent_10, 1):
            p_pct = t.get('profit_pct', 0.0)
            p_krw = t.get('profit_krw', 0)
            symbol = t.get('symbol', 'UNKNOWN')
            exit_time_str = t.get('exit_time', '')
            try:
                dt_obj = datetime.fromisoformat(exit_time_str)
                time_display = dt_obj.strftime("%m/%d %H:%M KST")
            except Exception:
                time_display = "-"
                
            sign_pct = "+" if p_pct > 0 else ""
            trade_lines.append(f"{idx}. {symbol} ({sign_pct}{p_pct:.1f}%) {time_display}")
            if p_pct > 0:
                wins += 1
            r10_profit_krw += p_krw
            
        trades_str = "\n".join(trade_lines)
        win_rate = round((wins / len(recent_10)) * 100)

    # 2. 🌟 마지막 코드 실행/업데이트 시점 이후 누적 손익 계산
    session_trades = []
    session_profit_krw = 0
    session_wins = 0
    for t in closed_trades:
        exit_time_str = t.get('exit_time', '')
        try:
            exit_dt = datetime.fromisoformat(exit_time_str)
            if exit_dt >= SESSION_START_TIME:
                session_trades.append(t)
                session_profit_krw += t.get('profit_krw', 0)
                if t.get('profit_pct', 0.0) > 0:
                    session_wins += 1
        except Exception:
            pass

    session_start_str = SESSION_START_TIME.strftime("%m/%d %H:%M")
    session_sign_krw = "+" if session_profit_krw > 0 else ""
    session_count = len(session_trades)
    session_win_rate = round((session_wins / session_count) * 100) if session_count > 0 else 0
    r10_sign_krw = "+" if r10_profit_krw > 0 else ""

    return f"""💼 [현재 매매 상황]
• 보유 종목 : {held_str}

📜 [최근 매도 이력]
{trades_str}

📊 최근 10건 승률 : {win_rate}%
💰 최근 10건 실현 손익 : {r10_sign_krw}{r10_profit_krw:,} KRW
---------------------------------
🚀 [현 세션 누적 성과] ({session_start_str} 가동 이후)
• 체결 완료 : {session_count}건 (승률 {session_win_rate}%)
• 실현 손익 : {session_sign_krw}{session_profit_krw:,} KRW"""

# ==========================================
# 2. GitHub 백업 & 파일 I/O
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

def sync_file_to_github(file_path, content_data):
    if not GH_TOKEN:
        return
    url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/contents/{file_path}"
    headers = {"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    sha = None
    try:
        res = requests.get(url, headers=headers, timeout=5)
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
        requests.put(url, headers=headers, json=payload, timeout=8)
    except Exception as e:
        logging.error(f"GitHub 동기화 실패 ({file_path}): {e}")

# ==========================================
# 3. 빗썸 API & 퀀트 피처
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

def get_real_krw_balance():
    if BITHUMB_API_KEY and BITHUMB_SECRET_KEY:
        try:
            url = "https://api.bithumb.com/v1/accounts"
            headers = get_bithumb_jwt_headers()
            res = requests.get(url, headers=headers, timeout=5).json()
            if isinstance(res, list):
                for acc in res:
                    if acc.get("currency") == "KRW":
                        bal = float(acc.get("balance", 0.0))
                        return bal
        except Exception as e:
            logging.error(f"빗썸 잔고 조회 통신 실패: {e}")
    return 100000.0

def execute_real_market_order(coin_code: str, side: str, amount_or_units: float):
    if PAPER_TRADING:
        return True, "모의투자 체결"
    try:
        url = "https://api.bithumb.com/v1/orders"
        market = f"KRW-{coin_code.upper()}"
        if side == "bid":
            body = {"market": market, "side": "bid", "price": str(amount_or_units), "ord_type": "price"}
        else:
            body = {"market": market, "side": "ask", "volume": str(amount_or_units), "ord_type": "market"}

        headers = get_bithumb_jwt_headers(body)
        res = requests.post(url, json=body, headers=headers, timeout=5).json()
        if "uuid" in res:
            return True, res["uuid"]
        return False, str(res)
    except Exception as e:
        return False, str(e)

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

def get_current_price(coin_code: str) -> float:
    try:
        url = f"https://api.bithumb.com/public/ticker/{coin_code}_KRW"
        res = requests.get(url, timeout=3).json()
        if res.get("status") == "0000":
            return float(res["data"]["closing_price"])
    except Exception:
        pass
    return None

def get_candles(coin_code, interval="5m", limit=50):
    try:
        url = f"https://api.bithumb.com/public/candlestick/{coin_code}_KRW/{interval}"
        res = requests.get(url, timeout=5).json()
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

def calculate_rsi(closes, period=14):
    if len(closes) < period + 1: return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i-1]
        if delta >= 0:
            gains.append(delta)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(abs(delta))
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

def calculate_ema(closes, period=20):
    if len(closes) < period: return closes[-1] if closes else 0.0
    k = 2.0 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = (price * k) + (ema * (1 - k))
    return round(ema, 4)

def calculate_quant_features(candles):
    if not candles: return {}
    closes = [c['close'] for c in candles]
    volumes = [c['volume'] for c in candles]
    
    cum_pv = sum(c['close'] * c['volume'] for c in candles)
    cum_vol = sum(volumes)
    vwap = (cum_pv / cum_vol) if cum_vol > 0 else closes[-1]
    vwap_gap_pct = round(((closes[-1] - vwap) / vwap) * 100, 2)

    avg_vol_20 = sum(volumes[-21:-1]) / 20 if len(volumes) >= 21 else (sum(volumes[:-1]) / max(len(volumes)-1, 1))
    surge_ratio = round(volumes[-1] / avg_vol_20, 2) if avg_vol_20 > 0 else 1.0

    rsi = calculate_rsi(closes, period=14)
    atr = calculate_atr(candles, period=14)
    atr_pct = round((atr / closes[-1]) * 100, 2) if closes[-1] > 0 else 0.0

    # 최근 3캔들(15분) 수익률
    ret_15m = round(((closes[-1] - closes[-4]) / closes[-4]) * 100, 2) if len(closes) >= 4 else 0.0

    tick_size = get_bithumb_tick_size(closes[-1])
    tick_ratio_pct = round((tick_size / closes[-1]) * 100, 3)

    return {
        "current_price": closes[-1],
        "vwap": round(vwap, 4),
        "vwap_gap_pct": vwap_gap_pct,
        "volume_surge_ratio": surge_ratio,
        "rsi_14": rsi,
        "atr_14": atr,
        "atr_pct": atr_pct,
        "ret_15m": ret_15m,
        "tick_ratio_pct": tick_ratio_pct
    }

# ==========================================
# 4. BTC 매크로 국면 판별 및 상태 알림
# ==========================================
def check_btc_regime():
    """
    ⚡ BTC 1시간봉/5분봉 종합 국면 판별
    반환값: (방어모드 여부: bool, 사유: str, btc_ret_15m: float)
    """
    btc_ret_15m = 0.0
    try:
        candles_1h = get_candles("BTC", interval="1h", limit=30)
        candles_5m = get_candles("BTC", interval="5m", limit=20)

        if len(candles_5m) >= 4:
            c5 = [c['close'] for c in candles_5m]
            btc_ret_15m = ((c5[-1] - c5[-4]) / c5[-4]) * 100.0
            rsi_5m = calculate_rsi(c5, period=14)

            # 1. 5분봉 급락 서킷 브레이커
            if btc_ret_15m <= -0.8:
                return True, f"비트코인 15분 급락 ({btc_ret_15m:.2f}%)", btc_ret_15m
            if rsi_5m < 35.0:
                return True, f"비트코인 단기 과매도 침체 (RSI: {rsi_5m:.1f})", btc_ret_15m

        if len(candles_1h) >= 20:
            c1h = [c['close'] for c in candles_1h]
            ema20_1h = calculate_ema(c1h, period=20)
            curr_btc = c1h[-1]

            # 2. 1시간봉 20 EMA 하회 및 역추세
            if curr_btc < ema20_1h * 0.995:
                gap_pct = ((curr_btc - ema20_1h) / ema20_1h) * 100.0
                return True, f"비트코인 1시간 추세 이탈 (20 EMA 대비 {gap_pct:.2f}%)", btc_ret_15m

    except Exception:
        pass

    return False, "BTC 상태 정상", btc_ret_15m

def calculate_quant_score(q_1h, q_5m, btc_ret_15m) -> int:
    """🎯 퀀트 스코어링 (상대강도 RS 가산점 포함, 0~100점)"""
    score = 0
    # 1. 1시간봉 VWAP 지지 (+25)
    if q_1h.get("vwap_gap_pct", -99) >= -0.8: score += 25

    # 2. 5분봉 거래량 급증 (+25)
    surge = q_5m.get("volume_surge_ratio", 0)
    if surge >= 1.4: score += 25
    elif surge >= 1.1: score += 15

    # 3. 비트코인 대비 상대강도 RS (+20)
    alt_ret = q_5m.get("ret_15m", 0.0)
    if alt_ret > btc_ret_15m + 0.3:
        score += 20
    elif alt_ret > btc_ret_15m:
        score += 10

    # 4. 5분봉 VWAP 지지 (+15)
    if q_5m.get("vwap_gap_pct", -99) >= -0.2: score += 15

    # 5. 5분봉 RSI 적정권 (+15)
    rsi = q_5m.get("rsi_14", 50)
    if 40 <= rsi <= 65: score += 15
    elif 35 <= rsi < 40 or 65 < rsi <= 70: score += 8

    return score

# ==========================================
# 5. AI 호출 및 타점 수립
# ==========================================
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
    cleaned = raw_text.strip()
    if "```json" in cleaned:
        cleaned = cleaned.split("```json")[1].split("```")[0].strip()
    elif "```" in cleaned:
        cleaned = cleaned.split("```")[1].split("```")[0].strip()

    try:
        return json.loads(cleaned)
    except Exception:
        pass

    res = {}
    try:
        sym_m = re.search(r'["\']selected_symbol["\']\s*:\s*["\']([^"\']+)["\']', raw_text)
        if sym_m: res["selected_symbol"] = sym_m.group(1)
        conf_m = re.search(r'["\']confidence_score["\']\s*:\s*([0-9]+)', raw_text)
        if conf_m: res["confidence_score"] = int(conf_m.group(1))
        disc_m = re.search(r'["\']entry_discount_pct["\']\s*:\s*([0-9.]+)', raw_text)
        if disc_m: res["entry_discount_pct"] = float(disc_m.group(1))
        sl_m = re.search(r'["\']stop_loss_pct["\']\s*:\s*(-?[0-9.]+)', raw_text)
        if sl_m: res["stop_loss_pct"] = float(sl_m.group(1))
        tp_m = re.search(r'["\']take_profit_pct["\']\s*:\s*([0-9.]+)', raw_text)
        if tp_m: res["take_profit_pct"] = float(tp_m.group(1))
        reason_m = re.search(r'["\']detailed_reason["\']\s*:\s*["\']([^"\']+)["\']', raw_text)
        if reason_m: res["detailed_reason"] = reason_m.group(1)
        if res: return res
    except Exception:
        pass
    return None

def execute_server_side_strategy():
    """서버 내장 퀀트+AI 타점 엔진 (상대강도 RS + BTC 국면 연동)"""
    global BTC_DEFENSIVE_MODE
    paper_db = load_json_file(PAPER_TRADES_FILE, {"active_positions": {}, "closed_trades": []})
    active_positions = paper_db.get("active_positions", {})
    
    if len(active_positions) >= MAX_HOLDING_COINS:
        logging.info("💼 최대 보유 종목(3개) 도달로 신규 AI 분석 스킵")
        return

    # 1. ⚡ BTC 시장 국면 체크 및 상태 전환 알림
    btc_is_defensive, btc_reason, btc_ret_15m = check_btc_regime()

    if btc_is_defensive:
        if not BTC_DEFENSIVE_MODE:
            BTC_DEFENSIVE_MODE = True
            msg = f"""🛑 [시장 방어 모드 가동]
• 사유 : {btc_reason}
• 조치 : 신규 알트코인 매수 탐색 일시정지 (현금 100% 방어)"""
            send_telegram_msg(msg)
            logging.warning(msg)
        return
    else:
        if BTC_DEFENSIVE_MODE:
            BTC_DEFENSIVE_MODE = False
            msg = "▶️ [시장 정상 회복]\n• 비트코인 추세가 안정권에 진입하여 신규 매수 탐색을 정상 재개합니다."
            send_telegram_msg(msg)
            logging.info(msg)

    held_codes = set(active_positions.keys())
    logging.info("🧠 [서버 퀀트+RS 스코어링 분석 시작 (모수 30개)]")
    
    url = "https://api.bithumb.com/public/ticker/ALL_KRW"
    try:
        res = requests.get(url, timeout=8).json()
    except Exception as e:
        logging.error(f"빗썸 전체 시세 조회 실패: {e}")
        return

    if res.get("status") != "0000": return

    raw_list = []
    for sym, info in res["data"].items():
        if sym == "date" or sym.upper() in STABLE_COINS: continue
        if sym in held_codes: continue
        try:
            raw_list.append((sym, float(info['closing_price']), float(info['fluctate_rate_24H']), float(info['acc_trade_value_24H'])))
        except Exception: pass

    sorted_list = sorted(raw_list, key=lambda x: x[3], reverse=True)[:30]
    scored_candidates = []

    for sym, price, change, _ in sorted_list:
        candles_1h = get_candles(sym, interval="1h", limit=24)
        candles_5m = get_candles(sym, interval="5m", limit=30)
        if len(candles_1h) < 15 or len(candles_5m) < 20: continue

        q_1h = calculate_quant_features(candles_1h)
        q_5m = calculate_quant_features(candles_5m)

        if q_5m.get("tick_ratio_pct", 0) > 0.35: continue

        score = calculate_quant_score(q_1h, q_5m, btc_ret_15m)
        scored_candidates.append({
            "symbol": f"{sym}/KRW",
            "code": sym,
            "price": price,
            "change_24h": change,
            "score": score,
            "quant_5m": q_5m,
            "candles_5m": [{"c": c['close'], "h": c['high'], "l": c['low'], "v": c['volume']} for c in candles_5m[-25:]]
        })

    if not scored_candidates:
        logging.info("⏸️ 유효 후보군이 없어 관망합니다.")
        return

    top3_scored = sorted(scored_candidates, key=lambda x: x['score'], reverse=True)[:3]
    top3_labels = [c['symbol'] + '(' + str(c['score']) + '점)' for c in top3_scored]
    logging.info(f"🎯 [퀀트+RS 상위 3개 선별]: {top3_labels}")

    # 2차 5분봉 AI 정밀 분석
    cand_5m_data = [{
        "symbol": c["symbol"],
        "quant_5m": c["quant_5m"],
        "candles_5m": c["candles_5m"]
    } for c in top3_scored]

    sys_2 = (
        "You are an intraday scalper. Analyze 5m 25-candle series to pick 1 trade plan or NONE.\n"
        "Rules:\n"
        "1. Entry: Set realistic shallow entry (entry_discount_pct: 0.0% to 0.15%) near market price.\n"
        "2. Anti-Chasing: Reject coins with 5m RSI > 70.\n"
        "3. Scalping Targets: take_profit_pct: +1.2% to +2.5%, stop_loss_pct: -0.8% to -1.3%.\n"
        "4. detailed_reason: Detailed Korean technical explanation.\n"
        "Output JSON ONLY."
    )
    user_2 = f"5m Series:\n{json.dumps(cand_5m_data, ensure_ascii=False)}\n\nSchema: {{\"selected_symbol\": \"BTC/KRW\", \"confidence_score\": 75, \"entry_discount_pct\": 0.1, \"stop_loss_pct\": -1.0, \"take_profit_pct\": 1.8, \"detailed_reason\": \"근거\"}}"
    res_2 = clean_and_parse_json(call_ai_api(sys_2, user_2))
    if not res_2: return

    selected = res_2.get("selected_symbol", "NONE")
    confidence = res_2.get("confidence_score", 0)
    if selected.upper() == "NONE" or confidence < MIN_CONFIDENCE_SCORE:
        logging.info(f"⏸️ 2차 분석 결과 신뢰도({confidence}점) 미달로 관망")
        return

    code = selected.split('/')[0]
    if code in held_codes: return

    curr_p = get_current_price(code)
    if not curr_p: return

    target_cand = next((c for c in top3_scored if c["code"] == code), None)
    atr_pct = target_cand["quant_5m"].get("atr_pct", 0.8) if target_cand else 0.8

    base_sl = float(res_2.get("stop_loss_pct", -1.0))
    base_tp = float(res_2.get("take_profit_pct", 1.8))
    
    sl_pct = max(min(round(min(base_sl, -atr_pct * 1.1), 2), -0.8), -1.5)
    tp_pct = min(max(round(max(base_tp, atr_pct * 1.8), 2), 1.2), 2.8)

    available_krw = get_real_krw_balance()
    calculated_buy_krw = max(round(available_krw * BUY_RATIO), MIN_BUY_KRW)

    discount = float(res_2.get("entry_discount_pct", 0.1))
    target_entry = curr_p * (1.0 - (discount / 100.0))

    now_iso = get_kst_now().isoformat()
    plan_data = {
        "symbol": selected,
        "current_price": curr_p,
        "target_entry": target_entry,
        "sl_pct": sl_pct,
        "tp_pct": tp_pct,
        "buy_amount_krw": calculated_buy_krw,
        "detailed_reason": res_2.get("detailed_reason", "5분봉 패턴 및 상대강도(RS) 분석"),
        "created_at": now_iso
    }

    server_state = load_json_file(STATE_FILE, {"pending_targets": {}, "last_updated": ""})
    server_state["pending_targets"] = {code: plan_data}
    server_state["last_updated"] = now_iso
    save_json_file(STATE_FILE, server_state)

    targets_payload = {"updated_at": now_iso, "paper_trading": PAPER_TRADING, "targets": {code: plan_data}}
    save_json_file(TARGETS_FILE, targets_payload)
    sync_file_to_github(TARGETS_FILE, targets_payload)

    tp_sign = "+" if tp_pct > 0 else ""
    plan_msg = f"""🎯 [매수 타점 선정 - 서버]
• 종목 : {selected} (신뢰도: {confidence}점)
• 현재가 : {curr_p:,.2f} KRW
• 진입 대기가 : {target_entry:,.2f} KRW (-{discount}%)

🎯 익절 목표 : {tp_sign}{tp_pct}% (ATR 가변)
🛡️ 손절 기준 : {sl_pct}% (ATR 가변)

💡 매수 근거 :
{plan_data['detailed_reason']}
=================================
⚡ 규칙: 20분 미체결 취소 / 트레일링 스탑 & 조건부 시간 손절 가동"""
    send_telegram_msg(plan_msg)

    # 💼 독립 메시지 전송
    portfolio_msg = format_portfolio_status_msg(paper_db.get("active_positions", {}), paper_db.get("closed_trades", []))
    send_telegram_msg(portfolio_msg)

# ==========================================
# 6. 실시간 감시 & 트레일링 스탑 & 조건부 시간 청산
# ==========================================
async def realtime_execution_engine():
    global EMERGENCY_STOP
    logging.info("⚡ 실시간 감시 & 조건부 트레일링 엔진 구동 시작")
    last_strategy_run = 0

    t = threading.Thread(target=telegram_listener_thread, daemon=True)
    t.start()

    while True:
        try:
            now = time.time()
            now_dt = get_kst_now()

            # 5분마다 AI 퀀트 분석 실행
            if now - last_strategy_run >= 300:
                last_strategy_run = now
                asyncio.create_task(asyncio.to_thread(execute_server_side_strategy))

            server_state = load_json_file(STATE_FILE, {"pending_targets": {}, "last_updated": ""})
            paper_db = load_json_file(PAPER_TRADES_FILE, {"active_positions": {}, "closed_trades": []})

            pending = server_state.get("pending_targets", {})
            active_positions = paper_db.get("active_positions", {})
            closed_trades = paper_db.get("closed_trades", [])

            # [1] 진입 대기 감시
            if not EMERGENCY_STOP and len(active_positions) < MAX_HOLDING_COINS:
                for coin_code, plan in list(pending.items()):
                    created_dt = datetime.fromisoformat(plan.get("created_at", now_dt.isoformat()))
                    if now_dt - created_dt >= timedelta(minutes=ENTRY_TIMEOUT_MINUTES):
                        logging.info(f"⌛ [{plan['symbol']}] 20분 내 미체결로 자동 취소")
                        del pending[coin_code]
                        save_json_file(STATE_FILE, server_state)
                        continue

                    curr_p = get_current_price(coin_code)
                    if curr_p and curr_p <= plan["target_entry"]:
                        if not PAPER_TRADING:
                            success, order_res = execute_real_market_order(coin_code, "bid", plan["buy_amount_krw"])
                            if not success:
                                logging.error(f"실전 매수 주문 실패: {order_res}")
                                continue

                        logging.info(f"🎯 [{plan['symbol']}] 진입 체결 완료")
                        exact_sl = curr_p * (1.0 + (plan["sl_pct"] / 100.0))
                        exact_tp = curr_p * (1.0 + (plan["tp_pct"] / 100.0))
                        units = plan["buy_amount_krw"] / curr_p

                        active_positions[coin_code] = {
                            "symbol": plan["symbol"],
                            "entry_price": curr_p,
                            "highest_price": curr_p,
                            "stop_loss": exact_sl,
                            "take_profit": exact_tp,
                            "buy_amount_krw": plan["buy_amount_krw"],
                            "units": units,
                            "sl_pct": plan["sl_pct"],
                            "tp_pct": plan["tp_pct"],
                            "detailed_reason": plan["detailed_reason"],
                            "entry_time": now_dt.isoformat(),
                            "break_even_triggered": False
                        }
                        del pending[coin_code]
                        paper_db["active_positions"] = active_positions
                        save_json_file(PAPER_TRADES_FILE, paper_db)
                        save_json_file(STATE_FILE, server_state)
                        sync_file_to_github(PAPER_TRADES_FILE, paper_db)

                        tp_sign = "+" if plan['tp_pct'] > 0 else ""
                        buy_msg = f"""⚡ [체결 완료] - {'모의투자' if PAPER_TRADING else '실전매매'}
• 종목 : {plan['symbol']}
• 진입 체결가 : {curr_p:,.2f} KRW

🎯 익절 목표 : {exact_tp:,.2f} KRW ({tp_sign}{plan['tp_pct']}%)
🛡️ 손절 목표 : {exact_sl:,.2f} KRW ({plan['sl_pct']}%)
💰 투입 금액 : {plan['buy_amount_krw']:,} KRW"""
                        send_telegram_msg(buy_msg)

            # [2] 보유 포지션 감시 (트레일링 스탑 & 조건부 시간 손절)
            for coin_code, pos in list(active_positions.items()):
                curr_p = get_current_price(coin_code)
                if not curr_p: continue

                entry_p = pos["entry_price"]
                entry_time = datetime.fromisoformat(pos["entry_time"])
                curr_profit_pct = ((curr_p - entry_p) / entry_p) * 100.0

                if curr_p > pos.get("highest_price", entry_p):
                    pos["highest_price"] = curr_p

                highest_profit_pct = ((pos["highest_price"] - entry_p) / entry_p) * 100.0

                # 본절 방어선 (+0.7% 도달 시 가동)
                if not pos.get("break_even_triggered", False) and curr_profit_pct >= BREAK_EVEN_TRIGGER_PCT:
                    pos["stop_loss"] = max(pos["stop_loss"], entry_p * 1.001)
                    pos["break_even_triggered"] = True
                    logging.info(f"🛡️ [{pos['symbol']}] +0.7% 도달로 본절 방어선 세팅")

                status = "HOLDING"
                exit_reason = ""

                # ① 고정 익절가 도달
                if curr_p >= pos["take_profit"]:
                    status = "CLOSED_TAKE_PROFIT"
                    exit_reason = "🎯 익절 목표가 도달"

                # ② 트레일링 스탑 (최고점 +1.2% 이상 후 0.4% 반락 시)
                elif highest_profit_pct >= TRAILING_START_PCT and (highest_profit_pct - curr_profit_pct) >= TRAILING_GAP_PCT:
                    status = "CLOSED_TRAILING_STOP"
                    exit_reason = f"📈 트레일링 스탑 (최고 +{highest_profit_pct:.1f}% 달성 후 이익 보존)"

                # ③ 손절 / 본절 방어선 도달
                elif curr_p <= pos["stop_loss"]:
                    status = "CLOSED_STOP_LOSS"
                    exit_reason = "🛡️ 본절 방어선 또는 손절가 도달"

                # ④ 25분 모멘텀 소멸 조기 탈출 (마이너스 수익률일 때만 정리)
                elif (now_dt - entry_time) >= timedelta(minutes=EARLY_EXIT_MINUTES) and curr_profit_pct <= 0.0:
                    status = "CLOSED_EARLY_EXIT"
                    exit_reason = f"⌛ {EARLY_EXIT_MINUTES}분간 모멘텀 소멸로 조기 탈출 (기회비용 확보)"

                # ⑤ 60분 횡보 청산 (상승 추세가 아닌 횡보/약세일 때만 정리)
                elif (now_dt - entry_time) >= timedelta(minutes=MAX_HOLD_MINUTES) and curr_profit_pct < BREAK_EVEN_TRIGGER_PCT:
                    status = "CLOSED_TIME_EXIT"
                    exit_reason = f"⏰ {MAX_HOLD_MINUTES}분 횡보 박스권 정체로 시장가 청산"

                if status != "HOLDING":
                    if not PAPER_TRADING:
                        execute_real_market_order(coin_code, "ask", pos.get("units", 0.0))

                    profit_pct = round(((curr_p - entry_p) / entry_p) * 100, 2)
                    profit_krw = round(pos["buy_amount_krw"] * (profit_pct / 100.0))

                    closed_trades.append({
                        "symbol": pos["symbol"],
                        "entry_price": entry_p,
                        "exit_price": curr_p,
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
                    sync_file_to_github(PAPER_TRADES_FILE, paper_db)

                    sign_pct = "+" if profit_pct > 0 else ""
                    sign_krw = "+" if profit_krw > 0 else ""
                    profit_icon = "🎉" if profit_pct > 0 else "🌧️"

                    exit_msg = f"""{profit_icon} [청산 완료] - {'모의투자' if PAPER_TRADING else '실전매매'}
• 종목 : {pos['symbol']}
• 진입가 : {entry_p:,.2f} KRW ➔ 청산가 : {curr_p:,.2f} KRW
• 실현 손익 : {sign_krw}{profit_krw:,} KRW ({sign_pct}{profit_pct:.1f}%)

📋 청산 사유 : {exit_reason}"""
                    send_telegram_msg(exit_msg)

            await asyncio.sleep(2)
        except Exception as e:
            logging.error(f"감시 루프 오류: {e}")
            await asyncio.sleep(3)

# ==========================================
# 7. 텔레그램 리스너 (/log, /status 등)
# ==========================================
def telegram_listener_thread():
    global EMERGENCY_STOP, LAST_TELEGRAM_UPDATE_ID
    if not TELEGRAM_BOT_TOKEN: return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"

    try:
        init_res = requests.get(url, params={"timeout": 1}, timeout=5).json()
        if init_res.get("ok") and init_res.get("result"):
            LAST_TELEGRAM_UPDATE_ID = init_res["result"][-1]["update_id"]
            requests.get(url, params={"offset": LAST_TELEGRAM_UPDATE_ID + 1, "timeout": 1}, timeout=5)
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
                        continue

                    if text == "/status":
                        server_state = load_json_file(STATE_FILE, {"pending_targets": {}, "last_updated": "-"})
                        paper_db = load_json_file(PAPER_TRADES_FILE, {"active_positions": {}, "closed_trades": []})
                        held = [v['symbol'] for v in paper_db.get('active_positions', {}).values()]
                        pending = list(server_state.get('pending_targets', {}).keys())
                        status_str = "🛑 일시정지 (STOP)" if EMERGENCY_STOP else "🟢 실시간 감시 중 (RUNNING)"
                        regime_str = "🛡️ BTC 방어 모드" if BTC_DEFENSIVE_MODE else "🟢 시장 정상"

                        res_msg = f"""📊 [시스템 상태 보고]
• 모드: {'🧪 모의투자' if PAPER_TRADING else '🔥 실전매매'}
• 인터락: {status_str} ({regime_str})
• 진입 대기 종목: {', '.join(pending) if pending else '(없음)'}
• 현재 보유 종목: {', '.join(held) if held else '(없음)'}
• 누적 거래 이력: {len(paper_db.get('closed_trades', []))}건"""
                        send_telegram_msg(res_msg)

                    elif text == "/log":
                        paper_db = load_json_file(PAPER_TRADES_FILE, {"active_positions": {}, "closed_trades": []})
                        summary_msg = format_portfolio_status_msg(paper_db.get("active_positions", {}), paper_db.get("closed_trades", []))
                        send_telegram_msg(summary_msg)

                    elif text == "/stop":
                        EMERGENCY_STOP = True
                        send_telegram_msg("🛑 [인터락 작동] 신규 매수 감시가 일시 중단되었습니다.")

                    elif text == "/start":
                        EMERGENCY_STOP = False
                        send_telegram_msg("▶️ [인터락 해제] 신규 매수 감시가 정상 재개되었습니다.")

                    elif text == "/update":
                        send_telegram_msg("🔄 [원격 업데이트] 코드를 동기화하고 서비스를 재시작합니다...")
                        try:
                            requests.get(url, params={"offset": LAST_TELEGRAM_UPDATE_ID + 1, "timeout": 1}, timeout=3)
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
    start_str = SESSION_START_TIME.strftime("%m/%d %H:%M KST")
    send_telegram_msg(
        f"🚀 [오라클 서버] BTC 국면 방어 & 동적 트레일링 엔진 가동\n"
        f"• 시작 시점: {start_str}\n"
        f"• 가동 모드: {'🧪 모의투자' if PAPER_TRADING else '🔥 실전매매'}\n\n"
        "📱 사용 가능한 명령어:\n"
        "• /status : 시스템 상태 및 포지션 확인\n"
        "• /log : 최근 매도 이력 및 세션 누적 손익\n"
        "• /update : GitHub 최신 코드 동기화 후 재시작"
    )
    asyncio.run(realtime_execution_engine())
