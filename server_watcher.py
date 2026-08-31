import os
import sys

# ==========================================
# [필수] 시스템 프록시 환경변수 원천 무효화 (402 에러 차단)
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
BUY_RATIO = 0.20                 # 📊 총 평가자산 대비 1회 투입 비중 (20%)

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
    return datetime.now(KST)

def parse_dt_safe(dt_str):
    """문자열 날짜를 KST offset-aware datetime으로 안전하게 변환"""
    if not dt_str:
        return get_kst_now()
    try:
        dt = datetime.fromisoformat(str(dt_str))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=KST)
        return dt
    except Exception:
        try:
            dt = datetime.strptime(str(dt_str)[:19], "%Y-%m-%d %H:%M:%S")
            return dt.replace(tzinfo=KST)
        except Exception:
            return get_kst_now()

# ==========================================
# 1. 텔레그램 유틸리티
# ==========================================
def send_telegram_msg(msg: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"[https://api.telegram.org/bot](https://api.telegram.org/bot){TELEGRAM_BOT_TOKEN}/sendMessage"
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
            exit_time_str = t.get('exit_time', '')
            dt_obj = parse_dt_safe(exit_time_str)
            time_display = dt_obj.strftime("%m/%d %H:%M KST")
                
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
    url = f"[https://api.github.com/repos/](https://api.github.com/repos/){GITHUB_REPOSITORY}/contents/{file_path}"
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
# 3. 빗썸 API (총 평가자산 및 시세 조회)
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

def get_real_total_asset_krw():
    """빗썸 계좌의 총 평가자산(원화 + 코인 평가금액 합산) 조회"""
    if BITHUMB_API_KEY and BITHUMB_SECRET_KEY:
        try:
            url = "[https://api.bithumb.com/v1/accounts](https://api.bithumb.com/v1/accounts)"
            headers = get_bithumb_jwt_headers()
            res = requests.get(url, headers=headers, timeout=5).json()
            if isinstance(res, list):
                total_krw = 0.0
                for acc in res:
                    curr = acc.get("currency", "")
                    bal = float(acc.get("balance", 0.0))
                    locked = float(acc.get("locked", 0.0))
                    total_units = bal + locked
                    if curr == "KRW":
                        total_krw += total_units
                    else:
                        price = get_current_price(curr) or float(acc.get("avg_buy_price", 0.0))
                        total_krw += (total_units * price)
                if total_krw > 0:
                    return total_krw
        except Exception as e:
            logging.error(f"빗썸 총 자산 조회 실패: {e}")
    return 100000.0

def execute_real_market_order(coin_code: str, side: str, amount_or_units: float):
    if PAPER_TRADING:
        return True, "모의투자 체결"
    try:
        url = "[https://api.bithumb.com/v1/orders](https://api.bithumb.com/v1/orders)"
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

def get_current_price(coin_code: str) -> float:
    try:
        url = f"[https://api.bithumb.com/public/ticker/](https://api.bithumb.com/public/ticker/){coin_code}_KRW"
        res = requests.get(url, timeout=3).json()
        if res.get("status") == "0000":
            return float(res["data"]["closing_price"])
    except Exception:
        pass
    return None

def get_candles(coin_code, interval="15m", limit=40):
    try:
        url = f"[https://api.bithumb.com/public/candlestick/](https://api.bithumb.com/public/candlestick/){coin_code}_KRW/{interval}"
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

def calculate_quant_features(candles_1h, candles_15m):
    closes_1h = [c['close'] for c in candles_1h]
    closes_15m = [c['close'] for c in candles_15m]
    
    rsi_1h = calculate_rsi(closes_1h, 14)
    rsi_15m = calculate_rsi(closes_15m, 14)
    ma20_1h = sum(closes_1h[-20:]) / 20.0 if len(closes_1h) >= 20 else closes_1h[-1]
    
    vol_avg_15m = sum(c['volume'] for c in candles_15m[-10:]) / 10.0 if len(candles_15m) >= 10 else 1.0
    vol_surge_ratio = round(candles_15m[-1]['volume'] / vol_avg_15m, 2) if vol_avg_15m > 0 else 1.0

    return {
        "rsi_1h": rsi_1h,
        "rsi_15m": rsi_15m,
        "ma20_1h": round(ma20_1h, 4),
        "curr_price": closes_15m[-1],
        "vol_surge_ratio": vol_surge_ratio
    }

# ==========================================
# 4. 장세 적응형 다이렉트 AI 브레인
# ==========================================
def call_ai_api(system_instruction, user_prompt):
    providers = []
    if GEMINI_API_KEY:
        providers.append({
            "name": "Gemini 3.5 Flash-Lite",
            "key": GEMINI_API_KEY,
            "base_url": "[https://generativelanguage.googleapis.com/v1beta/openai/](https://generativelanguage.googleapis.com/v1beta/openai/)",
            "model": "gemini-3.5-flash-lite"
        })
    if SAMBANOVA_API_KEY:
        providers.append({
            "name": "SambaNova Llama-3.3-70B",
            "key": SAMBANOVA_API_KEY,
            "base_url": "[https://api.sambanova.ai/v1](https://api.sambanova.ai/v1)",
            "model": "Meta-Llama-3.3-70B-Instruct"
        })
    if GROQ_API_KEY3 or GROQ_API_KEY2:
        providers.append({
            "name": "Groq SpecDec",
            "key": GROQ_API_KEY3 or GROQ_API_KEY2,
            "base_url": "[https://api.groq.com/openai/v1](https://api.groq.com/openai/v1)",
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
        # Markdown 백틱 코드 블록 안전 제거
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

def check_btc_trend():
    """비트코인 1시간봉 기준 매크로 하락세 감지"""
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

def execute_server_side_strategy():
    """장세 적응형 AI 전략 실행부"""
    paper_db = load_json_file(PAPER_TRADES_FILE, {"active_positions": {}, "closed_trades": []})
    active_positions = paper_db.get("active_positions", {})
    
    if len(active_positions) >= MAX_HOLDING_COINS:
        logging.info("💼 최대 보유 종목(3개) 도달로 신규 분석 스킵")
        return

    btc_ok, btc_reason = check_btc_trend()
    if not btc_ok:
        logging.info(f"🛑 [매크로 방어 작동] {btc_reason} ➔ 신규 매수 올스톱 및 현금 보존")
        return

    held_codes = set(active_positions.keys())
    logging.info("🧠 [장세 적응형 퀀트 선별 & AI 전략 분석 시작]")

    url = "[https://api.bithumb.com/public/ticker/ALL_KRW](https://api.bithumb.com/public/ticker/ALL_KRW)"
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

    sorted_list = sorted(raw_list, key=lambda x: x[3], reverse=True)[:25]
    candidates_pool = []

    for sym, price, change, val in sorted_list:
        c_1h = get_candles(sym, interval="1h", limit=25)
        time.sleep(0.06)
        c_15m = get_candles(sym, interval="15m", limit=30)
        time.sleep(0.06)

        if len(c_1h) < 20 or len(c_15m) < 20: continue

        q = calculate_quant_features(c_1h, c_15m)
        if q["rsi_1h"] > 72.0: continue
        if q["curr_price"] < (q["ma20_1h"] * 0.95): continue

        candidates_pool.append({
            "symbol": f"{sym}/KRW", "code": sym, "price": price, "change_24h": change,
            "quant": q, "candles_15m_recent": [{"c": c['close'], "h": c['high'], "l": c['low'], "v": c['volume']} for c in c_15m[-12:]]
        })

    if not candidates_pool:
        logging.info("⏸️ 조건에 부합하는 종목이 없어 관망합니다.")
        return

    total_asset_krw = get_real_total_asset_krw()
    calculated_buy_krw = max(int(total_asset_krw * BUY_RATIO), MIN_BUY_KRW)

    sys_prompt = (
        "You are an elite quantitative crypto hedge fund trader. Analyze market condition and 15m/1h indicators.\n"
        "Dynamically select the optimal trading mode between 'SCALPING' (rapid impulse/breakout, tighter target) and 'SWING' (deep pullback/trend following, larger target).\n"
        "Rules:\n"
        "1. Mode 'SCALPING': take_profit_pct (+1.5% to +2.5%), stop_loss_pct (-1.0% to -1.3%), entry_timeout (15 mins), max_hold_hours (2 hours).\n"
        "2. Mode 'SWING': take_profit_pct (+4.0% to +8.0%), stop_loss_pct (-2.5% to -3.5%), entry_timeout (45 mins), max_hold_hours (8 hours).\n"
        "3. Output 'NONE' if market risk is high.\n"
        "Output JSON ONLY."
    )
    user_prompt = f"Market Candidates:\n{json.dumps(candidates_pool[:8], ensure_ascii=False)}\n\nSchema: {{\"selected_symbol\": \"SYMBOL/KRW\", \"mode\": \"SCALPING\" or \"SWING\", \"confidence_score\": 85, \"entry_discount_pct\": 0.15, \"take_profit_pct\": 2.0, \"stop_loss_pct\": -1.2, \"entry_timeout_mins\": 15, \"max_hold_hours\": 2, \"detailed_reason\": \"근거\"}}"

    res_raw = call_ai_api(sys_prompt, user_prompt)
    decision = clean_and_parse_json(res_raw)

    if not decision or decision.get("selected_symbol", "NONE") == "NONE":
        logging.info("⏸️ AI 분석 결과 관망을 유지합니다.")
        return

    selected = decision["selected_symbol"]
    code = selected.split('/')[0]
    if code in held_codes: return

    curr_p = get_current_price(code)
    if not curr_p: return

    confidence = int(decision.get("confidence_score", 0))
    if confidence < 70:
        logging.info(f"⏸️ [{selected}] 신뢰도({confidence}점) 기준 미달로 진입 스킵")
        return

    mode = decision.get("mode", "SCALPING")
    discount = float(decision.get("entry_discount_pct", 0.1))
    target_entry = round(curr_p * (1.0 - (discount / 100.0)), 4)
    sl_pct = float(decision.get("stop_loss_pct", -1.2 if mode == "SCALPING" else -3.0))
    tp_pct = float(decision.get("take_profit_pct", 2.0 if mode == "SCALPING" else 5.0))
    timeout_mins = int(decision.get("entry_timeout_mins", 15 if mode == "SCALPING" else 45))
    max_hold_hours = int(decision.get("max_hold_hours", 2 if mode == "SCALPING" else 8))

    now_iso = get_kst_now().isoformat()
    plan_data = {
        "symbol": selected,
        "code": code,
        "mode": mode,
        "current_price": curr_p,
        "target_entry": target_entry,
        "sl_pct": sl_pct,
        "tp_pct": tp_pct,
        "timeout_mins": timeout_mins,
        "max_hold_hours": max_hold_hours,
        "buy_amount_krw": calculated_buy_krw,
        "detailed_reason": decision.get("detailed_reason", "장세 적응형 타점"),
        "created_at": now_iso
    }

    server_state = load_json_file(STATE_FILE, {"pending_targets": {}, "last_updated": ""})
    server_state.setdefault("pending_targets", {})[code] = plan_data
    server_state["last_updated"] = now_iso
    save_json_file(STATE_FILE, server_state)

    targets_payload = {"updated_at": now_iso, "paper_trading": PAPER_TRADING, "targets": server_state["pending_targets"]}
    save_json_file(TARGETS_FILE, targets_payload)
    sync_file_to_github(TARGETS_FILE, targets_payload)

    tp_sign = "+" if tp_pct > 0 else ""
    mode_icon = "⚡ [단타 SCALPING]" if mode == "SCALPING" else "🌊 [스윙 SWING]"

    # 1. 타점 선정 알림
    plan_msg = f"""🎯 {mode_icon} 타점 선정 - 서버
• 종목 : {selected} (신뢰도: {confidence}점)
• 현재가 : {curr_p:,.4f} KRW
• 진입 목표가 : {target_entry:,.4f} KRW (-{discount}%)
• 배정 투자금 : {calculated_buy_krw:,} KRW (총자산 20%)

🎯 목표 익절 : {tp_sign}{tp_pct}%
🛡️ 손절 기준 : {sl_pct}%
⏰ 유효 시간 : {timeout_mins}분 미체결 취소 / 최대 보유 {max_hold_hours}시간

💡 매수 근거 :
{plan_data['detailed_reason']}"""
    send_telegram_msg(plan_msg)

    # 2. 현재 매매 상황 독립 발송
    portfolio_msg = format_portfolio_status_msg(paper_db.get("active_positions", {}), paper_db.get("closed_trades", []))
    send_telegram_msg(portfolio_msg)

# ==========================================
# 5. 실시간 감시 & 다이내믹 트레일링 엔진
# ==========================================
async def realtime_execution_engine():
    global EMERGENCY_STOP
    logging.info("⚡ 장세 적응형 실시간 감시 & 다이내믹 트레일링 엔진 구동 시작")
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

            # [1] 진입 대기 감시
            if not EMERGENCY_STOP and len(active_positions) < MAX_HOLDING_COINS:
                for coin_code, plan in list(pending.items()):
                    created_dt = parse_dt_safe(plan.get("created_at", ""))
                    timeout_limit = timedelta(minutes=plan.get("timeout_mins", 30))
                    
                    if now_dt - created_dt >= timeout_limit:
                        logging.info(f"⌛ [{plan['symbol']}] {plan.get('timeout_mins', 30)}분 내 미체결로 자동 취소")
                        del pending[coin_code]
                        save_json_file(STATE_FILE, server_state)
                        continue

                    curr_p = get_current_price(coin_code)
                    if curr_p and curr_p <= plan["target_entry"]:
                        c_5m = get_candles(coin_code, interval="5m", limit=3)
                        is_rebounding = True
                        if len(c_5m) >= 2:
                            last_candle = c_5m[-1]
                            candle_range = last_candle['high'] - last_candle['low']
                            if candle_range > 0 and last_candle['close'] < last_candle['open']:
                                if (curr_p - last_candle['low']) < (candle_range * 0.2):
                                    is_rebounding = False

                        if not is_rebounding:
                            continue

                        buy_krw = plan.get("buy_amount_krw", MIN_BUY_KRW)
                        if not PAPER_TRADING:
                            success, order_res = execute_real_market_order(coin_code, "bid", buy_krw)
                            if not success:
                                logging.error(f"실전 매수 주문 실패: {order_res}")
                                continue

                        logging.info(f"🎯 [{plan['symbol']}] 진입 타점 체결 완료 ({plan.get('mode', 'SCALPING')})")
                        units = buy_krw / curr_p

                        active_positions[coin_code] = {
                            "symbol": plan["symbol"],
                            "mode": plan.get("mode", "SCALPING"),
                            "entry_price": curr_p,
                            "highest_price": curr_p,
                            "buy_amount_krw": buy_krw,
                            "units": units,
                            "sl_pct": plan["sl_pct"],
                            "tp_pct": plan["tp_pct"],
                            "max_hold_hours": plan.get("max_hold_hours", 3),
                            "entry_time": now_dt.isoformat(),
                            "break_even_triggered": False
                        }
                        del pending[coin_code]
                        paper_db["active_positions"] = active_positions
                        save_json_file(PAPER_TRADES_FILE, paper_db)
                        save_json_file(STATE_FILE, server_state)
                        sync_file_to_github(PAPER_TRADES_FILE, paper_db)

                        tp_sign = "+" if plan['tp_pct'] > 0 else ""
                        buy_msg = f"""⚡ [체결 완료] - {'모의투자' if PAPER_TRADING else '실전매매'} ({plan.get('mode', 'SCALPING')})
• 종목 : {plan['symbol']}
• 진입 체결가 : {curr_p:,.4f} KRW
• 매수 금액 : {buy_krw:,} KRW (총자산 20%)

🎯 목표 익절 : {tp_sign}{plan['tp_pct']}%
🛡️ 손절 기준 : {plan['sl_pct']}%
⏰ 체결 시간 : {now_dt.strftime('%m/%d %H:%M KST')}"""
                        send_telegram_msg(buy_msg)

            # [2] 보유 포지션 실시간 감시
            for coin_code, pos in list(active_positions.items()):
                curr_p = get_current_price(coin_code)
                if not curr_p: continue

                entry_p = pos["entry_price"]
                entry_time = parse_dt_safe(pos.get("entry_time", ""))
                curr_profit_pct = ((curr_p - entry_p) / entry_p) * 100.0

                if curr_p > pos.get("highest_price", entry_p):
                    pos["highest_price"] = curr_p
                    save_json_file(PAPER_TRADES_FILE, paper_db)

                highest_profit_pct = ((pos["highest_price"] - entry_p) / entry_p) * 100.0
                mode = pos.get("mode", "SCALPING")

                be_threshold = 1.0 if mode == "SCALPING" else 1.8
                if not pos.get("break_even_triggered", False) and curr_profit_pct >= be_threshold:
                    pos["break_even_triggered"] = True
                    logging.info(f"🛡️ [{pos['symbol']}] 수익률 +{curr_profit_pct:.2f}% 도달로 본절 방어선 가동")

                should_close = False
                close_reason = ""

                # ① 트레일링 스탑
                trailing_start = 1.3 if mode == "SCALPING" else 3.5
                trailing_pullback = 0.4 if mode == "SCALPING" else 1.2
                if highest_profit_pct >= trailing_start and (highest_profit_pct - curr_profit_pct) >= trailing_pullback:
                    should_close = True
                    close_reason = f"📈 트레일링 익절 (고점 +{highest_profit_pct:.2f}% 달성 후 -{trailing_pullback}% 반락)"

                # ② 본절 방어선 / 손절선
                elif pos.get("break_even_triggered", False) and curr_profit_pct <= 0.1:
                    should_close = True
                    close_reason = "🛡️ 본절 방어선 청산 (수수료 보전 탈출)"
                elif curr_profit_pct <= pos["sl_pct"]:
                    should_close = True
                    close_reason = f"🛡️ 손절가 도달 ({curr_profit_pct:.2f}%)"

                # ③ 모멘텀 소멸 조기 청산
                elif mode == "SCALPING" and (now_dt - entry_time) >= timedelta(minutes=40) and abs(curr_profit_pct) < 0.4:
                    should_close = True
                    close_reason = "⌛ 40분간 모멘텀 소멸로 조기 청산 (현금 회수)"

                # ④ 만기 타임아웃
                elif (now_dt - entry_time) >= timedelta(hours=pos.get("max_hold_hours", 3)):
                    should_close = True
                    close_reason = f"⏰ {pos.get('max_hold_hours', 3)}시간 만기 시장가 청산"

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
• 진입단가 : {entry_p:,.4f} KRW ➔ 청산단가 : {curr_p:,.4f} KRW
• 이번손익 : {sign_pct}{profit_pct:.2f}% ({sign_krw}{profit_krw:,}원)
• 구분 : {close_reason}"""
                    send_telegram_msg(exit_msg)

                    portfolio_msg = format_portfolio_status_msg(active_positions, closed_trades)
                    send_telegram_msg(portfolio_msg)

            await asyncio.sleep(2)
        except Exception as e:
            logging.error(f"감시 루프 오류: {e}")
            await asyncio.sleep(3)

# ==========================================
# 6. 텔레그램 리스너 스레드
# ==========================================
def telegram_listener_thread():
    global EMERGENCY_STOP, LAST_TELEGRAM_UPDATE_ID
    if not TELEGRAM_BOT_TOKEN: return
    url = f"[https://api.telegram.org/bot](https://api.telegram.org/bot){TELEGRAM_BOT_TOKEN}/getUpdates"

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
        f"🚀 [오라클 서버] 장세 적응형 하이브리드 엔진 가동\n"
        f"• 모드: {'🧪 모의투자' if PAPER_TRADING else '🔥 실전매매'}\n"
        f"• 투자금: 총 평가자산 20% 동적 배분 (최소 6,000원)\n"
        f"• 안전장치: BTC 매크로 필터 + 본절 방어선 + 다이내믹 트레일링\n\n"
        f"📱 명령어: /status, /log, /stop, /start, /update"
    )
    asyncio.run(realtime_execution_engine())
