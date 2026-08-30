import os
import sys
import time
import json
import logging
import asyncio
import threading
from datetime import datetime, timedelta, timezone
import requests
import re

# ==========================================
# 0. 로깅 설정
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("AutoTradeWatcher")

# ==========================================
# 1. 환경 변수 로드 (내장 파서)
# ==========================================
ENV_FILE = "/home/ubuntu/auto-trade/.env"
env_config = {}

def load_environment():
    global env_config
    if os.path.exists(ENV_FILE):
        try:
            with open(ENV_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        env_config[k.strip()] = v.strip().strip('"').strip("'")
        except Exception as e:
            logger.error(f".env 파일 로드 실패: {e}")

load_environment()

GEMINI_API_KEY = env_config.get("GEMINI_API_KEY", "")
BITHUMB_API_KEY = env_config.get("BITHUMB_API_KEY", "")
BITHUMB_SECRET_KEY = env_config.get("BITHUMB_SECRET_KEY", "")
TELEGRAM_BOT_TOKEN = env_config.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = env_config.get("TELEGRAM_CHAT_ID", "")

# ==========================================
# 2. 거래 및 감시 상수 설정
# ==========================================
STATE_FILE = "/home/ubuntu/auto-trade/server_state.json"
PAPER_TRADES_FILE = "/home/ubuntu/auto-trade/paper_trades.json"
LOG_FILE = "/home/ubuntu/auto-trade/bot_log.txt"

PAPER_TRADING = True  # 모의투자 모드
EMERGENCY_STOP = False
MAX_HOLDING_COINS = 3
ENTRY_TIMEOUT_MINUTES = 120
TRADE_ALLOCATION_KRW = 200000

STABLE_COINS = {"USDT", "USDC", "DAI", "BUSD", "TUSD", "FDUSD", "KRW", "BTC", "ETH"}
KST = timezone(timedelta(hours=9))

def get_kst_now():
    return datetime.now(KST)

def parse_kst_iso(dt_str):
    try:
        return datetime.fromisoformat(dt_str)
    except Exception:
        return get_kst_now()

def load_json_file(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json_file(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"JSON 저장 실패 ({path}): {e}")

# ==========================================
# 3. Gemini 3.5 기반 AI 브레인 클래스
# ==========================================
class GeminiBrainDirect:
    def __init__(self):
        self.key = GEMINI_API_KEY
        self.models = ["gemini-3.5-flash", "gemini-3.5-flash-lite"]

    def _call_api(self, prompt_text):
        if not self.key:
            logger.error("GEMINI_API_KEY가 설정되지 않았습니다.")
            return None
        
        for m in self.models:
            try:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent?key={self.key}"
                payload = {
                    "contents": [{"parts": [{"text": str(prompt_text)}]}],
                    "generationConfig": {
                        "response_mime_type": "application/json",
                        "temperature": 0.2
                    }
                }
                res = requests.post(url, json=payload, timeout=15)
                if res.status_code == 200:
                    txt = res.json()["candidates"][0]["content"]["parts"][0]["text"]
                    match = re.search(r"\{.*\}", txt, re.DOTALL)
                    if match:
                        return json.loads(match.group(0))
                    return json.loads(txt)
                else:
                    logger.warning(f"Gemini ({m}) HTTP {res.status_code}: {res.text[:100]}")
            except Exception as e:
                logger.warning(f"Gemini ({m}) 예외: {e}")
        return None

    def get_decision(self, prompt_text):
        return self._call_api(prompt_text)

    def decide(self, prompt_text):
        return self._call_api(prompt_text)

    def analyze(self, prompt_text):
        return self._call_api(prompt_text)

    def ask(self, prompt_text):
        return self._call_api(prompt_text)

def get_ai_brain():
    return GeminiBrainDirect()

# ==========================================
# 4. 빗썸 API 및 기술적 지표 퀀트 계산
# ==========================================
def get_current_price(symbol):
    try:
        url = f"https://api.bithumb.com/public/ticker/{symbol}_KRW"
        res = requests.get(url, timeout=5).json()
        if res.get("status") == "0000":
            return float(res["data"]["closing_price"])
    except Exception:
        pass
    return None

def get_candles(symbol, interval="15m", limit=30):
    try:
        url = f"https://api.bithumb.com/public/candlestick/{symbol}_KRW/{interval}"
        res = requests.get(url, timeout=6).json()
        if res.get("status") == "0000":
            data = res["data"][-limit:]
            candles = []
            for d in data:
                candles.append({
                    "time": d[0],
                    "open": float(d[1]),
                    "close": float(d[2]),
                    "high": float(d[3]),
                    "low": float(d[4]),
                    "volume": float(d[5])
                })
            return candles
    except Exception:
        pass
    return []

def calculate_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

def calculate_swing_quant_features(candles_1h, candles_15m):
    closes_1h = [c["close"] for c in candles_1h]
    closes_15m = [c["close"] for c in candles_15m]
    
    rsi_1h = calculate_rsi(closes_1h, 14)
    rsi_15m = calculate_rsi(closes_15m, 14)
    
    ma20_1h = sum(closes_1h[-20:]) / 20.0 if len(closes_1h) >= 20 else closes_1h[-1]
    curr_p = closes_15m[-1]
    
    vol_avg = sum(c["volume"] for c in candles_15m[-10:]) / 10.0 if len(candles_15m) >= 10 else 1.0
    vol_ratio = (candles_15m[-1]["volume"] / vol_avg) if vol_avg > 0 else 1.0

    return {
        "rsi_1h": round(rsi_1h, 2),
        "rsi_15m": round(rsi_15m, 2),
        "ma20_1h": round(ma20_1h, 2),
        "curr_price": curr_p,
        "vol_ratio": round(vol_ratio, 2)
    }

def calculate_swing_score(q, btc_ret_4h):
    score = 50.0
    if q["rsi_1h"] >= 55.0: score += 15.0
    if q["rsi_15m"] <= 45.0: score += 15.0
    if q["curr_price"] >= q["ma20_1h"]: score += 10.0
    if q["vol_ratio"] >= 1.5: score += 10.0
    return min(100.0, score)

def execute_real_market_order(coin_code, order_type, amount_krw):
    # 실전 주문 연동 위치 (모의투자 시 패스)
    return True, {"status": "success"}

# ==========================================
# 5. 전략 실행부 (퀀트 선별 + Gemini 3.5)
# ==========================================
def execute_server_side_strategy():
    logger.info("🧠 [서버 1시간 모멘텀 + 15분 눌림목 분석 시작 (모수 30개)]")
    
    server_state = load_json_file(STATE_FILE, {"pending_targets": {}, "last_updated": ""})
    paper_db = load_json_file(PAPER_TRADES_FILE, {"active_positions": {}, "closed_trades": []})
    held_codes = set(list(paper_db.get("active_positions", {}).keys()) + list(server_state.get("pending_targets", {}).keys()))

    btc_candles = get_candles("BTC", interval="1h", limit=5)
    btc_ret_4h = 0.0
    if len(btc_candles) >= 4:
        btc_ret_4h = (btc_candles[-1]["close"] - btc_candles[-4]["close"]) / btc_candles[-4]["close"] * 100.0

    url = "https://api.bithumb.com/public/ticker/ALL_KRW"
    try:
        res = requests.get(url, timeout=8).json()
    except Exception as e:
        logger.error(f"빗썸 전체 시세 조회 실패: {e}")
        return

    if res.get("status") != "0000":
        return

    raw_list = []
    for sym, info in res["data"].items():
        if sym == "date" or sym.upper() in STABLE_COINS or sym in held_codes:
            continue
        try:
            raw_list.append((sym, float(info['closing_price']), float(info['fluctate_rate_24H']), float(info['acc_trade_value_24H'])))
        except Exception:
            pass

    sorted_list = sorted(raw_list, key=lambda x: x[3], reverse=True)[:30]
    scored_candidates = []

    for sym, price, change, _ in sorted_list:
        candles_1h = get_candles(sym, interval="1h", limit=40)
        candles_15m = get_candles(sym, interval="15m", limit=30)
        if len(candles_1h) < 20 or len(candles_15m) < 20:
            continue

        q = calculate_swing_quant_features(candles_1h, candles_15m)
        if q.get("rsi_1h", 50) > 70.0:
            continue

        score = calculate_swing_score(q, btc_ret_4h)
        scored_candidates.append({
            "symbol": f"{sym}/KRW",
            "code": sym,
            "price": price,
            "change_24h": change,
            "score": score,
            "quant": q,
            "candles_15m": [{"c": c['close'], "h": c['high'], "l": c['low'], "v": c['volume']} for c in candles_15m[-15:]]
        })

    if not scored_candidates:
        logger.info("선별된 대상 종목이 없습니다.")
        return

    top_3 = sorted(scored_candidates, key=lambda x: x["score"], reverse=True)[:3]
    logger.info(f"🎯 [1시간 모멘텀 상위 3개 선별]: {[f\"{c['symbol']}({int(c['score'])}점)\" for c in top_3]}")

    brain = get_ai_brain()
    now_kst_str = get_kst_now().isoformat()

    for item in top_3:
        if len(paper_db.get("active_positions", {})) >= MAX_HOLDING_COINS:
            break

        prompt = f"""
당신은 암호화폐 퀀트-스윙 트레이딩 전문가입니다.
종목: {item['symbol']} (현재가: {item['price']} KRW, 24H 변동: {item['change_24h']}%)
퀀트 지표: {json.dumps(item['quant'])}
최근 15분봉 데이터: {json.dumps(item['candles_15m'])}

위 데이터를 바탕으로 15분 눌림목 매수 타점을 분석하여 아래 JSON 규격으로만 응답하세요:
{{
  "decision": "BUY_PULLBACK" 또는 "HOLD",
  "target_entry_price": 0.0,
  "stop_loss_pct": -3.5,
  "take_profit_pct": 5.0,
  "reason": "분석 요약 1줄"
}}
"""
        decision = brain.decide(prompt)
        if not decision:
            continue

        if decision.get("decision") == "BUY_PULLBACK":
            target_p = float(decision.get("target_entry_price", item["price"]))
            sl_pct = float(decision.get("stop_loss_pct", -3.5))
            tp_pct = float(decision.get("take_profit_pct", 5.0))
            reason = decision.get("reason", "눌림목 지지 확인")

            logger.info(f"💡 AI 승인: [{item['symbol']}] 진입목표가={target_p}, SL={sl_pct}%, TP={tp_pct}% | 사유: {reason}")
            
            server_state.setdefault("pending_targets", {})[item["code"]] = {
                "symbol": item["symbol"],
                "target_entry": target_p,
                "sl_pct": sl_pct,
                "tp_pct": tp_pct,
                "buy_amount_krw": TRADE_ALLOCATION_KRW,
                "created_at": now_kst_str,
                "reason": reason
            }
            save_json_file(STATE_FILE, server_state)

# ==========================================
# 6. 메인 감시 루프
# ==========================================
async def main_watcher_loop():
    logger.info("⚡ 실시간 모멘텀-스윙 감시 & 무제한 트레일링 엔진 구동 시작")
    last_strategy_run = 0

    while True:
        try:
            now = time.time()
            now_dt = get_kst_now()

            # 15분(900초) 주기 퀀트 + AI 분석
            if now - last_strategy_run >= 900:
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
                    created_dt = parse_kst_iso(plan.get("created_at", ""))
                    if now_dt - created_dt >= timedelta(minutes=ENTRY_TIMEOUT_MINUTES):
                        logger.info(f"⌛ [{plan['symbol']}] 2시간 내 미체결로 자동 취소")
                        del pending[coin_code]
                        save_json_file(STATE_FILE, server_state)
                        continue

                    curr_p = get_current_price(coin_code)
                    if curr_p and curr_p <= plan["target_entry"]:
                        if not PAPER_TRADING:
                            success, order_res = execute_real_market_order(coin_code, "bid", plan["buy_amount_krw"])
                            if not success:
                                logger.error(f"실전 매수 주문 실패: {order_res}")
                                continue

                        logger.info(f"🎯 [{plan['symbol']}] 15분 눌림목 체결 완료 (체결가: {curr_p})")
                        units = plan["buy_amount_krw"] / curr_p

                        active_positions[coin_code] = {
                            "symbol": plan["symbol"],
                            "entry_price": curr_p,
                            "highest_price": curr_p,
                            "units": units,
                            "buy_amount_krw": plan["buy_amount_krw"],
                            "sl_pct": plan["sl_pct"],
                            "tp_pct": plan["tp_pct"],
                            "entry_time": now_dt.isoformat()
                        }
                        del pending[coin_code]
                        save_json_file(STATE_FILE, server_state)
                        save_json_file(PAPER_TRADES_FILE, paper_db)

            # [2] 보유 포지션 익절 / 손절 / 트레일링 스탑 감시
            for coin_code, pos in list(active_positions.items()):
                curr_p = get_current_price(coin_code)
                if not curr_p:
                    continue

                if curr_p > pos["highest_price"]:
                    pos["highest_price"] = curr_p
                    save_json_file(PAPER_TRADES_FILE, paper_db)

                pnl_pct = ((curr_p - pos["entry_price"]) / pos["entry_price"]) * 100.0
                dd_pct = ((curr_p - pos["highest_price"]) / pos["highest_price"]) * 100.0

                should_close = False
                close_reason = ""

                # 1) 손절 조건
                if pnl_pct <= pos["sl_pct"]:
                    should_close = True
                    close_reason = f"손절 도달 ({pnl_pct:.2f}%)"
                # 2) 1차 익절 도달 후 고점 대비 1.5% 하락 시 트레일링 스탑
                elif pnl_pct >= pos["tp_pct"] and dd_pct <= -1.5:
                    should_close = True
                    close_reason = f"트레일링 익절 ({pnl_pct:.2f}%, 고점대비 {dd_pct:.2f}%)"

                if should_close:
                    logger.info(f"🚨 [{pos['symbol']}] 포지션 청산: {close_reason} (청산가: {curr_p})")
                    if not PAPER_TRADING:
                        execute_real_market_order(coin_code, "ask", pos["units"] * curr_p)

                    closed_trades.append({
                        "symbol": pos["symbol"],
                        "entry_price": pos["entry_price"],
                        "exit_price": curr_p,
                        "pnl_pct": round(pnl_pct, 2),
                        "reason": close_reason,
                        "exit_time": now_dt.isoformat()
                    })
                    del active_positions[coin_code]
                    save_json_file(PAPER_TRADES_FILE, paper_db)

        except Exception as e:
            logger.error(f"메인 감시 루프 예외: {e}")

        await asyncio.sleep(2)

if __name__ == "__main__":
    try:
        asyncio.run(main_watcher_loop())
    except KeyboardInterrupt:
        logger.info("사용자에 의해 감시 엔진이 종료되었습니다.")
