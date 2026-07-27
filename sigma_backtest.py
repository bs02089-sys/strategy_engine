#!/usr/bin/env python3
"""
═══════════════════════════════════════════════════════════════
  Sigma DCA Backtest — 1-Year Walk-Forward Simulation
═══════════════════════════════════════════════════════════════

Reuses sigma_DCA_manager.py's sigma-calculation & LOC logic.

Supports two modes:
  1. Normal    : single backtest with ENTRY_MULTIPLIER from config
  2. Sweep     : run over a range of multipliers and report optimal values

Usage:
  python3 sigma_backtest.py                          # normal single run
  python3 sigma_backtest.py --sweep                   # single-period multiplier sweep
  python3 sigma_backtest.py --multi-sweep              # multi-period cross-validation
  python3 sigma_backtest.py --portfolio-sweep           # TQQQ/SOXL weight optimization
  python3 sigma_backtest.py --multi-portfolio-sweep     # multi-period weight validation
"""

import sys
import os
import numpy as np
import pandas as pd
import yfinance as yf
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sigma_DCA_manager import (
    _calculate_volatility_from_closes,
    _calculate_loc_from_sigma,
)

# ══════════════════════════════════════════════
# Configuration
# ══════════════════════════════════════════════

import json

CONFIG_PATH       = "portfolio_config.json"
INITIAL_CASH      = 50_000
TICKER            = "SOXL"
LOOKBACK_DAYS     = 252
VOL_METHOD        = "EWMA"
EWMA_LAMBDA       = 0.94
BUY_AMOUNT        = 2_500
MAX_BUYS          = 20
BACKTEST_DAYS     = 252
FETCH_BUFFER_DAYS = 60

# Sweep range
SWEEP_START = 0.6
SWEEP_STOP  = 3.0
SWEEP_STEP  = 0.1


def load_entry_multiplier(ticker: str = None) -> float:
    """Read ENTRY_MULTIPLIER from portfolio_config.json for a given ticker.
    If ticker is None, uses the global TICKER, then falls back to first position."""
    target = ticker if ticker else TICKER
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    positions = cfg.get("POSITIONS", {})
    if target in positions and "ENTRY_MULTIPLIER" in positions[target]:
        return float(positions[target]["ENTRY_MULTIPLIER"])
    # Try first available position
    for pos in positions.values():
        if "ENTRY_MULTIPLIER" in pos:
            return float(pos["ENTRY_MULTIPLIER"])
    raise KeyError(f"ENTRY_MULTIPLIER not found in {CONFIG_PATH} for {target}")


ENTRY_MULTIPLIER = load_entry_multiplier()


# ══════════════════════════════════════════════
# Data Fetching
# ══════════════════════════════════════════════

def fetch_data(ticker: str, end_date: date = None) -> pd.DataFrame:
    """Download OHLCV data for backtest ending on end_date (default: today)."""
    if end_date is None:
        end_date = date.today()
    total_calendar = BACKTEST_DAYS + LOOKBACK_DAYS + FETCH_BUFFER_DAYS
    start_date = end_date - timedelta(days=total_calendar)

    print(f"📥 Downloading {ticker} ({start_date} → {end_date})...")
    stock = yf.Ticker(ticker)
    hist = stock.history(start=start_date, end=end_date, interval="1d",
                         auto_adjust=False)
    if hist.empty:
        raise RuntimeError(f"{ticker} returned no data.")

    df = hist[['Close', 'Low']].copy()
    df.columns = ['Close', 'Low']
    df = df.dropna(subset=['Close', 'Low'])
    print(f"   → {len(df)} trading days loaded.")
    return df


# ══════════════════════════════════════════════
# Backtest Engine (parameterized by multiplier)
# ══════════════════════════════════════════════

def run_backtest(df: pd.DataFrame, entry_multiplier: float = ENTRY_MULTIPLIER,
                 verbose: bool = False,
                 initial_cash: float = None, buy_amount: float = None) -> dict:
    """
    Walk forward through df.  Returns a flat result dict with all metrics.
    If verbose=True, prints buy events to stdout.
    """
    if initial_cash is None:
        initial_cash = float(INITIAL_CASH)
    if buy_amount is None:
        buy_amount = float(BUY_AMOUNT)

    closes    = df['Close'].values
    lows      = df['Low'].values
    dates_idx = df.index

    cash         = float(initial_cash)
    shares       = 0.0
    buys         = 0
    trade_log    = []
    daily_values = []
    start_idx    = LOOKBACK_DAYS

    for i in range(start_idx, len(df)):
        prev_close  = float(closes[i - 1])
        today_low   = float(lows[i])
        today_close = float(closes[i])
        today_date  = dates_idx[i]

        lookback_window = pd.Series(closes[i - LOOKBACK_DAYS : i])
        sigma, _ = _calculate_volatility_from_closes(
            lookback_window, LOOKBACK_DAYS, VOL_METHOD, EWMA_LAMBDA
        )
        loc_price = _calculate_loc_from_sigma(prev_close, sigma, entry_multiplier)
        triggered = today_low <= loc_price

        buy_price = min(today_close, loc_price) if triggered else None
        buy_amt = 0.0
        buy_shares = 0.0

        if triggered and cash >= buy_amount and buys < MAX_BUYS:
            buy_shares = buy_amount / buy_price
            buy_amt = buy_amount
            cash  -= buy_amt
            shares += buy_shares
            buys += 1

            trade_log.append({
                'date': today_date, 'price': round(buy_price, 2),
                'shares': round(buy_shares, 4), 'amount': round(buy_amt, 2),
                'sigma': round(sigma, 4), 'loc': round(loc_price, 2),
                'cash_remaining': round(cash, 2),
            })
            if verbose:
                print(f"  📌 {today_date.date()} | LOC ${loc_price:.2f} hit"
                      f" | Bought {buy_shares:.2f} sh @ ${buy_price:.2f}"
                      f" | Sigma {sigma:.4f}")

        portfolio_value = cash + shares * today_close
        daily_values.append({
            'date': today_date, 'close': today_close,
            'value': round(portfolio_value, 2),
        })

    # ── Compute metrics ──────────────────────────────────────────
    dv_array   = np.array([d['value'] for d in daily_values])
    daily_ret  = dv_array[1:] / dv_array[:-1] - 1
    sharpe     = float(np.sqrt(252) * daily_ret.mean() / daily_ret.std()) if daily_ret.std() > 0 else 0.0

    peak   = np.maximum.accumulate(dv_array)
    dd     = (dv_array - peak) / peak
    mdd    = float(dd.min() * 100)

    asset_start = float(daily_values[0]['close'])
    asset_end   = float(daily_values[-1]['close'])
    buy_hold_ret = (asset_end - asset_start) / asset_start * 100

    total_invested = sum(t['amount'] for t in trade_log)
    avg_buy_price  = np.mean([t['price'] for t in trade_log]) if trade_log else 0
    final_price    = float(daily_values[-1]['close'])

    wins = sum(1 for t in trade_log if final_price > t['price']) if trade_log else 0
    win_rate = wins / len(trade_log) * 100 if trade_log else 0.0

    final_val = float(daily_values[-1]['value'])
    total_ret = (final_val - initial_cash) / initial_cash * 100

    return {
        'multiplier':      entry_multiplier,
        'total_return':    round(total_ret, 2),
        'final_value':     round(final_val, 2),
        'sharpe':          round(sharpe, 2),
        'mdd':             round(mdd, 2),
        'buy_hold_ret':    round(buy_hold_ret, 2),
        'total_buys':      buys,
        'total_invested':  round(total_invested, 2),
        'avg_buy_price':   round(avg_buy_price, 2),
        'win_rate':        round(win_rate, 1),
        'final_price':     round(final_price, 2),
        'remaining_cash':  round(cash, 2),
        'final_shares':    round(shares, 4),
        'period_start':    daily_values[0]['date'],
        'period_end':      daily_values[-1]['date'],
        'trade_log':       trade_log,
        'daily_values':    daily_values,
    }


# ══════════════════════════════════════════════
# Single-run Reporting
# ══════════════════════════════════════════════

def print_report(r: dict):
    print("\n")
    print("═" * 62)
    print("  📊 Sigma DCA Backtest Report")
    print("═" * 62)
    print(f"  Ticker    : {TICKER}")
    print(f"  Strategy  : EWMA (λ={EWMA_LAMBDA}) × {r['multiplier']} LOC")
    print(f"  Capital   : ${INITIAL_CASH:,}")
    print(f"  Period    : {r['period_start'].date()}  →  {r['period_end'].date()}")
    print(f"  Buy size  : ${BUY_AMOUNT:,} / trigger (max {MAX_BUYS}×)")
    print("─" * 62)

    print(f"\n  📈 Performance")
    print(f"     Final Portfolio  : ${r['final_value']:,.2f}")
    print(f"     Total Return     : {r['total_return']:+.2f}%")
    print(f"     Buy & Hold (SOXL): {r['buy_hold_ret']:+.2f}%")
    print(f"     Alpha vs B&H     : {r['total_return'] - r['buy_hold_ret']:+.2f}%")
    print(f"     Sharpe Ratio     : {r['sharpe']}")
    print(f"     Max Drawdown     : {r['mdd']:.2f}%")

    print(f"\n  📋 DCA Activity")
    print(f"     Total LOC Fills  : {r['total_buys']}")
    print(f"     Total Invested   : ${r['total_invested']:,.2f}")
    print(f"     Avg Buy Price    : ${r['avg_buy_price']:.2f}")
    print(f"     Current Price    : ${r['final_price']:.2f}")
    print(f"     Win Rate         : {r['win_rate']:.1f}%")
    print(f"     Remaining Cash   : ${r['remaining_cash']:,.2f}")
    if r['final_shares'] > 0:
        unrealized = (r['final_price'] - r['avg_buy_price']) * r['final_shares']
        print(f"     Shares Held      : {r['final_shares']:.4f}")
        print(f"     Unrealized P&L   : ${unrealized:+,.2f}")

    if r['trade_log']:
        print(f"\n  📝 Trade Log")
        print(f"  {'Date':<14} {'Price':>8} {'Shares':>10} {'Amount':>9} {'Sigma':>8}")
        print(f"  {'─'*14} {'─'*8} {'─'*10} {'─'*9} {'─'*8}")
        for t in r['trade_log']:
            print(f"  {t['date'].strftime('%Y-%m-%d'):<14}"
                  f" ${t['price']:>6.2f} {t['shares']:>10.2f}"
                  f" ${t['amount']:>7,.0f} {t['sigma']:>8.4f}")

    # Monthly breakdown
    monthly = {}
    for d in r['daily_values']:
        mk = d['date'].strftime('%Y-%m')
        monthly.setdefault(mk, []).append(d['value'])
    if len(monthly) > 1:
        print(f"\n  📅 Monthly Portfolio Value")
        print(f"  {'Month':<8} {'Start':>10} {'End':>10} {'Return':>8}")
        print(f"  {'─'*8} {'─'*10} {'─'*10} {'─'*8}")
        for m in sorted(monthly):
            vals = monthly[m]
            s, e = vals[0], vals[-1]
            print(f"  {m:<8} ${s:>7,.0f} ${e:>7,.0f} {(e-s)/s*100:>+7.2f}%")

    print("\n" + "═" * 62)
    print("  ✅ Backtest Complete")
    print("═" * 62)


# ══════════════════════════════════════════════
# Multiplier Sweep
# ══════════════════════════════════════════════

def run_multiplier_sweep(df: pd.DataFrame):
    """Run backtest for each multiplier in [SWEEP_START .. SWEEP_STOP]."""
    multipliers = np.arange(SWEEP_START, SWEEP_STOP + 1e-9, SWEEP_STEP)
    results = []

    print(f"\n🧪 Multiplier Sensitivity Sweep")
    print(f"   Range : {SWEEP_START} → {SWEEP_STOP} (step {SWEEP_STEP})")
    print(f"   Tests : {len(multipliers)}")
    print("─" * 106)
    header = (f"  {'Mult':>5} | {'Return':>8} | {'Final $':>10} | {'Sharpe':>7}"
              f" | {'MDD':>7} | {'Buys':>5} | {'Avg$':>7} | {'WinRate':>7} | {'Cash':>8}")
    print(header)
    print("  " + "─" * 100)

    for mult in multipliers:
        r = run_backtest(df, entry_multiplier=round(mult, 1), verbose=False)
        results.append(r)
        print(f"  {mult:>5.1f} | {r['total_return']:>+7.2f}%"
              f" | ${r['final_value']:>8,.0f} | {r['sharpe']:>7.2f}"
              f" | {r['mdd']:>6.2f}% | {r['total_buys']:>3d}"
              f" | ${r['avg_buy_price']:>6.2f} | {r['win_rate']:>5.1f}%"
              f" | ${r['remaining_cash']:>7,.0f}")

    # ── Find optimum by several metrics ────────────────────────────
    print("─" * 106)

    best_return   = max(results, key=lambda r: r['total_return'])
    best_sharpe   = max(results, key=lambda r: r['sharpe'])
    best_mdd      = min(results, key=lambda r: r['mdd'])   # least negative
    best_winrate  = max(results, key=lambda r: r['win_rate'])
    best_alpha    = max(results, key=lambda r: r['total_return'] - r['buy_hold_ret'])

    print(f"\n  🏆 Optimum by metric:")
    print(f"     Max Return     : multiplier={best_return['multiplier']:.1f}"
          f"  → {best_return['total_return']:+.2f}%  (Sharpe {best_return['sharpe']})")
    print(f"     Max Sharpe     : multiplier={best_sharpe['multiplier']:.1f}"
          f"  → {best_sharpe['sharpe']:.2f}  (Return {best_sharpe['total_return']:+.2f}%)")
    print(f"     Min Drawdown   : multiplier={best_mdd['multiplier']:.1f}"
          f"  → {best_mdd['mdd']:.2f}%")
    print(f"     Max Win Rate   : multiplier={best_winrate['multiplier']:.1f}"
          f"  → {best_winrate['win_rate']:.1f}%")
    print(f"     Max Alpha      : multiplier={best_alpha['multiplier']:.1f}"
          f"  → {best_alpha['total_return'] - best_alpha['buy_hold_ret']:+.2f}%")

    # ── Show old 1.41 baseline for historical reference ───────────
    baseline = next((r for r in results if abs(r['multiplier'] - 1.41) < 0.05), None)
    if baseline:
        print(f"\n  📌 Previous baseline (mult=1.41, for reference):")
        print(f"     Return {baseline['total_return']:+.2f}%"
              f" | Sharpe {baseline['sharpe']:.2f}"
              f" | MDD {baseline['mdd']:.2f}%"
              f" | WinRate {baseline['win_rate']:.1f}%")
    print("\n" + "═" * 62)
    print("  ✅ Sweep Complete")
    print("═" * 62)

    return results


# ══════════════════════════════════════════════
# Multi-Period Sweep (Generalization Test)
# ══════════════════════════════════════════════

def run_multi_period_sweep():
    """
    Run multiplier sweep across multiple historical 1-year periods to test
    whether a single multiplier generalizes across market regimes.
    """
    today = date.today()
    periods = [
        (today - timedelta(days=365), today, "🔥 Current (Strong Bull)"),
        (today - timedelta(days=730), today - timedelta(days=365), "📈 Bull"),
        (today - timedelta(days=1095), today - timedelta(days=730), "📊 Recovery"),
        (today - timedelta(days=1460), today - timedelta(days=1095), "📉 Bear Bottom"),
        (today - timedelta(days=1825), today - timedelta(days=1460), "💥 Bear Crash"),
    ]

    multipliers = np.arange(SWEEP_START, SWEEP_STOP + 1e-9, SWEEP_STEP)

    print(f"\n🌍 Multi-Period Multiplier Generalization Test")
    print(f"   Testing {len(periods)} market regimes × {len(multipliers)} multipliers")
    print("=" * 120)

    all_results = {}  # period_label -> {multiplier -> result}

    for start_dt, end_dt, label in periods:
        print(f"\n📅 Period: {label}  ({start_dt} → {end_dt})")
        print("─" * 120)

        df = fetch_data(TICKER, end_date=end_dt)
        # Trim to only cover the intended backtest window (compare by date
        # to avoid timezone-aware vs timezone-naive dtype mismatch)
        df = df[df.index.date <= end_dt]

        period_results = []
        for mult in multipliers:
            mult_rounded = round(mult, 1)
            r = run_backtest(df, entry_multiplier=mult_rounded, verbose=False)
            period_results.append(r)

        all_results[label] = {r['multiplier']: r for r in period_results}

        # Print summary for this period
        best_by_return = max(period_results, key=lambda r: r['total_return'])
        best_by_sharpe = max(period_results, key=lambda r: r['sharpe'])

        # Also show baseline 0.9, 1.41, 1.618
        for ref_mult in [0.9, 1.41, 1.618]:
            ref = next((r for r in period_results if abs(r['multiplier'] - ref_mult) < 0.05), None)
            if ref:
                print(f"   mult={ref_mult:5.2f} → Return {ref['total_return']:>+7.2f}%"
                      f" | Sharpe {ref['sharpe']:>5.2f} | MDD {ref['mdd']:>6.2f}%"
                      f" | Buys {ref['total_buys']:>2d} | Win {ref['win_rate']:>5.1f}%")

        print(f"   🏆 Best Return   : mult={best_by_return['multiplier']:.1f}"
              f" → {best_by_return['total_return']:+.2f}% (Sharpe {best_by_return['sharpe']})")
        print(f"   🏆 Best Sharpe   : mult={best_by_sharpe['multiplier']:.1f}"
              f" → {best_by_sharpe['sharpe']:.2f} (Return {best_by_sharpe['total_return']:+.2f}%)")

    # ── Cross-period ranking ───────────────────────────────────────
    print("\n" + "═" * 120)
    print("  🏆 Cross-Period Consistency Ranking")
    print("  (lower rank = more consistent across all periods)")
    print("─" * 120)

    # For each multiplier, calculate its average rank across all periods
    mult_rankings = {}
    for mult_raw in multipliers:
        mult = round(float(mult_raw), 1)
        ranks = []
        for label in all_results:
            period_results = list(all_results[label].values())
            # Rank by return (1 = best)
            sorted_by_return = sorted(period_results, key=lambda r: -r['total_return'])
            rank = next(i + 1 for i, r in enumerate(sorted_by_return)
                        if abs(r['multiplier'] - mult) < 0.05)
            ranks.append(rank)

        avg_rank = np.mean(ranks)
        std_rank = np.std(ranks)
        mult_rankings[mult] = (avg_rank, std_rank)

    # Show top 10 most consistent multipliers
    print(f"  {'Rank':<6} {'Mult':>6} {'AvgRank':>8} {'StdRank':>8} {'Period1':>8} {'Period2':>8} {'Period3':>8} {'Period4':>8} {'Period5':>8}")
    print(f"  {'─'*6} {'─'*6} {'─'*8} {'─'*8} {'─'*8} {'─'*8} {'─'*8} {'─'*8} {'─'*8}")

    sorted_mults = sorted(mult_rankings.items(), key=lambda x: x[1][0])
    for rank_idx, (mult, (avg_rank, std_rank)) in enumerate(sorted_mults[:12], 1):
        # Get return for each period
        returns = []
        for label in all_results:
            r = all_results[label][mult]
            returns.append(f"{r['total_return']:>+7.2f}%")
        print(f"  {rank_idx:<6} {mult:>5.1f}  {avg_rank:>7.2f}  {std_rank:>7.2f}  "
              + "  ".join(returns))

    # ── Find the overall best ──────────────────────────────────────
    # Best by average rank
    best_consistent = sorted_mults[0]
    print(f"\n  🥇 Most Consistent Multiplier: {best_consistent[0]:.1f}"
          f" (avg rank {best_consistent[1][0]:.2f}, std {best_consistent[1][1]:.2f})")

    # Best average return across periods
    mult_avg_returns = {}
    for mult_raw in multipliers:
        mult = round(float(mult_raw), 1)
        rets = [all_results[label][mult]['total_return'] for label in all_results]
        if rets:
            mult_avg_returns[mult] = np.mean(rets)
    best_avg_return = max(mult_avg_returns.items(), key=lambda x: x[1])
    print(f"  🥇 Best Avg Return  : multiplier={best_avg_return[0]:.1f}"
          f" → avg {best_avg_return[1]:+.2f}% across all periods")

    # Best by Sharpe consistency
    mult_avg_sharpe = {}
    for mult_raw in multipliers:
        mult = round(float(mult_raw), 1)
        shs = [all_results[label][mult]['sharpe'] for label in all_results]
        if shs:
            mult_avg_sharpe[mult] = np.mean(shs)
    best_avg_sharpe = max(mult_avg_sharpe.items(), key=lambda x: x[1])
    print(f"  🥇 Best Avg Sharpe   : multiplier={best_avg_sharpe[0]:.1f}"
          f" → avg Sharpe {best_avg_sharpe[1]:.2f}")

    print("\n" + "═" * 120)
    print("  ✅ Multi-Period Sweep Complete")
    print("═" * 120)


# ══════════════════════════════════════════════
# Portfolio Weight Sweep (TQQQ + SOXL allocation)
# ══════════════════════════════════════════════

def _run_portfolio_sweep_core(dfs: dict, mults: dict, total_capital: float,
                               label: str = "", verbose: bool = True) -> list:
    """Core portfolio sweep logic. Returns list of result dicts for each weight."""
    weights = np.arange(0.1, 1.0, 0.1)  # 10% → 90%
    results = []

    for tqqq_w in weights:
        soxl_w = round(1.0 - tqqq_w, 1)
        tqqq_w = round(tqqq_w, 1)

        cap_tqqq = total_capital * tqqq_w
        cap_soxl = total_capital * soxl_w
        buy_tqqq = cap_tqqq / MAX_BUYS
        buy_soxl = cap_soxl / MAX_BUYS

        r_tqqq = run_backtest(dfs['TQQQ'], entry_multiplier=mults['TQQQ'],
                              initial_cash=cap_tqqq, buy_amount=buy_tqqq)
        r_soxl = run_backtest(dfs['SOXL'], entry_multiplier=mults['SOXL'],
                              initial_cash=cap_soxl, buy_amount=buy_soxl)

        dv_tqqq = {d['date'].date(): d['value'] for d in r_tqqq['daily_values']}
        dv_soxl = {d['date'].date(): d['value'] for d in r_soxl['daily_values']}
        all_dates = sorted(set(dv_tqqq.keys()) & set(dv_soxl.keys()))
        if not all_dates:
            continue

        combined_values = [dv_tqqq[d] + dv_soxl[d] for d in all_dates]
        portfolio_return = (combined_values[-1] - total_capital) / total_capital * 100

        cv_arr = np.array(combined_values)
        daily_ret = cv_arr[1:] / cv_arr[:-1] - 1
        sharpe = float(np.sqrt(252) * daily_ret.mean() / daily_ret.std()) if daily_ret.std() > 0 else 0.0

        peak = np.maximum.accumulate(cv_arr)
        dd = (cv_arr - peak) / peak
        mdd = float(dd.min() * 100)

        results.append({
            'tqqq_pct': tqqq_w * 100,
            'soxl_pct': soxl_w * 100,
            'total_return': round(portfolio_return, 2),
            'sharpe': round(sharpe, 2),
            'mdd': round(mdd, 2),
            'final_value': round(combined_values[-1], 2),
            'r_tqqq': r_tqqq,
            'r_soxl': r_soxl,
        })

        if verbose:
            print(f"  TQQQ {tqqq_w*100:>3.0f}% / SOXL {soxl_w*100:>3.0f}%"
                  f" | Return {portfolio_return:>+7.2f}% | Sharpe {sharpe:>5.2f}"
                  f" | MDD {mdd:>6.2f}% | Fills {r_tqqq['total_buys']}/{r_soxl['total_buys']}")

    return results


def run_portfolio_sweep():
    """Sweep over TQQQ/SOXL weights for the current 1-year period."""
    tickers = ["TQQQ", "SOXL"]
    mults = {t: load_entry_multiplier(t) for t in tickers}

    print(f"\n📊 Portfolio Weight Optimization — TQQQ + SOXL")
    print(f"   Total capital: ${INITIAL_CASH:,}")
    print(f"   Multipliers : TQQQ × {mults['TQQQ']}  |  SOXL × {mults['SOXL']}")
    print("=" * 130)

    dfs = {t: fetch_data(t) for t in tickers}
    results = _run_portfolio_sweep_core(dfs, mults, float(INITIAL_CASH), label="Current")

    if not results:
        return

    print("═" * 130)
    best_return = max(results, key=lambda r: r['total_return'])
    best_sharpe = max(results, key=lambda r: r['sharpe'])
    best_mdd    = min(results, key=lambda r: r['mdd'])

    print(f"\n  🏆 Optimal Allocations:")
    print(f"     Max Return : TQQQ {best_return['tqqq_pct']:.0f}% / SOXL {best_return['soxl_pct']:.0f}%"
          f"  → {best_return['total_return']:+.2f}% (Sharpe {best_return['sharpe']})")
    print(f"     Max Sharpe : TQQQ {best_sharpe['tqqq_pct']:.0f}% / SOXL {best_sharpe['soxl_pct']:.0f}%"
          f"  → Sharpe {best_sharpe['sharpe']:.2f} (Return {best_sharpe['total_return']:+.2f}%)")
    print(f"     Min MDD    : TQQQ {best_mdd['tqqq_pct']:.0f}% / SOXL {best_mdd['soxl_pct']:.0f}%"
          f"  → MDD {best_mdd['mdd']:.2f}%")

    best = best_sharpe
    b_tqqq = best['r_tqqq']
    b_soxl = best['r_soxl']
    print(f"\n  📋 Best Sharpe Details: TQQQ ${INITIAL_CASH*best['tqqq_pct']/100:,.0f}"
          f" / SOXL ${INITIAL_CASH*best['soxl_pct']/100:,.0f}"
          f" → {best['total_return']:+.2f}% | Sharpe {best['sharpe']}"
          f" | TQQQ {b_tqqq['total_buys']}buys {b_tqqq['win_rate']:.0f}%win"
          f" | SOXL {b_soxl['total_buys']}buys {b_soxl['win_rate']:.0f}%win")

    print("\n" + "═" * 130)
    print("  ✅ Portfolio Sweep Complete")
    print("═" * 130)


# ══════════════════════════════════════════════
# Multi-Period Portfolio Sweep (Generalization Test)
# ══════════════════════════════════════════════

def run_multi_period_portfolio_sweep():
    """
    Run portfolio weight sweep across multiple historical 1-year periods
    to test whether the optimal TQQQ/SOXL allocation generalizes.
    """
    today = date.today()
    periods = [
        (today - timedelta(days=365), today, "🔥 Strong Bull"),
        (today - timedelta(days=730), today - timedelta(days=365), "📈 Bull"),
        (today - timedelta(days=1095), today - timedelta(days=730), "📊 Recovery"),
        (today - timedelta(days=1460), today - timedelta(days=1095), "📉 Bear Bottom"),
        (today - timedelta(days=1825), today - timedelta(days=1460), "💥 Bear Crash"),
    ]

    tickers = ["TQQQ", "SOXL"]
    mults = {t: load_entry_multiplier(t) for t in tickers}

    print(f"\n🌍 Multi-Period Portfolio Weight Generalization Test")
    print(f"   Multipliers : TQQQ × {mults['TQQQ']}  |  SOXL × {mults['SOXL']}")
    print(f"   Testing {len(periods)} market regimes × 9 allocation ratios")
    print("=" * 185)

    all_optimals = {}   # period_label -> {'by_return': ..., 'by_sharpe': ..., 'all_results': [...]}

    for start_dt, end_dt, label in periods:
        print(f"\n📅 {label}  ({start_dt} → {end_dt})")
        print("─" * 130)

        dfs = {t: fetch_data(t, end_date=end_dt) for t in tickers}
        # Trim to backtest window
        for t in tickers:
            dfs[t] = dfs[t][dfs[t].index.date <= end_dt]

        results = _run_portfolio_sweep_core(dfs, mults, float(INITIAL_CASH),
                                            label=label, verbose=True)

        if not results:
            continue

        best_return = max(results, key=lambda r: r['total_return'])
        best_sharpe = max(results, key=lambda r: r['sharpe'])
        print(f"   🏆 Best Return : TQQQ {best_return['tqqq_pct']:.0f}%"
              f" → {best_return['total_return']:+.2f}%")
        print(f"   🏆 Best Sharpe : TQQQ {best_sharpe['tqqq_pct']:.0f}%"
              f" → Sharpe {best_sharpe['sharpe']:.2f}")

        all_optimals[label] = {
            'by_return': best_return,
            'by_sharpe': best_sharpe,
            'all_results': results,
        }

    # ── Cross-period analysis ──────────────────────────────────────
    if not all_optimals:
        print("\n⚠️  No valid periods to analyze.")
        return

    print("\n" + "═" * 185)
    print("  🏆 Cross-Period Optimal Allocation Comparison")
    print("─" * 185)
    print(f"  {'Period':<20} {'Best Ret TQQQ':>14} {'Best Ret %':>12}"
          f" {'Best Sh TQQQ':>14} {'Best Sh Sharpe':>15}")
    print(f"  {'─'*20} {'─'*14} {'─'*12} {'─'*14} {'─'*15}")

    allocation_votes_return = {}
    allocation_votes_sharpe = {}

    for label in all_optimals:
        br = all_optimals[label]['by_return']
        bs = all_optimals[label]['by_sharpe']
        print(f"  {label:<20} {br['tqqq_pct']:>13.0f}% {br['total_return']:>+11.2f}%"
              f" {bs['tqqq_pct']:>13.0f}% {bs['sharpe']:>14.2f}")

        key_r = f"TQQQ {br['tqqq_pct']:.0f}%"
        key_s = f"TQQQ {bs['tqqq_pct']:.0f}%"
        allocation_votes_return[key_r] = allocation_votes_return.get(key_r, 0) + 1
        allocation_votes_sharpe[key_s] = allocation_votes_sharpe.get(key_s, 0) + 1

    # ── Find consensus ────────────────────────────────────────────
    print("─" * 185)
    consensus_ret = max(allocation_votes_return, key=allocation_votes_return.get)
    consensus_sh = max(allocation_votes_sharpe, key=allocation_votes_sharpe.get)

    print(f"\n  🗳️  Consensus by Max Return  : {consensus_ret}"
          f" ({allocation_votes_return[consensus_ret]}/{len(all_optimals)} periods)")
    print(f"  🗳️  Consensus by Max Sharpe  : {consensus_sh}"
          f" ({allocation_votes_sharpe[consensus_sh]}/{len(all_optimals)} periods)")

    # Average return of current best (10/90) across all periods
    print(f"\n  📌 TQQQ 10% / SOXL 90% performance per period:")
    ref_returns = []
    ref_sharpes = []
    for label, opt in all_optimals.items():
        # Find the 10/90 result from all_results
        ref = next((r for r in opt['all_results'] if abs(r['tqqq_pct'] - 10) < 0.5), None)
        if ref:
            ref_returns.append(ref['total_return'])
            ref_sharpes.append(ref['sharpe'])
            print(f"     {label:<20} → {ref['total_return']:+.2f}%  (Sharpe {ref['sharpe']})")
        else:
            print(f"     {label:<20} → 10/90 not tested")

    if ref_returns:
        avg_ret = np.mean(ref_returns)
        avg_sh = np.mean(ref_sharpes)
        print(f"     {'─'*20} ────────────")
        print(f"     {'Average':<20} → {avg_ret:+.2f}%  (Sharpe {avg_sh:.2f})")

    print("\n" + "═" * 185)
    print("  ✅ Multi-Period Portfolio Sweep Complete")
    print("═" * 185)


# ══════════════════════════════════════════════
# Portfolio Detail Run (from config allocation)
# ══════════════════════════════════════════════

def load_allocation_pct(ticker: str) -> float:
    """Read ALLOCATION_PCT from portfolio_config.json for a given ticker."""
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    positions = cfg.get("POSITIONS", {})
    if ticker in positions and "ALLOCATION_PCT" in positions[ticker]:
        return float(positions[ticker]["ALLOCATION_PCT"])
    raise KeyError(f"ALLOCATION_PCT not found in {CONFIG_PATH} for {ticker}")


def print_portfolio_report(r_portfolio: dict):
    """Print a detailed portfolio report with combined and per-ticker results."""
    R = r_portfolio
    tqqq = R['r_tqqq']
    soxl = R['r_soxl']
    total = float(INITIAL_CASH)

    print("\n")
    print("═" * 72)
    print("  📊 Portfolio DCA Backtest Report — TQQQ + SOXL")
    print("═" * 72)
    print(f"  Allocation : TQQQ {R['tqqq_pct']:.0f}%  /  SOXL {R['soxl_pct']:.0f}%")
    print(f"  Capital    : ${total:,.0f}  (TQQQ ${total*R['tqqq_pct']/100:,.0f} / SOXL ${total*R['soxl_pct']/100:,.0f})")
    print(f"  Multiplier : TQQQ × {tqqq['multiplier']}  |  SOXL × {soxl['multiplier']}")
    print(f"  Period     : {R['period_start']}  →  {R['period_end']}")
    print("─" * 72)

    # Combined performance
    print(f"\n  📈 Portfolio Performance")
    print(f"     Total Return      : {R['total_return']:+.2f}%")
    print(f"     Final Value       : ${R['final_value']:,.2f}")
    print(f"     Sharpe Ratio      : {R['sharpe']}")
    print(f"     Max Drawdown      : {R['mdd']:.2f}%")

    # Per-ticker comparison
    print(f"\n  📋 Per-Ticker Comparison")
    print(f"  {'Ticker':<8} {'Capital':>10} {'Return':>10} {'Fill':>5} {'AvgBuy':>8}"
          f" {'WinRate':>8} {'CashRem':>10}")
    print(f"  {'─'*8} {'─'*10} {'─'*10} {'─'*5} {'─'*8} {'─'*8} {'─'*10}")
    for label, r in [('TQQQ', tqqq), ('SOXL', soxl)]:
        print(f"  {label:<8} ${total*R[f'{label.lower()}_pct']/100:>7,.0f}"
              f" {r['total_return']:>+9.2f}% {r['total_buys']:>3d}"
              f" ${r['avg_buy_price']:>6.2f} {r['win_rate']:>6.1f}%"
              f" ${r['remaining_cash']:>7,.0f}")

    # Monthly combined breakdown
    combined_dv = R['combined_daily_values']
    if combined_dv:
        monthly = {}
        for d in combined_dv:
            mk = d['date'].strftime('%Y-%m')
            monthly.setdefault(mk, []).append(d['value'])
        print(f"\n  📅 Monthly Portfolio Value")
        print(f"  {'Month':<8} {'Start':>10} {'End':>10} {'Return':>8}")
        print(f"  {'─'*8} {'─'*10} {'─'*10} {'─'*8}")
        for m in sorted(monthly):
            vals = monthly[m]
            s, e = vals[0], vals[-1]
            print(f"  {m:<8} ${s:>7,.0f} ${e:>7,.0f} {(e-s)/s*100:>+7.2f}%")

    # Buy logs
    for label, r in [('TQQQ', tqqq), ('SOXL', soxl)]:
        if r['trade_log']:
            print(f"\n  📝 {label} Buy Log")
            print(f"  {'Date':<14} {'Price':>8} {'Shares':>10} {'Amount':>9} {'Sigma':>8}")
            print(f"  {'─'*14} {'─'*8} {'─'*10} {'─'*9} {'─'*8}")
            for t in r['trade_log']:
                print(f"  {t['date'].strftime('%Y-%m-%d'):<14}"
                      f" ${t['price']:>6.2f} {t['shares']:>10.2f}"
                      f" ${t['amount']:>7,.0f} {t['sigma']:>8.4f}")

    print("\n" + "═" * 72)
    print("  ✅ Portfolio Run Complete")
    print("═" * 72)


def run_portfolio_detail():
    """Run a single portfolio backtest with the allocation from portfolio_config.json."""
    tickers = ["TQQQ", "SOXL"]
    mults = {t: load_entry_multiplier(t) for t in tickers}
    alloc = {t: load_allocation_pct(t) for t in tickers}

    total_alloc = sum(alloc.values())
    tqqq_pct = alloc['TQQQ'] / total_alloc * 100
    soxl_pct = alloc['SOXL'] / total_alloc * 100

    total_capital = float(INITIAL_CASH)
    cap_tqqq = total_capital * tqqq_pct / 100
    cap_soxl = total_capital * soxl_pct / 100

    dfs = {t: fetch_data(t) for t in tickers}

    r_tqqq = run_backtest(dfs['TQQQ'], entry_multiplier=mults['TQQQ'],
                          initial_cash=cap_tqqq, buy_amount=cap_tqqq / MAX_BUYS,
                          verbose=True)
    r_soxl = run_backtest(dfs['SOXL'], entry_multiplier=mults['SOXL'],
                          initial_cash=cap_soxl, buy_amount=cap_soxl / MAX_BUYS,
                          verbose=True)

    # Combine daily values
    dv_tqqq = {d['date'].date(): d['value'] for d in r_tqqq['daily_values']}
    dv_soxl = {d['date'].date(): d['value'] for d in r_soxl['daily_values']}
    all_dates = sorted(set(dv_tqqq.keys()) & set(dv_soxl.keys()))

    if not all_dates:
        print("⚠️  No overlapping trading days between tickers.")
        return

    combined_values = [dv_tqqq[d] + dv_soxl[d] for d in all_dates]
    combined_dv = [{'date': pd.Timestamp(d), 'value': round(cv, 2)}
                   for d, cv in zip(all_dates, combined_values)]

    portfolio_return = (combined_values[-1] - total_capital) / total_capital * 100
    cv_arr = np.array(combined_values)
    daily_ret = cv_arr[1:] / cv_arr[:-1] - 1
    sharpe = float(np.sqrt(252) * daily_ret.mean() / daily_ret.std()) if daily_ret.std() > 0 else 0.0
    peak = np.maximum.accumulate(cv_arr)
    dd = (cv_arr - peak) / peak
    mdd = float(dd.min() * 100)

    portfolio_result = {
        'tqqq_pct': round(tqqq_pct, 1),
        'soxl_pct': round(soxl_pct, 1),
        'total_return': round(portfolio_return, 2),
        'sharpe': round(sharpe, 2),
        'mdd': round(mdd, 2),
        'final_value': round(combined_values[-1], 2),
        'period_start': all_dates[0],
        'period_end': all_dates[-1],
        'combined_daily_values': combined_dv,
        'r_tqqq': r_tqqq,
        'r_soxl': r_soxl,
    }

    print_portfolio_report(portfolio_result)


# ══════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════

if __name__ == "__main__":
    sweep_mode      = "--sweep" in sys.argv
    multi_sweep     = "--multi-sweep" in sys.argv
    portfolio_sweep = "--portfolio-sweep" in sys.argv
    multi_portfolio = "--multi-portfolio-sweep" in sys.argv
    portfolio_run   = "--portfolio-run" in sys.argv

    if portfolio_run:
        run_portfolio_detail()
    elif multi_portfolio:
        run_multi_period_portfolio_sweep()
    elif portfolio_sweep:
        run_portfolio_sweep()
    elif multi_sweep:
        run_multi_period_sweep()
    elif sweep_mode:
        df = fetch_data(TICKER)
        run_multiplier_sweep(df)
    else:
        df = fetch_data(TICKER)
        print(f"\n🚀 Sigma DCA Backtest")
        print(f"   Asset  : {TICKER} (100%)")
        print(f"   Capital: ${INITIAL_CASH:,}")
        print(f"   Period : Recent {BACKTEST_DAYS} trading days (~1 year)")
        print(f"   Method : {VOL_METHOD} λ={EWMA_LAMBDA} × {ENTRY_MULTIPLIER}")
        print("─" * 62)
        r = run_backtest(df, entry_multiplier=ENTRY_MULTIPLIER, verbose=True)
        print_report(r)
