import os
import json
import numpy as np
import requests
import yfinance as yf
import pandas as pd
from datetime import datetime
from dotenv import load_dotenv
import subprocess   # Git push용 모듈

# ==========================================
# 1. 경로 및 본진 설정
# ==========================================
CURRENT_FILE_PATH = os.path.abspath(__file__)
WORKING_DIR = os.path.dirname(CURRENT_FILE_PATH)
os.chdir(WORKING_DIR)

# ==========================================
# 2. 환경 변수 및 설정 로드
# ==========================================
load_dotenv()
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK")
DISCORD_USER_ID = os.getenv("DISCORD_USER_ID")

config_path = os.path.join(WORKING_DIR, "config.json")
if os.path.exists(config_path):
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)
else:
    config = {"MY_AVG_PRICE": 111.05, "MY_TOTAL_SHARES": 163, "ANNUAL_QUOTA": 12, "CURRENT_USED": 1, "LAST_RUN_TIME": "N/A"}

# ==========================================
# 3. 기준값 (상수)
# ==========================================
TICKERS = ["SOXL"]
MIN_SIGMA_BULL = 0.10
MAX_SIGMA_BEAR = 0.20

ANNUAL_QUOTA = config.get("ANNUAL_QUOTA", 12)
MY_AVG_PRICE = config.get("MY_AVG_PRICE", 111.05)
CURRENT_USED = config.get("CURRENT_USED", 1)

def get_data_backup(ticker):
    try:
        df = yf.download(ticker, period="2y", auto_adjust=True, progress=False)
        if not df.empty:
            return df.sort_index(ascending=False)
    except Exception as e:
        print(f"❌ 데이터 수집 오류: {e}")
    return None

def calculate_rsi(df, period=10):
    closes = df["Close"].values.flatten()
    if len(closes) < period + 1: 
        return 50.0
    delta = pd.Series(closes).diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta > 0, 0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-10)
    return float(100 - (100 / (1 + rs.iloc[-1])))

def send_discord_message(webhook_url, message):
    try:
        response = requests.post(webhook_url, json={"content": message}, timeout=10)
        response.raise_for_status()
        print("✅ Discord 전송 성공")
    except Exception as e:
        print(f"❌ Discord 전송 실패: {e}")

def safe_git_push():
    try:
        subprocess.run(["git", "pull", "--rebase", "origin", "main"], check=True)
        subprocess.run(["git", "push", "origin", "main"], check=True)
        print("✅ Git push 성공")
    except subprocess.CalledProcessError as e:
        print(f"❌ Git push 실패: {e}")

def main():
    global CURRENT_USED
    for ticker in TICKERS:
        df = get_data_backup(ticker)
        if df is None or len(df) < 252:
            continue

        # ... (중략: RSI, 시그마, 엔벨로프 계산 및 메시지 생성 로직 동일)

        msg = (
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 **{ticker} 리포트**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            # ... (중략: 메시지 내용)
            f"⏰ 보고: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        )

        print(msg)

        if WEBHOOK_URL:
            ping = f"<@{DISCORD_USER_ID}>" if DISCORD_USER_ID else ""
            send_discord_message(WEBHOOK_URL, ping + "\n" + msg)

        # config.json 갱신
        config["LAST_RUN_TIME"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        config["CURRENT_USED"] = config.get("CURRENT_USED", 0) + 1
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)

        # 로컬에서는 안전한 git push 실행
        safe_git_push()

if __name__ == "__main__":
    main()