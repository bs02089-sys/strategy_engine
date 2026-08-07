from datetime import datetime
import os
import time
import requests
import pandas as pd
import yfinance as yf


# 환경 변수에서 디스코드 웹훅 가져오기
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "YOUR_DISCORD_WEBHOOK")
DISCORD_USER_ID = os.getenv("DISCORD_USER_ID", "YOUR_DISCORD_USER_ID")

# 환경 변수가 설정되지 않았을 경우를 대비한 안전장치
if not DISCORD_WEBHOOK:
  raise ValueError(
      "❌ DISCORD_WEBHOOK 환경 변수가 설정되지 않았습니다. .env 파일을"
      " 확인해주세요."
  )

# 모니터링할 종목 리스트 
TICKERS = ["TQQQ","SOXL"]


def send_discord_webhook(message):
  """디스코드 채널로 메시지를 전송하는 함수"""
  payload = {"content": message}
  try:
    response = requests.post(DISCORD_WEBHOOK, json=payload)
    if response.status_code == 204:
      print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 알림 전송 성공")
    else:
      print(f"전송 실패: {response.status_code}, {response.text}")
  except Exception as e:
    print(f"웹훅 전송 중 에러 발생: {e}")


def calculate_buy_signal(df, ticker):
  """15분봉 데이터 기반 상승 FVG 및 매수(진입/손절/익절) 시그널 생성 함수"""
  fvg_list = []
  for i in range(2, len(df)):
    c1_high = df["High"].iloc[i - 2]
    c3_low = df["Low"].iloc[i]

    if c3_low > c1_high:
      fvg_midpoint = (c1_high + c3_low) / 2
      fvg_list.append({
          "time": df.index[i],
          "bottom": c1_high,
          "top": c3_low,
          "midpoint": fvg_midpoint,
          "c3_low_idx": i,
      })

  if not fvg_list:
    return None

  latest_close = df["Close"].iloc[-1]
  latest_time = df.index[-1]
  latest_fvg = fvg_list[-1]

  # 현재 가격이 FVG 50% 되돌림 존 근처(0.2% 이내)에 도달했는지 확인
  in_fvg_zone = (
      latest_fvg["bottom"] <= latest_close <= latest_fvg["top"]
      or abs(latest_close - latest_fvg["midpoint"]) / latest_fvg["midpoint"]
      < 0.002
  )

  if in_fvg_zone:
    entry_price = latest_fvg["midpoint"]

    lookback_window = min(10, len(df))
    stop_loss = df["Low"].iloc[-lookback_window:].min()

    if stop_loss >= entry_price:
      stop_loss = entry_price * 0.99

    risk = entry_price - stop_loss
    take_profit = entry_price + (risk * 3.5)

    return {
        "ticker": ticker,
        "time": latest_time,
        "entry": entry_price,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "risk_reward_ratio": "1 : 3.5",
    }

  return None


def run_strategy():
  print("반자동 매매 FVG 전략 스캐너 실행 중...")
  for ticker in TICKERS:
    try:
      df = yf.download(ticker, period="5d", interval="15m", progress=False)
      if df.empty:
        continue

      if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

      signal = calculate_buy_signal(df, ticker)

      if signal:
        message = (
            f"🚨 **[매수(Long) 시그널 포착]** `{signal['ticker']}`\n"
            f"• **시간:** `{str(signal['time'])}`\n"
            f"• **추천 진입가 (Entry):** `{signal['entry']:.2f}`\n"
            f"• **손절가 (Stop Loss):** `{signal['stop_loss']:.2f}` 🛑\n"
            f"• **목표 익절가 (Take Profit):** `{signal['take_profit']:.2f}`"
            f" 🎯\n"
            f"• **손익비 (RR):** `{signal['risk_reward_ratio']}`\n\n"
            f"👉 1분봉 진입 모델 확인 후 수동/반자동 주문을 진행하세요!"
        )
        send_discord_webhook(message)

    except Exception as e:
      print(f"[{ticker}] 분석 중 오류 발생: {e}")


def main():
  while True:
    run_strategy()
    time.sleep(900)


if __name__ == "__main__":
  main()