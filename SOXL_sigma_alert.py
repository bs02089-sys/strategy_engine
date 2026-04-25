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
MIN_SIGMA_BULL = 0.09   # 불 마켓 최소 시그마 (9%)
MAX_SIGMA_BEAR = 0.20   # 베어 마켓 최대 시그마 (20%)

def get_data_backup(ticker):
    """API 우선 시도 후 실패 시 야후 파이낸스로 백업"""
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
    """Wilder's RSI"""
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
    for ticker in TICKERS:
        closes = get_data_backup(ticker)
        if not closes or len(closes) < 50:
            continue

        latest_price = closes[-1]
        
        # 최근 40일 변동성 계산
        log_returns_40d = np.diff(np.log(closes[-41:]))
        dynamic_sigma = np.std(log_returns_40d)
        
        # 추세 판단
        ma200 = sum(closes[-200:]) / 200 if len(closes) >= 200 else closes[0]
        is_bull = latest_price > ma200
        
        # 시그마 캡 적용
        if is_bull:
            dynamic_sigma = max(dynamic_sigma, MIN_SIGMA_BULL)
            status = "🔥 불 마켓"
        else:
            dynamic_sigma = min(dynamic_sigma, MAX_SIGMA_BEAR)
            status = "🛡️ 베어 마켓"

        # RSI 계산
        current_rsi = calculate_rsi(closes)
        
        # 베어 마켓 신호 판단 (가격 < MA200 + RSI < 40)
        bear_signal = (not is_bull) and (current_rsi < 40)
        
        # LOC 매수 포인트 계산
        p1 = latest_price * (1 - dynamic_sigma)
        p2 = latest_price * (1 - 2 * dynamic_sigma)

        # 상태 메시지
        sentiment = "🔴 강한 과매수" if current_rsi >= 80 else "🔴 과매수" if current_rsi >= 70 else "✅ 과매도" if current_rsi <= 30 else "⚪ 중립"

        # LOC 섹션
        if current_rsi >= 80:  # 강한 과매수
            loc_section = (
                f"⚠️ **강한 과매수 구간** (-1σ LOC 보류 추천)\n"
                f"   - 2단계(-2σ): **${p2:.2f}** (깊은 조정 시 소량 고려)\n"
            )
        elif bear_signal:
            loc_section = (
                f"🚨 **베어 마켓 신호 감지** (가격 < MA200 + RSI < 40)\n"
                f"   ⚠️ **긴급 리밸런싱 추천**\n"
                f"      → SPYM **75%** / SOXL **25%** 로 즉시 조정\n"
                f"      → SOXL LOC는 전체 자금의 25% 이내 소량만 실행 권장\n\n"
                f"   - 1단계(-1σ): **${p1:.2f}** (소량)\n"
                f"   - 2단계(-2σ): **${p2:.2f}** (소량)\n"
            )
        else:
            loc_section = (
                f"   - 1단계(-1σ): **${p1:.2f}**\n"
                f"   - 2단계(-2σ): **${p2:.2f}**\n"
            )

        msg = (
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 **{ticker} 자율주행 리포트**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ 현재 추세: {status} ({sentiment})\n"
            f"🌡️ 심리 지수: RSI {current_rsi:.1f}\n"
            f"💰 현재 종가: ${latest_price:.2f}\n"
            f"📍 적용 시그마(40D): {dynamic_sigma*100:.2f}%\n\n"
            f"🎯 **오늘의 낚시 포인트 (LOC)**\n"
            f"{loc_section}"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 **1회 매수금 배분 규칙**: -1σ **30%** / -2σ **70%** (모든 구간 적용)\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🛑 **Exit 규칙 (중요)**\n"
            f"   • 매수 후 **+60% 상승** 시 → 보유량의 **25% 익절** 추천\n"
            f"   • 나머지는 홀드 (버티기)\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⏰ 시각: {(datetime.now() + timedelta(hours=9)).strftime('%Y-%m-%d %H:%M')}\n"
            f"*(데이터 백업 시스템 가동 중)*"
        )
        
        print(msg)
        if WEBHOOK_URL:
            requests.post(WEBHOOK_URL, json={"content": msg})

if __name__ == "__main__":
    main()
