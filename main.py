import os
import sys
import time
import uuid
import json
import jwt
import requests
import httpx
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
    """구글 코랩 Secrets 및 깃허브 액션 환경변수를 모두 자동 지원"""
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

# 🎯 AI Provider API 키 수집 (1순위: Gemini, 2순위: SambaNova, 3순위: Groq KEY3, 4순위: Groq KEY2)
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
# 텔레그램 알림 발송 전용 모듈
# ==========================================
def send_telegram_msg(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] 텔레그램 토큰/Chat ID 미설정으로 알림 전송을 건너끡니다.")
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
# 1. 파일 데이터 관리 및 JSON 정제 모듈
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

def clean_and_parse_json(raw_text):
    """AI 응답 텍스트에서 불필요한 사족을 제거하고 pure JSON만 슬라이싱하여 파싱"""
    if not raw_text:
        return None
    try:
        cleaned = raw_text.strip()
        if "```json" in cleaned:
            cleaned = cleaned.split("```json")[1].split("```")[0].strip()
        elif "```" in cleaned:
            cleaned = cleaned.split("
