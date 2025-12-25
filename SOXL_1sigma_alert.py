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
LOOKBACK_DAYS = 252
FEES = 0.00065
K_FIXED = 2.0  # TP 고정 k 값 (현실적으로 낮춤)

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
    start_date = (ny_now - timedelta(days=LOOKBACK_DAYS + 7)).date()
    end_date = (ny_now + timedelta(days=1)).date()
    data = yf.download(TICKERS, start=start_date, end=end_date, auto_adjust=True, progress=False)["Close"]

    close = data.reindex(columns=TICKERS)
    daily_return = close.pct_change()

    if daily_return.empty or len(daily_return) < 2:
        today = pd.Timestamp.now().normalize()
        daily_return = pd.DataFrame({t: [np.nan] for t in TICKERS}, index=[today])
        close = pd.DataFrame({t: [np.nan] for t in TICKERS}, index=[today])

    return close, daily_return

close, daily_return = load_data()

# ==================== σ 및 거래횟수 계산 ====================
def calc_sigma_and_trades(returns: pd.DataFrame):
    sigma = {}
    trades = {}
    for t in TICKERS:
        if t not in returns.columns or returns[t].dropna().empty:
            sigma[t], trades[t] = np.nan, 0
            continue
        rr = returns[t].dropna()

        # 롤링 σ (백테스트와 동일)
        vol_roll = rr.rolling(252, min_periods=120).std()
        sigma_val = vol_roll.iloc[-1] if len(vol_roll) > 0 else np.nan
        sigma[t] = float(sigma_val) if pd.notna(sigma_val) else np.nan

        # 1년치 이벤트 횟수 계산
        ret_1y = rr.tail(252)
        vol_1y = vol_roll.reindex(ret_1y.index)
        mask = (~ret_1y.isna()) & (~vol_1y.isna()) & (vol_1y > 0) & (ret_1y <= -vol_1y)
        total_events = int(mask.sum())

        if len(ret_1y) > 1:
            years = (ret_1y.index[-1] - ret_1y.index[0]).days / 365.25
        else:
            years = 0
        annual_events = total_events / years if years > 0 else 0.0
        trades[t] = int(round(annual_events))
    return sigma, trades

# ==================== 전일 종가와 현재가 추출 ====================
def get_prev_and_current_price(symbol: str):
    s = close[symbol].dropna()
    if len(s) < 2:
        return None, None
    prev_close = float(s.iloc[-2])
    current_price = float(s.iloc[-1])
    return prev_close, current_price

# ==================== 메시지 생성 ====================
def build_alert_messages():
    sigma, trades = calc_sigma_and_trades(daily_return)
    now_kst = pd.Timestamp.now(tz=ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M:%S")
    messages = []

    for symbol in TICKERS:
        if symbol not in close.columns or close[symbol].dropna().empty:
            messages.append(f"❌ {symbol} 데이터 누락으로 분석 불가")
            continue

        prev_close, current_price = get_prev_and_current_price(symbol)
        if prev_close is None or current_price is None or np.isnan(sigma[symbol]):
            messages.append(f"❌ {symbol} 현재 값 추출 실패 또는 σ 계산 불가")
            continue

        ret_today = (current_price / prev_close) - 1.0
        condition_met = ret_today <= -sigma[symbol]
        ret_str = f"+{ret_today*100:.2f}%" if ret_today > 0 else f"{ret_today*100:.2f}%"
        sigma_down_price = prev_close * (1.0 - sigma[symbol])
        tp_pct = K_FIXED * sigma[symbol] * 100.0

        message = (
            f"📉 [{symbol} 매수 신호 체크]\n"
            f"알림 발생 시각: {now_kst}\n"
            f"1σ (롤링): {sigma[symbol]*100:.2f}% (도달가격: ${sigma_down_price:.2f})\n"
            f"최근 1년 이벤트 횟수(롤링): {trades[symbol]}회/년\n"
            f"전일 종가: ${prev_close:.2f}\n"
            f"현재 가격: ${current_price:.2f}\n"
            f"전일 대비: {ret_str}\n"
            f"매수 조건 충족: {'✅ Yes' if condition_met else '❌ No'}\n"
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
