import os
import numpy as np
import requests
import yfinance as yf
from datetime import datetime, timedelta
from dotenv import load_dotenv

# 환경변수 로드
load_dotenv()
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK")
MASSIVE_API_KEY = os.getenv("MASSIVE_API_KEY") 

TICKERS = ["SOXL"]
SIGMA_FIXED = 0.083  # 불 마켓 고정 σ (8.3%)

def get_data_backup(ticker):
    """1순위 Massive API 실패 시 2순위 yfinance로 데이터 보충"""
    url = f"https://api.massiveapi.com/v1/market/candles"
    params = {"symbol": ticker, "interval": "d", "limit": 250, "apikey": MASSIVE_API_KEY}
    
    try:
        res = requests.get(url, params=params, timeout=5)
        if res.status_code == 200:
            data = res.json()
            return [float(item['close']) for item in data['candles']]
    except Exception:
        pass # 실패 시 조용히 yfinance로 전환

    try:
        df = yf.download(ticker, period="3y", auto_adjust=True, progress=False)
        if not df.empty:
            return df["Close"].dropna().values.flatten().tolist()
    except Exception:
        return None

def main():
    for ticker in TICKERS:
        closes = get_data_backup(ticker)
        if not closes or len(closes) < 200:
            continue

        latest_price = closes[-1]
        ma200 = sum(closes[-200:]) / 200
        is_bull = latest_price > ma200
        
        if is_bull:
            sigma = SIGMA_FIXED
            status = "🔥 불 마켓 (상승장)"
        else:
            log_returns = np.diff(np.log(closes))
            sigma = np.std(log_returns)
            status = "🛡️ 베어 마켓 (하락장)"

        p1, p2 = latest_price * (1 - sigma), latest_price * (1 - 2 * sigma)

        msg = (
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 **{ticker} 전략 리포트**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ 현재 상태: {status}\n"
            f"💰 전일 종가: ${latest_price:.2f}\n"
            f"📍 적용 시그마: {sigma*100:.2f}%\n\n"
            f"🎯 **LOC 매수 가이드**\n"
            f"   - 1단계(-1σ): **${p1:.2f}**\n"
            f"   - 2단계(-2σ): **${p2:.2f}**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⏰ 시각: {(datetime.now() + timedelta(hours=9)).strftime('%Y-%m-%d %H:%M')}"
        )
        
        print(msg)
        if WEBHOOK_URL:
            requests.post(WEBHOOK_URL, json={"content": msg})

if __name__ == "__main__":
    main()
