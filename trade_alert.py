import yfinance as yf
import requests
import os
from dotenv import load_dotenv

# 환경변수 로드
load_dotenv()
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK")
DISCORD_USER_ID = os.getenv("DISCORD_USER_ID")

# 스크립트 위치 기준으로 작업 디렉토리 고정
WORKING_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(WORKING_DIR)

ticker = "SSO"
data = yf.download(ticker, period="120d", auto_adjust=True)
close_prices = data["Close"].squeeze()

daily_returns = close_prices.pct_change()
rolling_std = daily_returns.rolling(window=20).std() * 100

std_20d_avg   = float(rolling_std.tail(20).mean())
prev_close    = float(close_prices.iloc[-1])
prev_date     = data.index[-1].strftime('%Y-%m-%d')
current_price = float(yf.Ticker(ticker).fast_info["last_price"])

take_profit = prev_close * (1 + std_20d_avg / 100)
buy_target  = prev_close * (1 - std_20d_avg / 100)

mention = f"<@{DISCORD_USER_ID}>" if DISCORD_USER_ID else ""

# 실행 시각 확인 (UTC 기준)
from datetime import datetime, timezone
utc_hour = datetime.now(timezone.utc).hour

# 오전 9시 (UTC 0시) → 무조건 현황 알림
if utc_hour == 0:
    message = (
        f"{mention}\n"
        f"```\n"
        f"{'='*55}\n"
        f"  {ticker} 오전 현황 ({prev_date})\n"
        f"{'='*55}\n"
        f"  전일 종가 : ${prev_close:.2f}\n"
        f"  현재가    : ${current_price:.2f}\n"
        f"  익절 목표 : ${take_profit:.2f}  # 전일종가 × (1 + 20일평균σ {std_20d_avg:.4f}%)\n"
        f"  매수 목표 : ${buy_target:.2f}  # 전일종가 × (1 - 20일평균σ {std_20d_avg:.4f}%)\n"
        f"{'='*55}\n"
        f"```"
    )
    requests.post(WEBHOOK_URL, json={"content": message})
    print("오전 현황 알림 전송 완료!")

# 오후 7시 (UTC 10시) → 조건 충족 시에만 알림
elif utc_hour == 10:
    if current_price >= take_profit:
        alert_type = "🔴 익절 알림"
        alert_line = f"  현재가 ${current_price:.2f} → 익절 목표가 ${take_profit:.2f} 도달!"
    elif current_price <= buy_target:
        alert_type = "🟢 매수 알림"
        alert_line = f"  현재가 ${current_price:.2f} → 매수 목표가 ${buy_target:.2f} 도달!"
    else:
        print(f"조건 미충족 | 현재가: ${current_price:.2f} | 익절: ${take_profit:.2f} | 매수: ${buy_target:.2f}")
        exit()

    message = (
        f"{mention}\n"
        f"```\n"
        f"{'='*55}\n"
        f"  {ticker} ({prev_date}) {alert_type}\n"
        f"{'='*55}\n"
        f"  전일 종가 : ${prev_close:.2f}\n"
        f"  현재가    : ${current_price:.2f}\n"
        f"  익절 목표 : ${take_profit:.2f}  # 전일종가 × (1 + 20일평균σ {std_20d_avg:.4f}%)\n"
        f"  매수 목표 : ${buy_target:.2f}  # 전일종가 × (1 - 20일평균σ {std_20d_avg:.4f}%)\n"
        f"{'─'*55}\n"
        f"{alert_line}\n"
        f"{'='*55}\n"
        f"```"
    )
    requests.post(WEBHOOK_URL, json={"content": message})
    print(f"오후 얼럿 전송 완료! [{alert_type}]")