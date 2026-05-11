import os
import sys
import logging
from datetime import datetime
import pandas as pd
import requests
import yfinance as yf
from dotenv import load_dotenv
import pytz

# 환경변수 로드
load_dotenv()
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK")
DISCORD_USER_ID = os.getenv("DISCORD_USER_ID")
TICKER = os.getenv("TICKER", "SSO")

if not DISCORD_WEBHOOK:
    raise ValueError("❌ DISCORD_WEBHOOK 환경변수가 설정되지 않았습니다.")

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

KST = pytz.timezone('Asia/Seoul')

def get_market_data(ticker: str):
    """시장 데이터 다운로드 및 계산"""
    data = yf.download(ticker, period="130d", auto_adjust=True, progress=False)
    if data.empty:
        raise ValueError(f"{ticker} 데이터를 가져올 수 없습니다.")

    close_prices = data["Close"].squeeze()
    daily_returns = close_prices.pct_change().dropna()
    rolling_std = daily_returns.rolling(window=20).std() * 100
    std_20d_avg = float(rolling_std[-20:].mean())

    if len(rolling_std) < 20 or pd.isna(std_20d_avg):
        logger.warning("변동성 계산 데이터 부족 → 기본값 1.8% 사용")
        std_20d_avg = 1.8

    prev_close = float(close_prices.iloc[-1])
    current_price = float(yf.Ticker(ticker).fast_info["last_price"])

    return {
        "prev_close": prev_close,
        "current_price": current_price,
        "take_profit": prev_close * (1 + std_20d_avg / 100),
        "buy_target": prev_close * (1 - std_20d_avg / 100),
        "std_20d_avg": std_20d_avg,
    }

def create_message(data: dict, ticker: str):
    kst_now = datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')
    to_tp = (data['take_profit'] - data['current_price']) / data['current_price'] * 100
    to_buy = (data['buy_target'] - data['current_price']) / data['current_price'] * 100

    return (
        f"🔔 **{ticker} 시장 현황**\n\n"
        f"📍 현재 시각 : {kst_now}\n\n"
        f"💰 전일 종가 : ${data['prev_close']:.2f}\n"
        f"📊 현재가 : ${data['current_price']:.2f}\n"
        f"🎯 익절 목표 : ${data['take_profit']:.2f} (+{to_tp:.2f}%)\n"
        f"🛒 매수 목표 : ${data['buy_target']:.2f} ({to_buy:.2f}%)"
    )

def send_discord_message(content: str):
    mention = f"<@{DISCORD_USER_ID}>" if DISCORD_USER_ID else ""
    message = f"{mention}\n{content}" if mention else content
    response = requests.post(DISCORD_WEBHOOK, json={"content": message}, timeout=10)
    response.raise_for_status()
    logger.info("✅ Discord 메시지 전송 성공")

def main():
    try:
        data = get_market_data(TICKER)
        message = create_message(data, TICKER)
        send_discord_message(f"```\n{message}\n```")
    except Exception as e:
        logger.error(f"❌ 실행 중 오류 발생: {e}")
        raise

if __name__ == "__main__":
    main()
