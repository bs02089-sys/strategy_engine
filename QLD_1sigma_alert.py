import os
import pandas as pd
import numpy as np
import yfinance as yf
import requests
import subprocess
from dotenv import load_dotenv
from datetime import timedelta
from zoneinfo import ZoneInfo
from scipy.optimize import minimize  # 포트폴리오 비중(MDD) 최적화용

# ==================== 설정 ====================
TICKERS = ["QLD"]
TEST_LOOKBACK_DAYS = 252 * 5
FEES = 0.00065
K_FIXED = 10.0  # TP 고정 k 값

# ==================== .env 로드 ====================
load_dotenv()
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK")

# ==================== 디스코드 알림 ====================
def send_discord_message(content: str):
    if not WEBHOOK_URL:
        raise RuntimeError("❌ Webhook URL이 설정되지 않았습니다.")
    try:
        resp = requests.post(WEBHOOK_URL, json={"content": content}, timeout=10)
        if resp.status_code in (200, 204):
            print("✅ 디스코드 알림 전송 성공")
        else:
            print(f"❌ 디스코드 알림 실패: {resp.status_code} / {resp.text}")
    except Exception as e:
        print(f"❌ 디스코드 알림 예외: {e}")

# ==================== 데이터 로딩 ====================
def load_data():
    ny_now = pd.Timestamp.now(tz=ZoneInfo("Asia/Seoul")).normalize().tz_localize(None)
    start_date = (ny_now - timedelta(days=TEST_LOOKBACK_DAYS + 7)).date()
    end_date = (ny_now + timedelta(days=1)).date()
    data = yf.download(TICKERS, start=start_date, end=end_date, auto_adjust=True, progress=False)["Close"]

    # 티커 컬럼 강제 유지
    close = data.reindex(columns=TICKERS)

    # 데일리 리턴 계산 (빈 방지: fillna(0))
    daily_return = close.pct_change().fillna(0)

    # 최소 2행 이상 확보 (없으면 더미 데이터 추가)
    if daily_return.empty or len(daily_return) < 2:
        today = pd.Timestamp.now().normalize()
        daily_return = pd.DataFrame({t: [0.0] for t in TICKERS}, index=[today])
        close = pd.DataFrame({t: [0.0] for t in TICKERS}, index=[today])

    return close, daily_return

close, daily_return = load_data()

# ==================== σ 및 거래횟수 계산 ====================
def calc_sigma_and_trades(returns: pd.DataFrame):
    sigma = {}
    trades = {}
    for t in TICKERS:
        if t not in returns.columns or returns[t].empty:
            sigma[t], trades[t] = np.nan, 0
            continue
        rr = returns[t].dropna()
        sigma[t] = float(rr.tail(252).std())
        vol_roll = rr.rolling(252, min_periods=120).std()
        ret_5y = rr.tail(252 * 5)
        vol_5y = vol_roll.reindex(ret_5y.index)
        mask = (~ret_5y.isna()) & (~vol_5y.isna()) & (vol_5y > 0) & (ret_5y <= -vol_5y)
        total_events = int(mask.sum())
        if len(ret_5y) > 1:
            years = (ret_5y.index[-1] - ret_5y.index[0]).days / 365.25
        else:
            years = 0
        annual_events = total_events / years if years > 0 else 0.0
        trades[t] = int(round(annual_events))
    return sigma, trades

# ==================== 최신 값 추출 ====================
def get_latest_values(symbol: str):
    try:
        ret_today = float(daily_return[symbol].iloc[-1])
        current_price = float(close[symbol].iloc[-1])
        return ret_today, current_price
    except (IndexError, KeyError):
        return None, None

# ==================== 메시지 생성 ====================
def build_alert_messages():
    sigma, trades = calc_sigma_and_trades(daily_return)
    now_kst = pd.Timestamp.now(tz=ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M:%S")
    messages = []

    for symbol in TICKERS:
        if symbol not in daily_return.columns or daily_return[symbol].empty:
            messages.append(f"❌ {symbol} 데이터 누락으로 분석 불가")
            continue

        ret_today, current_price = get_latest_values(symbol)
        if ret_today is None or current_price is None:
            messages.append(f"❌ {symbol} 현재 값 추출 실패")
            continue

        condition_met = ret_today <= -sigma[symbol]
        ret_str = f"+{ret_today*100:.2f}%" if ret_today > 0 else f"{ret_today*100:.2f}%"
        sigma_down = current_price * (1.0 - sigma[symbol])
        tp_pct = K_FIXED * sigma[symbol] * 100.0

        message = (
            f"📉 [{symbol} 매수 신호 체크]\n"
            f"알림 발생 시각: {now_kst}\n"
            f"1시그마: {sigma[symbol]*100:.2f}% (도달가격: ${sigma_down:.2f})\n"
            f"최근 5년 평균 거래횟수(롤링): {trades[symbol]}회/년\n"
            f"현재 가격: ${current_price:.2f}\n"
            f"전일 대비: {ret_str}\n"
            f"매수 조건 충족: {'✅ Yes' if condition_met else '❌ No'}\n"
            f"TP (고정 k={K_FIXED}): {tp_pct:.2f}%"
        )
        messages.append(message)

    return "\n\n".join(messages)

# ==================== 월간 Ping (선택) ====================
def monthly_ping():
    now_kst = pd.Timestamp.now(tz=ZoneInfo("Asia/Seoul"))
    if now_kst.day == 1:
        send_discord_message(f"✅ Monthly Ping: 시스템 정상 작동 중 ({now_kst.strftime('%Y-%m-%d %H:%M:%S')})")

# ==================== 실행 ====================
if __name__ == "__main__":
    final_message = build_alert_messages()
    print(final_message)
    send_discord_message(final_message)
    # 필요 시 월간 핑 활성화
    # monthly_ping()
    # 자동 푸시 (원하면 주석 해제)
    # import subprocess
    # subprocess.run(["git", "add", "QLD_1sigma_alert.py"], check=True)
    # subprocess.run(["git", "commit", "-m", "Auto update alert script (separated logic)"], check=True)
    # subprocess.run(["git", "push", "origin", "main"], check=True)
