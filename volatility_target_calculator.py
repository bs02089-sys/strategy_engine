import numpy as np
import pandas as pd
import yfinance as yf


def calculate_realistic_days_to_target(tickers, target_prices):
    """최근 20일 종가 수익률의 평균을 구하여 목표가 도달까지의 현실적인 영업일을 계산합니다.

    tickers: 리스트 형태 (e.g., ['TSLA', 'IONQ', 'AAPL'])
    target_prices: 딕셔너리 형태 (e.g., {'TSLA': 470.0, 'IONQ': 75.0})
    """
    # 1. 데이터 다운로드 (최근 20영업일 확보를 위해 약 2달치 데이터 수집)
    df = yf.download(tickers, period="2mo", progress=False)

    # 2. 멀티인덱스 구조 재정렬 (Level 0: Ticker, Level 1: Price)
    df = df.swaplevel(axis=1).sort_index(axis=1)

    results = []

    for ticker in tickers:
        # 해당 종목 데이터 추출 및 결측치 제거
        ticker_df = df[ticker].dropna()

        # 최근 20영업일 슬라이싱
        recent_20 = ticker_df.tail(20).copy()

        if len(recent_20) < 20:
            print(
                f"⚠️ {ticker}의 데이터가 부족합니다. (확인된 데이터: {len(recent_20)}일)"
            )
            continue

        # 현재 가격 (가장 최근 종가) 및 목표가 설정
        current_price = recent_20["Close"].iloc[-1]
        target_price = target_prices.get(ticker)

        if not target_price:
            print(f"⚠️ {ticker}의 목표 가격이 입력되지 않았습니다.")
            continue

        # 일간 종가 변동률(수익률) 계산 후 20일 평균값 구하기
        recent_20["Return"] = recent_20["Close"].pct_change()
        avg_return = recent_20["Return"].mean()

        # 현재가 대비 목표가까지 가야 할 수익률 거리
        required_return = (target_price / current_price) - 1

        # 상황별 도달 영업일 산출 (예외 처리 반영)
        if required_return <= 0:
            # 이미 현재가가 목표가보다 높거나 같은 경우
            realistic_days = "0 (이미 달성)"
            avg_return_str = f"{round(avg_return * 100, 2)}%"
        elif avg_return <= 0:
            # 최근 20일 평균 수익률이 마이너스이거나 제자리인 경우 (수학적 오류 방지)
            realistic_days = "측정 불가 (최근 하락세)"
            avg_return_str = f"{round(avg_return * 100, 2)}% (하락)"
        else:
            # 정상적인 상승 추세인 경우 복리/단리 기준 일수 계산 (올림 처리)
            days = int(np.ceil(required_return / avg_return))
            realistic_days = f"{days} 영업일"
            avg_return_str = f"{round(avg_return * 100, 2)}%"

        results.append(
            {
                "Ticker": ticker,
                "Current Price": round(current_price, 2),
                "Target Price": target_price,
                "20D Avg Return": avg_return_str,
                "Realistic Days Required": realistic_days,
            }
        )

    # 결과를 멀티인덱스 관리에 용이하도록 데이터프레임으로 변환
    result_df = pd.DataFrame(results).set_index("Ticker")
    return result_df


# --- 실행 영역 ---
if __name__ == "__main__":
    # 💡 분석하고 싶은 종목과 '목표 가격'을 이곳에 적어주세요.
    # 상승세 종목, 하락세 종목, 이미 달성한 종목의 예시입니다.
    target_info = {
        "TSLA": 470.0,  # 테슬라 목표가
        "IONQ": 75.0,  # 아이온큐 목표가
        "AAPL": 150.0,  # 이미 달성한 가상의 낮은 목표가 예시
    }

    tickers_list = list(target_info.keys())

    print("📊 주가 데이터 수집 및 현실적 도달 기일 계산 중...")
    print("-" * 60)

    # 함수 실행
    analysis_result = calculate_realistic_days_to_target(
        tickers_list, target_info
    )

    print("\n[현실적 추세 반영 최종 분석 결과]")
    print("=" * 60)
    print(analysis_result)
    print("=" * 60)
    print(
        "💡 '측정 불가'는 최근 20일 추세가 하락세여서 현재 원동력으로는 목표가 도달이 어렵다는 의미입니다."
    )