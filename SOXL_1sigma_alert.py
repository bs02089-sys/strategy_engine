import os
import pandas as pd
import numpy as np
import yfinance as yf
import requests
from dotenv import load_dotenv
from datetime import timedelta
from zoneinfo import ZoneInfo

# ==================== 설정 ====================
TICKERS = ["SOXL"]
LOOKBACK_TRADING_DAYS = 252   # CNBC 방식: 최근 252 거래일
FEES = 0.00065
K_FIXED = 2.0

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
    ny_now = pd.Timestamp.now(tz=ZoneInfo("Asia/Seoul")).normalize().tz_localize(None)
    start_date = (ny_now - timedelta(days=LOOKBACK_TRADING_DAYS + 50)).date()  # 버퍼 포함
    end_date = (ny_now + timedelta(days=1)).date()
    data = yf.download(TICKERS, start=start_date, end=end_date, auto_adjust=True, progress=False)
    close = data["Close"].reindex(columns=TICKERS)
    return close

close = load_data()

# ==================== CNBC 방식 σ 계산 ====================
def compute_sigma(close_series: pd.Series):
    returns = close_series.pct_change().dropna()
    if len(returns) >= LOOKBACK_TRADING_DAYS:
        sigma = returns.tail(LOOKBACK_TRADING_DAYS).std()
    else:
        sigma = returns.std()
    sigma = float(sigma)
    return sigma if not np.isnan(sigma) else None

# ==================== 전일 종가와 현재가 추출 ====================
def get_prev_and_current_price(symbol: str):
    s = close[symbol].dropna()
    if len(s) < 2:
        return None, None
    prev_close = s.iloc[-2].item()
    current_price = s.iloc[-1].item()
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
            messages.append(f"❌ {symbol} 현재 값 추출 실패 또는 σ 계산 불가")
            continue

        sigma2 = 2 * sigma
        sigma_down_price = prev_close * (1.0 - sigma)
        sigma2_down_price = prev_close * (1.0 - sigma2)

        # 오늘 수익률
        ret_today = (current_price / prev_close) - 1.0
        ret_str = f"+{ret_today*100:.2f}%" if ret_today > 0 else f"{ret_today*100:.2f}%"

        # 매수 조건
        cond_1sigma = current_price <= sigma_down_price
        cond_2sigma = current_price <= sigma2_down_price
        tp_pct = K_FIXED * sigma * 100.0

        message = (
            f"📉 [{symbol} 매수 신호 체크]\n"
            f"알림 발생 시각: {now_kst}\n"
            f"1σ: {sigma*100:.2f}% (도달가격: ${sigma_down_price:.2f})\n"
            f"2σ: {sigma2*100:.2f}% (도달가격: ${sigma2_down_price:.2f})\n"
            f"전일 종가: ${prev_close:.2f}\n"
            f"현재 가격: ${current_price:.2f}\n"
            f"전일 대비: {ret_str}\n"
            f"매수 조건 충족: {'✅ 2σ' if cond_2sigma else ('✅ 1σ' if cond_1sigma else '❌ No')}\n"
            f"TP (고정 k={K_FIXED}): {tp_pct:.2f}%"
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
