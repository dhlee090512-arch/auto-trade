import os
import sys
import time
import uuid
import json
import re
import math
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

# 스테이블 코인 및 고정가 토큰 (단타 대상 원천 제외)
STABLE_COINS = {"USDT", "USDC", "DAI", "TUSD", "FDUSD", "USDD", "BUSD"}

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
        print("[WARN] 텔레그램 설정 누락으로 발송 건너뜀")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"⚠️ 텔레그램 발송 오류: {e}")

# ==========================================
# 2. GitHub API 통신 및 2중 JSON 파서
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
    """표준 JSON 파싱 시도 후 실패 시 정규식 기반 2차 안전 파서 구동"""
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
    except Exception as e:
        print(f"⚠️ 표준 JSON 파싱 실패 ({e}) ➔ 정규식 2차 복구 시도")
        
    # 2차 정규식 키값 강제 추출기 (문법 깨짐 방어)
    try:
        res = {}
        sym_match = re.search(r'"selected_symbol"\s*:\s*"([^"]+)"', raw_text)
        if sym_match: res["selected_symbol"] = sym_match.group(1)
        
        conf_match = re.search(r'"confidence_score"\s*:\s*([0-9]+)', raw_text)
        if conf_match: res["confidence_score"] = int(conf_match.group(1))
        
        disc_match = re.search(r'"entry_discount_pct"\s*:\s*([0-9.]+)', raw_text)
        if disc_match: res["entry_discount_pct"] = float(disc_match.group(1))
        
        sl_match = re.search(r'"stop_loss_pct"\s*:\s*(-?[0-9.]+)', raw_text)
        if sl_match: res["stop_loss_pct"] = float(sl_match.group(1))
        
        tp_match = re.search(r'"take_profit_pct"\s*:\s*([0-9.]+)', raw_text)
        if tp_match: res["take_profit_pct"] = float(tp_match.group(1))
        
        reason_match = re.search(r'"detailed_reason"\s*:\s*"([^"]+)"', raw_text)
        if reason_match: res["detailed_reason"] = reason_match.group(1)
        
        cands_match = re.search(r'"top3_candidates"\s*:\s*\[(.*?)\]', raw_text)
        if cands_match:
            cands_raw = cands_match.group(1)
            res["top3_candidates"] = [c.strip().strip('"').strip("'") for c in cands_raw.split(",") if c.strip()]
            
        if res:
            print("✅ 정규식 기반 JSON 복구 성공!")
            return res
    except Exception as ex:
        print(f"❌ 2차 정규식 파싱도 실패: {ex}")
    return None

# ==========================================
# 3. 빗썸 API & 퀀트 피처(보조지표) 연산
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

def get_candles(coin_code, interval="5m", limit=50):
    try:
        url = f"https://api.bithumb.com/public/candlestick/{coin_code}_KRW/{interval}"
        res = requests.get(url, proxies=PROXIES, timeout=6).json()
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
    if len(closes) < period + 1:
        return 50.0
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
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 1)

def calculate_atr(candles, period=14):
    if len(candles) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        h = candles[i]['high']
        l = candles[i]['low']
        prev_c = candles[i-1]['close']
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)
    return round(sum(trs[-period:]) / period, 2)

def calculate_quant_features(candles):
    """캔들 데이터로부터 VWAP, 볼륨 폭발 배수, RSI, ATR, 추세 상태 연산"""
    if not candles:
        return {}
    closes = [c['close'] for c in candles]
    volumes = [c['volume'] for c in candles]
    
    # 1. VWAP (구간 거래량 가중평균가)
    cum_pv = sum(c['close'] * c['volume'] for c in candles)
    cum_vol = sum(volumes)
    vwap = (cum_pv / cum_vol) if cum_vol > 0 else closes[-1]
    vwap_gap_pct = round(((closes[-1] - vwap) / vwap) * 100, 2)

    # 2. 거래량 폭발 배수 (직전 20캔들 평균 대비)
    avg_vol_20 = sum(volumes[-21:-1]) / 20 if len(volumes) >= 21 else (sum(volumes[:-1]) / max(len(volumes)-1, 1))
    surge_ratio = round(volumes[-1] / avg_vol_20, 2) if avg_vol_20 > 0 else 1.0

    # 3. RSI & ATR
    rsi = calculate_rsi(closes, period=14)
    atr = calculate_atr(candles, period=14)
    atr_pct = round((atr / closes[-1]) * 100, 2) if closes[-1] > 0 else 0.0

    # 4. 이평선 배열 (5 / 20)
    sma_5 = sum(closes[-5:]) / 5
    sma_20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else sum(closes) / len(closes)
    trend_state = "정배열(Bullish)" if sma_5 >= sma_20 else "역배열(Bearish)"

    return {
        "current_price": closes[-1],
        "vwap": round(vwap, 2),
        "vwap_gap_pct": vwap_gap_pct,
        "volume_surge_ratio": surge_ratio,
        "rsi_14": rsi,
        "atr_14": atr,
        "atr_pct": atr_pct,
        "trend_state": trend_state
    }

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
    print("\n📊 [Step 1] 빗썸 상위 코인 1시간봉(24개) 수집 및 퀀트/스테이블 필터링...")
    url = "https://api.bithumb.com/public/ticker/ALL_KRW"
    res = requests.get(url, proxies=PROXIES, timeout=10).json()
    if res.get("status") != "0000":
        return []
    
    data = res["data"]
    raw_list = []
    for symbol, info in data.items():
        if symbol == "date": continue
        if symbol.upper() in STABLE_COINS:
            continue
        try:
            acc_val = float(info['acc_trade_value_24H'])
            raw_list.append((symbol, float(info['closing_price']), float(info['fluctate_rate_24H']), round(acc_val)))
        except:
            pass
            
    sorted_list = sorted(raw_list, key=lambda x: x[3], reverse=True)[:15]
    filtered_data = []
    
    for symbol, price, change, volume_krw in sorted_list:
        # 1시간봉 24개(24시간 대추세) 수집
        candles_1h = get_candles(symbol, interval="1h", limit=24)
        if len(candles_1h) < 15: continue
        
        q_feat = calculate_quant_features(candles_1h)
        
        # 1시간봉 기준 저변동성(ATR < 0.8%) 또는 역배열 하락세 제외
        if q_feat["atr_pct"] < 0.8:
            continue
        if q_feat["trend_state"] == "역배열(Bearish)" and q_feat["vwap_gap_pct"] < -2.0:
            continue
            
        c_1h_light = [{"c": c['close'], "h": c['high'], "l": c['low'], "v": c['volume']} for c in candles_1h[-12:]]
        filtered_data.append({
            "symbol": f"{symbol}/KRW",
            "price": price,
            "change_24h": change,
            "quant_metrics": q_feat,
            "candles_1h_recent": c_1h_light
        })
        if len(filtered_data) >= 10:
            break
            
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
# 4. AI 연동 및 2단계 퀀트 정밀 스크리닝
# ==========================================
def call_ai_api(system_instruction, user_prompt, step_name="AI 분석"):
    providers = []
    if GEMINI_API_KEY:
        providers.append({"name": "Gemini 2.5 Flash", "key": GEMINI_API_KEY, "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/", "model": "gemini-2.5-flash"})
    if SAMBANOVA_API_KEY:
        providers.append({"name": "SambaNova Llama-3.3-70B", "key": SAMBANOVA_API_KEY, "base_url": "https://api.sambanova.ai/v1", "model": "Meta-Llama-3.3-70B-Instruct"})
    if GROQ_API_KEY3:
        providers.append({"name": "Groq (KEY3)", "key": GROQ_API_KEY3, "base_url": "https://api.groq.com/openai/v1", "model": "llama-3.3-70b-versatile"})
    if GROQ_API_KEY2:
        providers.append({"name": "Groq (KEY2)", "key": GROQ_API_KEY2, "base_url": "https://api.groq.com/openai/v1", "model": "llama-3.3-70b-versatile"})

    if not providers:
        print("❌ [AI Error] 등록된 AI API 키가 없습니다.")
        return None

    http_client = httpx.Client(proxy=WEBSHARE_URL, timeout=35.0) if WEBSHARE_URL else None

    print("\n" + "─"*60)
    print(f"📤 [{step_name}] AI 요청 프롬프트 (Preview):")
    preview_prompt = user_prompt if len(user_prompt) <= 600 else user_prompt[:600] + " ... (생략)"
    print(preview_prompt)
    print("─"*60)

    for prov in providers:
        try:
            print(f"🤖 [{prov['name']}] 호출 시도 중...")
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
            raw_response = res.choices[0].message.content

            print(f"✅ [{prov['name']}] 응답 수신 성공!")
            print(f"📥 [{step_name}] AI 반환 원본 결과:")
            print(raw_response.strip())
            print("─"*60 + "\n")
            return raw_response

        except Exception as e:
            print(f"⚠️ [{prov['name']}] 호출 실패! ({e}) ➔ 다음 AI로 자동 폴백합니다.")

    print(f"❌ [{step_name}] 모든 AI 호출 실패.")
    return None

def screen_coins_2step(top_data):
    if not top_data:
        print("⏸️ [추세 필터] 정배열 상승 모멘텀을 가진 후보 코인이 없어 관망합니다.")
        return None, [], "추세 필터에서 우상향 정배열 종목 없음"

    print("🤖 [Step 2-1] 1차 AI 스크리닝: 주도주 및 수급 폭발 종목 선별...")
    sys_1 = (
        "You are a top-tier algorithmic quant trader specializing in intraday breakout momentum.\n"
        "Select up to 3 leading breakout candidates from the 10 provided coins based on 1-hour timeframes and technical metrics.\n\n"
        "[Rules]\n"
        "1. Prioritize Leading Momentum: Focus on coins with volume_surge_ratio >= 1.5, positive VWAP gap, and 1h EMA5 >= EMA20.\n"
        "2. Exclude low-volatility/stable coins.\n"
        "3. Standby: If no coin qualifies, return top3_candidates: [] to keep 100% cash.\n"
        "4. Output format: JSON ONLY."
    )
    user_1 = f"Market and quant data:\n{json.dumps(top_data, ensure_ascii=False)}\n\n[Expected JSON Schema]\n{{\"top3_candidates\": [\"BTC/KRW\"], \"reason\": \"한국어로 작성된 상세한 1차 선별 근거\"}}"
    
    res_1 = call_ai_api(sys_1, user_1, step_name="Step 2-1: 1차 주도주 퀀트 스크리닝")
    data_1 = clean_and_parse_json(res_1)
    if not data_1 or not data_1.get("top3_candidates"):
        print("⏸️ [1차 AI 스크리닝] 매수 적합 종목 없음 (관망)")
        return None, [], "1차 AI 스크리닝에서 우상향 모멘텀 종목 미발견"

    candidates = data_1["top3_candidates"]
    print(f"🎯 [1차 선별된 후보 종목]: {candidates}")

    print("\n📊 [Step 2-2] 2차 정밀 분석용 5분봉 40개 차트 및 지표 수집 중...")
    cand_5m_data = []
    for sym in candidates:
        code = sym.split('/')[0]
        candles_5m = get_candles(code, interval="5m", limit=40)
        if len(candles_5m) < 25: continue
        
        q_5m = calculate_quant_features(candles_5m)
        c_light = [{"c": c['close'], "h": c['high'], "l": c['low'], "v": c['volume']} for c in candles_5m]
        
        cand_5m_data.append({
            "symbol": sym,
            "quant_5m_summary": q_5m,
            "candles_5m_timeseries": c_light
        })

    print("🤖 [Step 2-3] 2차 AI 5분봉 40캔들 차트패턴/현실적 손익비 타점 산출...")
    sys_2 = (
        "You are an expert intraday scalper executing 5-minute momentum & breakout pullback trades.\n"
        "Analyze the 5-minute 40-candle series (3h 20m) and quant metrics of the candidates to output exactly 1 best trade plan or NONE.\n\n"
        "[Execution & Win-Rate Optimization Constraints]\n"
        "1. 20-Min Expiration: Set a realistic, shallow entry discount (entry_discount_pct: 0.1% to 0.4%) to catch immediate momentum and avoid adverse selection.\n"
        "2. 3-Hour Time-Exit: Must pick a coin ready to hit target within 3 hours.\n"
        "3. Realistic Scalping Targets:\n"
        "   - take_profit_pct: +1.8% to +3.0% (highly achievable within 3 hours)\n"
        "   - stop_loss_pct: -1.0% to -1.8% (placed below key 5m support/ATR buffer)\n"
        "4. Anti-Chasing: Reject coins with 5m RSI > 72 or slowing volume.\n"
        "5. detailed_reason: Provide a comprehensive, detailed technical analysis in KOREAN (covering chart pattern, volume action, support level, and indicators). It does not need to be short.\n"
        "6. If unsure or risk is high, output selected_symbol: 'NONE' immediately."
    )
    user_2 = f"5-minute 40-candle and quant data:\n{json.dumps(cand_5m_data, ensure_ascii=False)}\n\n[Expected JSON Schema]\n{{\"selected_symbol\": \"BTC/KRW\", \"confidence_score\": 85, \"entry_discount_pct\": 0.2, \"stop_loss_pct\": -1.4, \"take_profit_pct\": 2.3, \"detailed_reason\": \"차트 패턴, 거래량 추이, 지지선 및 보조지표를 종합한 상세한 한국어 매수 분석 근거\"}}"

    res_2 = call_ai_api(sys_2, user_2, step_name="Step 2-3: 2차 5분봉 40캔들 정밀 타점 산출")
    result = clean_and_parse_json(res_2)
    if not result:
        return None, candidates, "2차 AI 응답 JSON 파싱 실패"

    selected = result.get("selected_symbol", "NONE")
    confidence = result.get("confidence_score", 0)
    detailed_reason = result.get("detailed_reason", "지지선 불명확")

    print(f"🔍 [2차 분석 판정] 종목: {selected} | 신뢰도 점수: {confidence}점 | 기준 점수: {MIN_CONFIDENCE_SCORE}점")

    if selected.upper() == "NONE" or confidence < MIN_CONFIDENCE_SCORE:
        print(f"⏸️ [2차 AI 스크리닝] 신뢰도 미달 또는 관망 판정 (점수: {confidence}점)")
        return None, candidates, f"5분봉 40캔들 분석 결과 신뢰도({confidence}점) 기준 미달 또는 진입 자리 불확실 ({detailed_reason})"

    plan = {
        "symbol": selected,
        "confidence": confidence,
        "entry_discount_pct": float(result.get("entry_discount_pct", 0.2)),
        "sl_pct": max(min(float(result.get("stop_loss_pct", -1.4)), -1.0), -2.0),
        "tp_pct": min(max(float(result.get("take_profit_pct", 2.3)), 1.8), 3.5),
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

    now_iso = datetime.now().isoformat()
    targets_payload = {
        "updated_at": now_iso,
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
                "detailed_reason": ai_plan['detailed_reason'],
                "created_at": now_iso
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

매수 근거 :
{ai_plan['detailed_reason']}
=================================
⚡ 규칙: 20분 내 미체결 시 자동 취소 / 체결 후 3시간 미도달 시 시장가 청산"""

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
