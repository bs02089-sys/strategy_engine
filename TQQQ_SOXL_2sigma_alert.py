import os
import pandas as pd
import numpy as np
import yfinance as yf
import requests
from dotenv import load_dotenv
from datetime import timedelta
from zoneinfo import ZoneInfo

# ==================== 설정 ====================
TICKERS = ["TQQQ", "SOXL"]
LOOKBACK_TRADING_DAYS = 252
FEES = 0.00065  # 현재 알림에는 사용하지 않지만 유지

# ==================== .env 로드 ====================
load_dotenv()
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK")

# ==================== 디스코드 알림 ====================
def send_discord_message(content: str):
    if not WEBHOOK_URL:
        raise RuntimeError("❌ Webhook URL이 설정되지 않았습니다.")
    try:
        resp = requests.post(WEBHOOK_URL, json={"content": f"@everyone {content}"}, timeout=10)
        if resp.status_code in (200, 204):
            print("✅ 디스코드 알림 전송 성공")
        else:
            print(f"❌ 디스코드 알림 실패: {resp.status_code} / {resp.text}")
    except Exception as e:
        print(f"❌ 디스코드 알림 예외: {e}")

# ==================== 데이터 로딩 ====================
def load_data():
    now = pd.Timestamp.now(tz=ZoneInfo("Asia/Seoul")).normalize().tz_localize(None)
    start_date = (now - timedelta(days=LOOKBACK_TRADING_DAYS + 150)).date()
    end_date = (now + timedelta(days=1)).date()
    data = yf.download(TICKERS, start=start_date, end=end_date, auto_adjust=True, progress=False)
    close = data["Close"].reindex(columns=TICKERS).dropna(how="all")
    return close

close = load_data()

# ==================== σ 계산 (오늘 제외) ====================
def compute_sigma(close_series: pd.Series, window: int = LOOKBACK_TRADING_DAYS) -> float | None:
    s = pd.Series(close_series).dropna()
    returns = s.pct_change().dropna()
    if len(returns) < window + 1:
        return None
    sigma = returns.iloc[-window-1:-1].std()
    return float(sigma) if np.isfinite(sigma) else None

# ==================== 전일 종가와 현재가 ====================
def get_prev_and_current_price(symbol: str):
    s = close[symbol].dropna()
    if len(s) < 2:
        return None, None
    prev_close = s.iloc[-2]
    current_price = s.iloc[-1]
    prev_close = prev_close.item() if hasattr(prev_close, "item") else float(prev_close)
    current_price = current_price.item() if hasattr(current_price, "item") else float(current_price)
    return prev_close, current_price

# ==================== 메시지 생성 ====================
def build_alert_messages():
    now_kst = pd.Timestamp.now(tz=ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M:%S")
    messages = []

    for symbol in TICKERS:
        if symbol not in close.columns or close[symbol].dropna().empty:
            messages.append(f"❌ {symbol} 데이터 누락으로 분석 불가")
            continue

        prev_close, current_price = get_prev_and_current_price(symbol)
        sigma = compute_sigma(close[symbol])
        if prev_close is None or current_price is None or sigma is None:
            messages.append(f"❌ {symbol} 시그마 계산 불가 (데이터 부족)")
            continue

        # 2σ 기준
        sigma2 = 2 * sigma
        threshold_2 = prev_close * (1 - sigma2)

        # 오늘 수익률
        ret_today = (current_price / prev_close) - 1.0
        ret_str = f"+{ret_today*100:.2f}%" if ret_today > 0 else f"{ret_today*100:.2f}%"

        # 매수 조건
        buy_signal = current_price <= threshold_2

        message = (
            f"📉 [{symbol} 매수 신호 체크]\n"
            f"알림 발생 시각: {now_kst}\n"
            f"2σ (전일까지 252일): {sigma2*100:.2f}% (도달가격: ${threshold_2:.2f})\n"
            f"전일 종가: ${prev_close:.2f}\n"
            f"현재 가격: ${current_price:.2f}\n"
            f"전일 대비: {ret_str}\n"
            f"매수 조건 충족: {'✅ 2σ' if buy_signal else '❌ No'}"
        )
        messages.append(message)

    return "\n\n".join(messages)

# ==================== 월간 Ping ====================
def monthly_ping():
    now_kst = pd.Timestamp.now(tz=ZoneInfo("Asia/Seoul"))
    if now_kst.day == 1:
        send_discord_message(f"✅ Monthly Ping: 시스템 정상 작동 중 ({now_kst.strftime('%Y-%m-%d %H:%M:%S')})")

# ==================== 실행 ====================
if __name__ == "__main__":
    final_message = build_alert_messages()
    print(final_message)
    send_discord_message(final_message)
    monthly_ping()
