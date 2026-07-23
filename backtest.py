"""
loc_dca_comparison.py — LOC 분할매수 비중 비교 (No Rebalancing)
SOXL 1년 계획 검증용
"""

import numpy as np
import pandas as pd
import yfinance as yf
import matplotlib.pyplot as plt

plt.rcParams['font.family'] = 'Malgun Gothic'
plt.rcParams['axes.unicode_minus'] = False

TARGET_TICKERS = ['SOXL']
START_DATE = '2022-01-01'
END_DATE = '2026-07-22'
ALREADY_SOXL_BUYS = 0
TOTAL_BUYS_PLANNED = 20
TEST_WEIGHTS = [0.2, 0.3, 0.4, 0.5, 0.6]
SIGMA_LOOKBACK = 365
SIGMA_MULTIPLIER = 1.41
DAYS_PER_YEAR = 365.25


def download_price_data(tickers, start, end):
    """Load adjusted historical price data for the requested tickers."""
    return yf.download(tickers, start=start, end=end, auto_adjust=True, group_by='ticker')


def add_loc_signal(df, multiplier=SIGMA_MULTIPLIER, lookback=SIGMA_LOOKBACK):
    """Compute the LOC entry signal for a price series."""
    df = df.copy()
    df['Return'] = df['Close'].pct_change()
    df['Sigma'] = df['Return'].rolling(lookback).std()
    df['LOC'] = df['Close'].shift(1) * np.exp(-multiplier * df['Sigma'].shift(1))
    df['Signal'] = (df['Close'] <= df['LOC']) & df['LOC'].notna()
    return df


def simulate_loc_dca(df, max_buys, already_buys=0):
    """Simulate no-rebalancing LOC DCA performance for a single ticker."""
    df = add_loc_signal(df)
    df = df.assign(Equity=1.0, Position=0.0)

    position = 0.0
    remaining_buys = max(0, max_buys - already_buys)
    add_size = 1.0 / remaining_buys if remaining_buys > 0 else 0.0

    for i in range(1, len(df)):
        if remaining_buys > 0 and df['Signal'].iat[i] and position < 1.0:
            position += add_size
            remaining_buys -= 1

        prev_price = df['Close'].iat[i - 1]
        price = df['Close'].iat[i]
        daily_ret = (price / prev_price - 1) * position if position > 0 else 0.0

        df.loc[df.index[i], 'Equity'] = df['Equity'].iat[i - 1] * (1 + daily_ret)
        df.loc[df.index[i], 'Position'] = position

    return df['Equity'], position


def calculate_metrics(equity_series):
    """Calculate CAGR, MDD and Calmar ratio for an equity curve."""
    years = (equity_series.index[-1] - equity_series.index[0]).days / DAYS_PER_YEAR
    total_return = equity_series.iloc[-1] - 1.0
    cagr = (equity_series.iloc[-1] ** (1 / years) - 1) if years > 0 else 0.0
    mdd = ((equity_series / equity_series.cummax()) - 1).min()
    calmar = cagr / abs(mdd) if mdd < 0 else 0.0
    return {
        'CAGR': cagr,
        'MDD': mdd,
        'Calmar': calmar,
        'Total_Return': total_return,
    }


def build_portfolio_equity(equity_curves, weights):
    """Create a weighted portfolio equity curve from individual ticker equity curves."""
    portfolio = None
    for ticker, series in equity_curves.items():
        weighted = series * weights.get(ticker, 0.0)
        portfolio = weighted if portfolio is None else portfolio.add(weighted, fill_value=0.0)
    return portfolio


def run_backtest(price_data, soxl_weight, plot=False):
    """Backtest the LOC DCA strategy for the requested SOXL weight."""
    weights = {ticker: soxl_weight for ticker in TARGET_TICKERS}
    equity_curves = {}

    for ticker in TARGET_TICKERS:
        series, final_position = simulate_loc_dca(
            price_data[ticker],
            max_buys=TOTAL_BUYS_PLANNED,
            already_buys=ALREADY_SOXL_BUYS if ticker == 'SOXL' else 0,
        )
        equity_curves[ticker] = series

    portfolio_equity = build_portfolio_equity(equity_curves, weights)
    portfolio_metrics = calculate_metrics(portfolio_equity)
    portfolio_metrics['SOXL_%'] = int(soxl_weight * 100)

    if plot:
        plt.figure(figsize=(14, 8))
        for ticker, series in equity_curves.items():
            plt.plot(series, label=f'{ticker} ({weights[ticker] * 100:.0f}%)')
        plt.plot(portfolio_equity, label='Portfolio', linewidth=3, color='red')
        plt.title(f"LOC DCA 전략 - SOXL {weights['SOXL'] * 100:.0f}%")
        plt.legend()
        plt.grid(True)
        plt.show()

    return portfolio_metrics


def main():
    print('🔍 LOC 분할매수 비중 백테스트\n')
    price_data = download_price_data(TARGET_TICKERS, START_DATE, END_DATE)

    print('비중별 LOC 분할매수 백테스트 시작...\n')
    comparison = [run_backtest(price_data, w) for w in TEST_WEIGHTS]

    for result in comparison:
        print(
            f"SOXL {result['SOXL_%']:2}% | CAGR {result['CAGR']:6.2%} | "
            f"MDD {result['MDD']:6.1%} | Calmar {result['Calmar']:.2f}"
        )

    comp_df = pd.DataFrame(comparison)
    print('\n' + '=' * 80)
    print('📊 LOC 분할매수 비중 비교 결과')
    print('=' * 80)
    print(comp_df.sort_values('Calmar', ascending=False).round(4))

    best = comp_df.loc[comp_df['Calmar'].idxmax()]
    print(f"\n🏆 Calmar 기준 최적 비중: SOXL {best['SOXL_%']}%")


if __name__ == '__main__':
    main()
