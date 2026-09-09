import os
import sys

# ==========================================
# [필수] 시스템 프록시 환경변수 원천 무효화 (API 402/통신 에러 차단)
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
# 0. 전역 설정 및 퀀트 인터락 파라미터
# ==========================================
PAPER_TRADING = True             # 🧪 True: 모의투자 / False: 빗썸 실전매매
MAX_HOLDING_COINS = 3            # 🛡️ 최대 동시 보유 종목 수
MIN_BUY_KRW = 6000               # 💵 최소 매수 금액 (원)
DEFAULT_BUY_RATIO = 0.20         # 📊 기본 1회 투입 비중 (20%)
MAX_TICK_RATIO_PCT = 0.20        # 🛡️ 1틱 변동률 상한선 (0.20% 초과 종목 배제)
MIN_24H_ACC_TRADE_VALUE = 3_000_000_000  # 🛡️ 24시간 누적 거래대금 하한선 (30억 원)
DAILY_LOSS_LIMIT_PCT = -3.0      # 🛑 일일 누적 손실 서킷브레이커 (-3.0% 도달 시 당일 매매 중단)

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
PROJECT_DIR = "/home/ubuntu/auto-trade"

EMERGENCY_STOP = False
CIRCUIT_BREAKER_ACTIVE = False
LAST_TELEGRAM_UPDATE_ID = 0

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
    """한국 표준시(KST) offset-aware datetime 반환"""
    return datetime.now(KST)

def parse_dt_safe(dt_str):
    """타임존 왜곡 없는 정밀 KST datetime 파서"""
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
# 1. 텔레그램 유틸리티
# ==========================================
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
    held_symbols = [v['symbol'] for v in active_positions.values()]
    held_str = f"{', '.join(held_symbols)} ({len(held_symbols)}개 보유 중)" if held_symbols else "(현재 보유 종목 없음)"

    recent_10 = closed_trades[-10:][::-1] if closed_trades else []
    
    if not recent_10:
        trades_str = "• 매도 이력이 없습니다."
        win_rate = 0.0
        total_profit_krw = 0
    else:
        trade_lines = []
        wins = 0
        total_profit_krw = 0
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
                wins += 1
            total_profit_krw += p_krw
            
        trades_str = "\n".join(trade_lines)
        win_rate = round((wins / len(recent_10)) * 100, 1)

    sign_krw = "+" if total_profit_krw > 0 else ""
    return f"""💼 [현재 매매 상황]
• 보유 종목 : {held_str}

📜 [최근 10건 매도 이력 (KST)]
{trades_str}

📊 최근 10건 승률 : {win_rate}%
💰 최근 10건 실현 손익 : {sign_krw}{total_profit_krw:,} KRW"""

# ==========================================
# 2. 파일 I/O 및 GitHub 동기화
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
# 3. 빗썸 API & 자금 정밀 검증
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

def get_current_price(coin_code: str):
    try:
        url = f"https://api.bithumb.com/public/ticker/{coin_code}_KRW"
        res = requests.get(url, timeout=3).json()
        if res.get("status") == "0000":
            price = float(res["data"]["closing_price"])
            if price > 0:
                return price
    except Exception:
        pass
    return None

def get_bithumb_account_summary():
    """총 평가자산과 가용 원화(KRW) 동시 조회 (빗썸 포인트 P 등 비거래 자산 파싱 예외 차단)"""
    if not BITHUMB_API_KEY or not BITHUMB_SECRET_KEY:
        return None, None
    try:
        url = "https://api.bithumb.com/v1/accounts"
        headers = get_bithumb_jwt_headers()
        res = requests.get(url, headers=headers, timeout=5).json()
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
                    try:
                        price = get_current_price(curr) or float(acc.get("avg_buy_price", 0.0))
                        total_krw += (total_units * price)
                    except Exception:
                        pass
            return round(total_krw, 2), round(available_krw, 2)
    except Exception as e:
        logging.error(f"빗썸 계좌 잔고 조회 실패: {e}")
    return None, None

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

def get_candles(coin_code, interval="15m", limit=40):
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

def calculate_quant_features(candles_1h, candles_15m):
    closes_1h = [c['close'] for c in candles_1h]
    closes_15m = [c['close'] for c in candles_15m]
    
    rsi_1h = calculate_rsi(closes_1h, 14)
    rsi_15m = calculate_rsi(closes_15m, 14)
    atr_1h = calculate_atr(candles_1h, 14)
    ma20_1h = sum(closes_1h[-20:]) / 20.0 if len(closes_1h) >= 20 else closes_1h[-1]
    
    vol_avg_15m = sum(c['volume'] for c in candles_15m[-10:]) / 10.0 if len(candles_15m) >= 10 else 1.0
    vol_surge_ratio = round(candles_15m[-1]['volume'] / vol_avg_15m, 2) if vol_avg_15m > 0 else 1.0

    curr_p = closes_15m[-1]
    tick_size = get_bithumb_tick_size(curr_p)
    tick_ratio_pct = round((tick_size / curr_p) * 100.0, 3) if curr_p > 0 else 1.0
    atr_pct = round((atr_1h / curr_p) * 100.0, 2) if curr_p > 0 else 0.0

    # ATR 기반 동적 맞춤 손절폭 연산 (1.2 * ATR_PCT, -1.8% ~ -2.8% 범위 클램핑)
    dynamic_sl_pct = -round(min(max(atr_pct * 1.2, 1.8), 2.8), 2)

    return {
        "rsi_1h": rsi_1h,
        "rsi_15m": rsi_15m,
        "atr_1h": atr_1h,
        "atr_pct": atr_pct,
        "dynamic_sl_pct": dynamic_sl_pct,
        "ma20_1h": round(ma20_1h, 4),
        "curr_price": curr_p,
        "vol_surge_ratio": vol_surge_ratio,
        "tick_ratio_pct": tick_ratio_pct
    }

# ==========================================
# 4. 리스크 관리 모듈 (서킷브레이커 & BTC 실시간 브레이크)
# ==========================================
def check_daily_circuit_breaker(closed_trades, total_asset):
    """당일 00:00 KST 기준 누적 손실률 점검"""
    now_kst = get_kst_now()
    today_start = now_kst.replace(hour=0, minute=0, second=0, microsecond=0)
    
    today_losses_krw = 0
    for t in closed_trades:
        exit_dt = parse_dt_safe(t.get('exit_time', ''))
        if exit_dt and exit_dt >= today_start:
            today_losses_krw += t.get('profit_krw', 0)
            
    loss_ratio_pct = (today_losses_krw / total_asset) * 100.0 if total_asset > 0 else 0.0
    if loss_ratio_pct <= DAILY_LOSS_LIMIT_PCT:
        return False, f"당일 누적 손실({loss_ratio_pct:.2f}%)이 일일 한도({DAILY_LOSS_LIMIT_PCT}%) 초과"
    return True, "당일 손실 허용 범위 내"

def check_btc_trend():
    """BTC 1시간 추세 및 15분 급락 실시간 브레이크"""
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

def build_reflection_prompt(closed_trades):
    if not closed_trades:
        return "No recent trade history available."

    recent_losses = [t for t in reversed(closed_trades) if t.get('profit_pct', 0.0) < 0][:3]
    recent_wins = sorted([t for t in closed_trades if t.get('profit_pct', 0.0) > 0], key=lambda x: x.get('profit_pct', 0.0), reverse=True)[:2]

    lines = []
    if recent_losses:
        lines.append("Recent Losses to Avoid:")
        for t in recent_losses:
            lines.append(f"- {t.get('symbol')}: {t.get('profit_pct')}% ({t.get('reason')})")
    if recent_wins:
        lines.append("Recent Profitable Trades:")
        for t in recent_wins:
            lines.append(f"- {t.get('symbol')}: +{t.get('profit_pct')}% ({t.get('reason')})")

    return "\n".join(lines)

def calculate_dynamic_buy_ratio(closed_trades):
    if not closed_trades or len(closed_trades) < 3:
        return DEFAULT_BUY_RATIO

    last_3 = closed_trades[-3:]
    if all(t.get('profit_pct', 0.0) < 0 for t in last_3):
        return 0.15

    last_5 = closed_trades[-5:]
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
# 5. 전략 실행 및 후보군 필터링 모듈
# ==========================================
def execute_server_side_strategy():
    global CIRCUIT_BREAKER_ACTIVE
    paper_db = load_json_file(PAPER_TRADES_FILE, {"active_positions": {}, "closed_trades": []})
    active_positions = paper_db.get("active_positions", {})
    closed_trades = paper_db.get("closed_trades", [])
    
    # 1. 최대 보유 종목 인터락
    if len(active_positions) >= MAX_HOLDING_COINS:
        logging.info("💼 최대 보유 종목(3개) 도달로 신규 분석 스킵")
        return

    # 2. 계좌 잔고 확인
    total_asset, available_krw = get_bithumb_account_summary()
    if total_asset is None or available_krw is None:
        if PAPER_TRADING:
            total_asset, available_krw = 100000.0, 100000.0
        else:
            logging.error("❌ 빗썸 잔고 수신 실패로 신규 전략 수립 중단")
            return

    # 3. 당일 손실 한도 서킷브레이커 인터락
    cb_ok, cb_reason = check_daily_circuit_breaker(closed_trades, total_asset)
    if not cb_ok:
        if not CIRCUIT_BREAKER_ACTIVE:
            CIRCUIT_BREAKER_ACTIVE = True
            send_telegram_msg(f"🛑 [서킷브레이커 발동] {cb_reason}\n금일 자정(24:00 KST)까지 신규 매수가 전면 중단됩니다.")
        logging.info(f"🛑 [서킷브레이커 작동 중] {cb_reason}")
        return
    else:
        CIRCUIT_BREAKER_ACTIVE = False

    # 4. 비트코인 매크로 & 15분 급락 브레이크
    btc_ok, btc_reason = check_btc_trend()
    if not btc_ok:
        logging.info(f"🛑 [매크로 방어] {btc_reason} ➔ 신규 매수 올스톱 및 현금 보존")
        return

    # 5. 자금 배분 및 가용 원화 인터락
    dynamic_ratio = calculate_dynamic_buy_ratio(closed_trades)
    calc_buy_krw = round(total_asset * dynamic_ratio)
    target_buy_krw = max(calc_buy_krw, MIN_BUY_KRW)

    if available_krw < MIN_BUY_KRW:
        logging.info(f"⏸️ 가용 원화 부족 (최소 {MIN_BUY_KRW:,}원 필요 / 현재 보유: {available_krw:,.0f}원) ➔ 관망")
        return

    if target_buy_krw > available_krw:
        logging.info(f"⏸️ 가용 원화 부족 (필요: {target_buy_krw:,}원 / 보유: {available_krw:,.0f}원) ➔ 주문 보류")
        return

    held_codes = set(active_positions.keys())
    logging.info(f"🧠 [장세 적응형 퀀트 선별 & AI 전략 분석 시작] (배정 비중: {int(dynamic_ratio*100)}%, 목표 투입금: {target_buy_krw:,}원)")

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
            val_24h = float(info['acc_trade_value_24H'])
            # 🛡️ 전략 1: 24시간 누적 거래대금 30억 원 미만 종목 원천 배제
            if val_24h < MIN_24H_ACC_TRADE_VALUE:
                continue
            raw_list.append((sym, float(info['closing_price']), float(info['fluctate_rate_24H']), val_24h))
        except Exception: pass

    sorted_list = sorted(raw_list, key=lambda x: x[3], reverse=True)[:25]
    candidates_pool = []

    for sym, price, change, val in sorted_list:
        c_1h = get_candles(sym, interval="1h", limit=25)
        time.sleep(0.06)
        c_15m = get_candles(sym, interval="15m", limit=30)
        time.sleep(0.06)

        if len(c_1h) < 20 or len(c_15m) < 20: continue

        q = calculate_quant_features(c_1h, c_15m)
        if q["tick_ratio_pct"] > MAX_TICK_RATIO_PCT: continue
        if q["rsi_1h"] > 70.0: continue
        if q["curr_price"] < (q["ma20_1h"] * 0.96): continue

        candidates_pool.append({
            "symbol": f"{sym}/KRW", "code": sym, "price": price, "change_24h": change,
            "quant": q, "candles_15m_recent": [{"c": c['close'], "h": c['high'], "l": c['low'], "v": c['volume']} for c in c_15m[-12:]]
        })

    if not candidates_pool:
        logging.info("⏸️ 거래대금 30억 및 퀀트 기준을 충족하는 후보가 없어 관망합니다.")
        return

    reflection_text = build_reflection_prompt(closed_trades)

    sys_prompt = (
        "You are an elite quantitative crypto hedge fund trader. Analyze market conditions, quant metrics, and historical reflection.\n"
        f"Memory Context:\n{reflection_text}\n\n"
        "Strict Trading Rules to maximize win rate:\n"
        "1. Mode 'SCALPING': Target pullbacks. entry_discount_pct MUST be 0.20% to 0.45% below current price. NEVER chase breakout tops. entry_timeout: 10 mins.\n"
        "2. Mode 'SWING': Deep pullbacks on trend. entry_discount_pct: 0.50% to 1.20%. entry_timeout: 45 mins.\n"
        "3. stop_loss_pct: Align with quant.dynamic_sl_pct (typically -1.8% to -2.8%). Do NOT use tight stops under -1.5% to avoid normal noise cuts.\n"
        "4. Output 'NONE' if market risk is elevated or setup is mediocre. When outputting 'NONE', state clear technical justification in 'detailed_reason'.\n"
        "Output JSON ONLY."
    )
    user_prompt = (
        f"Market Candidates (Filtered by >3B KRW vol & ATR):\n{json.dumps(candidates_pool[:8], ensure_ascii=False)}\n\n"
        "Schema: {\n"
        '  "selected_symbol": "SYMBOL/KRW" or "NONE",\n'
        '  "mode": "SCALPING" or "SWING",\n'
        '  "confidence_score": 85,\n'
        '  "entry_discount_pct": 0.25,\n'
        '  "stop_loss_pct": -2.1,\n'
        '  "entry_timeout_mins": 10,\n'
        '  "detailed_reason": "선정 이유 또는 관망 사유(기술적 리스크)"\n'
        "}"
    )

    res_raw = call_ai_api(sys_prompt, user_prompt)
    decision = clean_and_parse_json(res_raw)

    if not decision:
        logging.info("⏸️ AI 응답 파싱 실패로 관망 유지")
        return

    reason_msg = decision.get("detailed_reason", "사유 미기재")

    if decision.get("selected_symbol", "NONE") == "NONE":
        logging.info(f"⏸️ AI 분석 결과 관망 유지 | 사유: {reason_msg}")
        return

    selected = decision["selected_symbol"]
    code = selected.split('/')[0]
    if code in held_codes: return

    curr_p = get_current_price(code)
    if not curr_p: return

    confidence = int(decision.get("confidence_score", 0))
    if confidence < 75:
        logging.info(f"⏸️ [{selected}] 신뢰도({confidence}점) 기준(75점) 미달로 진입 스킵 | 사유: {reason_msg}")
        return

    mode = decision.get("mode", "SCALPING")
    # 최소 0.20% 눌림목 확보 강제
    discount = max(float(decision.get("entry_discount_pct", 0.25)), 0.20)
    target_entry = round(curr_p * (1.0 - (discount / 100.0)), 4)
    
    # ATR 기반 동적 손절선 자동 보정
    matching_cand = next((c for c in candidates_pool if c["code"] == code), None)
    default_sl = matching_cand["quant"]["dynamic_sl_pct"] if matching_cand else -2.0
    sl_pct = min(float(decision.get("stop_loss_pct", default_sl)), -1.8)
    timeout_mins = int(decision.get("entry_timeout_mins", 10 if mode == "SCALPING" else 45))

    now_iso = get_kst_now().isoformat()
    plan_data = {
        "symbol": selected,
        "code": code,
        "mode": mode,
        "current_price": curr_p,
        "target_entry": target_entry,
        "sl_pct": sl_pct,
        "timeout_mins": timeout_mins,
        "buy_amount_krw": target_buy_krw,
        "detailed_reason": reason_msg,
        "created_at": now_iso
    }

    server_state = load_json_file(STATE_FILE, {"pending_targets": {}, "last_updated": ""})
    server_state.setdefault("pending_targets", {})[code] = plan_data
    server_state["last_updated"] = now_iso
    save_json_file(STATE_FILE, server_state)

    targets_payload = {"updated_at": now_iso, "paper_trading": PAPER_TRADING, "targets": server_state["pending_targets"]}
    save_json_file(TARGETS_FILE, targets_payload)
    sync_file_to_github(TARGETS_FILE, targets_payload)

    mode_icon = "⚡ [눌림목 스캘핑]" if mode == "SCALPING" else "🌊 [추세 스윙]"

    plan_msg = f"""🎯 {mode_icon} 타점 선정 - 서버
• 종목 : {selected} (신뢰도: {confidence}점)
• 현재가 : {curr_p:,.4f} KRW
• 진입 목표가 : {target_entry:,.4f} KRW (-{discount:.2f}% 눌림목)
• 배정 투자금 : {target_buy_krw:,} KRW (총자산 {int(dynamic_ratio*100)}%)

🛡️ ATR 동적 손절선 : {sl_pct}%
📈 익절 전략 : +1.8% 도달 시 본절 방어선 가동 및 계단식 트레일링
⏰ 유효 시간 : {timeout_mins}분 미체결 시 취소

💡 매수 근거 :
{plan_data['detailed_reason']}"""
    send_telegram_msg(plan_msg)

    portfolio_msg = format_portfolio_status_msg(paper_db.get("active_positions", {}), paper_db.get("closed_trades", []))
    send_telegram_msg(portfolio_msg)

# ==========================================
# 6. 실시간 감시 & 계단식 트레일링 엔진
# ==========================================
async def realtime_execution_engine():
    global EMERGENCY_STOP
    logging.info("⚡ 장세 적응형 무결성 실행 엔진 가동 (승률 강화 퀀트 버전)")
    last_strategy_run = 0

    t = threading.Thread(target=telegram_listener_thread, daemon=True)
    t.start()

    while True:
        try:
            now = time.time()
            now_dt = get_kst_now()

            # 5분 주기 AI 장세 분석
            if now - last_strategy_run >= 300:
                last_strategy_run = now
                asyncio.create_task(asyncio.to_thread(execute_server_side_strategy))

            server_state = load_json_file(STATE_FILE, {"pending_targets": {}, "last_updated": ""})
            paper_db = load_json_file(PAPER_TRADES_FILE, {"active_positions": {}, "closed_trades": []})

            pending = server_state.get("pending_targets", {})
            active_positions = paper_db.get("active_positions", {})
            closed_trades = paper_db.get("closed_trades", [])

            # [1] 진입 대기 감시 (눌림목 체결 & 직전봉 매도세 진정 확인)
            if not EMERGENCY_STOP and not CIRCUIT_BREAKER_ACTIVE and len(active_positions) < MAX_HOLDING_COINS:
                for coin_code, plan in list(pending.items()):
                    created_dt = parse_dt_safe(plan.get("created_at", ""))
                    if created_dt is None:
                        del pending[coin_code]
                        save_json_file(STATE_FILE, server_state)
                        continue

                    timeout_limit = timedelta(minutes=plan.get("timeout_mins", 10))
                    if now_dt - created_dt >= timeout_limit:
                        logging.info(f"⌛ [{plan['symbol']}] {plan.get('timeout_mins', 10)}분 내 목표가 미도달로 자동 취소")
                        del pending[coin_code]
                        save_json_file(STATE_FILE, server_state)
                        continue

                    curr_p = get_current_price(coin_code)
                    if curr_p and curr_p <= plan["target_entry"]:
                        # 5분봉 캔들 확인: 직전봉이 과도한 장대음봉(투매)이 아닐 때 진입
                        c_5m = get_candles(coin_code, interval="5m", limit=5)
                        if len(c_5m) >= 3:
                            last_candle = c_5m[-2]
                            candle_drop_pct = ((last_candle['open'] - last_candle['close']) / last_candle['open']) * 100.0
                            if candle_drop_pct > 2.0:
                                logging.info(f"⏸️ [{plan['symbol']}] 직전 5분봉 장대음봉(-{candle_drop_pct:.1f}%) 투매로 체결 보류")
                                continue

                        buy_krw = plan.get("buy_amount_krw", MIN_BUY_KRW)
                        if not PAPER_TRADING:
                            success, order_res = execute_real_market_order(coin_code, "bid", buy_krw)
                            if not success:
                                logging.error(f"실전 매수 주문 실패: {order_res}")
                                continue

                        logging.info(f"🎯 [{plan['symbol']}] 눌림목 체결 완료 ({plan.get('mode', 'SCALPING')})")
                        units = buy_krw / curr_p

                        active_positions[coin_code] = {
                            "symbol": plan["symbol"],
                            "mode": plan.get("mode", "SCALPING"),
                            "entry_price": curr_p,
                            "highest_price": curr_p,
                            "buy_amount_krw": buy_krw,
                            "units": units,
                            "sl_pct": plan["sl_pct"],
                            "entry_time": now_dt.isoformat(),
                            "break_even_triggered": False,
                            "locked_floor_profit_pct": 0.0,
                            "price_history": [(now, curr_p)]
                        }
                        del pending[coin_code]
                        paper_db["active_positions"] = active_positions
                        save_json_file(PAPER_TRADES_FILE, paper_db)
                        save_json_file(STATE_FILE, server_state)
                        sync_file_to_github(PAPER_TRADES_FILE, paper_db)

                        buy_msg = f"""⚡ [체결 완료] - {'모의투자' if PAPER_TRADING else '실전매매'} ({plan.get('mode', 'SCALPING')})
• 종목 : {plan['symbol']}
• 체결가 : {curr_p:,.4f} KRW (눌림목)
• 매수금 : {buy_krw:,} KRW

🛡️ ATR 동적 손절선 : {plan['sl_pct']}%
📈 익절 목표 : 계단식 트레일링 스탑
⏰ 시간 : {now_dt.strftime('%m/%d %H:%M KST')}"""
                        send_telegram_msg(buy_msg)

            # [2] 보유 포지션 실시간 감시 (계단식 트레일링 & 본절 인터락 개선)
            for coin_code, pos in list(active_positions.items()):
                curr_p = get_current_price(coin_code)
                if not curr_p: continue

                entry_p = pos["entry_price"]
                entry_time = parse_dt_safe(pos.get("entry_time", ""))
                if entry_time is None:
                    continue

                hist = pos.get("price_history", [])
                hist.append((now, curr_p))
                hist = [(ts, p) for ts, p in hist if now - ts <= 3600]
                pos["price_history"] = hist

                curr_profit_pct = ((curr_p - entry_p) / entry_p) * 100.0

                if curr_p > pos.get("highest_price", entry_p):
                    pos["highest_price"] = curr_p

                highest_profit_pct = ((pos["highest_price"] - entry_p) / entry_p) * 100.0
                mode = pos.get("mode", "SCALPING")

                # 조기 스크래치 컷 방지: 최고수익 +1.8% 도달 시 +0.4% 본절 방어선 가동
                if not pos.get("break_even_triggered", False) and highest_profit_pct >= 1.8:
                    pos["break_even_triggered"] = True
                    pos["locked_floor_profit_pct"] = max(pos.get("locked_floor_profit_pct", 0.0), 0.4)
                    logging.info(f"🛡️ [{pos['symbol']}] 최고수익 +{highest_profit_pct:.2f}% 달성으로 본절 방어선(+0.4%) 가동")

                trailing_pullback = 0.6
                if highest_profit_pct >= 6.0:
                    pos["locked_floor_profit_pct"] = max(pos.get("locked_floor_profit_pct", 0.0), 5.0)
                    trailing_pullback = 1.8
                elif highest_profit_pct >= 3.0:
                    pos["locked_floor_profit_pct"] = max(pos.get("locked_floor_profit_pct", 0.0), 2.4)
                    trailing_pullback = 1.0

                save_json_file(PAPER_TRADES_FILE, paper_db)

                should_close = False
                close_reason = ""

                # ① 계단식 트레일링 스탑 청산
                if highest_profit_pct >= 1.8 and (highest_profit_pct - curr_profit_pct) >= trailing_pullback:
                    should_close = True
                    close_reason = f"📈 계단식 트레일링 익절 (고점 +{highest_profit_pct:.2f}% 대비 -{trailing_pullback}% 반락)"

                # ② 하한선 Lock / 본절 방어선 청산
                elif pos.get("break_even_triggered", False) and curr_profit_pct <= pos.get("locked_floor_profit_pct", 0.4):
                    should_close = True
                    close_reason = f"🛡️ 이익 보존 하한선(+{pos.get('locked_floor_profit_pct', 0.4)}%) 청산"

                # ③ ATR 동적 손절선 도달
                elif curr_profit_pct <= pos["sl_pct"]:
                    should_close = True
                    close_reason = f"🛡️ ATR 동적 손절가 도달 ({curr_profit_pct:.2f}%)"

                # ④ 가격 변동성 소멸 횡보 청산 (진입 후 충분한 관찰 시간 부여)
                else:
                    hold_duration = now_dt - entry_time
                    min_hold_time = timedelta(minutes=25) if mode == "SCALPING" else timedelta(minutes=60)

                    if hold_duration >= min_hold_time and curr_profit_pct < 1.5 and len(hist) >= 20:
                        recent_prices = [p for _, p in hist]
                        high_p = max(recent_prices)
                        low_p = min(recent_prices)
                        volatility_range_pct = ((high_p - low_p) / low_p) * 100.0 if low_p > 0 else 1.0
                        threshold = 0.5 if mode == "SCALPING" else 0.7

                        if volatility_range_pct < threshold:
                            should_close = True
                            close_reason = f"⌛ 가격 변동성 소멸 청산 (최근 변동폭 {volatility_range_pct:.2f}% 미달 횡보)"

                if should_close:
                    if not PAPER_TRADING:
                        execute_real_market_order(coin_code, "ask", pos.get("units", 0.0))

                    profit_pct = round(curr_profit_pct, 2)
                    buy_krw = pos.get("buy_amount_krw", MIN_BUY_KRW)
                    profit_krw = round(buy_krw * (profit_pct / 100.0))

                    closed_trades.append({
                        "symbol": pos["symbol"],
                        "entry_price": entry_p,
                        "exit_price": curr_p,
                        "buy_amount_krw": buy_krw,
                        "profit_krw": profit_krw,
                        "profit_pct": profit_pct,
                        "reason": close_reason,
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

                    exit_msg = f"""{icon} [청산 완료] - {'모의투자' if PAPER_TRADING else '실전매매'}
• 종목 : {pos['symbol']}
• 진입가 : {entry_p:,.4f} KRW ➔ 청산가 : {curr_p:,.4f} KRW
• 손익률 : {sign_pct}{profit_pct:.2f}% ({sign_krw}{profit_krw:,}원)
• 사유 : {close_reason}"""
                    send_telegram_msg(exit_msg)

                    portfolio_msg = format_portfolio_status_msg(active_positions, closed_trades)
                    send_telegram_msg(portfolio_msg)

            await asyncio.sleep(2)
        except Exception as e:
            logging.error(f"감시 루프 오류: {e}")
            await asyncio.sleep(3)

# ==========================================
# 7. 텔레그램 리스너 스레드
# ==========================================
def telegram_listener_thread():
    global EMERGENCY_STOP, CIRCUIT_BREAKER_ACTIVE, LAST_TELEGRAM_UPDATE_ID
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
                        
                        if EMERGENCY_STOP:
                            status_str = "🛑 일시정지 (STOP)"
                        elif CIRCUIT_BREAKER_ACTIVE:
                            status_str = "🚨 일일 서킷브레이커 작동 중 (당일 매수 제한)"
                        else:
                            status_str = "🟢 실시간 감시 중 (RUNNING)"

                        res_msg = f"""📊 [시스템 상태 보고]
• 모드: {'🧪 모의투자' if PAPER_TRADING else '🔥 실전매매'}
• 상태: {status_str}
• 진입 대기 종목: {', '.join(pending) if pending else '(없음)'}
• 현재 보유 종목: {', '.join(held) if held else '(없음)'}
• 누적 복기 거래수: {len(paper_db.get('closed_trades', []))}건"""
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
                        CIRCUIT_BREAKER_ACTIVE = False
                        send_telegram_msg("▶️ [인터락 해제] 신규 매수 감시가 정상 재개되었습니다.")

                    elif text == "/update":
                        send_telegram_msg("🔄 [원격 업데이트] 최신 코드를 다운로드하고 서비스를 재시작합니다...")
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
    send_telegram_msg(
        f"🚀 [오라클 서버] 승률 극대화 퀀트 엔진 가동\n"
        f"• 유동성 필터: 24시간 거래대금 30억 이상 엄선\n"
        f"• 진입: 0.20~0.45% 눌림목 체결 (불나방 추격 매수 차단)\n"
        f"• 손절: ATR 기반 변동성 동적 손절 (-1.8% ~ -2.8%)\n"
        f"• 방어: BTC 15분 급락 브레이크 & 일일 -3% 서킷브레이커\n\n"
        f"📱 명령어: /status, /log, /stop, /start, /update"
    )
    asyncio.run(realtime_execution_engine())
