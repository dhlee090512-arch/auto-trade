import os
import sys
import time
import uuid
import json
import jwt
import requests
import pandas as pd
from datetime import datetime, timedelta
from openai import OpenAI

# ==========================================
# 0. 설정 및 환경변수(Secrets) 수집
# ==========================================
PAPER_TRADING = True         # 🧪 모의투자 (True: 모의투자 정밀 추적 / False: 실전 매매)
MIN_CONFIDENCE_SCORE = 75 # 🎯 매수 최소 신뢰도 기준
MAX_HOLDING_COINS = 3     # 🛡️ 최대 보유 가능 종목 수
MIN_BUY_KRW = 6000        # 💵 최소 매수 금액 (빗썸 5천원 + 안전마진)
BUY_RATIO = 0.20          # 📊 가용 잔고의 20%
TIME_EXIT_HOURS = 3       # ⏰ 시간 손절 (3시간 횡보 시 시장가 청산)

POSITIONS_FILE = "positions.json"       # 실전 진입 기록
PAPER_TRADES_FILE = "paper_trades.json" # 모의투자 정밀 기록

def get_env(key_name):
    return os.getenv(key_name, "").strip()

# 🎯 API 키 이중화 수집 (1순위: KEY3, 2순위: KEY2)
GROQ_API_KEY3 = get_env("GROQ_API_KEY3")
GROQ_API_KEY2 = get_env("GROQ_API_KEY2")

BITHUMB_API_KEY = get_env("BITHUMB_API_KEY")
BITHUMB_SECRET_KEY = get_env("BITHUMB_SECRET_KEY")
WEBSHARE_URL = get_env("WEBSHARE_URL")
TELEGRAM_BOT_TOKEN = get_env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = get_env("TELEGRAM_CHAT_ID")

PROXIES = {'http': WEBSHARE_URL, 'https': WEBSHARE_URL} if WEBSHARE_URL else None


# ==========================================
# 텔레그램 알림 발송 전용 모듈
# ==========================================
def send_telegram_msg(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] 텔레그램 토큰/Chat ID 미설정으로 알림 전송을 건너뜁니다.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    try:
        res = requests.post(url, json=payload, timeout=10)
        if res.status_code == 200:
            print("📲 텔레그램 알림 전송 완료!")
        else:
            print(f"⚠️ 텔레그램 알림 발송 실패 (HTTP {res.status_code}): {res.text}")
    except Exception as e:
        print(f"⚠️ 텔레그램 알림 오류: {e}")


# ==========================================
# 1. 파일 데이터 관리
# ==========================================
def load_json_file(file_path, default_value):
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


# ==========================================
# 2. 빗썸 V2 API 통신 및 차트 데이터 수집
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
    except Exception as e:
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


# ==========================================
# 🧪 [정밀 모의투자 엔진] 캔들 파동 시간순 검증 및 통계
# ==========================================
def track_paper_trading_performance():
    print("\n" + "="*50)
    print("🧪 [모의투자 정밀 파동 검증 및 성과 추적 중]")
    print("="*50)
    
    paper_db = load_json_file(PAPER_TRADES_FILE, {"active_positions": {}, "closed_trades": []})
    active_positions = paper_db.get("active_positions", {})
    closed_trades = paper_db.get("closed_trades", [])
    
    if not active_positions:
        print("💡 현재 진행 중인 모의투자 포지션이 없습니다.")
        print_paper_trading_summary(closed_trades)
        print("="*50 + "\n")
        return

    now = datetime.now()
    closed_keys = []

    for coin_code, pos in active_positions.items():
        symbol = pos['symbol']
        entry_price = pos['entry_price']
        sl_price = pos['stop_loss']
        tp_price = pos['take_profit']
        buy_amount_krw = pos['buy_amount_krw']
        entry_time = datetime.fromisoformat(pos['entry_time'])
        
        candles_5m = get_candles(coin_code, interval="5m", limit=36)
        
        status = "HOLDING"
        exit_price = 0.0
        exit_reason = ""
        
        for c in candles_5m:
            c_time = datetime.fromtimestamp(c['timestamp'] / 1000.0)
            if c_time < entry_time - timedelta(minutes=5):
                continue
                
            c_high = c['high']
            c_low = c['low']
            
            # 동시 도달 시 보수적 손절 처리
            if c_low <= sl_price and c_high >= tp_price:
                status = "CLOSED_STOP_LOSS"
                exit_price = sl_price
                exit_reason = "동일 봉 내 변동성 폭발 (보수적 손절 처리)"
                break
                
            if c_low <= sl_price:
                status = "CLOSED_STOP_LOSS"
                exit_price = sl_price
                exit_reason = "하락 파동 중 스탑로스 먼저 체결 (손절)"
                break
                
            if c_high >= tp_price:
                status = "CLOSED_TAKE_PROFIT"
                exit_price = tp_price
                exit_reason = "상승 파동 중 목표가 먼저 체결 (익절)"
                break

        if status == "HOLDING" and (now - entry_time >= timedelta(hours=TIME_EXIT_HOURS)):
            status = "CLOSED_TIME_EXIT"
            curr_url = f"https://api.bithumb.com/public/ticker/{coin_code}_KRW"
            res = requests.get(curr_url, proxies=PROXIES, timeout=5).json()
            exit_price = float(res['data']['closing_price'])
            exit_reason = "3시간 이상 목표가/손절가 미도달로 시간 손절(시장가 청산)"

        if status != "HOLDING":
            closed_keys.append(coin_code)
            profit_pct = round(((exit_price - entry_price) / entry_price) * 100, 2)
            profit_krw = round(buy_amount_krw * (profit_pct / 100.0))
            
            trade_record = {
                "symbol": symbol,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "buy_amount_krw": buy_amount_krw,
                "profit_krw": profit_krw,
                "profit_pct": profit_pct,
                "status": status,
                "reason": exit_reason,
                "entry_time": pos['entry_time'],
                "exit_time": now.isoformat()
            }
            closed_trades.append(trade_record)
            
            status_tag = "🎯 익절 성공" if profit_pct > 0 else "🛡️ 손절 청산"
            msg = f"""🧪 <b>[모의투자 청산 완료 - {status_tag}]</b>

📌 <b>종목</b>: <code>{symbol}</code>
📈 <b>진입가</b>: <code>{entry_price:,.0f} KRW</code>
📉 <b>청산가</b>: <code>{exit_price:,.0f} KRW</code>
💵 <b>손익금</b>: <code>{profit_krw:+,.0f} KRW</code> (<b>{profit_pct:+.2f}%</b>)
⏳ <b>사유</b>: {exit_reason}"""
            send_telegram_msg(msg)

    for k in closed_keys:
        del active_positions[k]
        
    paper_db["active_positions"] = active_positions
    paper_db["closed_trades"] = closed_trades
    save_json_file(PAPER_TRADES_FILE, paper_db)
    
    print_paper_trading_summary(closed_trades)
    print("="*50 + "\n")

def print_paper_trading_summary(closed_trades):
    total_trades = len(closed_trades)
    if total_trades == 0:
        print("📊 [모의투자 누적 통계] 완료된 거래 기록이 아직 없습니다.")
        return

    wins = [t for t in closed_trades if t['profit_pct'] > 0]
    losses = [t for t in closed_trades if t['profit_pct'] <= 0]
    
    win_count = len(wins)
    loss_count = len(losses)
    win_rate = round((win_count / total_trades) * 100, 1)
    
    total_profit_krw = sum(t['profit_krw'] for t in closed_trades)
    total_profit_pct = round(sum(t['profit_pct'] for t in closed_trades), 2)

    print(f"📊 [모의투자 누적 성과 리포트]")
    print(f" ├─ 총 완료 거래: {total_trades}회 ({win_count}승 {loss_count}패)")
    print(f" ├─ 승률(Win Rate): {win_rate}%")
    print(f" ├─ 누적 손익금: {total_profit_krw:+,.0f} KRW")
    print(f" └─ 누적 수익률합: {total_profit_pct:+.2f}%")


# ==========================================
# [Step 1] 실전 계좌 정돈
# ==========================================
def manage_unfilled_and_time_exits():
    print("\n" + "="*50)
    print("🛠️ [Step 1] 실전 계좌 정돈 (미체결 취소 & 시간 손절 검사)")
    print("="*50)
    
    if PAPER_TRADING or not BITHUMB_API_KEY:
        print("💡 모의투자 실행 중이므로 실전 거래소 정돈 통신을 건너뜁니다.")
        print("="*50 + "\n")
        return

    positions = load_json_file(POSITIONS_FILE, {})
    now = datetime.now()

    try:
        url = 'https://api.bithumb.com/v1/orders/open'
        res = requests.get(url, headers=get_v2_headers(), proxies=PROXIES, timeout=10)
        if res.status_code == 200:
            open_orders = res.json()
            for ord_info in open_orders:
                created_at_str = ord_info.get('created_at', '')
                try:
                    created_at = datetime.fromisoformat(created_at_str.replace('Z', '+00:00')).replace(tzinfo=None)
                    if now - created_at > timedelta(minutes=15):
                        c_params = {'uuid': ord_info['uuid']}
                        requests.delete('https://api.bithumb.com/v1/order', params=c_params, headers=get_v2_headers(c_params), proxies=PROXIES, timeout=10)
                        print(f" 🧹 15분 초과 미체결 주문 취소: {ord_info['market']}")
                except:
                    pass
    except Exception as e:
        print(f" ⚠️ 미체결 주문 조회 오류: {e}")

    try:
        acc_res = requests.get('https://api.bithumb.com/v1/accounts', headers=get_v2_headers(), proxies=PROXIES, timeout=10).json()
        for acc in acc_res:
            currency = acc['currency']
            total_vol = float(acc['balance']) + float(acc['locked'])
            if currency != 'KRW' and total_vol > 0:
                buy_time_str = positions.get(currency)
                if buy_time_str and (now - datetime.fromisoformat(buy_time_str) >= timedelta(hours=TIME_EXIT_HOURS)):
                    market_id = f"{currency}_KRW"
                    sell_body = {'market': market_id, 'side': 'ask', 'volume': str(total_vol), 'ord_type': 'market'}
                    requests.post('https://api.bithumb.com/v1/orders', json=sell_body, headers=get_v2_headers(sell_body), proxies=PROXIES, timeout=10)
                    send_telegram_msg(f"⏰ [실전 시간 손절 청산] {currency}/KRW 전량 시장가 매도")
                    del positions[currency]
                    save_json_file(POSITIONS_FILE, positions)
    except Exception as e:
        print(f" ⚠️ 실전 시간 손절 검사 오류: {e}")
        
    print("="*50 + "\n")

def get_account_status():
    if not BITHUMB_API_KEY or not BITHUMB_SECRET_KEY:
        paper_db = load_json_file(PAPER_TRADES_FILE, {"active_positions": {}})
        held = list(paper_db.get("active_positions", {}).keys())
        return {"krw_free": 50000.0, "held_coins": held, "buy_amount_krw": MIN_BUY_KRW}

    try:
        res = requests.get('https://api.bithumb.com/v1/accounts', headers=get_v2_headers(), proxies=PROXIES, timeout=10)
        if res.status_code != 200:
            return {"krw_free": 0.0, "held_coins": [], "buy_amount_krw": MIN_BUY_KRW}

        accounts = res.json()
        krw_free = 0.0
        held_coins = []
        
        for acc in accounts:
            currency = acc['currency']
            total = float(acc['balance']) + float(acc['locked'])
            if currency == 'KRW':
                krw_free = float(acc['balance'])
            elif total > 0 and currency != 'P':
                eval_amount = total * float(acc.get('avg_buy_price', 0))
                if eval_amount >= 1000:
                    held_coins.append(currency)

        if PAPER_TRADING:
            paper_db = load_json_file(PAPER_TRADES_FILE, {"active_positions": {}})
            paper_held = list(paper_db.get("active_positions", {}).keys())
            held_coins = list(set(held_coins + paper_held))

        calc_buy_amount = round(krw_free * BUY_RATIO) if krw_free > 0 else MIN_BUY_KRW
        final_buy_amount = max(calc_buy_amount, MIN_BUY_KRW)

        print(f"💳 [잔고 현황] 주문 가능 KRW: {krw_free:,.0f} 원")
        print(f"💵 [이번회차 매수 산정액] {final_buy_amount:,.0f} 원 (잔고의 20%, 최소 6,000원 보장)")
        print(f"🪙 [보유 종목 수] {len(held_coins)}개 / 최대 허용: {MAX_HOLDING_COINS}개")
        if held_coins:
            print(f" 📌 보유 종목 목록: {', '.join(held_coins)}")
            
        return {"krw_free": krw_free, "held_coins": held_coins, "buy_amount_krw": final_buy_amount}

    except Exception as e:
        print(f"❌ 잔고 조회 실패: {e}")
        return {"krw_free": 0.0, "held_coins": [], "buy_amount_krw": MIN_BUY_KRW}


# ==========================================
# 🎯 [Step 3] Groq AI 연동 (API 키 이중화 Fallback 적용)
# ==========================================
def call_groq_api(system_instruction, user_prompt):
    """KEY3 우선 시도 ➔ 실패 시 KEY2 자동 백업 실행"""
    keys_to_try = []
    if GROQ_API_KEY3: keys_to_try.append(("GROQ_API_KEY3", GROQ_API_KEY3))
    if GROQ_API_KEY2: keys_to_try.append(("GROQ_API_KEY2", GROQ_API_KEY2))

    if not keys_to_try:
        err_msg = "⚠️ <b>[Groq API 연동 실패]</b>\n\n내용: <code>GROQ_API_KEY3 / GROQ_API_KEY2 설정이 없습니다.</code>"
        print(f"[ERROR] {err_msg}")
        send_telegram_msg(err_msg)
        return None

    last_error_msg = ""
    for key_name, api_key in keys_to_try:
        try:
            print(f"🤖 [{key_name}] 사용하여 Groq AI 분석 요청 중...")
            groq_client = OpenAI(
                base_url="https://api.groq.com/openai/v1",
                api_key=api_key
            )
            response = groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {"role": "system", "content": system_instruction},
                    {"role": "user", "content": user_prompt}
                ],
                response_format={"type": "json_object"},
                temperature=0.15
            )
            # 성공 시 즉시 응답 반환
            return response.choices[0].message.content

        except Exception as e:
            last_error_msg = str(e)
            print(f"⚠️ [{key_name}] 호출 실패: {last_error_msg}")
            print(f"🔄 다음 보조 API 키로 자동 백업 전환을 시도합니다...")

    # 모든 키가 실패한 경우에만 텔레그램 알림 발송
    err_notification = f"⚠️ <b>[Groq API 이중화 실패]</b>\n\n모든 API 키 호출이 실패했습니다.\n사유: <code>{last_error_msg}</code>"
    print(f"[ERROR] {err_notification}")
    send_telegram_msg(err_notification)
    return None

def screen_coins_2step(top10_data):
    print("🤖 [Step 3-1] 1차 AI 스크리닝: 상위 10개 종목 중 대추세 우량 후보 3개 추리기...")
    
    sys_prompt_1 = "당신은 퀀트 분석가입니다. 상위 10개 코인의 1시간봉(c_1h) 추세를 보고 가장 강력한 우상향/돌파 모멘텀을 가진 후보 종목 3개를 추려내세요."
    user_prompt_1 = f"상위 10개 코인 1시간봉 데이터:\n{json.dumps(top10_data, ensure_ascii=False)}\n\n[응답 포맷 (JSON)]\n{{\"top3_candidates\": [\"BTC/KRW\", \"ETH/KRW\", \"SOL/KRW\"], \"reason\": \"사유\"}}"
    
    res_1 = call_groq_api(sys_prompt_1, user_prompt_1)
    if not res_1: return None
    
    try:
        data_1 = json.loads(res_1)
        candidates = data_1.get("top3_candidates", [])
        print(f"🎯 [1차 선별 완료] 후보 종목 3개: {candidates}\n")
    except Exception as e:
        print(f"[ERROR] 1차 스크리닝 파싱 실패: {e}")
        return None

    if not candidates: return None

    print("📊 [Step 3-2] 2차 정밀 분석용 5분봉 차트 수집 중...")
    candidates_5m_data = []
    for cand_symbol in candidates:
        coin_code = cand_symbol.split('/')[0]
        candles_5m = get_candles(coin_code, interval="5m", limit=15)
        c_5m_light = [{"c": c['close'], "h": c['high'], "l": c['low'], "v": c['volume']} for c in candles_5m]
        candidates_5m_data.append({"symbol": cand_symbol, "candles_5m": c_5m_light})

    print("🤖 [Step 3-3] 2차 AI 정밀 스크리닝: 5분봉 파동 분석 및 맞춤 손익비 산출...")
    sys_prompt_2 = "당신은 수석 데이트레이더입니다. 후보 3개의 5분봉 파동을 분석하여 최종 1개 종목을 선정하고, 손절률(-1.0%~-2.5%) 및 익절률(+2.0%~+5.0%)을 산출하세요."
    user_prompt_2 = f"후보 3개 코인 5분봉 데이터:\n{json.dumps(candidates_5m_data, ensure_ascii=False)}\n\n[응답 포맷 (JSON)]\n{{\"selected_symbol\": \"BTC/KRW\", \"confidence_score\": 88, \"stop_loss_pct\": -1.8, \"take_profit_pct\": 3.5, \"trend_analysis_1h\": \"1시간봉 우상향\", \"trigger_analysis_5m\": \"5분봉 눌림목\", \"technical_summary\": \"요약\"}}"

    res_2 = call_groq_api(sys_prompt_2, user_prompt_2)
    if not res_2: return None

    try:
        result = json.loads(res_2)
        selected_symbol = result.get("selected_symbol", "NONE")
        confidence = result.get("confidence_score", 0)
        sl_pct = float(result.get("stop_loss_pct", -2.0))
        tp_pct = float(result.get("take_profit_pct", 3.5))

        sl_pct = max(min(sl_pct, -1.0), -2.5)
        tp_pct = min(max(tp_pct, 2.0), 5.0)

        if selected_symbol.upper() == "NONE" or confidence < MIN_CONFIDENCE_SCORE:
            print(f"⏸️ [Groq AI 분석 Result] 매수 타점 미달로 현금 유지 (신뢰도: {confidence}점)\n")
            return None

        print(f"🎯 [최종 AI 선정 종목] {selected_symbol} (신뢰도: {confidence}점)")
        print(f"📐 [AI 맞춤 손익비] 손절: {sl_pct}% | 익절: +{tp_pct}%\n")

        return {
            "symbol": selected_symbol,
            "confidence": confidence,
            "sl_pct": sl_pct,
            "tp_pct": tp_pct,
            "trend_1h": result.get('trend_analysis_1h', '-'),
            "trigger_5m": result.get('trigger_analysis_5m', '-'),
            "summary": result.get('technical_summary', '-')
        }

    except Exception as e:
        print(f"[ERROR] 2차 스크리닝 파싱 실패: {e}")
        return None


# ==========================================
# [Step 4] 가격 타점 및 목표가 계산
# ==========================================
def analyze_technical_levels(ai_plan):
    symbol = ai_plan['symbol']
    coin_code = symbol.split('/')[0]
    
    res = requests.get(f"https://api.bithumb.com/public/ticker/{coin_code}_KRW", proxies=PROXIES, timeout=5).json()
    entry_price = float(res['data']['closing_price'])
    
    stop_loss = round(entry_price * (1.0 + (ai_plan['sl_pct'] / 100.0)), 2)
    take_profit = round(entry_price * (1.0 + (ai_plan['tp_pct'] / 100.0)), 2)
    
    return {
        "symbol": symbol,
        "entry_price": entry_price,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "sl_pct": ai_plan['sl_pct'],
        "tp_pct": ai_plan['tp_pct'],
        "trend_1h": ai_plan['trend_1h'],
        "trigger_5m": ai_plan['trigger_5m'],
        "summary": ai_plan['summary']
    }


# ==========================================
# [Step 5] 매수 발주 및 모의투자 포지션 등록
# ==========================================
def execute_order_with_tp_sl(plan, buy_amount_krw):
    symbol = plan['symbol']
    price = plan['entry_price']
    sl = plan['stop_loss']
    tp = plan['take_profit']
    coin_code = symbol.split('/')[0]

    print("="*50)
    print("🚀 [Step 5] 매수 주문 및 손절/익절 감시 등록")
    
    if PAPER_TRADING:
        print(f"🧪 [SIMULATION MODE] 모의 매수 발주 등록 - {symbol}")
        print(f" ├─ 발주 금액: {buy_amount_krw:,.0f} KRW (잔고의 20% / 최소 6,000원)")
        print(f" ├─ 진입가(현재가): {price:,.0f} KRW")
        print(f" ├─ 모의 익절가 : {tp:,.0f} KRW (+{plan['tp_pct']}%)")
        print(f" └─ 모의 손절가 : {sl:,.0f} KRW ({plan['sl_pct']}%)")
        
        paper_db = load_json_file(PAPER_TRADES_FILE, {"active_positions": {}, "closed_trades": []})
        paper_db["active_positions"][coin_code] = {
            "symbol": symbol,
            "entry_price": price,
            "stop_loss": sl,
            "take_profit": tp,
            "buy_amount_krw": buy_amount_krw,
            "entry_time": datetime.now().isoformat()
        }
        save_json_file(PAPER_TRADES_FILE, paper_db)

        sim_msg = f"""🧪 <b>[모의투자 체결 - 정밀 추적 시작]</b>

📌 <b>종목</b>: <code>{symbol}</code>
💵 <b>매수금액</b>: <code>{buy_amount_krw:,.0f} KRW</code>
📈 <b>진입가</b>: <code>{price:,.0f} KRW</code>

🎯 <b>지정가 익절 목표</b>: <code>{tp:,.0f} KRW</code> (+{plan['tp_pct']}%)
🛡️ <b>스탑로스 손절 목표</b>: <code>{sl:,.0f} KRW</code> ({plan['sl_pct']}%)

📝 <b>매수 근거</b>:
• <b>대추세(1h)</b>: {plan['trend_1h']}
• <b>진입타점(5m)</b>: {plan['trigger_5m']}"""
        send_telegram_msg(sim_msg)

    else:
        print(f"🚨 [REAL TRADING] 실제 매수 및 손절/익절 예약 발주 - {symbol}")
        try:
            url = 'https://api.bithumb.com/v1/orders'
            market_id = symbol.replace('/', '_')
            
            buy_body = {'market': market_id, 'side': 'bid', 'price': str(buy_amount_krw), 'ord_type': 'price'}
            requests.post(url, json=buy_body, headers=get_v2_headers(buy_body), proxies=PROXIES, timeout=10)
            time.sleep(2)
            
            acc_res = requests.get('https://api.bithumb.com/v1/accounts', headers=get_v2_headers(), proxies=PROXIES, timeout=10).json()
            bought_volume = next((float(a['balance']) for a in acc_res if a['currency'] == coin_code), 0.0)

            if bought_volume > 0:
                tp_body = {'market': market_id, 'side': 'ask', 'volume': str(bought_volume), 'price': str(tp), 'ord_type': 'limit'}
                requests.post(url, json=tp_body, headers=get_v2_headers(tp_body), proxies=PROXIES, timeout=10)

                sl_body = {'market': market_id, 'side': 'ask', 'volume': str(bought_volume), 'stop_price': str(sl), 'price': str(sl), 'ord_type': 'price_reserve'}
                requests.post(url, json=sl_body, headers=get_v2_headers(sl_body), proxies=PROXIES, timeout=10)

                positions = load_json_file(POSITIONS_FILE, {})
                positions[coin_code] = datetime.now().isoformat()
                save_json_file(POSITIONS_FILE, positions)

                send_telegram_msg(f"🚨 <b>[실전 매수 체결]</b> {symbol} | 진입가: {price:,.0f} KRW")

        except Exception as e:
            send_telegram_msg(f"❌ <b>[실전 매수 오류]</b>: <code>{e}</code>")
            
    print("="*50 + "\n")


# ==========================================
# 메인 실행 흐름
# ==========================================
if __name__ == "__main__":
    mode_str = "🧪 모의투자(TEST)" if PAPER_TRADING else "🚨 실전매매(REAL)"
    print(f"🤖 빗썸 Groq AI 자동매매 시스템 시작 [{mode_str}]")
    
    # 🧪 모의투자 캔들 파동 정밀 검증
    if PAPER_TRADING:
        track_paper_trading_performance()
    
    # 1. 실전 계좌 정돈
    manage_unfilled_and_time_exits()
    
    # 2. 잔고 조회
    acc_status = get_account_status()
    held_coins = acc_status['held_coins']
    buy_amount_krw = acc_status['buy_amount_krw']
    
    # 🛡️ 보유 제한 검사
    if len(held_coins) >= MAX_HOLDING_COINS:
        print(f"🛑 [보유 제한] 현재 보유 종목({len(held_coins)}개)이 최대 한도({MAX_HOLDING_COINS}개)에 도달했습니다.")
        sys.exit(0)
    
    # 3. 데이터 수집 & 2단계 AI 스크리닝 (이중화 수집 적용)
    top10_data = get_top10_market_data()
    ai_plan = screen_coins_2step(top10_data)
    
    if not ai_plan:
        print("🛑 적합한 매수 종목이 없거나 AI 연동 실패로 금회차 매매를 종료합니다.")
    else:
        selected_coin_code = ai_plan['symbol'].split('/')[0]
        
        if selected_coin_code in held_coins:
            print(f"🛑 [중복 매수 방지] 추천 종목({ai_plan['symbol']})은 이미 보유 중입니다.")
        else:
            plan = analyze_technical_levels(ai_plan)
            execute_order_with_tp_sl(plan, buy_amount_krw=buy_amount_krw)
