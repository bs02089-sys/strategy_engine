import os
import sys
from datetime import datetime
import time
import requests
import pandas as pd
import yfinance as yf

# ==========================================
# 1. 환경 변수 로드 및 예외 처리
# ==========================================
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY")
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK")
DISCORD_USER_ID = os.getenv("DISCORD_USER_ID")

if not FINNHUB_API_KEY:
  print(
      "[오류] FINNHUB_API_KEY 환경 변수가 설정되지 않았습니다.",
      file=sys.stderr,
  )
  sys.exit(1)

if not DISCORD_WEBHOOK:
  print(
      "[오류] DISCORD_WEBHOOK 환경 변수가 설정되지 않았습니다.",
      file=sys.stderr,
  )
  sys.exit(1)


# ==========================================
# 2. 디스코드 알림 발송 함수
# ==========================================
def send_discord_alert(message):
  """디스코드 웹훅을 통해 메시지를 전송합니다. 유저 아이디가 있으면 멘션합니다."""
  mention = f"<@{DISCORD_USER_ID}> " if DISCORD_USER_ID else ""
  payload = {"content": f"{mention}\n{message}"}

  try:
    response = requests.post(DISCORD_WEBHOOK, json=payload, timeout=5)
    if response.status_code != 204:
      print(f"[디스코드 경고] 전송 실패 (상태 코드: {response.status_code})")
  except Exception as e:
    print(f"[디스코드 오류] 예외 발생: {e}")


# ==========================================
# 3. Finnhub 현재가 조회 함수 (옵션 활용 예시)
# ==========================================
def get_finnhub_quote(symbol):
  """Finnhub API를 사용하여 실시간 시세를 조회합니다."""
  url = f"https://finnhub.io/api/v1/quote?symbol={symbol}&token={FINNHUB_API_KEY}"
  try:
    res = requests.get(url, timeout=5)
    if res.status_code == 200:
      data = res.json()
      return data.get("c")  # 현재가 (Current price)
  except Exception as e:
    print(f"[Finnhub 오류] 시세 조회 실패: {e}")
  return None


# ==========================================
# 4. 박스돌파 전략 백테스팅 및 시그널 감지 코어
# ==========================================
def run_strategy(ticker_symbol="SOXL"):
  print(f"[{datetime.now()}] {ticker_symbol} 전략 실행 시작...")

  # Finnhub 실시간 가격 테스트 조회
  current_live_price = get_finnhub_quote(ticker_symbol)
  if current_live_price:
    print(f"[*] Finnhub 연동 성공 - {ticker_symbol} 현재가: {current_live_price}")

  # yfinance를 통한 데이터 수집 (예: SOXL 등 관심 종목)
  data = yf.download(
      ticker_symbol, period="5d", interval="5m", prepost=True, progress=False
  )

  if isinstance(data.columns, pd.MultiIndex):
    data.columns = data.columns.get_level_values(0)

  data["Datetime"] = data.index
  data["Date"] = data["Datetime"].dt.date
  data["Time"] = data["Datetime"].dt.time

  market_open_time = pd.to_datetime("09:30:00").time()
  market_end_time = pd.to_datetime("10:30:00").time()

  signals = []

  for trade_date, group in data.groupby("Date"):
    first_hour_data = group[
        (group["Time"] >= market_open_time) & (group["Time"] < market_end_time)
    ]
    if first_hour_data.empty:
      continue

    box_high = first_hour_data["High"].max()
    box_low = first_hour_data["Low"].min()
    box_mid = (box_high + box_low) / 2

    after_1hr_data = group[group["Time"] >= market_end_time]
    breakout_up = False

    for idx, row in after_1hr_data.iterrows():
      current_price = row["Close"]
      current_low = row["Low"]
      current_high = row["High"]

      if current_high > box_high:
        breakout_up = True

      # 눌림목 매수 조건 포착
      if breakout_up and (current_low <= box_high * 1.002) and (current_price >= box_high):
        signal_msg = (
            f"🚀 **[매수 타점 포착]** 종목: {ticker_symbol}\n"
            f"- 날짜: {trade_date}\n"
            f"- 진입가(추정): {current_price}\n"
            f"- 박스 상단(저항돌파): {box_high}\n"
            f"- 손절가(박스저점): {box_low}"
        )
        signals.append(signal_msg)
        # 디스코드 알림 발송 실행
        send_discord_alert(signal_msg)
        break  # 하루 한 번 시그널 발생 후 종료

  print(f"[{datetime.now()}] 분석 완료. 포착된 시그널 수: {len(signals)}")
  return signals


# ==========================================
# 5. 실행문 (Entry Point)
# ==========================================
if __name__ == "__main__":
  # 관심 있는 반도체/은퇴자 포트폴리오 종목 (예: SOXL) 설정 후 실행
  target_ticker = "SOXL"
  
  print("=" * 50)
  print(f"미장 시가봉 박스돌파 반자동 매매 봇 가동 (대상: {target_ticker})")
  print("=" * 50)
  
  run_strategy(target_ticker)