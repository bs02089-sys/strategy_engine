import numpy as np
import pandas as pd
import yfinance as yf


def calculate_days_to_target(tickers, target_prices):
    """티커별 20일 평균 변동성을 구하고, 목표가 도달까지 필요한 최소 영업일을 계산합니다.

    tickers: 리스트 형태 (e.g., ['AAPL', 'MSFT'])
    target_prices: 딕셔너리 형태 (e.g., {'AAPL': 190, 'MSFT': 420})
    """
    # 1. 데이터 다운로드 (최근 20영업일보다 여유 있게 약 2달치 가져옴)
    df = yf.download(tickers, period="2mo", progress=False)

    # 2. 멀티인덱스 구조 재정렬 (Level 0: Ticker, Level 1: Price Columns)
    # yf.download는 기본적으로 Column Level 0이 Price, Level 1이 Ticker입니다. 이를 뒤집어줍니다.
    df = df.swaplevel(axis=1).sort_index(axis=1)

    results = []

    for ticker in tickers:
        # 해당 종목의 데이터만 추출
        ticker_df = df[ticker].dropna()

        # 최근 20영업일 데이터만 슬라이싱
        recent_20 = ticker_df.tail(20)

        if len(recent_20) < 20:
            print(
                f"⚠️ {ticker}의 데이터가 부족합니다. (확인된 데이터: {len(recent_20)}일)"
            )
            continue

        # 현재 가격 (가장 최근 종가)
        current_price = recent_20["Close"].iloc[-1]
        target_price = target_prices.get(ticker)

        if not target_price:
            print(f"⚠️ {ticker}의 목표 가격이 입력되지 않았습니다.")
            continue

        # 일간 변동성 계산 (고가 - 저가) 후 20일 평균값 구하기
        daily_volatility = recent_20["High"] - recent_20["Low"]
        avg_volatility = daily_volatility.mean()

        # 목표가까지 남은 절대 금액 차이
        price_gap = abs(target_price - current_price)

        # 최소 소요 영업일 계산 (남은 금액 / 일평균 변동성) -> 올림 처리
        # 변동성이 0인 경우를 대비해 안전장치 추가
        if avg_volatility > 0:
            min_days_required = int(np.ceil(price_gap / avg_volatility))
        else:
            min_days_required = float("inf")

        results.append(
            {
                "Ticker": ticker,
                "Current Price": round(current_price, 2),
                "Target Price": target_price,
                "20D Avg Volatility": round(avg_volatility, 2),
                "Min Days Required": min_days_required,
            }
        )

    # 결과를 데이터프레임으로 변환
    result_df = pd.DataFrame(results).set_index("Ticker")
    return result_df


# --- 실행 예시 ---
if __name__ == "__main__":
    # 테스트할 종목과 각 종목의 목표가 설정
    target_info = {
        "TSLA": 500.0,  # 테슬라 목표가
        "IONQ": 100.0,  # IONQ 목표가
    }

    tickers_list = list(target_info.keys())

    print("📊 주가 데이터 수집 및 분석 중...")
    analysis_result = calculate_days_to_target(tickers_list, target_info)

    print("\n[최종 분석 결과]")
    print(analysis_result)