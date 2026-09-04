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
# 0. 전역 설정 및 환경 변수
# ==========================================
PAPER_TRADING = True             # 🧪 True: 모의투자 / False: 빗썸 실전매매
MAX_HOLDING_COINS = 3            # 🛡️ 최대 보유 가능 종목 수
MIN_BUY_KRW = 6000               # 💵 최소 매수 금액 (원)
DEFAULT_BUY_RATIO = 0.20         # 📊 기본 1회 투입 비중 (20%)
MAX_TICK_RATIO_PCT = 0.20        # 🛡️ 1틱 변동률 상한선 (0.20% 초과 종목 배제)

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
    """
    타임존 왜곡 없는 정밀 KST datetime 파서 (눈가림용 now 대체 완전 배제)
    파싱 실패 시 None을 반환하여 호출자가 비정상 데이터를 안전하게 격리하도록 유도.
    """
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

def get_bithumb_account_summary():
    """총 평가자산과 가용 원화(KRW) 동시 조회 (눈가림 기본값 반환 배제)"""
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
                else:
                    price = get_current_price(curr) or float(acc.get("avg_buy_price", 0.0))
                    total_krw += (total_units * price)
            return total_krw, available_krw
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

def get_current_price(coin_code: str):
    """현재가 조회 실패 시 None 반환 (대체값으로 조작하지 않음)"""
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

    return {
        "rsi_1h": rsi_1h,
        "rsi_15m": rsi_15m,
        "atr_1h": atr_1h,
        "atr_pct": atr_pct,
        "ma20_1h": round(ma20_1h, 4),
        "curr_price": curr_p,
        "vol_surge_ratio": vol_surge_ratio,
        "tick_ratio_pct": tick_ratio_pct
    }

# ==========================================
# 4. 복기 프롬프트 & AI 전략 수립 모듈
# ==========================================
def build_reflection_prompt(closed_trades):
    """최근 매매 기록 중 손절 3건과 최대 익절 2건을 요약하여 자가 교정 프롬프트 생성"""
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
    """승률 기반 켈리 베팅 비중 연산 (15% ~ 25%)"""
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
        cleaned = re.sub(r"
