# 📡 실시간 가격 기반 전략 판단
import pandas as pd

def evaluate_live_prices(ticker: str, historical: pd.Series, live_price: float, threshold_sd: float = 2.0) -> bool:
    """
    실시간 가격을 기반으로 전략 판단을 수행합니다.
    기준: 평균 - N * 표준편차 하단 돌파 여부

    Parameters:
    - ticker (str): 종목 코드
    - historical (pd.Series): 과거 가격 시계열
    - live_price (float): 실시간 가격
    - threshold_sd (float): 기준 표준편차 배수 (기본값: 2.0)

    Returns:
    - bool: 매수 시그널 여부
    """
    if historical.empty or live_price is None or pd.isna(live_price):
        print(f"❌ {ticker} 실시간 판단 불가 (데이터 없음)")
        return False

    mean = historical.mean()
    std = historical.std()
    threshold = mean - threshold_sd * std

    signal = live_price <= threshold
    if signal:
        print(f"📉 {ticker} 실시간가 {live_price:.2f} → {threshold_sd}SD 하단 돌파 → 매수 시그널 발생")
    else:
        print(f"📊 {ticker} 실시간가 {live_price:.2f} → 시그널 없음")

    return signal