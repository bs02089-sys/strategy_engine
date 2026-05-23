from datetime import datetime
import numpy as np
import pandas as pd
import yfinance as yf


def calculate_target_dates(tickers, target_prices):
    """보수적 목표가 도달 예측 - 컬럼 누락 완전 해결 버전"""
    
    df = yf.download(tickers, period="2mo", progress=False)
    df = df.swaplevel(axis=1).sort_index(axis=1)

    today = datetime.today().date()
    results = []

    for ticker in tickers:
        ticker_df = df[ticker].dropna()
        recent_20 = ticker_df.tail(20)

        if len(recent_20) < 20:
            print(f"⚠️ {ticker} 데이터 부족")
            continue

        current_price = recent_20["Close"].iloc[-1]
        target_price = target_prices.get(ticker)

        if not target_price or target_price <= current_price:
            continue

        returns = recent_20["Close"].pct_change().dropna()
        avg_daily_return = returns.mean()
        daily_vol = returns.std()
        annualized_vol = daily_vol * np.sqrt(252)

        required_return = (target_price / current_price) - 1

        scenarios = {
            "Base": avg_daily_return,
            "Conservative": max(avg_daily_return - 0.3 * daily_vol, avg_daily_return * 0.4)
        }

        for scenario_name, daily_r in scenarios.items():
            if required_return <= 0:
                days = 0
                date_str = "오늘 (이미 달성)"
            elif daily_r <= 0:
                days = "추정 불가"
                date_str = "변동성 과다"
            else:
                days = int(np.ceil(np.log(1 + required_return) / np.log(1 + daily_r)))
                days = max(1, days)
                
                b_days = pd.bdate_range(start=today, periods=days + 1)
                target_date = b_days[-1]
                
                weekday_dict = {0: "월", 1: "화", 2: "수", 3: "목", 4: "금", 5: "토", 6: "일"}
                date_str = f"{target_date.strftime('%Y-%m-%d')} ({weekday_dict[target_date.weekday()]})"

            # 모든 컬럼을 명확히 포함
            results.append({
                "종목": ticker,
                "현재가": round(current_price, 2),
                "목표가": target_price,
                "필요 상승률": f"{required_return:+.1%}",
                "시나리오": scenario_name,
                "예상 영업일": days,
                "예상 도달일": date_str,
                "평균 일수익률": f"{avg_daily_return:+.3%}",
                "연환산 변동성": f"{annualized_vol:.1%}"
            })

    result_df = pd.DataFrame(results)

    # 컬럼 순서 강제 재정렬 (이 방식이 가장 안전)
    final_columns = [
        "종목", "현재가", "목표가", "필요 상승률",
        "시나리오", "예상 영업일", "예상 도달일",
        "평균 일수익률", "연환산 변동성"
    ]
    
    result_df = result_df.reindex(columns=final_columns)

    return result_df.set_index(["종목", "시나리오"])


# ====================== 실행 영역 ======================
if __name__ == "__main__":
    target_info = {
        "TSLA": 450.0,
        "IONQ": 100.0,
        "SOXL": 200.0
    }

    tickers_list = list(target_info.keys())

    print("📅 보수적 목표가 도달 예측")
    print("   → Conservative를 가장 현실적인 참고값으로 추천합니다.")
    print("=" * 120)

    analysis_result = calculate_target_dates(tickers_list, target_info)

    print("\n[분석 결과]")
    print("=" * 120)
    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", None)
    pd.set_option("display.max_colwidth", None)
    print(analysis_result.to_string())
    print("=" * 120)
    
    print("\n💡 해석 가이드:")
    print("   • Base         : 최근 20일 평균 추세")
    print("   • Conservative : 변동성 고려 보수적 예측 (추천)")
    print("   • 예상 도달일은 주말 제외 영업일 기준입니다.")