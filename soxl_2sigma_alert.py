import os
import yfinance as yf
import pandas as pd
import numpy as np
import requests
import yaml
from datetime import datetime, timedelta
from pathlib import Path

# ==================== 설정 불러오기 ====================
def load_config():
    config_path = Path(__file__).parent / "config.yml"
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    return {}

config = load_config()

# 환경변수에서 웹훅 가져오기 (필수)
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

if not DISCORD_WEBHOOK_URL:
    raise ValueError(
        "환경변수 DISCORD_WEBHOOK_URL이 설정되지 않았습니다.\n"
        "설정 방법 예시:\n"
        "  Windows: set DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...\n"
        "  Mac/Linux: export DISCORD_WEBHOOK_URL='https://discord.com/api/webhooks/...'"
    )

# config.yml에서 가져오거나 기본값 사용
TICKER = config.get("ticker", "SOXL")
LOOKBACK_DAYS = config.get("lookback_days", 60)
ROLLING_WINDOW = config.get("rolling_window", 20)
SIGMA_MULTIPLIER = config.get("sigma_multiplier", 2.0)
# ======================================================

def send_discord_message(content: str):
    """디스코드 웹훅으로 메시지 전송"""
    data = {"content": content}
    try:
        response = requests.post(DISCORD_WEBHOOK_URL, json=data, timeout=10)
        if response.status_code == 204:
            print("✅ 디스코드 알림 전송 성공")
        else:
            print(f"❌ 디스코드 전송 실패: {response.status_code} - {response.text}")
    except Exception as e:
        print(f"❌ 디스코드 전송 중 오류: {e}")

def check_signal():
    end_date = datetime.now()
    start_date = end_date - timedelta(days=LOOKBACK_DAYS)

    print(f"📡 {TICKER} 데이터 불러오는 중...")
    df = yf.download(
        TICKER,
        start=start_date.strftime("%Y-%m-%d"),
        end=end_date.strftime("%Y-%m-%d"),
        progress=False
    )

    if df.empty:
        print("❌ 데이터를 가져오지 못했습니다.")
        return

    df['Return'] = df['Close'].pct_change()
    df['Vol_20'] = df['Return'].rolling(window=ROLLING_WINDOW).std()
    df['Sigma2'] = df['Vol_20'] * SIGMA_MULTIPLIER

    latest = df.dropna().iloc[-1]
    prev_close = df['Close'].iloc[-2]
    latest_close = float(latest['Close'].iloc[0] if hasattr(latest['Close'], 'iloc') else latest['Close'])
    latest_return = float(latest['Return'])
    latest_sigma2 = float(latest['Sigma2'])

    is_buy_signal = latest_return < -latest_sigma2

    print("=" * 55)
    print(f"체크 시간      : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"종목           : {TICKER}")
    print(f"전일 종가      : ${prev_close:.2f}")
    print(f"당일 종가      : ${latest_close:.2f}")
    print(f"당일 수익률    : {latest_return*100:.2f}%")
    print(f"20일 롤링 2σ   : {latest_sigma2*100:.2f}%")
    print(f"매수 신호      : {'✅ 발생!' if is_buy_signal else '❌ 없음'}")
    print("=" * 55)

    if is_buy_signal:
        message = (
            f"🚨 **{TICKER} 20일 롤링 {SIGMA_MULTIPLIER}σ 매수 신호 발생!** 🚨\n\n"
            f"**날짜**: {latest.name.strftime('%Y-%m-%d')}\n"
            f"**종가**: ${latest_close:.2f}\n"
            f"**하락률**: {latest_return*100:.2f}%\n"
            f"**{SIGMA_MULTIPLIER}σ 임계값**: {latest_sigma2*100:.2f}%\n"
            f"**전일 종가**: ${prev_close:.2f}\n\n"
            f"→ 전일 종가 대비 **{abs(latest_return)*100:.2f}%** 하락하여 매수 조건 충족!"
        )
        send_discord_message(message)
    else:
        print("오늘은 매수 신호가 없습니다.")

if __name__ == "__main__":
    check_signal()