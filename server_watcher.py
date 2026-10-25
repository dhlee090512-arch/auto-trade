import os
import sys
import time
import uuid
import json
import re
import hashlib
import urllib.parse
import requests
from datetime import datetime, timedelta
import logging
import threading
import dotenv
import jwt

# .env 파일 명시적 로드 (가상환경 서비스 구동 시 환경변수 누락 방지)
dotenv.load_dotenv()

# ==========================================
# 0. 로깅 설정
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(filename)s:%(lineno)d] - %(message)s",
    handlers=[
        logging.FileHandler("quant_scalping_bot.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)

def get_env(key_name):
    val = os.getenv(key_name)
    if val and isinstance(val, str):
        return val.strip()
    return ""

class Config:
    BITHUMB_API_KEY = get_env("BITHUMB_API_KEY")
    BITHUMB_SECRET_KEY = get_env("BITHUMB_SECRET_KEY")
    GEMINI_API_KEY = get_env("GEMINI_API_KEY")
    TELEGRAM_BOT_TOKEN = get_env("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID = get_env("TELEGRAM_CHAT_ID")
    WEBSHARE_URL = get_env("WEBSHARE_URL")

    IS_PAPER_TRADING = True           # 모의투자(True) / 실전매매(False)
    MIN_BUY_KRW = 6000                # 최소 매수 금액
    POSITION_RATIO = 0.20             # 가용 잔고의 20% 투입
    MAX_POSITIONS = 2                 # 최대 동시 보유 종목 수

    MIN_TRADING_VALUE = 5_000_000_000  # 최소 거래대금 (50억)
    GAP_MIN = 0.005                    # 골디락스 갭 하한 (0.5%)
    GAP_MAX = 0.010                    # 골디락스 갭 상한 (1.0%)
    STOP_LOSS_BUFFER = 0.002           # 지지선 밑 손절 버퍼 (0.2%)

PROXIES = {'http': Config.WEBSHARE_URL, 'https': Config.WEBSHARE_URL} if Config.WEBSHARE_URL else None

class QuantScalpingBot:
    def __init__(self):
        logging.info("initialize: 봇 엔진 초기화 시작...")
        
        print(f"🔍 [디버그] 토큰 길이: {len(Config.TELEGRAM_BOT_TOKEN)}, 챗 ID: {Config.TELEGRAM_CHAT_ID}")
        self.send_telegram("🚀 [부팅 완료] 퀀트 스캘핑 봇이 정상적으로 시작되었습니다!")

        self.support_cache = {}          
        self.active_positions = {}       
        self.trade_history = []          
        self.macro_bearish = False       

        # 1. 텔레그램 컨트롤러 백그라운드 스레드 가동
        threading.Thread(target=self.listen_telegram_commands, daemon=True).start()
        
        # 2. Slow Loop (60초 주기: 스크리닝 및 제미나이 AI 분석) 가동
        threading.Thread(target=self.run_slow_loop_worker, daemon=True).start()

        mode_str = "모의투자 (Paper)" if Config.IS_PAPER_TRADING else "실전매매 (LIVE)"
        self.send_telegram(f"🚀 퀀트 스캘핑 봇 부팅 완료\n- 운영 모드: {mode_str}\n- Fast Loop: 2초 주기 (출구 감시)")

    # ==========================================
    # 1. 텔레그램 연동 (마크다운 파싱 에러 방지)
    # ==========================================
    def send_telegram(self, message: str):
        if not Config.TELEGRAM_BOT_TOKEN or not Config.TELEGRAM_CHAT_ID:
            print("❌ [Telegram Error] TOKEN 또는 CHAT_ID가 비어있습니다!")
            return
        try:
            url = f"https://api.telegram.org/bot{Config.TELEGRAM_BOT_TOKEN}/sendMessage"
            # 마크다운 파싱 충돌 방지를 위해 parse_mode 제거 (일반 텍스트 전송)
            payload = {"chat_id": Config.TELEGRAM_CHAT_ID, "text": message}
            res = requests.post(url, json=payload, timeout=5, proxies=PROXIES)
            if res.status_code == 200:
                print("✅ [Telegram] 메시지 전송 성공!")
            else:
                print(f"⚠️ [Telegram API 에러] 코드: {res.status_code}, 내용: {res.text}")
        except Exception as e:
            logging.error(f"telegram 발송 실패: {e}")
            print(f"❌ [Telegram 예외 발생]: {e}")

    def listen_telegram_commands(self):
        offset = 0
        while True:
            try:
                if not Config.TELEGRAM_BOT_TOKEN:
                    time.sleep(5)
                    continue
                url = f"https://api.telegram.org/bot{Config.TELEGRAM_BOT_TOKEN}/getUpdates?offset={offset}&timeout=10"
                res = requests.get(url, timeout=12, proxies=PROXIES).json()
                if res.get("ok"):
                    for result in res.get("result", []):
                        offset = result["update_id"] + 1
                        msg = result.get("message", {}).get("text", "").strip()
                        chat_id = str(result.get("message", {}).get("chat", {}).get("id", ""))
                        if chat_id != str(Config.TELEGRAM_CHAT_ID):
                            continue

                        if msg == "/paper":
                            Config.IS_PAPER_TRADING = True
                            self.send_telegram("🔄 모드 전환: 모의투자(Paper) 모드")
                        elif msg == "/live":
                            Config.IS_PAPER_TRADING = False
                            self.send_telegram("⚠️ 경고: 실전매매(LIVE) 모드 전환!")
                        elif msg == "/status":
                            self.send_status_report()
            except Exception as e:
                logging.error(f"telegram_listener 에러: {e}")
            time.sleep(2)

    def send_status_report(self):
        mode_str = "모의투자 (Paper)" if Config.IS_PAPER_TRADING else "실전매매 (LIVE)"
        total = len(self.trade_history)
        wins = sum(1 for t in self.trade_history if t['pnl_pct'] > 0)
        win_rate = (wins / total * 100) if total > 0 else 0.0
        cum_pnl = sum(t['pnl_pct'] for t in self.trade_history)

        report = f"📊 [봇 현재 상태]\n- 모드: {mode_str}\n- 보유 포지션: {len(self.active_positions)} / {Config.MAX_POSITIONS}개\n- 전적: {total}전 {wins}승 (승률: {win_rate:.1f}%)\n- 합산 수익률: {cum_pnl:+.2f}%"
        self.send_telegram(report)

    # ==========================================
    # 2. 빗썸 API 연동
    # ==========================================
    def get_bithumb_jwt_headers(self, query_params=None):
        payload = {
            'access_key': Config.BITHUMB_API_KEY,
            'nonce': str(uuid.uuid4()),
            'timestamp': int(time.time() * 1000)
        }
        if query_params:
            q_str = urllib.parse.urlencode(query_params).encode()
            payload['query_hash'] = hashlib.sha512(q_str).hexdigest()
            payload['query_hash_alg'] = 'SHA512'
        jwt_token = jwt.encode(payload, Config.BITHUMB_SECRET_KEY, algorithm='HS256')
        return {'Authorization': f'Bearer {jwt_token}', 'Content-Type': 'application/json'}

    def get_bithumb_krw_balance(self) -> float:
        krw_free = 100_000.0
        if Config.BITHUMB_API_KEY and Config.BITHUMB_SECRET_KEY:
            try:
                res = requests.get('https://api.bithumb.com/v1/accounts', headers=self.get_bithumb_jwt_headers(), proxies=PROXIES, timeout=5)
                if res.status_code == 200:
                    for acc in res.json():
                        if acc['currency'] == 'KRW':
                            krw_free = float(acc['balance'])
                            break
            except Exception as e:
                logging.error(f"잔고 조회 오류: {e}")
        return max(krw_free * Config.POSITION_RATIO, Config.MIN_BUY_KRW)

    def execute_order(self, coin_code: str, side: str, volume: float):
        if Config.IS_PAPER_TRADING:
            logging.info(f"🧪 [PAPER 주문 시뮬레이션] {coin_code} | {side.upper()} | 수량: {volume}")
            return True, "simulated_uuid"

        try:
            url = "https://api.bithumb.com/v1/orders"
            payload = {
                "market": f"KRW-{coin_code}",
                "side": side,
                "ord_type": "market",
                "volume": str(volume)
            }
            headers = self.get_bithumb_jwt_headers(payload)
            res = requests.post(url, json=payload, headers=headers, proxies=PROXIES, timeout=5)
            if res.status_code in [200, 201]:
                return True, res.json().get("uuid")
            else:
                logging.error(f"실전 주문 실패: {res.text}")
                return False, res.text
        except Exception as e:
            logging.error(f"주문 API 에러: {e}")
            return False, str(e)

    # ==========================================
    # 3. 제미나이 AI 연동
    # ==========================================
    def call_gemini_analysis(self, symbol: str, ohlcv_data):
        try:
            if not Config.GEMINI_API_KEY:
                return ohlcv_data[-1][3] * 0.98, 80

            candles_summary = "\n".join([f"Open:{x[1]}, High:{x[2]}, Low:{x[3]}, Close:{x[4]}, Vol:{x[5]}" for x in ohlcv_data[-30:]])
            prompt = f"""
            [스캘핑 퀀트 차트 패턴 분석]
            종목: {symbol}
            최근 5분봉 30개 요약:
            {candles_summary}
            
            다음 JSON 형식으로만 응답해라:
            {{"detected_pattern": "SUPPORT_BOUNCE", "pattern_score": 85, "structural_support_price": 0.0}}
            """

            headers = {"Content-Type": "application/json"}
            payload = {"contents": [{"parts": [{"text": prompt}]}]}
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash-lite:generateContent?key={Config.GEMINI_API_KEY}"
            
            res = requests.post(url, headers=headers, json=payload, timeout=6, proxies=PROXIES)
            raw_text = res.json()['candidates'][0]['content']['parts'][0]['text']
            
            match = re.search(r'\{.*\}', raw_text, re.DOTALL)
            if match:
                parsed = json.loads(match.group(0))
                return float(parsed.get('structural_support_price', ohlcv_data[-1][3] * 0.98)), int(parsed.get('pattern_score', 80))
        except Exception as e:
            logging.error(f"제미나이 AI 분석 에러 ({symbol}): {e}")
        
        return ohlcv_data[-1][3] * 0.98, 80

    # ==========================================
    # 4. 퀀트 스캘핑 루프
    # ==========================================
    def check_macro_guard(self) -> bool:
        try:
            url = f"https://api.bithumb.com/public/candlestick/BTC_KRW/1h"
            res = requests.get(url, proxies=PROXIES, timeout=5).json()
            if res.get("status") == "0000":
                closes = [float(x[2]) for x in res['data'][-50:]]
                ema_50 = sum(closes) / len(closes)
                current_btc = closes[-1]
                if current_btc < ema_50:
                    if not self.macro_bearish:
                        self.send_telegram("⚠️ Macro Guard 경보: 비트코인 추세 이탈로 신규 매매 차단.")
                    self.macro_bearish = True
                    return True
        except Exception:
            pass
        self.macro_bearish = False
        return False

    def manage_exits(self):
        for symbol, pos in list(self.active_positions.items()):
            try:
                res = requests.get(f"https://api.bithumb.com/public/ticker/{symbol}_KRW", proxies=PROXIES, timeout=2).json()
                if res.get("status") != "0000": continue
                curr_price = float(res['data']['closing_price'])

                if curr_price > pos['peak_price']:
                    pos['peak_price'] = curr_price

                pnl_pct = (curr_price - pos['entry_price']) / pos['entry_price']
                elapsed_min = (datetime.now() - pos['entry_time']).total_seconds() / 60

                if curr_price <= pos['stop_loss']:
                    self.execute_sell(symbol, "HARD_STOP_LOSS", curr_price)
                    continue

                if pnl_pct >= 0.012:
                    if (pos['peak_price'] - curr_price) / pos['peak_price'] >= 0.005:
                        self.execute_sell(symbol, "TRAILING_STOP", curr_price)
                        continue

                if elapsed_min >= 35:
                    self.execute_sell(symbol, "TIME_LIMIT_EXPIRED", curr_price)
                    continue
            except Exception as e:
                logging.error(f"exit_manager 에러 ({symbol}): {e}")

    def execute_buy(self, symbol: str, support_price: float):
        if len(self.active_positions) >= Config.MAX_POSITIONS: return
        try:
            res = requests.get(f"https://api.bithumb.com/public/ticker/{symbol}_KRW", proxies=PROXIES, timeout=3).json()
            if res.get("status") != "0000": return
            entry_price = float(res['data']['closing_price'])

            allocation_krw = self.get_bithumb_krw_balance()
            amount = allocation_krw / entry_price
            stop_loss = support_price * (1 - Config.STOP_LOSS_BUFFER)

            success, _ = self.execute_order(symbol, "bid", amount)
            if success:
                self.active_positions[symbol] = {
                    'entry_price': entry_price, 'amount': amount, 'allocation_krw': allocation_krw,
                    'stop_loss': stop_loss, 'peak_price': entry_price, 'entry_time': datetime.now()
                }
                mode_str = "[PAPER]" if Config.IS_PAPER_TRADING else "[LIVE]"
                self.send_telegram(f"🛒 매수 체결 ({symbol}/KRW) {mode_str}\n- 진입가: {entry_price:,.0f}원\n- 손절가: {stop_loss:.1f}원")
        except Exception as e:
            logging.error(f"execute_buy 에러: {e}")

    def execute_sell(self, symbol: str, reason: str, exit_price: float):
        try:
            pos = self.active_positions[symbol]
            success, _ = self.execute_order(symbol, "ask", pos['amount'])
            if not success: return

            pnl_pct = (exit_price - pos['entry_price']) / pos['entry_price']
            profit_krw = pos['allocation_krw'] * pnl_pct
            exit_time_str = datetime.now().strftime("%H:%M")

            self.trade_history.insert(0, {'time': exit_time_str, 'symbol': f"{symbol}/KRW", 'pnl_pct': pnl_pct * 100, 'profit_krw': profit_krw, 'reason': reason})
            self.trade_history = self.trade_history[:10]
            del self.active_positions[symbol]

            total = len(self.trade_history)
            wins = sum(1 for t in self.trade_history if t['pnl_pct'] > 0)
            win_rate = (wins / total * 100) if total > 0 else 0.0
            cum_pnl = sum(t['pnl_pct'] for t in self.trade_history)

            mode_str = "[PAPER]" if Config.IS_PAPER_TRADING else "[LIVE]"
            msg = f"📤 청산 완료 ({symbol}/KRW) {mode_str}\n- 사유: {reason}\n- 실현 손익: {pnl_pct*100:+.2f}% ({profit_krw:+,.0f}원)\n\n"
            msg += f"📊 [최근 히스토리 통계 ({total}개)]\n- 전적: {total}전 {wins}승 (승률: {win_rate:.1f}%) | 합산: {cum_pnl:+.2f}%"
            self.send_telegram(msg)
        except Exception as e:
            logging.error(f"execute_sell 에러: {e}")

    def run_slow_loop_worker(self):
        while True:
            try:
                if self.check_macro_guard():
                    time.sleep(60)
                    continue

                if len(self.active_positions) >= Config.MAX_POSITIONS:
                    time.sleep(10)
                    continue

                res = requests.get("https://api.bithumb.com/public/ticker/ALL_KRW", proxies=PROXIES, timeout=5).json()
                if res.get("status") != "0000":
                    time.sleep(10)
                    continue

                candidates = []
                for symbol, info in res["data"].items():
                    if symbol == "date" or not symbol.endswith('_KRW'): continue
                    if float(info.get('acc_trade_value_24H', 0)) >= Config.MIN_TRADING_VALUE:
                        candidates.append(symbol.replace('_KRW', ''))

                for symbol in candidates[:5]:
                    if len(self.active_positions) >= Config.MAX_POSITIONS: break

                    c_res = requests.get(f"https://api.bithumb.com/public/candlestick/{symbol}_KRW/5m", proxies=PROXIES, timeout=5).json()
                    if c_res.get("status") != "0000": continue
                    ohlcv = [[int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])] for x in c_res['data'][-40:]]
                    
                    if symbol not in self.support_cache:
                        support_price, score = self.call_gemini_analysis(symbol, ohlcv)
                        self.support_cache[symbol] = {'support_price': support_price, 'pattern_score': score, 'timestamp': datetime.now()}

                    cache = self.support_cache[symbol]
                    if cache['pattern_score'] >= 80:
                        closes = [x[4] for x in ohlcv]
                        volumes = [x[5] for x in ohlcv]
                        current_price = closes[-1]
                        ema_20 = sum(closes[-20:]) / 20
                        avg_vol = sum(volumes[-21:-1]) / 20
                        prev_vol = volumes[-2]

                        gap_pct = (current_price - cache['support_price']) / current_price
                        if Config.GAP_MIN <= gap_pct <= Config.GAP_MAX and current_price > ema_20 and prev_vol >= (avg_vol * 2.5):
                            self.execute_buy(symbol, cache['support_price'])
            except Exception as e:
                logging.error(f"slow_loop 에러: {e}")
            time.sleep(60)

    def run_fast_loop(self):
        while True:
            try:
                self.manage_exits()
            except Exception as e:
                logging.error(f"fast_loop 에러: {e}")
            time.sleep(2)

if __name__ == "__main__":
    bot = QuantScalpingBot()
    bot.run_fast_loop()
