import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config.config import CONFIG
from data.historical_prices import fetch_historical_prices
from backtest.backtest_logic import run_backtest_for_ticker

def run_backtest_strategy():
    tickers = CONFIG["strategy_tickers"]
    start = CONFIG["start_date"]
    end = CONFIG["end_date"]
    initial_capital = CONFIG["initial_capital"]
    fees = CONFIG["fees"]
    hold_ratios = CONFIG["hold_ratios"]
    params = {"lookback": 30, "threshold": 0.05}

    print(f"\n🚀 백테스트 시작: {start.date()} ~ {end.date()}")
    print(f"📌 초기 자본금: ${initial_capital:,.2f}")
    print(f"📌 투자 종목: {tickers}")
    print(f"📌 회고 기간: {params['lookback']}일\n")

    historical_df = fetch_historical_prices(tickers, start, end)

    total_return = 0.0
    total_trades = 0
    max_drawdown = 0.0
    weighted_results = {}

    for ticker in tickers:
        if ticker not in historical_df.columns:
            print(f"❌ {ticker} 과거 데이터 없음 → 건너뜀")
            continue

        series = historical_df[ticker]
        weight = hold_ratios.get(ticker, 0) / 100

        # 기본 시그널: 전체 기간 매수(hold). 실제 신호 시리즈가 있으면 대체하세요.
        signal_series = series.copy()
        signal_series[:] = 1

        result = run_backtest_for_ticker(
            price_series=series,
            signal_series=signal_series,
            **params,
        )
        # Validate result to avoid runtime errors (e.g., None or wrong type)
        if result is None or not isinstance(result, dict):
            print(f"❌ {ticker} 백테스트 실패 또는 결과 없음 → 건너뜀")
            continue
        weighted_results[ticker] = result
        total_return += result["return"] * weight
        total_trades += result["trades"]
        max_drawdown = min(max_drawdown, result["mdd"])

        print(f"\n📊 백테스트 결과: {ticker}")
        print(f"  - 투자 비중: {weight*100:.1f}%")
        print(f"  - 총 수익률: {result['return']*100:.2f}%")
        print(f"  - 최대 낙폭(MDD): {result['mdd']*100:.2f}%")
        print(f"  - 샤프지수: {result['sharpe']:.2f}")
        print(f"  - 거래 횟수: {result['trades']}")

    print("\n✅ 전체 전략 요약")
    print(f"  - 총 수익률: {total_return*100:.2f}%")
    print(f"  - 최대 낙폭(MDD): {max_drawdown*100:.2f}%")
    print(f"  - 총 거래 횟수: {total_trades}")

if __name__ == "__main__":
    run_backtest_strategy()