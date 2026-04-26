import os
import numpy as np
import requests
import yfinance as yf
import pandas as pd
from datetime import datetime
from dotenv import load_dotenv

# 환경변수 로드
load_dotenv()
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK")
MASSIVE_API_KEY = os.getenv("MASSIVE_API_KEY") 

TICKERS = ["SOXL"]
MIN_SIGMA_BULL = 0.10
MAX_SIGMA_BEAR = 0.20

# ⭐ [설정] SOXL 평균 매수 단가 (매수 후 이 값을 업데이트하세요)
MY_AVG_PRICE = 100.00 

def get_data_backup(ticker):
    """Massive API 우선 시도 후 실패 시 야후 파이낸스로 백업"""
    url = f"https://api.massiveapi.com/v1/market/candles"
    params = {"symbol": ticker, "interval": "d", "limit": 250, "apikey": MASSIVE_API_KEY}
    try:
        res = requests.get(url, params=params, timeout=5)
        if res.status_code == 200:
            data = res.json()
            return [float(item['close']) for item in data['candles']]
    except Exception: pass
    try:
        df = yf.download(ticker, period="3y", auto_adjust=True, progress=False)
        if not df.empty: return df["Close"].dropna().values.flatten().tolist()
    except Exception: return None

def calculate_rsi(closes, period=14):
    """Wilder's RSI 계산"""
    if len(closes) < period + 1: return 50.0
    df = pd.DataFrame({'close': closes[-60:]})
    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0); loss = -delta.where(delta < 0, 0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-10)
    return 100 - (100 / (1 + rs)).iloc[-1]

def main():
    for ticker in TICKERS:
        closes = get_data_backup(ticker)
        if not closes or len(closes) < 200: continue

        latest_price = closes[-1]
        log_returns_40d = np.diff(np.log(closes[-41:]))
        dynamic_sigma = np.std(log_returns_40d)
        ma200 = sum(closes[-200:]) / 200
        is_bull = latest_price > ma200
        
        if is_bull:
            dynamic_sigma = max(dynamic_sigma, MIN_SIGMA_BULL)
            status = "🔥 불 마켓"
        else:
            dynamic_sigma = min(dynamic_sigma, MAX_SIGMA_BEAR)
            status = "🛡️ 베어 마켓"

        current_rsi = calculate_rsi(closes)
        bear_signal = (not is_bull) and (current_rsi < 40)
        bull_recovery_signal = is_bull and (current_rsi >= 50)
        p1 = latest_price * (1 - dynamic_sigma); p2 = latest_price * (1 - 2 * dynamic_sigma)
        sentiment = "🚨 강한 과매수" if current_rsi >= 80 else "🔴 과매수" if current_rsi >= 70 else "✅ 과매도" if current_rsi <= 30 else "⚪ 중립"

        # 🎯 낚시 포인트
        if current_rsi >= 80:
            loc_section = f"⚠️ **강한 과매수 구간** (-1σ 보류 추천)\n\n   📍 2단계(-2σ) 몰빵 전략 가동: **${p2:.2f}**\n"
        elif bear_signal:
            loc_section = f"🚨 **베어 마켓 신호 감지** (리밸런싱 추천)\n\n   👉 SPYM 75% / SOXL 25% 로 즉시 조정\n   - 1단계: ${p1:.2f} / 2단계: ${p2:.2f}\n"
        elif bull_recovery_signal:
            loc_section = f"✅ **불 마켓 전환 신호 감지** (리밸런싱 추천)\n\n   👉 SPYM 50% / SOXL 50% 로 즉시 조정\n   - 1단계: ${p1:.2f} / 2단계: ${p2:.2f}\n"
        else:
            loc_section = f"   - 1단계(-1σ): **${p1:.2f}** (30%)\n   - 2단계(-2σ): **${p2:.2f}** (70%)\n"

        # 🛑 매도 규칙
        if MY_AVG_PRICE > 0:
            targets = [("+50%", 1.5), ("+100%", 2.0), ("+200%", 3.0), ("+300%", 4.0)]
            exit_lines = []
            hit_signals = []

            for label, mult in targets:
                target_p = MY_AVG_PRICE * mult
                if latest_price >= target_p:
                    status_icon = "🎊 **[도달! 지금 매도하세요]**"
                    hit_signals.append(f"💰 **{label} 익절 구간 도달!**")
                else:
                    status_icon = "⚪"
                exit_lines.append(f"   • **{label} 익절가**:  **${target_p:.2f}** {status_icon}")

            current_status = "\n".join(hit_signals) if hit_signals else "아직 목표가에 도달하지 않았습니다. 느긋하게 기다리세요. 🧘"
            exit_section = "🛑 **매도 규칙 (실시간 감시 중)**\n" + "\n".join(exit_lines) + f"\n\n📢 **익절 상태**: {current_status}\n"
        else:
            exit_section = "🛑 **매도 규칙**: `MY_AVG_PRICE`를 설정하면 감시가 시작됩니다.\n"

        msg = (
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 **{ticker} 자율주행 리포트**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ 현재 추세: {status} ({sentiment})\n"
            f"🌡️ 심리 지수: RSI {current_rsi:.1f}\n"
            f"💰 전일 종가: ${latest_price:.2f}\n"
            f"📍 적용 시그마(40D): {dynamic_sigma*100:.2f}%\n\n"
            f"🎯 낚시 포인트 (LOC)\n"
            f"{loc_section}\n"
            f"📌 배분 규칙: -1σ 30% / -2σ 70% (과매수 시 조절 가능)\n\n"
            f"{exit_section}"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⏰ 시각: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
            f"*(데이터 백업 시스템 가동 중)*"
        )
        
        print(msg)
        if WEBHOOK_URL: requests.post(WEBHOOK_URL, json={"content": msg})

if __name__ == "__main__":
    main()
