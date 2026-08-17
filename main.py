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
MIN_CONFIDENCE_SCORE = 65    # 🎯 매수 최소 신뢰도 (횡보장 활성화를 위해 65점으로 완화)
MAX_HOLDING_COINS = 3        # 🛡️ 최대 보유 가능 종목 수
MIN_BUY_KRW = 6000           # 💵 최소 매수 금액
BUY_RATIO = 0.20             # 📊 가용 잔고의 20%
TIME_EXIT_HOURS = 3          # ⏰ 시간 손절 기준

# 스테이블 코인 (단타 대상 원천 제외)
STABLE_COINS = {"USDT", "USDC", "DAI", "TUSD", "FDUSD", "USDD", "BUSD"}

TARGETS_FILE = "targets.json"
GITHUB_REPOSITORY = "dhlee090512-arch/auto-trade"

def get_env(key_name):
    val = None
    try:
        from google.colab import userdata
        val = userdata.get(key_name)
    except Exception:
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
# 2. GitHub API 통신 및 디버그 강화형 JSON 파서
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

def clean_and_parse_json(raw_text, step_name="AI 분석"):
    if raw_text is None:
        print(f"❌ [디버그 원인] AI API 반환값이 None입니다.")
        return None
    
    cleaned = raw_text.strip()
    if "```json" in cleaned:
        cleaned = cleaned.split("```json")[1].split("```")[0].strip()
    elif "```" in cleaned:
        cleaned = cleaned.split("```")[1].split("```")[0].strip()

    try:
        return json.loads(cleaned)
    except Exception:
        pass

    # 중괄호 슬라이싱
    start_idx = cleaned.find('{')
    end_idx = cleaned.rfind('}')
    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
        try:
            return json.loads(cleaned[start_idx:end_idx+1])
        except Exception:
            pass

    # 정규식 긴급 추출
    res = {}
    try:
        sym_match = re.search(r'["\']selected_symbol["\']\s*:\s*["\']([^"\']+)["\']', raw_text)
        if sym_match: res["selected_symbol"] = sym_match.group(1)

        conf_match = re.search(r'["\']confidence_score["\']\s*:\s*([0-9]+)', raw_text)
        if conf_match: res["confidence_score"] = int(conf_match.group(1))

        disc_match = re.search(r'["\']entry_discount_pct["\']\s*:\s*([0-9.]+)', raw_text)
        if disc_match: res["entry_discount_pct"] = float(disc_match.group(1))

        sl_match = re.search(r'["\']stop_loss_pct["\']\s*:\s*(-?[0-9.]+)', raw_text)
        if sl_match: res["stop_loss_pct"] = float(sl_match.group(1))

        tp_match = re.search(r'["\']take_profit_pct["\']\s*:\s*([0-9.]+)', raw_text)
        if tp_match: res["take_profit_pct"] = float(tp_match.group(1))

        reason_match = re.search(r'["\']detailed_reason["\']\s*:\s*["\']([^"\']+)["\']', raw_text)
        if reason_match: res["detailed_reason"] = reason_match.group(1)

        cands_match = re.search(r'["\']top3_candidates["\']\s*:\s*\[(.*?)\]', raw_text, re.DOTALL)
        if cands_match:
            cands_raw = cands_match.group(1)
            res["top3_candidates"] = [c.strip().strip('"').strip("'") for c in cands_raw.split(",") if c.strip()]

        if "selected_symbol" in res or "top3_candidates" in res:
            return res
    except Exception:
        pass

    return None

# ==========================================
# 3. 빗썸 API & 퀀트 피처 연산
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
    if not candles:
        return {}
    closes = [c['close'] for c in candles]
    volumes = [c['volume'] for c in candles]
    
    # 1. VWAP
    cum_pv = sum(c['close'] * c['volume'] for c in candles)
    cum_vol = sum(volumes)
    vwap = (cum_pv / cum_vol) if cum_vol > 0 else closes[-1]
    vwap_gap_pct = round(((closes[-1] - vwap) / vwap) * 100, 2)

    # 2. 거래량 비율
    avg_vol_20 = sum(volumes[-21:-1]) / 20 if len(volumes) >= 21 else (sum(volumes[:-1]) / max(len(volumes)-1, 1))
    surge_ratio = round(volumes[-1] / avg_vol_20, 2) if avg_vol_20 > 0 else 1.0

    # 3. RSI & ATR
    rsi = calculate_rsi(closes, period=14)
    atr = calculate_atr(candles, period=14)
    atr_pct = round((atr / closes[-1]) * 100, 2) if closes[-1] > 0 else 0.0

    # 4. 이평선 상태
    sma_5 = sum(closes[-5:]) / 5
    sma_20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else sum(closes) / len(closes)
    trend_state = "정배열(Bullish)" if sma_5 >= sma_20 else "역배열/박스권(Range)"

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
    """비트코인(BTC) 기준 시장 대추세 필터 (급락장만 방어: -3.0%)"""
    try:
        url = "https://api.bithumb.com/public/ticker/BTC_KRW"
        res = requests.get(url, proxies=PROXIES, timeout=5).json()
        if res.get("status") == "0000":
            btc_change = float(res['data']['fluctate_rate_24H'])
            if btc_change <= -3.0:
                print(f"⚠️ [시장 매크로 필터] BTC 급락 중 ({btc_change:.2f}%). 매수를 전면 차단합니다.")
                return False, btc_change
            return True, btc_change
    except Exception as e:
        print(f"BTC 시세 확인 오류: {e}")
    return True, 0.0

def get_top10_market_data():
    print("\n📊 [Step 1] 빗썸 상위 코인 1시간봉(24개) 수집 및 하이브리드 필터링...")
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
        except Exception:
            pass
            
    sorted_list = sorted(raw_list, key=lambda x: x[3], reverse=True)[:15]
    filtered_data = []
    
    for symbol, price, change, volume_krw in sorted_list:
        candles_1h = get_candles(symbol, interval="1h", limit=24)
        if len(candles_1h) < 15: continue
        
        q_feat = calculate_quant_features(candles_1h)
        
        # 횡보장 허용을 위해 필터 완화 (ATR 0.5% 이상, VWAP 이격도 -3.5% 이내)
        if q_feat["atr_pct"] < 0.5:
            continue
        if q_feat["vwap_gap_pct"] < -3.5:
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
# 4. AI 연동 및 하이브리드 단타 스크리닝
# ==========================================
def call_ai_api(system_instruction, user_prompt, step_name="AI 분석"):
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
    if GROQ_API_KEY3:
        providers.append({
            "name": "Groq SpecDec (KEY3)",
            "key": GROQ_API_KEY3,
            "base_url": "https://api.groq.com/openai/v1",
            "model": "llama-3.3-70b-specdec"
        })
    if GROQ_API_KEY2:
        providers.append({
            "name": "Groq SpecDec (KEY2)",
            "key": GROQ_API_KEY2,
            "base_url": "https://api.groq.com/openai/v1",
            "model": "llama-3.3-70b-specdec"
        })

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
            print(f"🤖 [{prov['name']}] 호출 시도 중 (모델: {prov['model']})...")
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
            print(f"✅ [{prov['name']}] 응답 수신 성공! (길이: {len(raw_response)}자)")
            return raw_response

        except Exception as e:
            print(f"⚠️ [{prov['name']}] 호출 실패! ({e}) ➔ 다음 AI로 자동 폴백합니다.")

    print(f"❌ [{step_name}] 모든 AI 호출 실패.")
    return None

def screen_coins_2step(top_data):
    if not top_data:
        print("⏸️ [필터] 적합한 후보 코인이 없어 관망합니다.")
        return None, [], "후보 종목 없음"

    print("🤖 [Step 2-1] 1차 AI 스크리닝: 상승 모멘텀 또는 박스권 반등 후보 선별...")
    sys_1 = (
        "You are an intraday quant trader. Select up to 3 candidates from the provided 10 coins based on 1-hour timeframes.\n\n"
        "[Hybrid Strategy Rules]\n"
        "1. Trending Setup: Strong volume surge (>= 1.2x) with price above VWAP.\n"
        "2. Range/Sideways Setup: Box-range bottom support with RSI between 40 and 60 ready for mean-reversion tick gain.\n"
        "3. Exclude dead volume/stable coins.\n"
        "4. Standby: Only return [] if ALL coins are in a free-fall severe downtrend."
    )
    user_1 = f"Market data:\n{json.dumps(top_data, ensure_ascii=False)}\n\n[Expected JSON Schema]\n{{\"top3_candidates\": [\"BTC/KRW\"], \"reason\": \"상세한 한국어 선별 근거\"}}"
    
    res_1 = call_ai_api(sys_1, user_1, step_name="Step 2-1: 1차 하이브리드 스크리닝")
    data_1 = clean_and_parse_json(res_1, step_name="Step 2-1: 1차 스크리닝")
    if not data_1 or not data_1.get("top3_candidates"):
        print("⏸️ [1차 AI 스크리닝] 매수 적합 종목 없음 (관망)")
        return None, [], "1차 AI 스크리닝에서 적합 종목 미발견"

    candidates = data_1["top3_candidates"]
    print(f"🎯 [1차 선별된 후보 종목]: {candidates}")

    print("\n📊 [Step 2-2] 2차 분석용 5분봉 40개 수집 중...")
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

    print("🤖 [Step 2-3] 2차 AI 5분봉 타점 산출 (상승 돌파 또는 박스권 틱 수익)...")
    sys_2 = (
        "You are an active intraday scalper. Analyze the 5-minute 40-candle series to output exactly 1 trade plan or NONE.\n\n"
        "[Scalping Constraints for Active Trading]\n"
        "1. Entry: Set immediate realistic entry (entry_discount_pct: 0.0% to 0.3%) near current price.\n"
        "2. Realistic Targets (Take quick profit in sideways/trending markets):\n"
        "   - take_profit_pct: +1.2% to +2.5%\n"
        "   - stop_loss_pct: -0.8% to -1.4%\n"
        "3. Confidence Score: Score from 0 to 100 based on setup clarity.\n"
        "4. detailed_reason: Comprehensive Korean explanation of the chart pattern/indicators.\n"
        "5. Output raw JSON ONLY."
    )
    user_2 = f"5m data:\n{json.dumps(cand_5m_data, ensure_ascii=False)}\n\n[Expected JSON Schema]\n{{\"selected_symbol\": \"BTC/KRW\", \"confidence_score\": 75, \"entry_discount_pct\": 0.1, \"stop_loss_pct\": -1.0, \"take_profit_pct\": 1.8, \"detailed_reason\": \"차트 패턴 및 지표 분석 근거\"}}"

    res_2 = call_ai_api(sys_2, user_2, step_name="Step 2-3: 2차 정밀 타점 산출")
    result = clean_and_parse_json(res_2, step_name="Step 2-3: 2차 타점 산출")
    if not result:
        return None, candidates, "2차 AI 응답 JSON 파싱 실패"

    selected = result.get("selected_symbol", "NONE")
    confidence = result.get("confidence_score", 0)
    detailed_reason = result.get("detailed_reason", "지지선 불명확")

    print(f"🔍 [2차 분석 판정] 종목: {selected} | 신뢰도: {confidence}점 | 기준: {MIN_CONFIDENCE_SCORE}점")

    if selected.upper() == "NONE" or confidence < MIN_CONFIDENCE_SCORE:
        print(f"⏸️ [2차 AI 스크리닝] 신뢰도 미달 (점수: {confidence}점)")
        return None, candidates, f"5분봉 분석 결과 신뢰도({confidence}점) 기준 미달 ({detailed_reason})"

    plan = {
        "symbol": selected,
        "confidence": confidence,
        "entry_discount_pct": float(result.get("entry_discount_pct", 0.1)),
        "sl_pct": max(min(float(result.get("stop_loss_pct", -1.0)), -0.8), -1.8),
        "tp_pct": min(max(float(result.get("take_profit_pct", 1.8)), 1.2), 3.0),
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

    buy_amount_krw = get_account_status()
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
