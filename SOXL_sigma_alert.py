import os
import numpy as np
import requests
import yfinance as yf
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK")
MASSIVE_API_KEY = os.getenv("MASSIVE_API_KEY") 

TICKERS = ["SOXL"]
SIGMA_FIXED = 0.083

def get_data_backup(ticker):
    """1순위 Massive API 실패 시 2순위 yfinance로 데이터 보충 (오류 수정 완료)"""
    # 1. Massive API 시도
    url = f"https://api.massiveapi.com/v1/market/candles"
    params = {"symbol": ticker, "interval": "d", "limit": 250, "apikey": MASSIVE_API_KEY}
    
    try:
        print(f"🔄 {ticker} 데이터 호출 중 (Massive API)...")
        res = requests.get(url, params=params, timeout=5)
        if res.status_code == 200:
            data = res.json()
            # API 응답 구조에 따라 'candles' 내 'close' 추출
            return [float(item['close']) for item in data['candles']]
    except Exception:
        print(f"⚠️ Massive 접속 실패. yfinance로 전환합니다.")

    # 2. yfinance 시도 (DataFrame 처리 오류 수정)
    try:
        df = yf.download(ticker, period="3y", auto_adjust=True, progress=False)
        if not df.empty:
            # .tolist() 대신 values.flatten().tolist() 또는 간단히 list() 사용
            closes = df["Close"].dropna().values.flatten().tolist()
            return closes
    except Exception as e:
        print(f"❌ 모든 데이터 호출 실패: {e}")
    return None

def main():
    for ticker in TICKERS:
        closes = get_data_backup(ticker)
        
        # 데이터가 최소 200개는 있어야 이평선 계산이 가능합니다.
        if not closes or len(closes) < 200:
            print(f"⚠️ {ticker} 분석을 위한 충분한 데이터를 가져오지 못했습니다.")
            continue

        latest_price = closes[-1]
        # 최근 200일 종가 평균
        ma200 = sum(closes[-200:]) / 200
        
        is_bull = latest_price > ma200
        
        if is_bull:
            sigma = SIGMA_FIXED
            status = "🔥 불 마켓 (상승장)"
        else:
            # 실시간 변동성 계산
            log_returns = np.diff(np.log(closes))
            sigma = np.std(log_returns)
            status = "🛡️ 베어 마켓 (하락장)"

        p1, p2 = latest_price * (1 - sigma), latest_price * (1 - 2 * sigma)

        msg = (
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 **{ticker} 전략 리포트**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ 현재 상태: {status}\n"
            f"💰 현재 종가: ${latest_price:.2f}\n"
            f"📍 적용 시그마: {sigma*100:.2f}%\n\n"
            f"🎯 **밤 11:30 LOC 매수 가이드**\n"
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
