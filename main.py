import os
import sys
import time
import uuid
import json
import re
import base64
import jwt
import requests
import httpx
import pandas as pd
from datetime import datetime, timedelta
from openai import OpenAI

# ==========================================
# 0. 설정 및 환경변수(Secrets) 수집
# ==========================================
PAPER_TRADING = True         # 🧪 모의투자 (True: 가상 시뮬레이션 / False: 실전 매매)
MIN_CONFIDENCE_SCORE = 75    # 🎯 매수 최소 신뢰도 기준
MAX_HOLDING_COINS = 3        # 🛡️ 최대 보유 가능 종목 수
MIN_BUY_KRW = 6000           # 💵 최소 매수 금액 (빗썸 5천원 + 안전마진)
BUY_RATIO = 0.20             # 📊 가용 잔고의 20%
TIME_EXIT_HOURS = 3          # ⏰ 시간 손절 기준

POSITIONS_FILE = "positions.json"
PAPER_TRADES_FILE = "paper_trades.json"
TARGETS_FILE = "targets.json"

GITHUB_REPOSITORY = "dhlee090512-arch/auto-trade"

def get_env(key_name):
    val = None
    try:
        from google.colab import userdata
        val = userdata.get(key_name)
    except:
        pass
    if not val:
        val = os.getenv(key_name)
    if val and isinstance(val, str):
        return val.strip()
    return ""

GITHUB_TOKEN = get_env("GH_TOKEN") or get_env("GITHUB_TOKEN")

GEMINI_API_KEY = get_env("GEMINI_API_KEY")
SAMBANOVA_API_KEY = get_env("SAMBANOVA_API_KEY")
GROQ_API_KEY3 = get_env("GROQ_API_KEY3")
GROQ_API_KEY2 = get_env("GROQ_API_KEY2")

BITHUMB_API_KEY = get_env("BITHUMB_API_KEY")
BITHUMB_SECRET_KEY = get_env("BITHUMB_SECRET_KEY")
WEBSHARE_URL = get_env("WEBSHARE_URL")
TELEGRAM_BOT_TOKEN = get_env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = get_env("TELEGRAM_CHAT_ID")

PROXIES = {'http': WEBSHARE_URL, 'https': WEBSHARE_URL} if WEBSHARE_URL else None
SYNCED_FILES = set()

# ==========================================
# 텔레그램 알림 발송 전용 모듈
# ==========================================
def send_telegram_msg(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] 텔레그램 토큰/Chat ID 미설정")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        res = requests.post(url, json=payload, timeout=10)
        if res.status_code == 200:
            print("📲 텔레그램 알림 전송 완료!")
        else:
            print(f"⚠️ 텔레그램 발송 실패: {res.text}")
    except Exception as e:
        print(f"⚠️ 텔레그램 오류: {e}")

# ==========================================
# 1. GitHub API 파일 관리 모듈
# ==========================================
def get_github_file_info(file_path):
    url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/contents/{file_path}"
    headers = {"Accept": "application/vnd.github.v3+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
        
    try:
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code == 200:
            data = res.json()
            content = base64.b64decode(data['content']).decode('utf-8')
            sha = data['sha']
            return json.loads(content), sha
    except Exception as e:
        print(f"⚠️ GitHub API 읽기 오류 ({file_path}): {e}")
    return None, None

def save_github_file(file_path, content_data, sha=None):
    if not GITHUB_TOKEN:
        print("💡 [알림] GITHUB_TOKEN 없음. 로컬 파일만 저장합니다.")
        return

    url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/contents/{file_path}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }

    if not sha:
        _, sha = get_github_file_info(file_path)

    json_str = json.dumps(content_data, indent=2, ensure_ascii=False)
    encoded_content = base64.b64encode(json_str.encode('utf-8')).decode('utf-8')

    payload = {
        "message": f"update: {file_path} auto update via API",
        "content": encoded_content
    }
    if sha:
        payload["sha"] = sha

    try:
        res = requests.put(url, headers=headers, json=payload, timeout=10)
        if res.status_code in [200, 201]:
            print(f"🚀 [GitHub API] '{file_path}' 파일이 저장소에 업로드되었습니다.")
    except Exception as e:
        print(f"❌ [GitHub API] 통신 에러: {e}")

def load_json_file(file_path, default_value):
    global SYNCED_FILES
    if file_path not in SYNCED_FILES:
        remote_data, _ = get_github_file_info(file_path)
        if remote_data is not None:
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(remote_data, f, indent=2, ensure_ascii=False)
            SYNCED_FILES.add(file_path)
            return remote_data
        SYNCED_FILES.add(file_path)

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
    save_github_file(file_path, data)

def clean_and_parse_json(raw_text):
    if not raw_text:
        return None
    try:
        cleaned = raw_text.strip()
        if "```json" in cleaned:
            cleaned = cleaned.split("```json")[1].split("```")[0].strip()
        elif "```" in cleaned:
            cleaned = cleaned.split("```")[1].split("```")[0].strip()

        start_idx = cleaned.find('{')
        if start_idx != -1:
            target_str = cleaned[start_idx:]
            try:
                decoder = json.JSONDecoder()
                obj, _ = decoder.raw_decode(target_str)
                return obj
            except Exception:
                pass

            sanitized = re.sub(r'[\r\n\t]+', ' ', target_str)
            end_idx = sanitized.rfind('}')
            if end_idx != -1:
                sanitized = sanitized[:end_idx+1]
            try:
                return json.loads(sanitized)
            except Exception:
                pass

        return json.loads(cleaned)
    except Exception as e:
        print(f"⚠️ JSON 파싱 실패: {e}")
        return None

# ==========================================
# 2. 빗썸 API 및 차트 데이터 수집
# ==========================================
def get_v2_headers(query_params=None):
    payload = {
        'access_key': BITHUMB_API_KEY,
        'nonce': str(uuid.uuid4()),
        'timestamp': int(time.time() * 1000)
    }
    if query_params:
        import urllib.parse
        from hashlib import sha512
        query_string = urllib.parse.urlencode(query_params).encode()
        m = sha512()
        m.update(query_string)
        payload['query_hash'] = m.hexdigest()
        payload['query_hash_alg'] = 'SHA512'

    jwt_token = jwt.encode(payload, BITHUMB_SECRET_KEY)
    return {
        'Authorization': f'Bearer {jwt_token}',
        'Content-Type': 'application/json'
    }

def get_candles(coin_code, interval="5m", limit=30):
    try:
        url = f"https://api.bithumb.com/public/candlestick/{coin_code}_KRW/{interval}"
        res = requests.get(url, proxies=PROXIES, timeout=5).json()
        if res.get("status") == "0000":
            raw_candles = res['data'][-limit:]
            candles = []
            for c in raw_candles:
                candles.append({
                    "timestamp": int(c[0]),
                    "open": float(c[1]),
                    "close": float(c[2]),
                    "high": float(c[3]),
                    "low": float(c[4]),
                    "volume": round(float(c[5]), 1)
                })
            return candles
    except Exception:
        pass
    return []

def get_top10_market_data():
    print("\n📊 [Step 2] 빗썸 상위 10개 코인 데이터 수집 중...")
    url = "https://api.bithumb.com/public/ticker/ALL_KRW"
    res = requests.get(url, proxies=PROXIES, timeout=10).json()
    if res.get("status") != "0000":
        return []
        
    data = res["data"]
    raw_list = []
    for symbol, info in data.items():
        if symbol == "date": continue
        try:
            acc_val = float(info['acc_trade_value_24H'])
            raw_list.append((symbol, float(info['closing_price']), float(info['fluctate_rate_24H']), round(acc_val)))
        except:
            pass
            
    sorted_list = sorted(raw_list, key=lambda x: x[3], reverse=True)[:10]
    top10_data = []
    for symbol, price, change, volume_krw in sorted_list:
        candles_1h = get_candles(symbol, interval="1h", limit=12)
        c_1h_light = [{"c": c['close'], "h": c['high'], "l": c['low'], "v": c['volume']} for c in candles_1h]
        top10_data.append({"s": f"{symbol}/KRW", "p": price, "r": change, "c_1h": c_1h_light})
            
    return top10_data

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

def get_account_status():
    krw_free = 50000.0
    held_coins = []

    if BITHUMB_API_KEY and BITHUMB_SECRET_KEY:
        try:
            res = requests.get('https://api.bithumb.com/v1/accounts', headers=get_v2_headers(), proxies=PROXIES, timeout=10)
            if res.status_code == 200:
                accounts = res.json()
                for acc in accounts:
                    if acc['currency'] == 'KRW':
                        krw_free = float(acc['balance'])
                    elif not PAPER_TRADING and float(acc['balance']) + float(acc['locked']) > 0 and acc['currency'] != 'P':
                        eval_amount = (float(acc['balance']) + float(acc['locked'])) * float(acc.get('avg_buy_price', 0))
                        if eval_amount >= 1000:
                            held_coins.append(acc['currency'])
        except Exception as e:
            print(f"⚠️ 잔고 조회 오류: {e}")

    if PAPER_TRADING:
        paper_db = load_json_file(PAPER_TRADES_FILE, {"active_positions": {}})
        held_coins = list(paper_db.get("active_positions", {}).keys())

    calc_buy_amount = round(krw_free * BUY_RATIO) if krw_free > 0 else MIN_BUY_KRW
    final_buy_amount = max(calc_buy_amount, MIN_BUY_KRW)
    return {"krw_free": krw_free, "held_coins": held_coins, "buy_amount_krw": final_buy_amount}

# ==========================================
# 3. AI 연동 모듈 (Gemini ➔ SambaNova ➔ Groq)
# ==========================================
def call_ai_api(system_instruction, user_prompt):
    providers = []
    if GEMINI_API_KEY:
        providers.append({
            "name": "Google Gemini (3.5 Flash Lite)",
            "key": GEMINI_API_KEY,
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
            "model": "gemini-3.5-flash-lite"
        })
    if SAMBANOVA_API_KEY:
        providers.append({
            "name": "SambaNova Cloud",
            "key": SAMBANOVA_API_KEY,
            "base_url": "https://api.sambanova.ai/v1",
            "model": "Meta-Llama-3.3-70B-Instruct"
        })
    if GROQ_API_KEY3:
        providers.append({
            "name": "Groq (KEY3)",
            "key": GROQ_API_KEY3,
            "base_url": "https://api.groq.com/openai/v1",
            "model": "llama-3.3-70b-versatile"
        })
    if GROQ_API_KEY2:
        providers.append({
            "name": "Groq (KEY2)",
            "key": GROQ_API_KEY2,
            "base_url": "https://api.groq.com/openai/v1",
            "model": "llama-3.3-70b-versatile"
        })

    if not providers:
        return None

    http_client = None
    if WEBSHARE_URL:
        http_client = httpx.Client(proxy=WEBSHARE_URL, timeout=30.0)

    for prov in providers:
        try:
            client = OpenAI(base_url=prov['base_url'], api_key=prov['key'], http_client=http_client)
            enhanced_sys_prompt = system_instruction + "\nStrictly output valid JSON format ONLY."
            response = client.chat.completions.create(
                model=prov['model'],
                messages=[
                    {"role": "system", "content": enhanced_sys_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                response_format={"type": "json_object"},
                temperature=0.1
            )
            return response.choices[0].message.content
        except Exception as e:
            print(f"⚠️ [{prov['name']}] 호출 실패: {e}")
    return None

def screen_coins_2step(top10_data):
    print("🤖 [Step 3-1] 1차 AI 스크리닝: 상위 10개 종목 중 후보 3개 추리기...")
    sys_prompt_1 = "당신은 퀀트 분석가입니다. 상위 10개 코인의 1시간봉 추세를 보고 가장 강력한 우상향/돌파 모멘텀을 가진 후보 종목 3개를 추려내세요. JSON으로만 응답하세요."
    user_prompt_1 = f"상위 10개 코인 1시간봉 데이터:\n{json.dumps(top10_data, ensure_ascii=False)}\n\n[응답 포맷 (JSON)]\n{{\"top3_candidates\": [\"BTC/KRW\", \"ETH/KRW\", \"SOL/KRW\"], \"reason\": \"사유\"}}"
    
    res_1 = call_ai_api(sys_prompt_1, user_prompt_1)
    data_1 = clean_and_parse_json(res_1)
    if not data_1: return None
    candidates = data_1.get("top3_candidates", [])
    if not candidates: return None

    print("📊 [Step 3-2] 2차 정밀 분석용 5분봉 수집 중...")
    candidates_5m_data = []
    for cand_symbol in candidates:
        coin_code = cand_symbol.split('/')[0]
        candles_5m = get_candles(coin_code, interval="5m", limit=15)
        c_5m_light = [{"c": c['close'], "h": c['high'], "l": c['low'], "v": c['volume']} for c in candles_5m]
        candidates_5m_data.append({"symbol": cand_symbol, "candles_5m": c_5m_light})

    print("🤖 [Step 3-3] 2차 AI 정밀 스크리닝: 5분봉 파동 분석 및 맞춤 손익비/진입 타점 산출...")
    sys_prompt_2 = "당신은 수석 데이트레이더입니다. 후보 3개의 5분봉 파동을 분석하여 최종 1개 종목을 선정하고, 목표 진입 눌림목 할인율(entry_discount_pct, 0.2~0.8%), 손절률(-1.0%~-2.5%) 및 익절률(+2.0%~+5.0%)을 산출하세요."
    user_prompt_2 = f"후보 3개 코인 5분봉 데이터:\n{json.dumps(candidates_5m_data, ensure_ascii=False)}\n\n[응답 포맷 (JSON)]\n{{\"selected_symbol\": \"BTC/KRW\", \"confidence_score\": 88, \"entry_discount_pct\": 0.5, \"stop_loss_pct\": -1.8, \"take_profit_pct\": 3.5, \"detailed_reason\": \"5분봉 MACD 골든크로스 및 지지 확인\"}}"

    res_2 = call_ai_api(sys_prompt_2, user_prompt_2)
    result = clean_and_parse_json(res_2)
    if not result: return None

    selected_symbol = result.get("selected_symbol", "NONE")
    confidence = result.get("confidence_score", 0)
    if selected_symbol.upper() == "NONE" or confidence < MIN_CONFIDENCE_SCORE:
        print(f"⏸️ [AI 분석] 타점 미달로 관망 (신뢰도: {confidence}점)")
        return None

    entry_discount = float(result.get("entry_discount_pct", 0.5))
    sl_pct = max(min(float(result.get("stop_loss_pct", -2.0)), -1.0), -2.5)
    tp_pct = min(max(float(result.get("take_profit_pct", 3.5)), 2.0), 5.0)

    return {
        "symbol": selected_symbol,
        "confidence": confidence,
        "entry_discount_pct": entry_discount,
        "sl_pct": sl_pct,
        "tp_pct": tp_pct,
        "detailed_reason": result.get("detailed_reason", "")
    }

# ==========================================
# 4. 가격 타점 산출 및 targets.json 동기화
# ==========================================
def calculate_and_save_targets(ai_plan, buy_amount_krw):
    symbol = ai_plan['symbol']
    coin_code = symbol.split('/')[0]
    
    res = requests.get(f"https://api.bithumb.com/public/ticker/{coin_code}_KRW", proxies=PROXIES, timeout=5).json()
    curr_price = float(res['data']['closing_price'])
    
    # 지정 진입가(눌림목 감시가), 익절가, 손절가 계산
    discount_ratio = 1.0 - (ai_plan.get('entry_discount_pct', 0.5) / 100.0)
    target_entry = round(curr_price * discount_ratio, 2 if curr_price < 100 else 0)
    stop_loss = round(target_entry * (1.0 + (ai_plan['sl_pct'] / 100.0)), 2 if target_entry < 100 else 0)
    take_profit = round(target_entry * (1.0 + (ai_plan['tp_pct'] / 100.0)), 2 if target_entry < 100 else 0)

    # 깃허브 targets.json 데이터 구조화
    targets_payload = {
        "updated_at": datetime.now().isoformat(),
        "paper_trading": PAPER_TRADING,
        "targets": {
            coin_code: {
                "symbol": symbol,
                "current_price": curr_price,
                "target_entry": target_entry,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "sl_pct": ai_plan['sl_pct'],
                "tp_pct": ai_plan['tp_pct'],
                "buy_amount_krw": buy_amount_krw,
                "detailed_reason": ai_plan['detailed_reason']
            }
        }
    }

    save_json_file(TARGETS_FILE, targets_payload)

    # 텔레그램 전략 알림 발송
    tp_sign = "+" if ai_plan['tp_pct'] > 0 else ""
    plan_msg = f"""[전략 타점 갱신] - {'모의투자' if PAPER_TRADING else '실전매매'}
종목 : {symbol} (신뢰도: {ai_plan['confidence']}점)
현재가 : {curr_price:,.0f} KRW
진입 대기가 : {target_entry:,.0f} KRW (-{ai_plan.get('entry_discount_pct', 0.5)}%)

익절 목표 : {take_profit:,.0f} KRW ({tp_sign}{ai_plan['tp_pct']}%)
손절 목표 : {stop_loss:,.0f} KRW ({ai_plan['sl_pct']}%)
매수 배정 : {buy_amount_krw:,.0f} KRW

매수 근거 : {ai_plan['detailed_reason']}
=================================
⚡ 오라클 서버에서 진입 타점을 실시간 초 단위로 감시합니다."""

    send_telegram_msg(plan_msg)
    print("✅ targets.json 갱신 및 텔레그램 발송 완료!")

# ==========================================
# 메인 실행 흐름
# ==========================================
if __name__ == "__main__":
    mode_str = "🧪 모의투자(TEST)" if PAPER_TRADING else "🚨 실전매매(REAL)"
    print(f"🤖 빗썸 AI 전략 수립 엔진 시작 [{mode_str}]")

    acc_status = get_account_status()
    held_coins = acc_status['held_coins']
    buy_amount_krw = acc_status['buy_amount_krw']

    if len(held_coins) >= MAX_HOLDING_COINS:
        print(f"🛑 [보유 제한] 현재 보유 종목({len(held_coins)}개)이 최대 한도에 도달했습니다.")
        sys.exit(0)

    top10_data = get_top10_market_data()
    ai_plan = screen_coins_2step(top10_data)

    if ai_plan:
        selected_coin_code = ai_plan['symbol'].split('/')[0]
        if selected_coin_code in held_coins:
            print(f"🛑 [중복 방지] 추천 종목({ai_plan['symbol']})은 이미 보유 중입니다.")
        else:
            calculate_and_save_targets(ai_plan, buy_amount_krw)
