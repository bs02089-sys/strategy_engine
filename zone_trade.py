import os
import requests
import pandas as pd
import numpy as np

def send_discord_alert(message):
    """
    디스코드 웹훅으로 알림을 전송합니다.
    """
    webhook_url = os.environ.get("DISCORD_WEBHOOK")
    
    if not webhook_url:
        print(f"[알림 미발송 - 웹훅 URL 없음] {message}")
        return

    payload = {"content": message}
    try:
        response = requests.post(webhook_url, json=payload)
        if response.status_code != 204:
            print(f"디스코드 전송 실패: 상태 코드 {response.status_code}")
    except Exception as e:
        print(f"디스코드 전송 중 에러 발생: {e}")

def run_daily_swing_simulation():
    ticker = os.environ.get("TRADING_TICKER", "PLTR")
    window = int(os.environ.get("ZONE_WINDOW", 10))
    
    # 1. 일봉 테스트 데이터 생성 (실제 운영 시 증권사 API 데이터로 대체)
    np.random.seed(42)
    dates = pd.date_range(start="2026-01-01", periods=60, freq="B")
    price_walk = 50 + np.cumsum(np.random.randn(60) * 1.2)

    df = pd.DataFrame({
        'High': price_walk + np.random.rand(60) * 1.5,
        'Low': price_walk - np.random.rand(60) * 1.5,
        'Close': price_walk
    }, index=dates)

    # 2. 전일 기준 일봉 지지/저항 존(네모 박스) 설정
    df['Resistance_Zone'] = df['High'].shift(1).rolling(window=window).max()
    df['Support_Zone'] = df['Low'].shift(1).rolling(window=window).min()
    
    # 3. Strategy_Plan 컬럼 기본값 'HOLD'로 초기화
    df['Strategy_Plan'] = "HOLD"
    
    # 4. 가장 마지막 날(오늘) 데이터 추출 및 조건 판별
    i = len(df) - 1
    prev_close = df['Close'].iloc[i-1]
    res_zone = df['Resistance_Zone'].iloc[i]
    sup_zone = df['Support_Zone'].iloc[i]
    
    order_plan = "HOLD"
    
    if prev_close >= res_zone * 0.99:
        order_plan = f"[{ticker}] 일봉 저항 존 도달 -> 익일 숏 지정가 예약 주문 설정 (저항선: {res_zone:.2f})"
        send_discord_alert(order_plan)
    elif prev_close <= sup_zone * 1.01:
        order_plan = f"[{ticker}] 일봉 지지 존 도달 -> 익일 롱 지정가 예약 주문 설정 (지지선: {sup_zone:.2f})"
        send_discord_alert(order_plan)
    else:
        # [변경] 조건에 부합하는 존이 없을 때도 디스코드로 알림 발송
        order_plan = f"[{ticker}] 현재 조건에 부합하는 존이 없습니다. (HOLD)"
        send_discord_alert(order_plan)

    # 마지막 날의 계획을 데이터프레임에 반영
    df.loc[df.index[i], 'Strategy_Plan'] = order_plan
    return df

if __name__ == "__main__":
    ticker = os.environ.get("TRADING_TICKER", "PLTR")
    print("=" * 50)
    print(f"[{ticker}] 일봉 스윙 전략 시뮬레이션 및 디스코드 알림 봇 구동 시작")
    print("=" * 50)
    
    result_df = run_daily_swing_simulation()
    print("=" * 50)