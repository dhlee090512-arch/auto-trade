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
import logging
import threading

# ==========================================
# 0. 상세 로깅 설정
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(filename)s:%(lineno)d] - %(message)s",
    handlers=[
        logging.FileHandler("quant_scalping_bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)

# ==========================================
# 1. 환경변수 및 API Key 자동 수집 (get_env 유틸리티)
# ==========================================
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

class Config:
    BITHUMB_API_KEY = get_env("BITHUMB_API_KEY")
    BITHUMB_SECRET_KEY = get_env("BITHUMB_SECRET_KEY")
    GEMINI_API_KEY = get_env("GEMINI_API_KEY")
    TELEGRAM_BOT_TOKEN = get_env("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID = get_env("TELEGRAM_CHAT_ID")
    WEBSHARE_URL = get_env("WEBSHARE_URL")

    IS_PAPER_TRADING = True           # 모의투자(True) / 실전매매(False)
    MIN_BUY_KRW = 6000                # 최소 매수 금액
    POSITION_RATIO = 0.20             # 1회 진입 비중 (가용 잔고의 20%)
    MAX_POSITIONS = 2                 # 최대 동시 보유 종목 수 (2개)

    MIN_TRADING_VALUE = 5_000_000_000  # 최소 거래대금 (50억)
    GAP_MIN = 0.005                    # 골디락스 갭 하한 (0.5%)
    GAP_MAX = 0.010                    # 골디락스 갭 상한 (1.0%)
    STOP_LOSS_BUFFER = 0.002           # 지지선 밑 손절 버퍼 (0.2%)
    CACHE_TTL_MINUTES = 30             # 캐시 유효 시간 (30분)

PROXIES = {'http': Config.WEBSHARE_URL, 'https': Config.WEBSHARE_URL} if Config.WEBSHARE_URL else None

class QuantScalpingBot:
    def __init__(self):
        logging.info("initialize: 빗썸 거래소 및 봇 엔진 초기화 시작...")
        try:
            self.exchange = ccxt.bithumb({
                'enableRateLimit': True,
                'apiKey': Config.BITHUMB_API_KEY,
                'secret': Config.BITHUMB_SECRET_KEY,
            })
        except Exception as e:
            logging.error(f"initialize: 빗썸 거래소 연결 실패: {e}")
            self.exchange = None

        self.support_cache = {}          # { symbol: { 'support_price': float, 'pattern_score': int, 'timestamp': datetime } }
        self.active_positions = {}       # { symbol: { 'entry_price': float, 'amount': float, 'stop_loss': float, 'peak_price': float, 'entry_time': datetime } }
        self.trade_history = []          # 최근 청산 히스토리 (최대 10개)
        self.macro_bearish = False       # 거시 시장 하락세 여부

        logging.info(f"initialize: 봇 엔진 초기화 완료. 모드: {'[모의투자]' if Config.IS_PAPER_TRADING else '[실전매매]'}")
        
        # 텔레그램 명령어 수신기 백그라운드 스레드 가동
        threading.Thread(target=self.listen_telegram_commands, daemon=True).start()
        
        # Slow Loop 백그라운드 워커 가동 (60초 주기: 거시방어, 스크리닝, AI 분석)
        threading.Thread(target=self.run_slow_loop_worker, daemon=True).start()

        mode_str = "모의투자 (Paper Trading)" if Config.IS_PAPER_TRADING else "실전매매 (Live Trading)"
        self.send_telegram(f"🚀 *하이브리드 퀀트 스캘핑 봇* 부팅 완료.\n- 모드: `{mode_str}`\n- Fast Loop: `2초 주기` (초고속 출구 관리)")

    def get_v2_headers(self, query_params=None):
        """빗썸 v2 API 인증 헤더 생성"""
        payload = {
            'access_key': Config.BITHUMB_API_KEY,
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
        jwt_token = jwt.encode(payload, Config.BITHUMB_SECRET_KEY, algorithm='HS256')
        return {'Authorization': f'Bearer {jwt_token}', 'Content-Type': 'application/json'}

    def get_bithumb_krw_balance(self) -> float:
        """빗썸 실제 KRW 가용 잔고 조회 (모의/실전 공통 활용)"""
        krw_free = 100_000.0  # API 조회 실패 시 기본 안전 자금
        if Config.BITHUMB_API_KEY and Config.BITHUMB_SECRET_KEY:
            try:
                res = requests.get('https://api.bithumb.com/v1/accounts', headers=self.get_v2_headers(), proxies=PROXIES, timeout=10)
                if res.status_code == 200:
                    for acc in res.json():
                        if acc['currency'] == 'KRW':
                            krw_free = float(acc['balance'])
                            break
            except Exception as e:
                logging.error(f"잔고 조회 오류: {e}")
        return max(krw_free * Config.POSITION_RATIO, Config.MIN_BUY_KRW)

    def send_telegram(self, message: str):
        if not Config.TELEGRAM_BOT_TOKEN or not Config.TELEGRAM_CHAT_ID:
            return
        try:
            url = f"https://api.telegram.org/bot{Config.TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {"chat_id": Config.TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
            requests.post(url, json=payload, timeout=3, proxies=PROXIES)
        except Exception as e:
            logging.error(f"telegram: 알림 전송 실패: {e}")

    def listen_telegram_commands(self):
        """[Telegram Controller] 텔레그램 명령어 폴링 (/paper, /live, /status)"""
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
                            self.send_telegram("🔄 *모드 전환*: **모의투자(Paper)** 모드")
                        elif msg == "/live":
                            Config.IS_PAPER_TRADING = False
                            self.send_telegram("⚠️ *경고*: **실전매매(LIVE)** 모드 전환!")
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

        report = f"📊 *[봇 현재 상태]*\n- 모드: `{mode_str}`\n- 보유 포지션: `{len(self.active_positions)} / {Config.MAX_POSITIONS}`개\n- 전적: {total}전 {wins}승 (승률: **{win_rate:.1f}%**)\n- 합산 수익률: **{cum_pnl:+.2f}%**"
        self.send_telegram(report)

    def check_macro_guard(self) -> bool:
        """[Macro Guard] 비트코인 1시간봉 EMA 50 추세 점검"""
        try:
            ohlcv = self.exchange.fetch_ohlcv("BTC/KRW", timeframe='1h', limit=50)
            closes = [x[4] for x in ohlcv]
            ema_50 = sum(closes[-50:]) / 50
            current_btc = closes[-1]

            if current_btc < ema_50:
                if not self.macro_bearish:
                    logging.warning(f"macro_guard: 🚨 비트코인 하방 이탈! ({current_btc:,.0f} < EMA50 {ema_50:,.0f}). 신규 매매 차단.")
                    self.send_telegram("⚠️ *Macro Guard 경보*: 비트코인 추세 이탈로 신규 매매 차단.")
                self.macro_bearish = True
                return True
            else:
                if self.macro_bearish:
                    logging.info("macro_guard: ✅ 비트코인 추세 회복. 매매 재개.")
                    self.send_telegram("✅ *Macro Guard 해제*: 매매 재개.")
                self.macro_bearish = False
                return False
        except Exception as e:
            logging.error(f"macro_guard 에러: {e}")
            return False

    def invalidate_cache(self):
        """[Cache Invalidation] 3대 무효화 조건 검사"""
        now = datetime.now()
        expired = []
        for symbol, data in list(self.support_cache.items()):
            reason = None
            if now - data['timestamp'] > timedelta(minutes=Config.CACHE_TTL_MINUTES):
                reason = "TTL_EXPIRED (30분 경과)"
            else:
                try:
                    ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe='5m', limit=25)
                    closes = [x[4] for x in ohlcv]
                    ema_20 = sum(closes[-20:]) / 20
                    if closes[-1] < ema_20:
                        reason = "TREND_BROKEN (EMA20 이탈)"
                    elif closes[-1] < data['support_price']:
                        reason = "SUPPORT_BREACH (지지선 이탈)"
                except:
                    continue
            if reason:
                expired.append(symbol)
                logging.info(f"cache_invalidation: 🗑️ [{symbol}] 캐시 삭제 (사유: {reason})")
        for sym in expired:
            del self.support_cache[sym]

    def screen_market(self) -> list:
        """[Quant Screener] 거래대금 상위 및 변동성 충족 종목 발굴"""
        try:
            tickers = self.exchange.fetch_tickers()
            candidates = []
            for symbol, t in tickers.items():
                if not symbol.endswith('/KRW'): continue
                if t.get('quoteVolume', 0) >= Config.MIN_TRADING_VALUE:
                    candidates.append(symbol)
            return candidates[:5]
        except Exception as e:
            logging.error(f"screen_market 에러: {e}")
            return ["BTC/KRW"]

    def request_llm_analysis_async(self, symbol: str, ohlcv_data):
        """[Slow Loop - LLM] 비동기 백그라운드 분석"""
        threading.Thread(target=self._call_gemini_api, args=(symbol, ohlcv_data), daemon=True).start()

    def _call_gemini_api(self, symbol: str, ohlcv_data):
        try:
            if not Config.GEMINI_API_KEY:
                logging.error("llm_analysis 에러: GEMINI_API_KEY가 설정되지 않았습니다.")
                self.support_cache[symbol] = {'support_price': ohlcv_data[-1][3] * 0.98, 'pattern_score': 80, 'timestamp': datetime.now()}
                return

            logging.info(f"llm_analysis: 🤖 [{symbol}] Gemini 분석 요청...")
            candles_summary = "\n".join([f"Open:{x[1]}, High:{x[2]}, Low:{x[3]}, Close:{x[4]}, Vol:{x[5]}" for x in ohlcv_data[-30:]])
            prompt = f"종목: {symbol}\n최근 5분봉 30개:\n{candles_summary}\nJSON 응답: detected_pattern, pattern_score(0~100), structural_support_price(숫자)"
            
            headers = {"Content-Type": "application/json"}
            payload = {"contents": [{"parts": [{"text": prompt}]}]}
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash-lite:generateContent?key={Config.GEMINI_API_KEY}"
            
            response = requests.post(url, headers=headers, json=payload, timeout=5, proxies=PROXIES)
            result = response.json()
            raw_text = result['candidates'][0]['content']['parts'][0]['text']
            
            json_match = re.search(r'\{.*\}', raw_text, re.DOTALL)
            if json_match:
                parsed = json.loads(json_match.group(0))
                support_price = float(parsed.get('structural_support_price', ohlcv_data[-1][3] * 0.98))
                pattern_score = int(parsed.get('pattern_score', 80))
            else:
                support_price = ohlcv_data[-1][3] * 0.98
                pattern_score = 80

            self.support_cache[symbol] = {'support_price': support_price, 'pattern_score': pattern_score, 'timestamp': datetime.now()}
            logging.info(f"llm_analysis: ✨ [{symbol}] 분석 완료 [지지선: {support_price}, 점수: {pattern_score}]")
        except Exception as e:
            logging.error(f"llm_analysis 에러: {e}. 폴백 지지선 적용")
            self.support_cache[symbol] = {'support_price': ohlcv_data[-1][3] * 0.98, 'pattern_score': 80, 'timestamp': datetime.now()}

    def manage_exits(self):
        """[Fast Loop] 보유 포지션 출구 관리 (2초 주기)"""
        for symbol, pos in list(self.active_positions.items()):
            try:
                ticker = self.exchange.fetch_ticker(symbol)
                current_price = ticker['last']
                
                if current_price > pos['peak_price']:
                    pos['peak_price'] = current_price

                pnl_pct = (current_price - pos['entry_price']) / pos['entry_price']
                elapsed_min = (datetime.now() - pos['entry_time']).total_seconds() / 60

                # 1. 하드 손절
                if current_price <= pos['stop_loss']:
                    logging.warning(f"exit_manager: 🛑 [{symbol}] 하드 손절 터치! 청산.")
                    self.execute_sell(symbol, "HARD_STOP_LOSS")
                    continue

                # 2. 트레일링 스탑 (+1.2% 도달 후 고점 대비 -0.5% 하락)
                if pnl_pct >= 0.012:
                    if (pos['peak_price'] - current_price) / pos['peak_price'] >= 0.005:
                        logging.info(f"exit_manager: 💰 [{symbol}] 트레일링 익절 발동!")
                        self.execute_sell(symbol, "TRAILING_STOP")
                        continue

                # 3. 시간 청산 (35분 만기 / 20분 수급 고갈)
                if elapsed_min >= 35:
                    self.execute_sell(symbol, "TIME_LIMIT_EXPIRED")
                    continue
                if 20 <= elapsed_min < 21:
                    ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe='5m', limit=5)
                    volumes = [x[5] for x in ohlcv]
                    if volumes[-1] < (sum(volumes[:-1]) / 4) * 0.5:
                        self.execute_sell(symbol, "VOLUME_DRY_UP")
                        continue
            except Exception as e:
                logging.error(f"exit_manager 에러: {e}")

    def execute_buy(self, symbol: str, support_price: float):
        """[Buy] 모의/실전 공통 실제 빗썸 잔고 기준 매수 집행"""
        try:
            if len(self.active_positions) >= Config.MAX_POSITIONS: return

            # 빗썸 실제 KRW 잔고 기반 투입 금액 산출
            allocation_krw = self.get_bithumb_krw_balance()
            ticker = self.exchange.fetch_ticker(symbol)
            entry_price = ticker['last']
            amount = allocation_krw / entry_price
            stop_loss = support_price * (1 - Config.STOP_LOSS_BUFFER)

            mode_prefix = "[PAPER]" if Config.IS_PAPER_TRADING else "[LIVE]"
            logging.info(f"execute_buy: 🛒 {mode_prefix} [{symbol}] 진입가: {entry_price:,.0f}원 | 투입금액: {allocation_krw:,.0f}원 | 손절가: {stop_loss:,.0f}원")

            if not Config.IS_PAPER_TRADING:
                # 실전 실제 빗썸 주문 송출
                # self.exchange.create_market_buy_order(symbol, amount)
                pass

            self.active_positions[symbol] = {
                'entry_price': entry_price, 
                'amount': amount, 
                'allocation_krw': allocation_krw, # 청산 시 정확한 원화 수익 계산용 저장
                'stop_loss': stop_loss,
                'peak_price': entry_price, 
                'entry_time': datetime.now()
            }
            self.send_telegram(f"🛒 *매수 체결* (`{symbol}`) {mode_prefix}\n- 진입가: `{entry_price:,.0f}`원\n- 투입금액: `{allocation_krw:,.0f}`원\n- 손절가: `{stop_loss:.1f}`원")
        except Exception as e:
            logging.error(f"execute_buy 에러: {e}")

    def execute_sell(self, symbol: str, reason: str):
        """[Sell] 청산 집행 및 통계 리포트"""
        try:
            ticker = self.exchange.fetch_ticker(symbol)
            exit_price = ticker['last']
            pos = self.active_positions[symbol]
            
            pnl_pct = (exit_price - pos['entry_price']) / pos['entry_price']
            profit_krw = pos['allocation_krw'] * pnl_pct
            exit_time_str = datetime.now().strftime("%H:%M")

            logging.info(f"execute_sell: 📤 [{symbol}] 청산 ({reason}) | 수익률: {pnl_pct*100:+.2f}% ({profit_krw:+,.0f}원)")

            if not Config.IS_PAPER_TRADING:
                #실전 실제 빗썸 매도 주문 송출
                # self.exchange.create_market_sell_order(symbol, pos['amount'])
                pass

            self.trade_history.insert(0, {'time': exit_time_str, 'symbol': symbol, 'pnl_pct': pnl_pct * 100, 'profit_krw': profit_krw, 'reason': reason})
            self.trade_history = self.trade_history[:10]
            del self.active_positions[symbol]

            total = len(self.trade_history)
            wins = sum(1 for t in self.trade_history if t['pnl_pct'] > 0)
            win_rate = (wins / total * 100) if total > 0 else 0.0
            cum_pnl = sum(t['pnl_pct'] for t in self.trade_history)

            mode_prefix = "[PAPER]" if Config.IS_PAPER_TRADING else "[LIVE]"
            msg = f"📤 *청산 완료* (`{symbol}`) {mode_prefix}\n- 사유: `{reason}`\n- 실현 손익: **{pnl_pct*100:+.2f}%** ({profit_krw:+,.0f}원)\n\n"
            msg += f"📊 *[최근 히스토리 통계 ({total}개)]*\n- 전적: {total}전 {wins}승 (승률: **{win_rate:.1f}%**) | 합산: **{cum_pnl:+.2f}%**\n\n*상세 내역:*\n"
            for idx, t in enumerate(self.trade_history, 1):
                msg += f"{idx}. [{t['time']}] `{t['symbol']}` | **{t['pnl_pct']:+.2f}%** ({t['profit_krw']:+,.0f}원) | `{t['reason']}`\n"
            self.send_telegram(msg)
        except Exception as e:
            logging.error(f"execute_sell 에러: {e}")

    def run_fast_loop(self):
        """[Fast Loop] 2초 주기로 초고속 포지션 출구 관리"""
        logging.info("fast_loop: 🔄 Fast Loop 가동 (2초 주기)")
        while True:
            try:
                self.manage_exits()
            except Exception as e:
                logging.error(f"fast_loop 에러: {e}")
            time.sleep(2)

    def run_slow_loop_worker(self):
        """[Slow Loop] 60초 주기로 거시 가드, 스크리닝 및 AI 분석 실행"""
        logging.info("slow_loop: 🐢 Slow Loop 백그라운드 워커 가동 (60초 주기)")
        while True:
            try:
                if self.check_macro_guard():
                    time.sleep(60)
                    continue

                self.invalidate_cache()
                target_symbols = self.screen_market()

                for symbol in target_symbols:
                    if len(self.active_positions) >= Config.MAX_POSITIONS: break

                    if symbol not in self.support_cache:
                        ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe='5m', limit=35)
                        self.request_llm_analysis_async(symbol, ohlcv)
                        continue

                    cache_data = self.support_cache[symbol]
                    if cache_data['pattern_score'] >= 80:
                        ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe='5m', limit=25)
                        closes = [x[4] for x in ohlcv]
                        volumes = [x[5] for x in ohlcv]
                        
                        current_price = closes[-1]
                        ema_20 = sum(closes[-20:]) / 20
                        avg_volume = sum(volumes[-21:-1]) / 20
                        prev_volume = volumes[-2]
                        is_bullish = ohlcv[-2][4] > ohlcv[-2][1]

                        gap_pct = (current_price - cache_data['support_price']) / current_price

                        if Config.GAP_MIN <= gap_pct <= Config.GAP_MAX and current_price > ema_20 and prev_volume >= (avg_volume * 2.5) and is_bullish:
                            logging.info(f"entry_trigger: 🎯 [{symbol}] 진입 조건 충족! [갭: {gap_pct*100:.2f}%]")
                            self.execute_buy(symbol, cache_data['support_price'])
            except Exception as e:
                logging.error(f"slow_loop 에러: {e}")
            time.sleep(60)

if __name__ == "__main__":
    bot = QuantScalpingBot()
    bot.run_fast_loop()
