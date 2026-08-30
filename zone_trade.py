import os
import requests
import pandas as pd
import numpy as 


def send_discord_alert(message):
    """
    환경 변수에 설정된 디스코드 웹훅으로 메시지를 전송합니다.
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
    
    # 일봉 테스트 데이터 생성
    np.random.seed(42)
    dates = pd.date_range(start="2026-01-01", periods=60, freq="B")
    price_walk = 50 + np.cumsum(np.random.randn(60) * 1.2)

    df = pd.DataFrame({
        'High': price_walk + np.random.rand(60) * 1.5,
        'Low': price_walk - np.random.rand(60) * 1.5,
        'Close': price_walk
    }, index=dates)

    # 전일 기준 일봉 지지/저항 존(네모 박스) 설정
    df['Resistance_Zone'] = df['High'].shift(1).rolling(window=window).max()
    df['Support_Zone'] = df['Low'].shift(1).rolling(window=window).min()
    
    # [수정] 과거 전체를 도는 대신, '가장 마지막 날(오늘)'의 데이터만 확인
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
        print(f"[{ticker}] 현재 조건에 부합하는 존이 없습니다. (HOLD)")

    return df


if __name__ == "__main__":
    ticker = os.environ.get("TRADING_TICKER", "PLTR")
    print("=" * 50)
    print(f"[{ticker}] 일봉 스윙 전략 시뮬레이션 및 디스코드 알림 봇 구동 시작")
    print("=" * 50)
    
    result_df = run_daily_swing_simulation()
    
    active_plans = result_df[result_df['Strategy_Plan'] != "HOLD"]
    print(f"총 {len(active_plans)}개의 지정가 예약 주문 가이드가 생성되었습니다.")
    print("=" * 50)