import os
import sys
import time
import json
import base64
import logging
import asyncio
import threading
import subprocess
import requests
import httpx
import jwt
import uuid
import re
from datetime import datetime, timedelta, timezone
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

# ==========================================
# 0. 전역 설정 및 환경 변수
# ==========================================
PAPER_TRADING = True             # 🧪 모의투자 (True / False)
MIN_CONFIDENCE_SCORE = 65        # 🎯 최소 신뢰도
MAX_HOLDING_COINS = 3            # 🛡️ 최대 보유 가능 종목 수
MIN_BUY_KRW = 6000               # 💵 최소 매수 금액
BUY_RATIO = 0.20                 # 📊 가용 잔고 비중
ENTRY_TIMEOUT_MINUTES = 20       # ⏰ 진입 대기 만료 시간
TIME_EXIT_HOURS = 3              # ⏰ 최대 보유 시간 (강제 청산)

TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
TELEGRAM_CHAT_ID = str(os.getenv("TELEGRAM_CHAT_ID") or "").strip()
GH_TOKEN = os.getenv("GH_TOKEN2") or os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
GITHUB_REPOSITORY = "dhlee090512-arch/auto-trade"

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SAMBANOVA_API_KEY = os.getenv("SAMBANOVA_API_KEY")
GROQ_API_KEY3 = os.getenv("GROQ_API_KEY3")
GROQ_API_KEY2 = os.getenv("GROQ_API_KEY2")
WEBSHARE_URL = os.getenv("WEBSHARE_URL")
BITHUMB_API_KEY = os.getenv("BITHUMB_API_KEY")
BITHUMB_SECRET_KEY = os.getenv("BITHUMB_SECRET_KEY")

PROXIES = {'http': WEBSHARE_URL, 'https': WEBSHARE_URL} if WEBSHARE_URL else None
STABLE_COINS = {"USDT", "USDC", "DAI", "TUSD", "FDUSD", "USDD", "BUSD"}

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

# ==========================================
# 1. 텔레그램 및 KST 시간 유틸리티
# ==========================================
def get_kst_now():
    return datetime.now(timezone(timedelta(hours=9)))

def send_telegram_msg(msg: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg}
    try:
        requests.post(url, json=payload, timeout=8)
    except Exception as e:
        logging.error(f"텔레그램 발송 오류: {e}")

def format_portfolio_status_msg(active_positions, closed_trades):
    held_symbols = [v['symbol'] for v in active_positions.values()]
    held_str = f"{', '.join(held_symbols)} ({len(held_symbols)}개 보유 중)" if held_symbols else "(현재 보유 종목 없음)"
    recent_10 = closed_trades[-10:] if closed_trades else []
    
    if not recent_10:
        trades_str = "• 매도 이력이 없습니다."
        win_rate = 0
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
            try:
                dt_obj = datetime.fromisoformat(exit_time_str)
                time_display = dt_obj.strftime("%m/%d %H:%M KST")
            except Exception:
                time_display = "-"
                
            sign_pct = "+" if p_pct > 0 else ""
            icon = "🟢" if p_pct > 0 else "🔴"
            trade_lines.append(f"{icon} {idx}. {symbol} ({sign_pct}{p_pct:.1f}%) {time_display}")
            if p_pct > 0:
                wins += 1
            total_profit_krw += p_krw
            
        trades_str = "\n".join(trade_lines)
        win_rate = round((wins / len(recent_10)) * 100)

    sign_krw = "+" if total_profit_krw > 0 else ""
    return f"""💼 [현재 매매 상황]
• 보유 종목 : {held_str}

📜 [최근 매도 이력]
{trades_str}

📊 최근 10건 승률 : {win_rate}%
💰 최근 10건 실현 손익 : {sign_krw}{total_profit_krw:,} KRW"""

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
# 3. 빗썸 호가단위 & 퀀트 피처
# ==========================================
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
        "tick_ratio_pct": tick_ratio_pct
    }

# ==========================================
# 4. 서버 내장 AI 전략 수립 모듈
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
        cands_m = re.search(r'["\']top3_candidates["\']\s*:\s*\[(.*?)\]', raw_text, re.DOTALL)
        if cands_m:
            res["top3_candidates"] = [c.strip().strip('"').strip("'") for c in cands_m.group(1).split(",") if c.strip()]
        if res: return res
    except Exception:
        pass
    return None

def execute_server_side_strategy():
    logging.info("🧠 [서버 자체 AI 전략 분석 시작]")
    url = "https://api.bithumb.com/public/ticker/ALL_KRW"
    res = requests.get(url, proxies=PROXIES, timeout=10).json()
    if res.get("status") != "0000": return

    raw_list = []
    for sym, info in res["data"].items():
        if sym == "date" or sym.upper() in STABLE_COINS: continue
        try:
            raw_list.append((sym, float(info['closing_price']), float(info['fluctate_rate_24H']), float(info['acc_trade_value_24H'])))
        except Exception: pass

    sorted_list = sorted(raw_list, key=lambda x: x[3], reverse=True)[:15]
    top_data = []

    for sym, price, change, _ in sorted_list:
        candles_1h = get_candles(sym, interval="1h", limit=24)
        if len(candles_1h) < 15: continue
        q_feat = calculate_quant_features(candles_1h)

        if q_feat["tick_ratio_pct"] > 0.35: continue
        if q_feat["atr_pct"] < 0.5 or q_feat["vwap_gap_pct"] < -3.5: continue

        c_1h_light = [{"c": c['close'], "h": c['high'], "l": c['low'], "v": c['volume']} for c in candles_1h[-10:]]
        top_data.append({
            "symbol": f"{sym}/KRW", "price": price, "change_24h": change,
            "quant_metrics": q_feat, "candles_1h_recent": c_1h_light
        })
        if len(top_data) >= 8: break

    if not top_data:
        logging.info("⏸️ 조건에 부합하는 종목이 없어 관망합니다.")
        return

    sys_1 = "Select up to 3 candidates for momentum/range scalping. Return JSON ONLY."
    user_1 = f"Market Data:\n{json.dumps(top_data, ensure_ascii=False)}\n\nSchema: {{\"top3_candidates\": [\"BTC/KRW\"], \"reason\": \"한국어 선별 사유\"}}"
    res_1 = clean_and_parse_json(call_ai_api(sys_1, user_1))
    if not res_1 or not res_1.get("top3_candidates"): return

    candidates = res_1["top3_candidates"]
    logging.info(f"🎯 [1차 선별 완료]: {candidates}")

    cand_5m_data = []
    for sym in candidates:
        code = sym.split('/')[0]
        candles_5m = get_candles(code, interval="5m", limit=40)
        if len(candles_5m) < 25: continue
        q_5m = calculate_quant_features(candles_5m)
        c_light = [{"c": c['close'], "h": c['high'], "l": c['low'], "v": c['volume']} for c in candles_5m]
        cand_5m_data.append({"symbol": sym, "quant_5m": q_5m, "candles_5m": c_light})

    sys_2 = (
        "You are an intraday scalper. Analyze 5m 40-candle series to pick 1 trade plan or NONE.\n"
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
        logging.info(f"⏸️ 2차 분석 결과 신뢰도({confidence}점) 미달로 관망 유지")
        return

    code = selected.split('/')[0]
    curr_p = get_current_price(code)
    if not curr_p: return

    discount = float(res_2.get("entry_discount_pct", 0.1))
    target_entry = curr_p * (1.0 - (discount / 100.0))
    sl_pct = max(min(float(res_2.get("stop_loss_pct", -1.0)), -0.8), -1.5)
    tp_pct = min(max(float(res_2.get("take_profit_pct", 1.8)), 1.2), 2.8)

    now_iso = get_kst_now().isoformat()
    plan_data = {
        "symbol": selected,
        "current_price": curr_p,
        "target_entry": target_entry,
        "sl_pct": sl_pct,
        "tp_pct": tp_pct,
        "buy_amount_krw": 10000,
        "detailed_reason": res_2.get("detailed_reason", "5분봉 패턴 및 지표 분석"),
        "created_at": now_iso
    }

    server_state = load_json_file(STATE_FILE, {"pending_targets": {}, "last_updated": ""})
    paper_db = load_json_file(PAPER_TRADES_FILE, {"active_positions": {}, "closed_trades": []})
    
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

🎯 익절 목표 : {tp_sign}{tp_pct}%
🛡️ 손절 기준 : {sl_pct}%

💡 매수 근거 :
{plan_data['detailed_reason']}
=================================
⚡ 규칙: 20분 미체결 취소 / 트레일링 스탑 및 거래량 소멸 시 조기 청산"""
    send_telegram_msg(plan_msg)

    time.sleep(0.5)
    portfolio_msg = format_portfolio_status_msg(paper_db.get("active_positions", {}), paper_db.get("closed_trades", []))
    send_telegram_msg(portfolio_msg)

# ==========================================
# 5. 실시간 감시 & 트레일링 스탑 & 조기 청산 엔진
# ==========================================
async def realtime_execution_engine():
    global EMERGENCY_STOP
    logging.info("⚡ 오라클 실시간 감시 & 트레일링 스탑 엔진 구동 시작")
    last_strategy_run = 0

    t = threading.Thread(target=telegram_listener_thread, daemon=True)
    t.start()

    while True:
        try:
            now = time.time()
            now_dt = get_kst_now()

            if now - last_strategy_run >= 300:
                execute_server_side_strategy()
                last_strategy_run = now

            server_state = load_json_file(STATE_FILE, {"pending_targets": {}, "last_updated": ""})
            paper_db = load_json_file(PAPER_TRADES_FILE, {"active_positions": {}, "closed_trades": []})

            pending = server_state.get("pending_targets", {})
            active_positions = paper_db.get("active_positions", {})
            closed_trades = paper_db.get("closed_trades", [])

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
                        logging.info(f"🎯 [{plan['symbol']}] 진입 타점 도달! 매수 체결")
                        exact_sl = curr_p * (1.0 + (plan["sl_pct"] / 100.0))
                        exact_tp = curr_p * (1.0 + (plan["tp_pct"] / 100.0))

                        active_positions[coin_code] = {
                            "symbol": plan["symbol"],
                            "entry_price": curr_p,
                            "highest_price": curr_p,
                            "stop_loss": exact_sl,
                            "take_profit": exact_tp,
                            "buy_amount_krw": plan["buy_amount_krw"],
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

            for coin_code, pos in list(active_positions.items()):
                curr_p = get_current_price(coin_code)
                if not curr_p: continue

                entry_p = pos["entry_price"]
                entry_time = datetime.fromisoformat(pos["entry_time"])
                curr_profit_pct = ((curr_p - entry_p) / entry_p) * 100.0

                if curr_p > pos.get("highest_price", entry_p):
                    pos["highest_price"] = curr_p

                highest_profit_pct = ((pos["highest_price"] - entry_p) / entry_p) * 100.0

                if not pos.get("break_even_triggered", False) and curr_profit_pct >= 0.9:
                    pos["stop_loss"] = max(pos["stop_loss"], entry_p * 1.001)
                    pos["break_even_triggered"] = True
                    logging.info(f"🛡️ [{pos['symbol']}] 수익 +0.9% 도달로 본절 방어선 가동")

                status = "HOLDING"
                exit_reason = ""

                if curr_p >= pos["take_profit"]:
                    status = "CLOSED_TAKE_PROFIT"
                    exit_reason = "🎯 익절 목표가 달성"
                elif highest_profit_pct >= 1.2 and (highest_profit_pct - curr_profit_pct) >= 0.4:
                    status = "CLOSED_TRAILING_STOP"
                    exit_reason = f"📈 트레일링 스탑 (최고 +{highest_profit_pct:.1f}% 달성 후 이익 보존)"
                elif curr_p <= pos["stop_loss"]:
                    status = "CLOSED_STOP_LOSS"
                    exit_reason = "🛡️ 본절 방어선 또는 손절가 도달"
                elif (now_dt - entry_time) >= timedelta(minutes=35) and abs(curr_profit_pct) < 0.4:
                    status = "CLOSED_EARLY_EXIT"
                    exit_reason = "⌛ 35분간 모멘텀 소멸로 조기 청산 (기회비용 확보)"
                elif (now_dt - entry_time) >= timedelta(hours=TIME_EXIT_HOURS):
                    status = "CLOSED_TIME_EXIT"
                    exit_reason = f"⏰ {TIME_EXIT_HOURS}시간 횡보로 시장가 청산"

                if status != "HOLDING":
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
            await asyncio.sleep(5)

# ==========================================
# 6. 텔레그램 명령어 리스너 (무한 루프 방어)
# ==========================================
def telegram_listener_thread():
    global EMERGENCY_STOP, LAST_TELEGRAM_UPDATE_ID
    if not TELEGRAM_BOT_TOKEN: return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"

    # 💡 [핵심 버그 수정] 시작 시 큐에 남아있는 과거 메시지(재부팅 전 /update 등)를 모두 읽고 버림
    try:
        init_res = requests.get(url, params={"timeout": 1}, timeout=5).json()
        if init_res.get("ok") and init_res.get("result"):
            LAST_TELEGRAM_UPDATE_ID = init_res["result"][-1]["update_id"]
            # 텔레그램 서버에 과거 메시지 읽음 처리 (오프셋 갱신)
            requests.get(url, params={"offset": LAST_TELEGRAM_UPDATE_ID + 1, "timeout": 1}, timeout=5)
            logging.info(f"📱 텔레그램 과거 메시지 플러시 완료 (최신 Update ID: {LAST_TELEGRAM_UPDATE_ID})")
    except Exception as e:
        logging.warning(f"텔레그램 초기화 오류: {e}")

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
• 인터락 상태: {status_str}
• 진입 대기 종목: {', '.join(pending) if pending else '(없음)'}
• 현재 보유 종목: {', '.join(held) if held else '(없음)'}
• 누적 복기 거래수: {len(paper_db.get('closed_trades', []))}건"""
                        send_telegram_msg(res_msg)

                    elif text == "/stop":
                        EMERGENCY_STOP = True
                        send_telegram_msg("🛑 [인터락 작동] 신규 매수 감시가 일시 중단되었습니다.")

                    elif text == "/start":
                        EMERGENCY_STOP = False
                        send_telegram_msg("▶️ [인터락 해제] 신규 매수 감시가 정상 재개되었습니다.")

                    elif text == "/update":
                        send_telegram_msg("🔄 [원격 업데이트] 최신 코드를 다운로드하고 서비스를 재시작합니다...")
                        
                        # 💡 재시작 전에 현재 Update ID를 텔레그램 서버에 확실히 커밋
                        try:
                            requests.get(url, params={"offset": LAST_TELEGRAM_UPDATE_ID + 1, "timeout": 1}, timeout=3)
                        except Exception:
                            pass

                        def do_restart():
                            time.sleep(1.5)
                            try:
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
        "🚀 [오라클 서버] 트레일링 스탑 & 지연 제로 퀀트 엔진 가동\n\n"
        "📱 사용 가능한 명령어:\n"
        "• /status : 시스템 상태 및 포지션 확인\n"
        "• /update : GitHub 최신 코드 동기화 후 재시작\n"
        "• /stop : 신규 매수 일시정지\n"
        "• /start : 매매 재개"
    )
    asyncio.run(realtime_execution_engine())
