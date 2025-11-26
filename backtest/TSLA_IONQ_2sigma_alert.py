import os
import yfinance as yf
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK")

def send(msg: str):
    if not WEBHOOK_URL:
        print("웹훅 없음 → 로컬 테스트:", msg)
        return
    try:
        requests.post(WEBHOOK_URL, json={"content": msg}, timeout=10)
    except Exception as e:
        print(f"전송 실패: {e}")

# 뉴욕 시간 설정
ny_time = datetime.now(ZoneInfo("America/New_York"))
now_str = ny_time.strftime("%Y-%m-%d %H:%M")

# 1. 정규장 종가 기준으로 60일 이동평균과 2σ 밴드 계산
data = yf.download(["TSLA", "IONQ"], period="730d", auto_adjust=True, progress=False)["Close"]

# 2. 실시간 가격 가져오기 (안전 장치 포함)
def get_live_price(symbol: str) -> float:
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.info
        # currentPrice(실시간) -> regularMarketPrice(정규장) -> previousClose(전일종가) 순서로 조회
        price = info.get("currentPrice") or info.get("regularMarketPrice") or info.get("previousClose")
        if price is None:
            raise ValueError("가격 정보 없음")
        return price
    except:
        return data[symbol].iloc[-1]  # 실패 시 데이터프레임의 마지막 종가 사용

alerts = []

for ticker in ["TSLA", "IONQ"]:
    try:
        series = data[ticker].dropna()
        if len(series) < 60:
            continue
        
        # 실시간 가격 조회
        live_price = get_live_price(ticker)
        
        # 지표 계산
        ma60 = series.rolling(60).mean().iloc[-1]
        std60 = series.rolling(60).std().iloc[-1]
        lower_band = ma60 - 2 * std60
        
        # 매수 신호 포착
        if live_price <= lower_band:
            drop_pct = (ma60 - live_price) / ma60 * 100
            alerts.append(
                f"🚨 **2σ 급락 매수 신호 (실시간)**\n"
                f"📊 **{ticker}**\n"
                f"• 현재가: ${live_price:,.2f}\n"
                f"• 60일평균: ${ma60:,.2f}\n"
                f"• 하락률: -{drop_pct:.1f}%\n"
                f"• 2σ 하단: ${lower_band:,.2f}\n"
                f"👉 **지금이 매수 기회일 수 있습니다!**"
            )
    except Exception as e:
        print(f"{ticker} 처리 중 오류 발생: {e}")
        continue

# 알림 전송 로직
if alerts:
    message = f"@everyone\n📅 **{now_str} (뉴욕시간)**\n\n" + "\n\n".join(alerts)
    send(message)
    print("실시간 2σ 알림 전송 완료!")
else:
    print(f"{now_str} 기준: 매수 신호 없음")

# 매달 1일 작동 확인 알림
# 스케줄러가 하루에 한 번 실행되므로, 날짜만 1일이면 무조건 발송
if ny_time.day == 1:
    send(f"✅ Monthly Ping: 시스템 정상 작동 중 ({now_str})")