from datetime import datetime
import numpy as np
import pandas as pd
import yfinance as yf


def calculate_target_dates(tickers, target_prices):
    """최근 20일 수익률 추세를 바탕으로 목표가 도달 예상 '실제 달력 날짜'를 계산합니다."""
    # 1. 데이터 다운로드 및 멀티인덱스 정렬
    df = yf.download(tickers, period="2mo", progress=False)
    df = df.swaplevel(axis=1).sort_index(axis=1)

    # 기준일 설정 (오늘 날짜)
    today = datetime.today().date()

    results = []

    for ticker in tickers:
        ticker_df = df[ticker].dropna()
        recent_20 = ticker_df.tail(20).copy()

        if len(recent_20) < 20:
            print(f"⚠️ {ticker} 데이터 부족")
            continue

        current_price = recent_20["Close"].iloc[-1]
        target_price = target_prices.get(ticker)

        if not target_price:
            continue

        # 최근 20일 평균 수익률 계산
        recent_20["Return"] = recent_20["Close"].pct_change()
        avg_return = recent_20["Return"].mean()
        required_return = (target_price / current_price) - 1

        # 상태 분류 및 실제 달력 날짜 계산
        if required_return <= 0:
            realistic_days = 0
            status = "0 영업일"
            target_date_str = "오늘 (이미 달성)"

        elif avg_return <= 0:
            status = "측정 불가"
            target_date_str = "하락 추세로 인해 추정 불가"

        else:
            # 필요 영업일 계산
            days = int(np.ceil(required_return / avg_return))
            status = f"{days} 영업일"

            # 💡 [핵심] 주말을 제외한 '영업일 기준' 미래 날짜 계산
            # 오늘(periods=1)부터 필요한 영업일수만큼 영업일 날짜 배열을 생성합니다.
            # 주말을 건너뛰기 때문에 실질적으로 periods는 (days + 1)이 됩니다.
            b_days = pd.bdate_range(start=today, periods=days + 1)
            target_date = b_days[-1]  # 맨 마지막 영업일이 목표 날짜

            # 출력 포맷팅 (예: 2026-05-25 (월))
            weekday_dict = {
                0: "월",
                1: "화",
                2: "수",
                3: "목",
                4: "금",
                5: "토",
                6: "일",
            }
            target_date_str = (
                f"{target_date.strftime('%Y-%m-%d')} ({weekday_dict[target_date.weekday()]})"
            )

        results.append(
            {
                "Ticker": ticker,
                "Current Price": round(current_price, 2),
                "Target Price": target_price,
                "Days Required": status,
                "Estimated Target Date": target_date_str,  # 📅 추가된 실제 날짜 컬럼
            }
        )

    result_df = pd.DataFrame(results).set_index("Ticker")
    return result_df


# --- 실행 영역 ---
if __name__ == "__main__":
    # 테스트할 종목과 목표가 (현재 가격보다 높게 설정해야 날짜가 계산됩니다)
    target_info = {
        "TSLA": 470.0,
        "IONQ": 75.0,
        "AAPL": 150.0,
    }

    tickers_list = list(target_info.keys())

    print("📅 주말을 제외한 달력 기준 도달 예측 날짜 계산 중...")
    print("-" * 75)

    analysis_result = calculate_target_dates(tickers_list, target_info)

    print("\n[최종 달력 날짜 반영 결과]")
    print("=" * 75)
    print(analysis_result)
    print("=" * 75)