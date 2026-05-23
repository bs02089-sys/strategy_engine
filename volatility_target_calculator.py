from datetime import datetime
import numpy as np
import pandas as pd
import yfinance as yf


def calculate_target_dates(tickers, target_prices):
    """보수적 관점 목표가 도달 예측 (현실적 참고용)"""
    
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
            "Conservative": avg_daily_return - 0.5 * daily_vol   # 0.8 → 0.5로 완화
        }

        for scenario_name, daily_r in scenarios.items():
            if required_return <= 0:
                days = 0
                date_str = "오늘 (이미 달성)"
            elif daily_r <= 0:
                days = "추정 불가"
                date_str = "변동성 과다 (하락 가능성 높음)"
            else:
                days = int(np.ceil(np.log(1 + required_return) / np.log(1 + daily_r)))
                days = max(1, days)
                
                b_days = pd.bdate_range(start=today, periods=days + 1)
                target_date = b_days[-1]
                
                weekday_dict = {0: "월", 1: "화", 2: "수", 3: "목", 4: "금", 5: "토", 6: "일"}
                date_str = f"{target_date.strftime('%Y-%m-%d')} ({weekday_dict[target_date.weekday()]})"

            results.append({
                "Ticker": ticker,
                "현재가": round(current_price, 2),
                "목표가": target_price,
                "필요상승률": f"{required_return:+.1%}",
                "평균일수익률": f"{avg_daily_return:+.3%}",
                "연환산변동성": f"{annualized_vol:.1%}",
                "시나리오": scenario_name,
                "예상영업일": days,
                "예상도달일": date_str
            })

    result_df = pd.DataFrame(results)
    cols = ["Ticker", "현재가", "목표가", "필요상승률", "평균일수익률", 
            "연환산변동성", "시나리오", "예상영업일", "예상도달일"]
    
    return result_df[cols].set_index(["Ticker", "시나리오"])


# ====================== 실행 영역 ======================
if __name__ == "__main__":
    target_info = {
        "TSLA": 450.0,
        "IONQ": 100.0,
        "SOXL": 200.0
    }

    tickers_list = list(target_info.keys())

    print("📅 보수적 관점 목표가 도달 예측 (현실적 참고용)")
    print("   → Conservative를 주 참고 시나리오로 사용하세요")
    print("=" * 100)

    analysis_result = calculate_target_dates(tickers_list, target_info)

    print("\n[분석 결과]")
    print("=" * 100)
    print(analysis_result)
    print("=" * 100)
    print("\n💡 Conservative는 변동성을 상당히 고려한 보수적 예측입니다.")