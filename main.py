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
PAPER_TRADING = True         # 🧪 모의투자 (True: 시뮬레이션 / False: 실전 매매)
MIN_CONFIDENCE_SCORE = 75    # 🎯 매수 최소 신뢰도 기준
MAX_HOLDING_COINS = 3        # 🛡️ 최대 보유 가능 종목 수
MIN_BUY_KRW = 6000           # 💵 최소 매수 금액 (빗썸 5천원 + 안전마진)
BUY_RATIO = 0.20             # 📊 가용 잔고의 20%
TIME_EXIT_HOURS = 3          # ⏰ 시간 손절 기준

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

GITHUB_TOKEN = get_env("GH_TOKEN2") or get_env("GH_TOKEN") or get_env("GITHUB_TOKEN")

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

# ==========================================
# 1. 텔레그램 유틸리티
# ==========================================
def send_telegram_msg(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] 텔레그램 설정 누락")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"⚠️ 텔레그램 발송 오류: {e}")

# ==========================================
# 2. GitHub API 통신 모듈
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
            return json.loads(content), data['sha']
    except Exception as e:
        print(f"⚠️ GitHub 읽기 오류 ({file_path}): {e}")
    return None, None

def save_github_file(file_path, content_data):
    if not GITHUB_TOKEN:
        print("💡 [알림] GITHUB_TOKEN 없음. 로컬 파일만 저장합니다.")
        return
    url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/contents/{file_path}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }
    _, sha = get_github_file_info(file_path)
    json_str = json.dumps(content_data, indent=2, ensure_ascii=False)
    encoded = base64.b64encode(json_str.encode('utf-8')).decode('utf-8')
    payload = {"message": f"update: {file_path} via API", "content": encoded}
    if sha:
        payload["sha"] = sha
    try:
        res = requests.put(url, headers=headers, json=payload, timeout=10)
        if res.status_code in [200, 201]:
            print(f"🚀 [GitHub API] '{file_path}' 업로드 완료")
    except Exception as e:
        print(f"❌ [GitHub API] 업로드 에러: {e}")

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
                return json.loads(target_str)
            except Exception:
                pass
            sanitized = re.sub(r'[\r\n\t]+', ' ', target_str)
            end_idx = sanitized.rfind('}')
            if end_idx != -1:
                return json.loads(sanitized[:end_idx+1])
        return json.loads(cleaned)
    except Exception:
        return None

# ==========================================
# 3. 빗썸 API & 시장 데이터 수집
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
        q_str = urllib.parse.urlencode(query_params).encode()
        m = sha512()
        m.update(q_str)
        payload['query_hash'] = m.hexdigest()
        payload['query_hash_alg'] = 'SHA512'
    jwt_token = jwt.encode(payload, BITHUMB_SECRET_KEY)
    return {'Authorization': f'Bearer {jwt_token}', 'Content-Type': 'application/json'}

def get_candles(coin_code, interval="5m", limit=30):
    try:
        url = f"https://api.bithumb.com/public/candlestick/{coin_code}_KRW/{interval}"
        res = requests.get(url, proxies=PROXIES, timeout=5).json()
        if res.get("status") == "0000":
            return [{
                "timestamp": int(c[0]),
                "open": float(c[1]),
                "close": float(c[2]),
                "high": float(c[3]),
                "low": float(c[4]),
                "volume": round(float(c[5]), 1)
            } for c in res['data'][-limit:]]
    except Exception:
        pass
    return []

def check_btc_macro_trend():
    """비트코인(BTC) 기준 시장 대추세 필터 (급락장 매수 방어)"""
    try:
        url = "https://api.bithumb.com/public/ticker/BTC_KRW"
        res = requests.get(url, proxies=PROXIES, timeout=5).json()
        if res.get("status") == "0000":
            btc_change = float(res['data']['fluctate_rate_24H'])
            if btc_change <= -2.5:
                print(f"⚠️ [시장 매크로 필터] BTC 급락 중 ({btc_change:.2f}%). 매수를 전면 차단합니다.")
                return False, btc_change
            return True, btc_change
    except Exception as e:
        print(f"BTC 시세 확인 오류: {e}")
    return True, 0.0

def get_top10_market_data():
    print("\n📊 [Step 1] 빗썸 상위 10개 코인 데이터 수집 및 추세 필터링...")
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
    filtered_data = []
    
    for symbol, price, change, volume_krw in sorted_list:
        candles_1h = get_candles(symbol, interval="1h", limit=12)
        if not candles_1h: continue
        
        # 1시간봉 기준 역배열 하락세 종목은 1차 필터링
        closes = [c['close'] for c in candles_1h]
        sma_short = sum(closes[-3:]) / 3
        sma_long = sum(closes) / len(closes)
        if sma_short < sma_long * 0.985:
            continue
            
        c_1h_light = [{"c": c['close'], "h": c['high'], "l": c['low'], "v": c['volume']} for c in candles_1h]
        filtered_data.append({"s": f"{symbol}/KRW", "p": price, "r": change, "c_1h": c_1h_light})
            
    return filtered_data

def get_account_status():
    krw_free = 50000.0
    if BITHUMB_API_KEY and BITHUMB_SECRET_KEY:
        try:
            res = requests.get('https://api.bithumb.com/v1/accounts', headers=get_v2_headers(), proxies=PROXIES, timeout=10)
            if res.status_code == 200:
                for acc in res.json():
                    if acc['currency'] == 'KRW':
                        krw_free = float(acc['balance'])
        except Exception:
            pass
    calc_buy = round(krw_free * BUY_RATIO) if krw_free > 0 else MIN_BUY_KRW
    return max(calc_buy, MIN_BUY_KRW)

# ==========================================
# 4. AI 연동 및 2단계 정밀 스크리닝
# ==========================================
def call_ai_api(system_instruction, user_prompt):
    providers = []
    if GEMINI_API_KEY:
        providers.append({"name": "Gemini", "key": GEMINI_API_KEY, "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/", "model": "gemini-2.5-flash"})
    if SAMBANOVA_API_KEY:
        providers.append({"name": "SambaNova", "key": SAMBANOVA_API_KEY, "base_url": "https://api.sambanova.ai/v1", "model": "Meta-Llama-3.3-70B-Instruct"})
    if GROQ_API_KEY3:
        providers.append({"name": "Groq3", "key": GROQ_API_KEY3, "base_url": "https://api.groq.com/openai/v1", "model": "llama-3.3-70b-versatile"})
    if GROQ_API_KEY2:
        providers.append({"name": "Groq2", "key": GROQ_API_KEY2, "base_url": "https://api.groq.com/openai/v1", "model": "llama-3.3-70b-versatile"})

    if not providers: return None
    http_client = httpx.Client(proxy=WEBSHARE_URL, timeout=30.0) if WEBSHARE_URL else None

    for prov in providers:
        try:
            client = OpenAI(base_url=prov['base_url'], api_key=prov['key'], http_client=http_client)
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
        except Exception:
            pass
    return None

def screen_coins_2step(top_data):
    if not top_data:
        print("⏸️ [추세 필터] 정배열 상승 모멘텀을 가진 후보 코인이 없어 관망합니다.")
        return None, [], "추세 필터에서 우상향 정배열 종목 없음"

    print("🤖 [Step 2-1] 1차 AI 스크리닝: 우상향 돌파 후보 선별...")
    sys_1 = "당신은 보수적 퀀트입니다. 1시간봉(c_1h)이 명확한 우상향/골든크로스인 종목만 최대 3개 선별하세요. 마땅한 상승세가 없으면 top3_candidates를 빈 배열 []로 반환하세요."
    user_1 = f"데이터:\n{json.dumps(top_data, ensure_ascii=False)}\n\n[응답 (JSON)]\n{{\"top3_candidates\": [\"BTC/KRW\"], \"reason\": \"이유\"}}"
    
    res_1 = call_ai_api(sys_1, user_1)
    data_1 = clean_and_parse_json(res_1)
    if not data_1 or not data_1.get("top3_candidates"):
        print("⏸️ [1차 AI 스크리닝] 매수 적합 종목 없음 (관망)")
        return None, [], "1차 AI 스크리닝에서 우상향 모멘텀 종목 미발견"

    candidates = data_1["top3_candidates"]
    print(f"🎯 [1차 후보] {candidates}")

    print("📊 [Step 2-2] 2차 정밀 분석용 5분봉 수집 중...")
    cand_5m_data = []
    for sym in candidates:
        code = sym.split('/')[0]
        candles_5m = get_candles(code, interval="5m", limit=15)
        c_light = [{"c": c['close'], "h": c['high'], "l": c['low'], "v": c['volume']} for c in candles_5m]
        cand_5m_data.append({"symbol": sym, "candles_5m": c_light})

    print("🤖 [Step 2-3] 2차 AI 정밀 파동 분석 및 손익비 산출...")
    sys_2 = "당신은 수석 데이트레이더입니다. 5분봉 파동을 분석하여 최종 1개 종목을 선정하고 할인 진입율(entry_discount_pct: 0.2~0.8%), 손절률(-1.0%~-2.5%), 익절률(+2.0%~+5.0%)을 산출하세요. 진입 자리가 불확실하거나 하락 반전 위험이 있으면 selected_symbol을 'NONE'으로 반환하세요."
    user_2 = f"5분봉 데이터:\n{json.dumps(cand_5m_data, ensure_ascii=False)}\n\n[응답 (JSON)]\n{{\"selected_symbol\": \"BTC/KRW\", \"confidence_score\": 85, \"entry_discount_pct\": 0.5, \"stop_loss_pct\": -1.8, \"take_profit_pct\": 3.5, \"detailed_reason\": \"근거\"}}"

    res_2 = call_ai_api(sys_2, user_2)
    result = clean_and_parse_json(res_2)
    if not result:
        return None, candidates, "2차 AI 응답 JSON 파싱 실패"

    selected = result.get("selected_symbol", "NONE")
    confidence = result.get("confidence_score", 0)
    detailed_reason = result.get("detailed_reason", "지지선 불명확")

    if selected.upper() == "NONE" or confidence < MIN_CONFIDENCE_SCORE:
        print(f"⏸️ [2차 AI 스크리닝] 신뢰도 미달 또는 관망 판정 (점수: {confidence}점)")
        return None, candidates, f"5분봉 분석 결과 신뢰도({confidence}점) 기준 미달 또는 진입 자리 불확실 ({detailed_reason})"

    plan = {
        "symbol": selected,
        "confidence": confidence,
        "entry_discount_pct": float(result.get("entry_discount_pct", 0.5)),
        "sl_pct": max(min(float(result.get("stop_loss_pct", -2.0)), -1.0), -2.5),
        "tp_pct": min(max(float(result.get("take_profit_pct", 3.5)), 2.0), 5.0),
        "detailed_reason": detailed_reason
    }
    return plan, candidates, "타점 도출 성공"

# ==========================================
# 5. 타점 산출 및 targets.json 생성
# ==========================================
def calculate_and_save_targets(ai_plan, buy_amount_krw):
    symbol = ai_plan['symbol']
    coin_code = symbol.split('/')[0]
    
    res = requests.get(f"https://api.bithumb.com/public/ticker/{coin_code}_KRW", proxies=PROXIES, timeout=5).json()
    curr_price = float(res['data']['closing_price'])
    
    discount_ratio = 1.0 - (ai_plan['entry_discount_pct'] / 100.0)
    target_entry = round(curr_price * discount_ratio, 2 if curr_price < 100 else 0)
    stop_loss = round(target_entry * (1.0 + (ai_plan['sl_pct'] / 100.0)), 2 if target_entry < 100 else 0)
    take_profit = round(target_entry * (1.0 + (ai_plan['tp_pct'] / 100.0)), 2 if target_entry < 100 else 0)

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

    tp_sign = "+" if ai_plan['tp_pct'] > 0 else ""
    plan_msg = f"""[전략 타점 갱신] - {'모의투자' if PAPER_TRADING else '실전매매'}
종목 : {symbol} (신뢰도: {ai_plan['confidence']}점)
현재가 : {curr_price:,.0f} KRW
진입 대기가 : {target_entry:,.0f} KRW (-{ai_plan['entry_discount_pct']}%)

익절 목표 : {take_profit:,.0f} KRW ({tp_sign}{ai_plan['tp_pct']}%)
손절 목표 : {stop_loss:,.0f} KRW ({ai_plan['sl_pct']}%)
매수 배정 : {buy_amount_krw:,.0f} KRW

매수 근거 : {ai_plan['detailed_reason']}
=================================
⚡ 오라클 서버에서 진입 타점을 실시간 초 단위로 감시합니다."""

    send_telegram_msg(plan_msg)
    print("✅ targets.json 생성 및 텔레그램 발송 완료!")

# ==========================================
# 메인 실행 흐름
# ==========================================
if __name__ == "__main__":
    mode_str = "🧪 모의투자(TEST)" if PAPER_TRADING else "🚨 실전매매(REAL)"
    kst_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"🤖 빗썸 AI 전략 수립 엔진 시작 [{mode_str}]")

    # 1. 비트코인 시장 매크로 점검 (급락장 방어)
    btc_ok, btc_rate = check_btc_macro_trend()
    if not btc_ok:
        hold_msg = f"""🛡️ [하락장 방어 작동] - {mode_str}
• 점검 시각 : {kst_now}
• BTC 24H 변동률 : {btc_rate:.2f}% (급락세 감지)

• 판정 결과 : 전면 매수 차단 및 현금 관망
• 사유 : 비트코인 급락으로 인한 시장 전체 리스크 회피
=================================
🛡️ 안전을 위해 신규 매수 진입을 중단하고 대기합니다."""
        send_telegram_msg(hold_msg)
        sys.exit(0)

    # 2. 주문 가능 금액 산출
    buy_amount_krw = get_account_status()

    # 3. 2단계 AI 스크리닝 및 타점 산출
    top_data = get_top10_market_data()
    ai_plan, candidates, reason = screen_coins_2step(top_data)

    if ai_plan:
        calculate_and_save_targets(ai_plan, buy_amount_krw)
    else:
        # ⏸️ 관망 판정 시 텔레그램 상세 브리핑 발송
        cand_str = ", ".join(candidates) if candidates else "1차 부적합"
        hold_msg = f"""⏸️ [전략 분석 결과 - 현금 관망]
• 분석 시각 : {kst_now}
• 운영 모드 : {mode_str}

• 검토 후보 : {cand_str}
• 판정 결과 : 진입 타점 미달 (관망 유지)
• 판단 사유 : {reason}
=================================
🛡️ 무리한 진입을 방지하고 다음 분석 주기까지 안전하게 대기합니다."""
        send_telegram_msg(hold_msg)
        print("⏸️ 적합한 매수 타점이 없어 현금 관망 리포트를 전송했습니다.")
