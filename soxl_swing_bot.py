import os
import time
import pandas as pd
import requests

# ==========================================
# [사용자 설정 영역]
# ==========================================
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "YOUR_FINNHUB_API_KEY")
DISCORD_WEBHOOK = os.getenv(
    "DISCORD_WEBHOOK_URL", "YOUR_DISCORD_WEBHOOK_URL"
)
DISCORD_USER_ID = os.getenv("DISCORD_USER_ID", "YOUR_DISCORD_USER_ID")
TICKER = "SOXL"


def fetch_finnhub_hourly_data(ticker, api_key):
  """Finnhub API를 통해 최근 1시간봉 데이터 수집"""
  end_time = int(time.time())
  start_time = end_time - (60 * 24 * 60 * 60)  # 60일 전

  url = f"https://finnhub.io/api/v1/stock/candle?symbol={ticker}&resolution=60&from={start_time}&to={end_time}&token={api_key}"
  response = requests.get(url)
  data = response.json()

  if data.get("s") != "ok":
    print(f"[오류] 데이터 로드 실패: {data}")
    return pd.DataFrame()

  df = pd.DataFrame({
      "Open": data["o"],
      "High": data["h"],
      "Low": data["l"],
      "Close": data["c"],
      "Volume": data["v"],
  }, index=pd.to_datetime(data["t"], unit="s"))

  return df


def send_discord_alert(message):
  """디스코드 웹훅 알림 전송"""
  if not DISCORD_WEBHOOK or DISCORD_WEBHOOK == "YOUR_DISCORD_WEBHOOK_URL":
    print(message)
    return

  content = (
      f"<@{DISCORD_USER_ID}> \n{message}"
      if DISCORD_USER_ID
      else message
  )
  requests.post(DISCORD_WEBHOOK, json={"content": content})


def run_swing_strategy():
  print(
      f"[{TICKER}] 3중 EMA + 변동성 수축 + [첫 번째 신호 필터링] 전략 분석 시작..."
  )

  # 1. 1시간봉 수집 및 4시간봉 리샘플링
  df_1h = fetch_finnhub_hourly_data(TICKER, FINNHUB_API_KEY)
  if df_1h.empty:
    return

  df_4h = df_1h.resample("4h").agg({
      "Open": "first",
      "High": "max",
      "Low": "min",
      "Close": "last",
      "Volume": "sum",
  }).dropna()

  # 2. 지표 계산 (EMA 10, 20, 50)
  df_4h["EMA_10"] = df_4h["Close"].ewm(span=10, adjust=False).mean()
  df_4h["EMA_20"] = df_4h["Close"].ewm(span=20, adjust=False).mean()
  df_4h["EMA_50"] = df_4h["Close"].ewm(span=50, adjust=False).mean()

  # 3. 거래량 표준편차 조건
  df_4h["Vol_SMA"] = df_4h["Volume"].rolling(window=20).mean()
  df_4h["Vol_Std"] = df_4h["Volume"].rolling(window=20).std()
  df_4h["Vol_Breakout"] = df_4h["Volume"] > (
      df_4h["Vol_SMA"] + df_4h["Vol_Std"]
  )

  # 4. 전체 기간을 순회하며 '하락 후 첫 신호'를 카운트 및 필터링
  signal_state = "WAITING"  # WAITING(하락/대기), FIRST_FOUND(첫 신호 감지됨, 거름), READY_FOR_BUY(두 번째 이후 정상 매수 가능)
  consecutive_signals = 0

  for i in range(50, len(df_4h)):
    sub_df = df_4h.iloc[: i + 1]
    latest = sub_df.iloc[-1]

    # 조건 체크
    is_aligned = (
        latest["EMA_10"] > latest["EMA_20"]
    ) and (latest["EMA_20"] > latest["EMA_50"])
    recent_highs = sub_df["High"].iloc[-6:-1].max()
    is_breakout = (
        is_aligned
        and latest["Vol_Breakout"]
        and (latest["Close"] > recent_highs)
    )

    # 장기 이평선(EMA 50) 아래에 있다면 확실한 하락/조정 구간으로 판단하여 상태 리셋
    if latest["Close"] < latest["EMA_50"]:
      signal_state = "WAITING"

    # 매수 조건 만족 시
    if is_breakout:
      if signal_state == "WAITING":
        # ⚠️ 하락 추세 직후 나타난 '첫 번째 매수 신호' -> 영상 내용대로 과감히 건너뜀!
        signal_state = "FIRST_FOUND"
      elif signal_state == "FIRST_FOUND":
        # 첫 번째 신호를 거른 뒤, 변동성 수축을 거쳐 나타난 '두 번째 진입 타이밍' -> 실전 매수 인정!
        signal_state = "READY_FOR_BUY"

  # 5. 최신 4시간봉 최종 판별
  latest = df_4h.iloc[-1]
  current_close = latest["Close"]
  time_str = df_4h.index[-1].strftime("%Y-%m-%d %H:%M")

  # 최종 시그널 판정
  is_aligned_latest = (
      latest["EMA_10"] > latest["EMA_20"]
  ) and (latest["EMA_20"] > latest["EMA_50"])
  recent_highs_latest = df_4h["High"].iloc[-6:-1].max()
  is_current_breakout = (
      is_aligned_latest
      and latest["Vol_Breakout"]
      and (current_close > recent_highs_latest)
  )

  is_sell_signal = current_close < latest["EMA_20"]

  print(f"분석 기준 시간(4시간봉): {time_str} | 종가: ${current_close:.2f}")

  if is_current_breakout:
    if signal_state == "FIRST_FOUND":
      print(
          "[필터링됨] 하락 후 첫 번째 반등 신호입니다. 영상 가이드에 따라"
          " 매수를 건너뜁니다."
      )
    elif signal_state == "READY_FOR_BUY":
      msg = (
          f"🚨 **[{TICKER}] 4시간봉 스윙 매수(BUY) 시그널 포착!**\n"
          f"- 시간: {time_str}\n"
          f"- 가격: ${current_close:.2f}\n"
          f"- 상태: 첫 번째 속임수 신호 필터링 완료 후, 정식 돌파 매수 타점 도달"
      )
      send_discord_alert(msg)
  elif is_sell_signal:
    msg = (
        f"⚠️ **[{TICKER}] 4시간봉 스윙 매도/익절(SELL) 시그널 포착!**\n"
        f"- 시간: {time_str}\n"
        f"- 가격: ${current_close:.2f}\n"
        f"- 상태: 20 EMA 이탈"
    )
    send_discord_alert(msg)
  else:
    print("현재 조건에 부합하는 새로운 시그널이 없습니다. (HOLD 상태)")


if __name__ == "__main__":
  run_swing_strategy()