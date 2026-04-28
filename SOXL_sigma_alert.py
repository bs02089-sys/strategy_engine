import os
import json
import numpy as np
import requests
import yfinance as yf
import pandas as pd
import subprocess
from datetime import datetime
from dotenv import load_dotenv

# 환경변수 로드
load_dotenv()
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK")
MASSIVE_API_KEY = os.getenv("MASSIVE_API_KEY") 

TICKERS = ["SOXL"]
MIN_SIGMA_BULL = 0.10
MAX_SIGMA_BEAR = 0.20

# ---------------------------------------------------------
# ⭐ JSON에서 데이터 불러오기 (항상 현재 파일 위치 기준)
# ---------------------------------------------------------
base_dir = os.path.dirname(__file__)   # backtest 폴더
config_path = os.path.join(base_dir, "config.json")

with open(config_path, "r") as f:
    config = json.load(f)

MY_AVG_PRICE = config["MY_AVG_PRICE"]
MY_TOTAL_SHARES = config["MY_TOTAL_SHARES"]
ANNUAL_QUOTA = config["ANNUAL_QUOTA"]
CURRENT_USED = config["CURRENT_USED"]
PREMARKET_PRICE = config["PREMARKET_PRICE"]
# ---------------------------------------------------------

def get_data_backup(ticker):
    url = f"https://api.massiveapi.com/v1/market/candles"
    params = {"symbol": ticker, "interval": "d", "limit": 250, "apikey": MASSIVE_API_KEY}
    try:
        res = requests.get(url, params=params, timeout=5)
        if res.status_code == 200:
            data = res.json()
            return [float(item['close']) for item in data['candles']]
    except Exception:
        pass
    try:
        df = yf.download(ticker, period="3y", auto_adjust=True, progress=False)
        if not df.empty:
            return df["Close"].dropna().values.flatten().tolist()
    except Exception:
        return None

def calculate_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    df = pd.DataFrame({'close': closes[-60:]})
    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-10)
    return 100 - (100 / (1 + rs)).iloc[-1]

def main():
    global CURRENT_USED

    for ticker in TICKERS:
        closes = get_data_backup(ticker)
        if not closes or len(closes) < 200:
            continue

        latest_close = closes[-1]
        current_rsi = calculate_rsi(closes)
        ma200 = sum(closes[-200:]) / 200

        # 불마켓 정의: MA200 위 + RSI ≥ 50
        if latest_close > ma200 and current_rsi >= 50:
            is_bull = True
        elif latest_close < ma200 and current_rsi <= 50:
            is_bull = False
        else:
            is_bull = None

        # 시그마 계산
        if is_bull is True:
            sentiment = "🔥 불 마켓"
            sigma_label = "40D"
            log_returns = np.diff(np.log(closes[-41:]))
            dynamic_sigma = np.std(log_returns)
            dynamic_sigma = max(dynamic_sigma, MIN_SIGMA_BULL)
        else:
            sentiment = "🛡️ 베어/중립"
            sigma_label = "1Y"
            log_returns = np.diff(np.log(closes[-252:])) if len(closes) >= 252 else np.diff(np.log(closes))
            dynamic_sigma = np.std(log_returns)
            dynamic_sigma = min(dynamic_sigma, MAX_SIGMA_BEAR)

        p1 = latest_close * (1 - dynamic_sigma)
        p2 = latest_close * (1 - 2 * dynamic_sigma)

        remaining = ANNUAL_QUOTA - CURRENT_USED
        return_str = f"{((latest_close - MY_AVG_PRICE) / MY_AVG_PRICE) * 100:+.2f}%" if MY_AVG_PRICE > 0 else "평단가 정보 없음"

        premarket_msg = ""
        skip_p1 = False
        if PREMARKET_PRICE > 0 and PREMARKET_PRICE <= p1:
            premarket_msg = f"⚡ 프리마켓 지정가 매수 체결 (${PREMARKET_PRICE:.2f}) → 낚시 {CURRENT_USED}/{ANNUAL_QUOTA}회\n\n"
            skip_p1 = True

        loc_msg = ""
        if not skip_p1 and latest_close <= p1 and CURRENT_USED < ANNUAL_QUOTA:
            CURRENT_USED += 1
            loc_msg += f"🎣 정규장 LOC -1σ 체결 (${p1:.2f}) → 낚시 {CURRENT_USED}/{ANNUAL_QUOTA}회\n\n"
        if latest_close <= p2 and CURRENT_USED < ANNUAL_QUOTA:
            CURRENT_USED += 1
            loc_msg += f"🎣 정규장 LOC -2σ 체결 (${p2:.2f}) → 낚시 {CURRENT_USED}/{ANNUAL_QUOTA}회\n\n"

        msg = (
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 **{ticker} 자율주행 리포트**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ 현재 추세: {sentiment}\n"
            f"🌡️ 심리 지수: RSI {current_rsi:.1f}\n"
            f"💰 전일 종가: ${latest_close:.2f}\n"
            f"📍 적용 시그마({sigma_label}): {dynamic_sigma*100:.2f}%\n\n"
            f"{premarket_msg}{loc_msg}"
            f"🎯 **오늘의 낚시 포인트 (이중 저인망 전략)**\n"
            f"   📍 -1σ 가격: **${p1:.2f}**\n"
            f"   📍 -2σ 가격: **${p2:.2f}**\n\n"
            f"📊 **시즌 낚시 현황: {CURRENT_USED}/{ANNUAL_QUOTA}회** (잔여: {remaining}회)\n"
            f"📌 규칙: 연간 20회 한정 낚시\n\n"
            f"🛑 **매도 규칙**\n"
            f"   (현재 나의 수익률: **{return_str}**)\n"
            f"   ✅ 20회 완료 후 80% SPYM 전환\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⏰ 시각: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        )

        print(msg)
        if WEBHOOK_URL:
            try:
                requests.post(WEBHOOK_URL, json={"content": msg})
            except:
                pass

    # JSON 파일 업데이트 (낚시 횟수 반영)
    config["CURRENT_USED"] = CURRENT_USED
    config["PREMARKET_PRICE"] = 0  # 장 마감 후 초기화
    with open(config_path, "w") as f:
        json.dump(config, f, indent=4)

def git_push():
    try:
        subprocess.run(["git", "add", "config.json"], check=True)
        subprocess.run(["git", "commit", "-m", "Update config.json after market close"], check=True)
        subprocess.run(["git", "push", "origin", "main"], check=True)
        print("✅ GitHub push 완료")
    except Exception as e:
        print(f"⚠️ GitHub push 실패: {e}")
if __name__ == "__main__":
    main()
    git_push()
