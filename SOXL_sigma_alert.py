import os
import numpy as np
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv
import yfinance as yf

# 환경변수 로드
load_dotenv()
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK")
MASSIVE_API_KEY = os.getenv("MASSIVE_API_KEY")   

# 설정값
TICKERS = ["SOXL"]   # 여러 종목을 넣을 수 있음
SIGMA_FIXED = 0.083  # 불 마켓일 때 사용할 고정 σ 값 (8.3%)

# ==================== 유틸 ====================
def kst_now_str():
    """한국 표준시 현재 시각 문자열"""
    now = datetime.now() + timedelta(hours=9)
    return now.strftime("%Y-%m-%d %H:%M:%S")

def send_discord(content: str):
    """Discord 웹훅으로 메시지 전송"""
    if not WEBHOOK_URL:
        print("⚠️ DISCORD_WEBHOOK이 설정되지 않았습니다.")
        return
    try:
        requests.post(WEBHOOK_URL, json={"content": content})
    except Exception as e:
        print(f"⚠️ Discord 전송 실패: {e}")

# ==================== 시그마 계산 로직 ====================
def get_sigma_and_levels(ticker):
    """불 마켓 여부 판정 후 σ 값과 -1σ, -2σ 가격 계산"""
    df = yf.download(ticker, period="3y", auto_adjust=True, progress=False)
    close = df["Close"].dropna().astype(float)

    # 200일 이동평균
    ma200 = close.rolling(200).mean()

    # 전일 종가와 200일선 값
    latest_price = close.iloc[-1].item()
    latest_ma200 = ma200.iloc[-1].item()

    # 불 마켓 여부 판정
    is_bull_market = latest_price > latest_ma200

    # σ 값 선택
    if is_bull_market:
        sigma_value = SIGMA_FIXED
    else:
        sigma_value = np.log(close / close.shift(1)).dropna().std().item()

    # -1σ, -2σ 가격 수준 계산
    price_1sigma = latest_price * (1 - sigma_value)
    price_2sigma = latest_price * (1 - 2 * sigma_value)

    return sigma_value, is_bull_market, latest_price, price_1sigma, price_2sigma

# ==================== 실행 ====================
if __name__ == "__main__":
    for ticker in TICKERS:
        sigma, bull, latest, p1, p2 = get_sigma_and_levels(ticker)
        msg = (
            f"✅ {ticker} σ 값: {sigma*100:.2f}% (불 마켓={bull}) | {kst_now_str()}\n"
            f"   전일 종가: {latest:.2f}\n"
            f"   -1σ 가격: {p1:.2f}\n"
            f"   -2σ 가격: {p2:.2f}"
        )
        print(msg)
        send_discord(msg)
