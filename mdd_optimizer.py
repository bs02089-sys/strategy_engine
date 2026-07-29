#!/usr/bin/env python3
"""
══════════════════════════════════════════════════════════════════════════════
MDD 3-Split DCA Optimizer — TQQQ & SOXL
══════════════════════════════════════════════════════════════════════════════

전략:
  - Buy only (기간 동안 매도 없음)
  - 3차 분할매수:
    1차: ATH에서 DD1% 하락 → 투자금의 R1% 매수
    2차: ATH에서 DD2% 하락 → 투자금의 R2% 매수 (DD2 > DD1)
    3차: ATH에서 DD3% 하락 → 잔여 (100-R1-R2)% 매수 (DD3 > DD2)
  - 총 투자금: $50,000 (USD)
  - 기간: 최근 10년

검증 목표:
  TQQQ와 SOXL 각 종목에 대해 CAGR이 최대가 되는
  (DD1, DD2, DD3, R1%, R2%) 조합을 탐색

Usage:
  python3 mdd_optimizer.py
══════════════════════════════════════════════════════════════════════════════
"""

import sys
import os
import json
import itertools
import numpy as np
import pandas as pd
import yfinance as yf
from datetime import date, timedelta
from dataclasses import dataclass, field, asdict
from typing import List, Tuple, Optional

# ══════════════════════════════════════════════
# Configuration
# ══════════════════════════════════════════════

TICKERS        = ["TQQQ", "SOXL"]
INITIAL_CASH   = 50_000
YEARS_BACK     = 10
FETCH_BUFFER   = 120

# ── Phase 1: Broad sweep (coarse grid) ──────────────────────
DD1_MIN, DD1_MAX, DD1_STEP = 3.0, 28.0, 2.5
DD2_MIN, DD2_MAX, DD2_STEP = 5.0, 50.0, 2.5   # actual min = DD1 + 2.5
DD3_MIN, DD3_MAX, DD3_STEP = 8.0, 75.0, 2.5   # actual min = DD2 + 2.5

R1_VALUES = [15, 20, 25, 30, 35, 40, 45, 50]   # 1차 매수 비율 (%)
R2_VALUES = [15, 20, 25, 30, 35, 40, 45]        # 2차 매수 비율 (%)

# ── Phase 2: Fine sweep (around best region) ────────────────
REFINE_RADIUS_DD   = 5.0    # ±5% around best DDs
REFINE_RADIUS_R    = 10     # ±10% around best ratios
REFINE_DD_STEP     = 1.0
REFINE_R_STEP      = 5

# ══════════════════════════════════════════════
# Data structures
# ══════════════════════════════════════════════

@dataclass
class StrategyParams:
    """5-parameter strategy definition."""
    dd1: float    # Drawdown % for 1st buy
    dd2: float    # Drawdown % for 2nd buy
    dd3: float    # Drawdown % for 3rd buy
    r1: float     # % of total capital for 1st buy
    r2: float     # % of total capital for 2nd buy

    @property
    def r3(self) -> float:
        return 100.0 - self.r1 - self.r2

    def validate(self) -> bool:
        """Check parameter constraints."""
        if not (0 < self.dd1 < self.dd2 < self.dd3):
            return False
        if not (5 <= self.r1 <= 85):
            return False
        if not (5 <= self.r2 <= 85):
            return False
        if not (5 <= self.r3 <= 85):
            return False
        return True

    def __hash__(self):
        return hash((self.dd1, self.dd2, self.dd3, self.r1, self.r2))


@dataclass
class SimulationResult:
    """Outcome of one simulation."""
    params: StrategyParams
    cagr: float
    total_return: float
    final_value: float
    bh_cagr: float
    bh_return: float
    total_buys: int
    invested_pct: float       # % of capital actually deployed
    avg_dd_at_buy: float      # avg drawdown % at which buys occurred
    trade_log: list = field(default_factory=list)
    alpha: float = 0.0

    def __post_init__(self):
        self.alpha = self.cagr - self.bh_cagr


# ══════════════════════════════════════════════
# Data Fetching
# ══════════════════════════════════════════════

def fetch_10yr_data(ticker: str) -> pd.DataFrame:
    """Download ~10 years of daily OHLCV data."""
    end_date   = date.today()
    start_date = end_date - timedelta(days=YEARS_BACK * 365 + FETCH_BUFFER)

    print(f"📥 Downloading {ticker} ({start_date} → {end_date})...")
    stock = yf.Ticker(ticker)
    hist = stock.history(start=start_date, end=end_date, interval="1d",
                         auto_adjust=False)
    if hist.empty:
        raise RuntimeError(f"{ticker} returned no data.")

    df = hist[['Close']].copy()
    df.columns = ['Close']
    df = df.dropna(subset=['Close'])
    print(f"   → {len(df)} trading days loaded.")
    return df


# ══════════════════════════════════════════════
# Pre-compute drawdown array
# ══════════════════════════════════════════════

def compute_drawdown_series(closes: np.ndarray) -> np.ndarray:
    """
    Compute running-peak drawdown for each day (as %).
    drawdown[i] = (peak[i] - close[i]) / peak[i] * 100
    """
    peak = np.maximum.accumulate(closes)
    return (peak - closes) / peak * 100.0


# ══════════════════════════════════════════════
# Core Simulation
# ══════════════════════════════════════════════

def simulate(params: StrategyParams, closes: np.ndarray,
             dates: pd.Index, verbose: bool = False) -> SimulationResult:
    """
    Run 3-split DCA simulation with given parameters.

    The running peak is the ALL-TIME HIGH and NEVER resets.
    Buys are placed at CLOSE price on the day the drawdown threshold is met.
    Buy amounts are pre-determined % of total capital.
    """
    if not params.validate():
        raise ValueError(f"Invalid params: {params}")

    total_cash  = float(INITIAL_CASH)
    buy1_amount = total_cash * params.r1 / 100.0
    buy2_amount = total_cash * params.r2 / 100.0
    buy3_amount = total_cash * params.r3 / 100.0

    # Pre-computed drawdown series
    dd_series = compute_drawdown_series(closes)

    cash   = total_cash
    shares = 0.0

    # Track which buys have been triggered
    buy1_done = False
    buy2_done = False
    buy3_done = False

    trade_log = []

    for i in range(len(closes)):
        price    = float(closes[i])
        dd_val   = float(dd_series[i])
        today    = dates[i]

        # Try 1st buy
        if not buy1_done and dd_val >= params.dd1 and cash >= buy1_amount:
            shares += buy1_amount / price
            cash   -= buy1_amount
            buy1_done = True
            trade_log.append({
                'buy_no': 1, 'date': today,
                'price': round(price, 2), 'amount': round(buy1_amount, 2),
                'shares': round(buy1_amount / price, 4),
                'dd_pct': round(dd_val, 2), 'cash_rem': round(cash, 2),
            })
            if verbose:
                print(f"  📌 Buy#1 {today.date()} @ ${price:.2f} | DD {dd_val:.1f}%"
                      f" | ${buy1_amount:,.0f}")

        # Try 2nd buy
        if not buy2_done and dd_val >= params.dd2 and cash >= buy2_amount:
            shares += buy2_amount / price
            cash   -= buy2_amount
            buy2_done = True
            trade_log.append({
                'buy_no': 2, 'date': today,
                'price': round(price, 2), 'amount': round(buy2_amount, 2),
                'shares': round(buy2_amount / price, 4),
                'dd_pct': round(dd_val, 2), 'cash_rem': round(cash, 2),
            })
            if verbose:
                print(f"  📌 Buy#2 {today.date()} @ ${price:.2f} | DD {dd_val:.1f}%"
                      f" | ${buy2_amount:,.0f}")

        # Try 3rd buy
        if not buy3_done and dd_val >= params.dd3 and cash >= buy3_amount:
            shares += buy3_amount / price
            cash   -= buy3_amount
            buy3_done = True
            trade_log.append({
                'buy_no': 3, 'date': today,
                'price': round(price, 2), 'amount': round(buy3_amount, 2),
                'shares': round(buy3_amount / price, 4),
                'dd_pct': round(dd_val, 2), 'cash_rem': round(cash, 2),
            })
            if verbose:
                print(f"  📌 Buy#3 {today.date()} @ ${price:.2f} | DD {dd_val:.1f}%"
                      f" | ${buy3_amount:,.0f}")

    # ── Compute metrics ─────────────────────────────────
    final_price  = float(closes[-1])
    final_value  = cash + shares * final_price
    total_return = (final_value - total_cash) / total_cash * 100.0

    n_days = len(closes)
    years_elapsed = n_days / 252.0
    cagr = (final_value / total_cash) ** (1.0 / years_elapsed) - 1.0 if years_elapsed > 0 else 0.0

    # Buy & Hold
    bh_shares = total_cash / float(closes[0])
    bh_final  = bh_shares * final_price
    bh_ret    = (bh_final - total_cash) / total_cash * 100.0
    bh_cagr   = (bh_final / total_cash) ** (1.0 / years_elapsed) - 1.0 if years_elapsed > 0 else 0.0

    total_buys = sum([buy1_done, buy2_done, buy3_done])
    invested   = sum(t['amount'] for t in trade_log)
    avg_dd     = np.mean([t['dd_pct'] for t in trade_log]) if trade_log else 0.0

    result = SimulationResult(
        params=params,
        cagr=cagr * 100,
        total_return=total_return,
        final_value=final_value,
        bh_cagr=bh_cagr * 100,
        bh_return=bh_ret,
        total_buys=total_buys,
        invested_pct=(invested / total_cash) * 100,
        avg_dd_at_buy=round(avg_dd, 2),
        trade_log=trade_log,
    )
    return result


# ══════════════════════════════════════════════
# Grid Search
# ══════════════════════════════════════════════

def generate_param_grid(
    dd1_min: float, dd1_max: float, dd1_step: float,
    dd2_min: float, dd2_max: float, dd2_step: float,
    dd3_min: float, dd3_max: float, dd3_step: float,
    r1_values: list, r2_values: list,
) -> List[StrategyParams]:
    """Generate all valid (DD1, DD2, DD3, R1, R2) combinations."""
    dd1_options = np.arange(dd1_min, dd1_max + 1e-9, dd1_step)
    params_list = []

    for dd1 in dd1_options:
        dd1 = round(float(dd1), 1)
        for dd2 in np.arange(max(dd2_min, dd1 + dd2_step), dd2_max + 1e-9, dd2_step):
            dd2 = round(float(dd2), 1)
            for dd3 in np.arange(max(dd3_min, dd2 + dd3_step), dd3_max + 1e-9, dd3_step):
                dd3 = round(float(dd3), 1)
                for r1 in r1_values:
                    for r2 in r2_values:
                        p = StrategyParams(dd1=dd1, dd2=dd2, dd3=dd3, r1=r1, r2=r2)
                        if p.validate():
                            params_list.append(p)
    return params_list


def refine_grid(best: StrategyParams, dd_step: float, r_step: int) -> List[StrategyParams]:
    """Generate finer grid around best known parameters."""
    dd1_vals = np.arange(
        max(1.0, best.dd1 - REFINE_RADIUS_DD),
        min(35.0, best.dd1 + REFINE_RADIUS_DD + 1e-9),
        dd_step
    )
    dd2_vals = np.arange(
        max(3.0, best.dd2 - REFINE_RADIUS_DD),
        min(60.0, best.dd2 + REFINE_RADIUS_DD + 1e-9),
        dd_step
    )
    dd3_vals = np.arange(
        max(5.0, best.dd3 - REFINE_RADIUS_DD),
        min(80.0, best.dd3 + REFINE_RADIUS_DD + 1e-9),
        dd_step
    )
    r1_vals = range(
        max(10, best.r1 - REFINE_RADIUS_R),
        min(60, best.r1 + REFINE_RADIUS_R + 1),
        r_step
    )
    r2_vals = range(
        max(10, best.r2 - REFINE_RADIUS_R),
        min(60, best.r2 + REFINE_RADIUS_R + 1),
        r_step
    )

    params_list = []
    for dd1 in dd1_vals:
        dd1 = round(float(dd1), 1)
        for dd2 in dd2_vals:
            dd2 = round(float(dd2), 1)
            if dd2 <= dd1:
                continue
            for dd3 in dd3_vals:
                dd3 = round(float(dd3), 1)
                if dd3 <= dd2:
                    continue
                for r1 in r1_vals:
                    for r2 in r2_vals:
                        p = StrategyParams(dd1=dd1, dd2=dd2, dd3=dd3, r1=r1, r2=r2)
                        if p.validate():
                            params_list.append(p)
    return params_list


def run_grid(closes: np.ndarray, dates: pd.Index,
             params_list: List[StrategyParams],
             label: str = "") -> List[SimulationResult]:
    """Run simulation for all param combinations."""
    n = len(params_list)
    print(f"  Running {n:,} simulations...", end="", flush=True)

    results = []
    for idx, p in enumerate(params_list):
        r = simulate(p, closes, dates)
        results.append(r)

        # Progress every 20%
        if n > 1000 and (idx + 1) % max(1, n // 5) == 0:
            pct = (idx + 1) / n * 100
            print(f" {pct:.0f}%", end="", flush=True)

    print(f" done!")
    return results


# ══════════════════════════════════════════════
# Ranking & Reporting
# ══════════════════════════════════════════════

def rank_results(results: List[SimulationResult],
                 top_n: int = 20) -> List[SimulationResult]:
    """Rank by CAGR, only full-3-buy scenarios."""
    valid = [r for r in results if r.total_buys == 3]
    if not valid:
        valid = [r for r in results if r.total_buys > 0]
    return sorted(valid, key=lambda r: -r.cagr)[:top_n]


def print_phase_header(ticker: str, phase: str, n_params: int,
                        closes: np.ndarray, dates: pd.Index):
    years = len(closes) / 252.0
    print(f"\n{'█' * 100}")
    print(f"  {phase} — {ticker}")
    print(f"{'█' * 100}")
    print(f"  Period : {dates[0].date()} → {dates[-1].date()}  ({years:.1f} years)")
    print(f"  Capital: ${INITIAL_CASH:,}")
    print(f"  Search : {n_params:,} combinations")


def print_top_results(top: List[SimulationResult], phase_label: str = ""):
    """Print the top-N results in a formatted table."""
    if not top:
        print("  ⚠️  No valid results (no buys triggered).")
        return

    print(f"\n  🏆 Top {len(top)} Results {phase_label}")
    print(f"  {'Rank':<5} {'DD1':>6} {'DD2':>6} {'DD3':>6}"
          f" {'R1%':>5} {'R2%':>5} {'R3%':>5}"
          f" {'CAGR':>8} {'Return':>10} {'Final $':>12}"
          f" {'Invest%':>7} {'AvgDD':>6}")
    print(f"  {'─'*5} {'─'*6} {'─'*6} {'─'*6}"
          f" {'─'*5} {'─'*5} {'─'*5}"
          f" {'─'*8} {'─'*10} {'─'*12}"
          f" {'─'*7} {'─'*6}")

    for rank, r in enumerate(top, 1):
        p = r.params
        print(f"  {rank:<5} {p.dd1:>5.1f}% {p.dd2:>5.1f}% {p.dd3:>5.1f}%"
              f" {p.r1:>4.0f}% {p.r2:>4.0f}% {p.r3:>4.0f}%"
              f" {r.cagr:>+7.2f}% {r.total_return:>+9.2f}%"
              f" ${r.final_value:>10,.0f}"
              f" {r.invested_pct:>6.1f}% {r.avg_dd_at_buy:>5.1f}%")


def print_consensus(ticker: str, results: List[SimulationResult],
                    top_n: int = 50):
    """Analyze parameter patterns among top results."""
    top = rank_results(results, top_n)
    if not top:
        return

    dd1_vals = [r.params.dd1 for r in top]
    dd2_vals = [r.params.dd2 for r in top]
    dd3_vals = [r.params.dd3 for r in top]
    r1_vals  = [r.params.r1 for r in top]
    r2_vals  = [r.params.r2 for r in top]

    print(f"\n  📊 Top-{top_n} Parameter Distribution ({ticker}):")
    print(f"  {'Param':<6} {'Mean':>7} {'Median':>8} {'StdDev':>7} {'Min':>6} {'Max':>6} {'Mode (approx)':>15}")
    print(f"  {'─'*6} {'─'*7} {'─'*8} {'─'*7} {'─'*6} {'─'*6} {'─'*15}")

    for name, vals in [("DD1", dd1_vals), ("DD2", dd2_vals), ("DD3", dd3_vals),
                        ("R1%", r1_vals), ("R2%", r2_vals)]:
        # Approximate mode by rounding to nearest integer
        rounded = [round(v) for v in vals]
        from collections import Counter
        mode_val = Counter(rounded).most_common(1)[0][0]
        print(f"  {name:<6} {np.mean(vals):>7.2f} {np.median(vals):>8.2f}"
              f" {np.std(vals):>7.2f} {min(vals):>6.1f} {max(vals):>6.1f}"
              f" {mode_val:>14.0f}")


def print_strategy_detail(r: SimulationResult):
    """Print detailed info for the best strategy."""
    p = r.params
    print(f"\n{'═' * 80}")
    print(f"  🥇 BEST STRATEGY")
    print(f"{'═' * 80}")
    print(f"  📋 Parameters:")
    print(f"     1차 매수 (DD1): {p.dd1:.1f}% 하락 → 투자금의 {p.r1:.0f}%  (${INITIAL_CASH * p.r1 / 100:,.0f})")
    print(f"     2차 매수 (DD2): {p.dd2:.1f}% 하락 → 투자금의 {p.r2:.0f}%  (${INITIAL_CASH * p.r2 / 100:,.0f})")
    print(f"     3차 매수 (DD3): {p.dd3:.1f}% 하락 → 잔여 {p.r3:.0f}%  (${INITIAL_CASH * p.r3 / 100:,.0f})")

    print(f"\n  📈 Performance:")
    print(f"     CAGR              : {r.cagr:+.2f}%")
    print(f"     Total Return      : {r.total_return:+.2f}%")
    print(f"     Final Value       : ${r.final_value:,.2f}")
    print(f"     Buy & Hold CAGR   : {r.bh_cagr:+.2f}%")
    print(f"     Alpha vs B&H      : {r.alpha:+.2f}%")
    print(f"     실제 투자 비율     : {r.invested_pct:.1f}%")
    print(f"     평균 매수 DD      : {r.avg_dd_at_buy:.1f}%")

    if r.trade_log:
        print(f"\n  📝 Buy Log:")
        print(f"  {'Buy#':<5} {'Date':<12} {'Price':>8} {'Amount':>10}"
              f" {'Shares':>10} {'DD%':>7} {'CashRem':>10}")
        print(f"  {'─'*5} {'─'*12} {'─'*8} {'─'*10} {'─'*10} {'─'*7} {'─'*10}")
        for t in r.trade_log:
            dt = t['date']
            print(f"  {t['buy_no']:<5} {dt.strftime('%Y-%m-%d') if hasattr(dt, 'strftime') else dt:<12}"
                  f" ${t['price']:>6.2f} ${t['amount']:>7,.0f}"
                  f" {t['shares']:>9.2f} {t['dd_pct']:>6.1f}% ${t['cash_rem']:>8,.0f}")


# ══════════════════════════════════════════════
# Main Pipeline
# ══════════════════════════════════════════════

def optimize_ticker(ticker: str) -> None:
    """Full optimization pipeline for one ticker."""
    df = fetch_10yr_data(ticker)
    closes = df['Close'].values
    dates  = df.index

    # ── Phase 1: Broad sweep ────────────────────────────────
    broad_params = generate_param_grid(
        DD1_MIN, DD1_MAX, DD1_STEP,
        DD2_MIN, DD2_MAX, DD2_STEP,
        DD3_MIN, DD3_MAX, DD3_STEP,
        R1_VALUES, R2_VALUES,
    )
    print_phase_header(ticker, "🔍 Phase 1: Broad Sweep", len(broad_params), closes, dates)
    broad_results = run_grid(closes, dates, broad_params, "broad")

    top_broad = rank_results(broad_results, 15)
    print_top_results(top_broad, "(Broad Sweep)")
    print_consensus(ticker, broad_results, 30)

    best_broad = top_broad[0] if top_broad else None
    if not best_broad:
        print("⚠️  No valid broad-sweep results.")
        return

    # ── Phase 2: Fine sweep around best ─────────────────────
    fine_params = refine_grid(best_broad.params, REFINE_DD_STEP, REFINE_R_STEP)
    print_phase_header(ticker, "🔬 Phase 2: Fine Sweep", len(fine_params), closes, dates)
    fine_results = run_grid(closes, dates, fine_params, "fine")

    # Combine broad + fine, re-rank
    all_results = broad_results + fine_results
    top_final = rank_results(all_results, 20)
    print_top_results(top_final, "(Final)")
    print_consensus(ticker, all_results, 50)

    best = top_final[0] if top_final else best_broad
    print_strategy_detail(best)

    # ── Save results ────────────────────────────────────────
    save_data = []
    for r in top_final[:50]:
        p = r.params
        save_data.append({
            'dd1': p.dd1, 'dd2': p.dd2, 'dd3': p.dd3,
            'r1': p.r1, 'r2': p.r2, 'r3': p.r3,
            'cagr': r.cagr, 'total_return': r.total_return,
            'final_value': r.final_value,
            'bh_cagr': r.bh_cagr, 'alpha': r.alpha,
            'total_buys': r.total_buys, 'invested_pct': r.invested_pct,
            'avg_dd_at_buy': r.avg_dd_at_buy,
        })

    out_path = f"mdd_opt_{ticker}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(save_data, f, indent=2, ensure_ascii=False)
    print(f"\n  💾 Results saved to {out_path}")


# ══════════════════════════════════════════════
# Entry Point
# ══════════════════════════════════════════════

if __name__ == "__main__":
    import collections  # needed for Counter in print_consensus
    from collections import Counter

    print(f"\n{'█' * 100}")
    print(f"  🚀 MDD 3-Split DCA Optimizer — TQQQ & SOXL")
    print(f"  10-Year Analysis: Optimal DD% & Allocation Ratio")
    print(f"{'█' * 100}")
    print(f"\n  전략 개요:")
    print(f"  • 투자금     : ${INITIAL_CASH:,}")
    print(f"  • 분할       : 3차 분할매수")
    print(f"  • 1차 조건   : ATH에서 DD1% 하락 → R1% 투자")
    print(f"  • 2차 조건   : ATH에서 DD2% 하락 → R2% 투자 (DD2 > DD1)")
    print(f"  • 3차 조건   : ATH에서 DD3% 하락 → 잔여 R3% 투자 (DD3 > DD2)")
    print(f"  • Peak       : All-Time High (리셋 없음, MDD 기반)")
    print(f"  • 목표       : CAGR 최대화")
    print(f"\n  Phase 1 (Broad):  DD: {DD1_MIN}%–{DD3_MAX}% step {DD1_STEP}% | R: {min(R1_VALUES)}%–{max(R2_VALUES)}%")
    print(f"  Phase 2 (Fine):   Refine ±{REFINE_RADIUS_DD}% DD, ±{REFINE_RADIUS_R}% R | step {REFINE_DD_STEP}%/{REFINE_R_STEP}%")

    for ticker in TICKERS:
        try:
            optimize_ticker(ticker)
        except Exception as e:
            print(f"\n⚠️  Error with {ticker}: {e}")

    print(f"\n{'█' * 100}")
    print(f"  ✅ Optimization Complete")
    print(f"{'█' * 100}\n")
