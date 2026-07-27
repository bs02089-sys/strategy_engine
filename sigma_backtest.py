#!/usr/bin/env python3
"""
═══════════════════════════════════════════════════════════════
  Sigma DCA Backtest — 1-Year Walk-Forward Simulation
═══════════════════════════════════════════════════════════════

Reuses sigma_DCA_manager.py's sigma-calculation & LOC logic.

Strategy (LONG_YEAR / INFRASTRUCTURE type):
  1. Daily sigma calculated via EWMA (λ=0.94) over 252-trading-day window
  2. LOC target price = previous_close * (1 − sigma × 1.41)
  3. When next day's LOW ≤ LOC target → limit order fills
  4. Standing order resets each day at the new LOC target

Parameters:
  Asset : SOXL (100%)
  Capital: $50,000
  Period : Recent 1 trading year (~252 sessions)
  Buy size: $2,500 / trigger (up to 20 fills)
"""

import json
import sys
import os
import numpy as np
import pandas as pd
import yfinance as yf
from datetime import date, timedelta

# ── Reuse sigma_DCA_manager's core math ──────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sigma_DCA_manager import (
    _calculate_volatility_from_closes,
    _calculate_loc_from_sigma,
)


# ═══════════════════════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════════════════════

INITIAL_CASH      = 50_000       # $50,000
TICKER            = "SOXL"
LOOKBACK_DAYS     = 252          # Matching portfolio_config.json
ENTRY_MULTIPLIER  = 1.41         # Matching portfolio_config.json
VOL_METHOD        = "EWMA"
EWMA_LAMBDA       = 0.94
BUY_AMOUNT        = 2_500        # Dollars deployed per LOC fill
MAX_BUYS          = 20           # 20 × $2,500 = $50,000 total
BACKTEST_DAYS     = 252          # ~1 trading year
FETCH_BUFFER_DAYS = 60           # Extra calendar days beyond lookback


# ═══════════════════════════════════════════════════════════════════
# Data Fetching
# ═══════════════════════════════════════════════════════════════════

def fetch_data(ticker: str) -> pd.DataFrame:
    """Download OHLCV data, returns a DataFrame with 'Close' and 'Low'."""
    end_date = date.today()
    # Need LOOKBACK_DAYS trading days before backtest start, with calendar buffer
    total_calendar = BACKTEST_DAYS + LOOKBACK_DAYS + FETCH_BUFFER_DAYS
    start_date = end_date - timedelta(days=total_calendar)

    print(f"📥 Downloading {ticker} ({start_date} → {end_date})...")
    stock = yf.Ticker(ticker)
    hist = stock.history(start=start_date, end=end_date, interval="1d",
                         auto_adjust=False)
    if hist.empty:
        raise RuntimeError(f"{ticker} returned no data.")

    # Build clean dataframe
    df = hist[['Close', 'Low']].copy()
    df.columns = ['Close', 'Low']
    df = df.dropna(subset=['Close', 'Low'])

    print(f"   → {len(df)} trading days loaded.")
    return df


# ═══════════════════════════════════════════════════════════════════
# Backtest Engine
# ═══════════════════════════════════════════════════════════════════

def run_backtest(df: pd.DataFrame) -> dict:
    """
    Walk forward through df, one day at a time.
    Returns a result dict with trade_log, portfolio_curve, and stats.
    """
    closes    = df['Close'].values
    lows      = df['Low'].values
    dates_idx = df.index

    cash         = float(INITIAL_CASH)
    shares       = 0.0
    buys         = 0
    trade_log    = []
    daily_values = []

    # Start index: first day where we have LOOKBACK_DAYS of prior data
    start_idx = LOOKBACK_DAYS

    for i in range(start_idx, len(df)):
        prev_close  = float(closes[i - 1])
        today_low   = float(lows[i])
        today_close = float(closes[i])
        today_date  = dates_idx[i]

        # ── Calculate sigma using only prior data (no lookahead) ──
        lookback_window = pd.Series(closes[i - LOOKBACK_DAYS : i])
        sigma, method = _calculate_volatility_from_closes(
            lookback_window, LOOKBACK_DAYS, VOL_METHOD, EWMA_LAMBDA
        )

        # ── LOC target price ──
        loc_price = _calculate_loc_from_sigma(prev_close, sigma, ENTRY_MULTIPLIER)

        # ── Check if LOC triggered ──
        triggered = today_low <= loc_price
        buy_price = min(today_close, loc_price) if triggered else None
        buy_amount = 0.0
        buy_shares = 0.0

        if triggered and cash >= BUY_AMOUNT and buys < MAX_BUYS:
            buy_shares = BUY_AMOUNT / buy_price
            buy_amount = BUY_AMOUNT
            cash  -= buy_amount
            shares += buy_shares
            buys += 1

            trade_log.append({
                'date':    today_date,
                'type':    'BUY',
                'price':   round(buy_price, 2),
                'shares':  round(buy_shares, 4),
                'amount':  round(buy_amount, 2),
                'sigma':   round(sigma, 4),
                'loc':     round(loc_price, 2),
                'cash_remaining': round(cash, 2),
            })

            print(
                f"  📌 {today_date.date()}"
                f" | LOC ${loc_price:.2f} hit"
                f" | Bought {buy_shares:.2f} sh @ ${buy_price:.2f}"
                f" | Sigma {sigma:.4f}"
            )

        # ── Daily portfolio value ──
        portfolio_value = cash + shares * today_close
        daily_values.append({
            'date':   today_date,
            'close':  today_close,
            'loc':    loc_price,
            'sigma':  sigma,
            'cash':   cash,
            'shares': shares,
            'value':  round(portfolio_value, 2),
            'triggered': triggered,
        })

    # Build results
    result = {
        'df':          df,
        'trade_log':   trade_log,
        'daily_values': daily_values,
        'final_cash':  cash,
        'final_shares': shares,
        'total_buys':  buys,
    }
    return result


# ═══════════════════════════════════════════════════════════════════
# Reporting
# ═══════════════════════════════════════════════════════════════════

def print_report(result: dict):
    dv = result['daily_values']
    trades = result['trade_log']

    if not dv:
        print("\n❌ No trading days in simulation.")
        return

    start_val = float(INITIAL_CASH)
    end_val   = dv[-1]['value']

    # Portfolio returns
    total_return  = (end_val - start_val) / start_val * 100
    period_start  = dv[0]['date']
    period_end    = dv[-1]['date']

    # Daily return series for Sharpe
    prices = np.array([d['value'] for d in dv])
    daily_returns = prices[1:] / prices[:-1] - 1
    sharpe_ratio  = float(np.sqrt(252) * daily_returns.mean() / daily_returns.std()) \
                    if daily_returns.std() > 0 else 0.0

    # Max drawdown
    peak   = np.maximum.accumulate(prices)
    dd     = (prices - peak) / peak
    mdd_pct = float(dd.min() * 100)

    # Price return of the asset itself (buy & hold)
    asset_start = dv[0]['close']
    asset_end   = dv[-1]['close']
    asset_return = (asset_end - asset_start) / asset_start * 100

    # Buy stats
    total_invested = sum(t['amount'] for t in trades)
    avg_buy_price  = np.mean([t['price'] for t in trades]) if trades else 0
    final_price    = dv[-1]['close']

    # Win rate: how many trades are in profit at the end
    if trades:
        wins = sum(1 for t in trades if final_price > t['price'])
        win_rate = wins / len(trades) * 100
    else:
        win_rate = 0.0

    # ─────────────────────────────────────────────
    # Print Report
    # ─────────────────────────────────────────────
    print("\n")
    print("═" * 62)
    print("  📊 Sigma DCA Backtest Report")
    print("═" * 62)
    print(f"  Ticker    : {TICKER}")
    print(f"  Strategy  : EWMA Sigma (λ={EWMA_LAMBDA}) × {ENTRY_MULTIPLIER} LOC")
    print(f"  Capital   : ${INITIAL_CASH:,}")
    print(f"  Period    : {period_start.date() if hasattr(period_start,'date') else period_start}"
          f"  →  {period_end.date() if hasattr(period_end,'date') else period_end}")
    print(f"  Buy size  : ${BUY_AMOUNT:,} / trigger (max {MAX_BUYS}×)")
    print("─" * 62)

    print(f"\n  📈 Performance")
    print(f"     Final Portfolio  : ${end_val:,.2f}")
    print(f"     Total Return     : {total_return:+.2f}%")
    print(f"     Buy & Hold (SOXL): {asset_return:+.2f}%")
    print(f"     Alpha vs B&H     : {total_return - asset_return:+.2f}%")
    print(f"     Sharpe Ratio     : {sharpe_ratio:.2f}")
    print(f"     Max Drawdown     : {mdd_pct:.2f}%")

    print(f"\n  📋 DCA Activity")
    print(f"     Total LOC Fills  : {result['total_buys']}")
    print(f"     Total Invested   : ${total_invested:,.2f}")
    print(f"     Avg Buy Price    : ${avg_buy_price:.2f}")
    print(f"     Current Price    : ${final_price:.2f}")
    print(f"     Win Rate         : {win_rate:.1f}%")
    print(f"     Remaining Cash   : ${result['final_cash']:,.2f}")

    # Shares & P&L
    if result['final_shares'] > 0:
        unrealized_pnl = (final_price - avg_buy_price) * result['final_shares']
        print(f"     Shares Held      : {result['final_shares']:.4f}")
        print(f"     Unrealized P&L   : ${unrealized_pnl:+,.2f}")

    # ── Detailed trade log ──────────────────────
    if trades:
        print(f"\n  📝 Trade Log")
        print(f"  {'Date':<14} {'Price':>8} {'Shares':>10} {'Amount':>9} {'Sigma':>8}")
        print(f"  {'─'*14} {'─'*8} {'─'*10} {'─'*9} {'─'*8}")
        for t in trades:
            d = t['date']
            d_str = d.strftime('%Y-%m-%d') if hasattr(d, 'strftime') else str(d)
            print(
                f"  {d_str:<14} ${t['price']:>6.2f} {t['shares']:>10.2f} ${t['amount']:>7,.0f} {t['sigma']:>8.4f}"
            )



    # ── Monthly breakdown ──────────────────────
    monthly_vals = {}
    for d in dv:
        dt = d['date']
        month_key = dt.strftime('%Y-%m') if hasattr(dt, 'strftime') else str(dt)[:7]
        monthly_vals.setdefault(month_key, []).append(d['value'])
    if len(monthly_vals) > 1:
        print(f"\n  📅 Monthly Portfolio Value")
        print(f"  {'Month':<8} {'Start':>10} {'End':>10} {'Return':>8}")
        print(f"  {'─'*8} {'─'*10} {'─'*10} {'─'*8}")
        months_sorted = sorted(monthly_vals.keys())
        for m in months_sorted:
            vals = monthly_vals[m]
            m_start = vals[0]
            m_end   = vals[-1]
            m_ret   = (m_end - m_start) / m_start * 100
            print(f"  {m:<8} ${m_start:>7,.0f} ${m_end:>7,.0f} {m_ret:>+7.2f}%")

    print("\n" + "═" * 62)
    print("  ✅ Backtest Complete")
    print("═" * 62)


# ═══════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print(f"\n🚀 Sigma DCA Backtest")
    print(f"   Asset  : {TICKER} (100%)")
    print(f"   Capital: ${INITIAL_CASH:,}")
    print(f"   Period : Recent {BACKTEST_DAYS} trading days (~1 year)")
    print(f"   Method : {VOL_METHOD} λ={EWMA_LAMBDA} × {ENTRY_MULTIPLIER}")
    print("─" * 62)

    df = fetch_data(TICKER)
    result = run_backtest(df)
    print_report(result)
