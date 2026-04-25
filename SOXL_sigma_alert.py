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

def get_data_backup(ticker):
    """API 우선 시도 후 실패 시 야후 파이낸스로 백업 (이중 잠금)"""
    url = f"https://api.massiveapi.com/v1/market/candles"
    params = {"symbol": ticker, "interval": "d", "limit": 250, "apikey": MASSIVE_API_KEY}
    
    # 1차 시도: Massive API
    try:
        res = requests.get(url, params=params, timeout=5)
        if res.status_code == 200:
            data = res.json()
            return [float(item['close']) for item in data['candles']]
    except Exception:
        pass

    # 2차 시도: Yahoo Finance
    try:
        df = yf.download(ticker, period="3y", auto_adjust=True, progress=False)
        if not df.empty:
            return df["Close"].dropna().values.flatten().tolist()
    except Exception:
        return None

def calculate_rsi(closes, period=14):
    """Wilder's RSI - 시장의 심리적 과열 상태 측정"""
    if len(closes) < period + 1: return 50.0
    df = pd.DataFrame({'close': closes[-60:]})
    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-10)
    return 100 - (100 / (1 + rs)).iloc[-1]

def main():
    for ticker in TICKERS:
        closes = get_data_backup(ticker)
        if not closes or len(closes) < 30: continue

        latest_price = closes[-1]
        
        # [핵심] 최근 20일 변동성(σ)을 시장 상황에 맞춰 스스로 계산
        log_returns_20d = np.diff(np.log(closes[-21:]))
        dynamic_sigma = np.std(log_returns_20d)
        
        # RSI 및 추세 확인
        current_rsi = calculate_rsi(closes)
        ma200 = sum(closes[-200:]) / 200 if len(closes) >= 200 else closes[0]
        is_bull = latest_price > ma200
        
        # LOC 매수 포인트 계산
        p1 = latest_price * (1 - dynamic_sigma)
        p2 = latest_price * (1 - 2 * dynamic_sigma)

        # 상태 리포트 작성
        status = "🔥 불 마켓" if is_bull else "🛡️ 베어 마켓"
        sentiment = "🔴 과매수" if current_rsi >= 70 else "✅ 과매도" if current_rsi <= 30 else "⚪ 중립"

        msg = (
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 **{ticker} 자율주행 리포트**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ 현재 추세: {status} ({sentiment})\n"
            f"🌡️ 심리 지수: RSI {current_rsi:.1f}\n"
            f"💰 현재 종가: ${latest_price:.2f}\n"
            f"📍 적용 시그마(20D): {dynamic_sigma*100:.2f}%\n\n"
            f"🎯 **오늘의 낚시 포인트 (LOC)**\n"
            f"   - 1단계(-1σ): **${p1:.2f}**\n"
            f"   - 2단계(-2σ): **${p2:.2f}**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⏰ 시각: {(datetime.now() + timedelta(hours=9)).strftime('%Y-%m-%d %H:%M')}\n"
            f"*(데이터 백업 시스템 가동 중)*"
        )
        
        print(msg)
        if WEBHOOK_URL:
            requests.post(WEBHOOK_URL, json={"content": msg})

if __name__ == "__main__":
    main()
