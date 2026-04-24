import os
import numpy as np
import requests
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
from dotenv import load_dotenv

# 환경변수 로드
load_dotenv()
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK")
MASSIVE_API_KEY = os.getenv("MASSIVE_API_KEY") 

TICKERS = ["SOXL"]
SIGMA_FIXED = 0.083  # 불 마켓 고정 σ (8.3%)

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
    """Wilder's RSI - 극단 상황에서도 안정적으로 계산"""
    if len(closes) < period + 1:
        return None
    
    # 최근 60일만 사용 (더 현실적이고 빠른 반응)
    closes_recent = closes[-60:] if len(closes) > 60 else closes
    
    df = pd.DataFrame({'close': closes_recent})
    delta = df['close'].diff()
    
    # Wilder's smoothing 방식
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    
    avg_gain = gain.ewm(alpha=1/period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period).mean()
    
    # loss가 0에 너무 가까울 때 방지
    avg_loss = avg_loss.replace(0, 1e-10)
    
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    
    return rsi.iloc[-1] if not pd.isna(rsi.iloc[-1]) else 50.0  # 안전값

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

        current_rsi = calculate_rsi(closes)
        
        p1 = latest_price * (1 - sigma)
        p2 = latest_price * (1 - 2 * sigma)

        if current_rsi is None:
            rsi_status = " | RSI 계산 오류"
        else:
            rsi_status = f" | RSI(14): {current_rsi:.1f}"
            if current_rsi <= 30:
                rsi_status += " ✅ 강한 과매도"
            elif current_rsi <= 40:
                rsi_status += " ⚠️ 과매도"
            elif current_rsi >= 70:
                rsi_status += " 🔴 강한 과매수"
            else:
                rsi_status += " ⚪ 중립"

        msg = (
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 **{ticker} 전략 리포트**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ 현재 상태: {status}{rsi_status}\n"
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
