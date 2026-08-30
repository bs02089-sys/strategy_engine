import os
import requests
import pandas as pd
import yfinance as yf

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
    
    print(f"[{ticker}] 야후 파이낸스에서 실제 일봉 데이터를 불러오는 중...")
    
    # yfinance를 통해 실제 최근 일봉 데이터 다운로드 (최근 60 영업일)
    df = yf.download(ticker, period="60d", interval="1d", progress=False)
    
    if df.empty:
        error_msg = f"[{ticker}] 데이터를 불러오지 못했습니다. 종목 코드를 확인해주세요."
        print(error_msg)
        send_discord_alert(error_msg)
        return None

    # MultiIndex 컬럼 구조 대응 (yfinance 버전별 구조 차이 방어)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    # 전일 기준 일봉 지지/저항 존(네모 박스) 설정
    df['Resistance_Zone'] = df['High'].shift(1).rolling(window=window).max()
    df['Support_Zone'] = df['Low'].shift(1).rolling(window=window).min()
    
    # Strategy_Plan 컬럼 기본값 'HOLD'로 초기화
    df['Strategy_Plan'] = "HOLD"
    
    # 가장 마지막 날(오늘) 데이터 추출 및 조건 판별
    i = len(df) - 1
    prev_close = df['Close'].iloc[i-1]
    res_zone = df['Resistance_Zone'].iloc[i]
    sup_zone = df['Support_Zone'].iloc[i]
    
    order_plan = "HOLD"
    
    # 실제 주가($180대 등)에 맞춰 저항/지지 존 근처 도달 여부 확인
    if prev_close >= res_zone * 0.99:
        order_plan = f"[{ticker}] 일봉 저항 존 도달 -> 익일 숏 지정가 예약 주문 설정 (저항선: {res_zone:.2f})"
        send_discord_alert(order_plan)
    elif prev_close <= sup_zone * 1.01:
        order_plan = f"[{ticker}] 일봉 지지 존 도달 -> 익일 롱 지정가 예약 주문 설정 (지지선: {sup_zone:.2f})"
        send_discord_alert(order_plan)
    else:
        order_plan = f"[{ticker}] 현재 조건에 부합하는 존이 없습니다. (HOLD)"
        send_discord_alert(order_plan)

    # 마지막 날의 계획을 데이터프레임에 반영
    df.loc[df.index[i], 'Strategy_Plan'] = order_plan
    return df

if __name__ == "__main__":
    ticker = os.environ.get("TRADING_TICKER", "PLTR")
    print("=" * 50)
    print(f"[{ticker}] 실전 일봉 스윙 전략 시뮬레이션 및 디스코드 알림 봇 구동 시작")
    print("=" * 50)
    
    result_df = run_daily_swing_simulation()
    
    if result_df is not None and 'Strategy_Plan' in result_df.columns:
        print(f"[{ticker}] 분석 완료 및 디스코드 알림 발송 완료.")
    else:
        print("[경고] 분석 과정에 문제가 발생했습니다.")
        
    print("=" * 50)