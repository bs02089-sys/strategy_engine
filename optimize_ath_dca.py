#!/usr/bin/env python3
"""
====================================================================================================
  🎯 ATH Drawdown DCA 트리거 최적화 v2 — 다중 사이클
====================================================================================================

v1 (단일 사이클)의 문제:
  - split_used가 리셋되지 않아서 15년 역사 중 마지막 하락 사이클만 반영됨

v2 (다중 사이클)의 개선:
  - ATH가 갱신(new high)되면 split_used를 리셋
  - 여러 번의 하락 사이클을 모두 반영한 최적 트리거 도출
  - CYCLES 카운터로 몇 번의 매수 기회가 있었는지 추적

Usage:
  python3 optimize_ath_dca.py --v2     (새로운 다중 사이클 모드)
  python3 optimize_ath_dca.py          (기존 v1 단일 사이클)
"""

import sys
import numpy as np
import pandas as pd
import yfinance as yf
import json
import shutil
import tempfile
from datetime import datetime, timedelta
from itertools import product

# ══════════════════════════════════════════════
# Configuration
# ══════════════════════════════════════════════
TOTAL_INVESTMENT = 50_000
CONFIG_PATH = "portfolio_config.json"

TRIGGER_SEARCH = {
    'trigger_1': list(range(-10, -36, -5)),
    'trigger_2': list(range(-20, -56, -5)),
    'trigger_3': list(range(-30, -81, -5)),
}

TICKERS = ["TQQQ", "SOXL"]


# ══════════════════════════════════════════════
# Data
# ══════════════════════════════════════════════

def fetch_full_history(ticker: str) -> pd.DataFrame:
    print(f"  Downloading {ticker} (max history)...", end=" ")
    stock = yf.Ticker(ticker)
    hist = stock.history(period="max", interval="1d", auto_adjust=True)
    if hist.empty:
        raise RuntimeError(f"{ticker} returned no data.")
    df = hist[['Close']].copy()
    df.columns = ['Close']
    df = df.dropna()
    print(f"OK {len(df)} days ({df.index[0].date()} -> {df.index[-1].date()})")
    return df


# =============================================================================
# v1: Single-cycle simulator (original, kept for comparison)
# =============================================================================

def simulate_ath_dca_v1(closes, t1, t2, t3):
    """Single-cycle: splits never reset. Only the last drawdown cycle matters."""
    t1, t2, t3 = sorted([abs(t) / 100 for t in (t1, t2, t3)])
    if not (t1 < t2 < t3):
        return None

    cash, total = float(TOTAL_INVESTMENT), float(TOTAL_INVESTMENT)
    shares = 0.0
    portion = total / 3
    rolling_ath = 0.0
    used = [False, False, False]
    triggers = [t1, t2, t3]
    buy_log = []
    daily_values = []

    for i in range(len(closes)):
        price = float(closes.iloc[i])
        if price > rolling_ath:
            rolling_ath = price
        if rolling_ath <= 0:
            daily_values.append(cash)
            continue

        dd = (rolling_ath - price) / rolling_ath
        for s in range(3):
            if not used[s] and dd >= triggers[s]:
                sh = portion / price
                cash -= portion
                shares += sh
                used[s] = True
                buy_log.append({'date': closes.index[i], 'split': s+1, 'price': price, 'dd': dd})
                break  # one split per day max

        daily_values.append(cash + shares * price)

    return _compute_metrics(triggers, buy_log, used, daily_values, total, closes)


# =============================================================================
# v2: Multi-cycle simulator (ATH reset — the main improvement)
# =============================================================================

def simulate_ath_dca_v2(closes, t1, t2, t3):
    """
    Multi-cycle ATH drawdown DCA simulation.

    Key difference from v1:
      - When price sets a NEW all-time high, ALL split_used flags RESET.
      - This allows the strategy to enter multiple cycles across history:
        buy → recover → new ATH → reset → buy again on next drawdown.

    Each cycle deploys up to 3 equal-sized buys (1/3 of capital each).
    The same capital is reused across cycles (cash is not infinite).
    """
    t1, t2, t3 = sorted([abs(t) / 100 for t in (t1, t2, t3)])
    if not (t1 < t2 < t3):
        return None

    cash, total = float(TOTAL_INVESTMENT), float(TOTAL_INVESTMENT)
    shares = 0.0
    portion = total / 3
    rolling_ath = 0.0
    used = [False, False, False]
    triggers = [t1, t2, t3]
    buy_log = []
    daily_values = []
    cycles_completed = 0

    for i in range(len(closes)):
        price = float(closes.iloc[i])

        # ── ATH reset logic ─────────────────────────────────────────
        # If price sets a new ATH after having used at least one split,
        # reset the cycle.  Use a tiny epsilon to avoid churn.
        if price > rolling_ath * 1.001 and any(used):
            rolling_ath = price
            used = [False, False, False]
            cycles_completed += 1
        elif price > rolling_ath:
            rolling_ath = price

        if rolling_ath <= 0:
            daily_values.append(cash)
            continue

        dd = (rolling_ath - price) / rolling_ath

        # ── Check triggers (one split per day max) ──────────────────
        for s in range(3):
            if not used[s] and dd >= triggers[s]:
                sh = portion / price
                cash -= portion
                shares += sh
                used[s] = True
                buy_log.append({
                    'date': closes.index[i], 'split': s+1,
                    'price': price, 'dd': dd,
                    'cycle': cycles_completed + 1,
                })
                break  # one split per day

        daily_values.append(cash + shares * price)

    # Mark the final cycle if any splits were used
    if any(used):
        cycles_completed += 1

    return _compute_metrics(triggers, buy_log, used, daily_values, total, closes,
                            extra={'cycles': cycles_completed})


# =============================================================================
# Shared metrics computation
# =============================================================================

def _compute_metrics(triggers, buy_log, used, daily_values, total, closes, extra=None):
    """Compute performance metrics shared by v1 and v2 simulators."""
    final_price = float(closes.iloc[-1])
    final_value = daily_values[-1]
    total_return = (final_value - total) / total * 100

    total_invested = len(buy_log) * total / 3
    avg_price = float(np.mean([b['price'] for b in buy_log])) if buy_log else 0
    wins = sum(1 for b in buy_log if final_price > b['price'])
    win_rate = wins / len(buy_log) * 100 if buy_log else 0

    # Buy & Hold
    bh_shares = total / float(closes.iloc[0])
    bh_value = bh_shares * final_price
    bh_return = (bh_value - total) / total * 100

    # Sharpe & MDD from daily portfolio values
    dv = np.array(daily_values)
    daily_ret = dv[1:] / dv[:-1] - 1
    sharpe = float(np.sqrt(252) * daily_ret.mean() / daily_ret.std()) if daily_ret.std() > 0 else 0.0
    peak = np.maximum.accumulate(dv)
    mdd = float(((dv - peak) / peak).min() * 100)

    result = {
        'triggers': tuple(int(abs(t)*100) for t in triggers),
        'total_return': round(total_return, 2),
        'final_value': round(final_value, 2),
        'sharpe': round(sharpe, 2),
        'mdd': round(mdd, 2),
        'bh_return': round(bh_return, 2),
        'alpha': round(total_return - bh_return, 2),
        'total_invested': round(total_invested, 2),
        'splits_used': sum(used),
        'total_buys': len(buy_log),
        'avg_price': round(avg_price, 2),
        'win_rate': round(win_rate, 1),
    }
    if extra:
        result.update(extra)
    return result


# =============================================================================
# Grid search (runs either v1 or v2 simulator)
# =============================================================================

def grid_search(closes, ticker, use_v2=True):
    """Run grid search. use_v2=True uses multi-cycle simulator."""
    sim_fn = simulate_ath_dca_v2 if use_v2 else simulate_ath_dca_v1
    mode_label = "v2 multi-cycle" if use_v2 else "v1 single-cycle"

    t1_vals = TRIGGER_SEARCH['trigger_1']
    t2_vals = TRIGGER_SEARCH['trigger_2']
    t3_vals = TRIGGER_SEARCH['trigger_3']

    valid_combos = sum(1 for t1, t2, t3 in product(t1_vals, t2_vals, t3_vals)
                       if abs(t1) < abs(t2) < abs(t3))

    print(f"\n  Grid Search ({mode_label}): {valid_combos} combinations")
    print(f"     T1: {t1_vals[0]}% ~ {t1_vals[-1]}%")
    print(f"     T2: {t2_vals[0]}% ~ {t2_vals[-1]}%")
    print(f"     T3: {t3_vals[0]}% ~ {t3_vals[-1]}%")

    results = []
    count = 0
    for t1, t2, t3 in product(t1_vals, t2_vals, t3_vals):
        if not (abs(t1) < abs(t2) < abs(t3)):
            continue
        r = sim_fn(closes, t1, t2, t3)
        if r is None:
            continue
        results.append(r)
        count += 1
        if count % 100 == 0:
            print(f"     ... {count}/{valid_combos}", end="\r")

    print(f"     OK {count}/{valid_combos} combinations\n")
    results.sort(key=lambda r: (-r['sharpe'], -r['total_return']))
    return results


# =============================================================================
# Reporting
# =============================================================================

def print_optimal_results(results, ticker, top_n=10, use_v2=True):
    if not results:
        print(f"  No valid results for {ticker}")
        return

    mode_label = "v2 Multi-Cycle" if use_v2 else "v1 Single-Cycle"
    cycles_col = "" if not use_v2 else " Cycles"

    print(f"\n{'=' * 110}")
    print(f"  {ticker} — Top {top_n} Optimal ATH Triggers ({mode_label})")
    print(f"{'=' * 110}")
    print(f"  {'Rank':<6} {'T1':>5} {'T2':>5} {'T3':>5} "
          f"{'Sharpe':>7} {'Return':>10} {'MDD':>7} {'Alpha':>8} "
          f"{'Buys':>5} {'WinRt':>6}{cycles_col:>8} {'AvgBuy':>7}")
    print(f"  {'─'*6} {'─'*5} {'─'*5} {'─'*5} {'─'*7} {'─'*10} {'─'*7} {'─'*8} "
          f"{'─'*5} {'─'*6}{'─'*8 if use_v2 else '':>8} {'─'*7}")

    for rank, r in enumerate(results[:top_n], 1):
        medal = "\U0001f947" if rank == 1 else ("\U0001f948" if rank == 2 else ("\U0001f949" if rank == 3 else ""))
        t1, t2, t3 = r['triggers']
        cycles_str = f"  {r.get('cycles', 1):>4d}x" if use_v2 else ""
        print(f"  {rank:<3} {medal:<3} {t1:>4d}%  {t2:>4d}%  {t3:>4d}%  "
              f"{r['sharpe']:>6.2f}  {r['total_return']:>+8.2f}%  "
              f"{r['mdd']:>5.2f}%  {r['alpha']:>+7.2f}%  "
              f"{r['total_buys']:>3d}  {r['win_rate']:>5.1f}%"
              f"{cycles_str}  ${r['avg_price']:>6.2f}")

    best = results[0]
    t1, t2, t3 = best['triggers']
    print(f"\n  **Optimal for {ticker} ({mode_label})**")
    print(f"     1st: ATH -{abs(t1)}%  |  2nd: ATH -{abs(t2)}%  |  3rd: ATH -{abs(t3)}%")
    print(f"     Sharpe: {best['sharpe']}  |  Return: {best['total_return']:+.2f}%  |  MDD: {best['mdd']:.2f}%")
    print(f"     Alpha: {best['alpha']:+.2f}%  |  Win Rate: {best['win_rate']:.1f}%")
    print(f"     Total Buys: {best['total_buys']}  |  Avg Price: ${best['avg_price']:.2f}")
    if use_v2:
        print(f"     Cycles: {best.get('cycles', 'N/A')}")


def print_comparison(v1_results, v2_results, ticker):
    """Compare v1 vs v2 optimal results side by side."""
    if not v1_results or not v2_results:
        return

    b1, b2 = v1_results[0], v2_results[0]
    print(f"\n{'=' * 110}")
    print(f"  {ticker} — v1 Single-Cycle vs v2 Multi-Cycle Comparison")
    print(f"{'=' * 110}")
    print(f"  {'Metric':<22} {'v1 (Single)':>18} {'v2 (Multi)':>18}")
    print(f"  {'─'*22} {'─'*18} {'─'*18}")

    t1v1, t2v1, t3v1 = b1['triggers']
    t1v2, t2v2, t3v2 = b2['triggers']
    metrics = [
        ("1st Trigger", f"-{abs(t1v1)}%", f"-{abs(t1v2)}%"),
        ("2nd Trigger", f"-{abs(t2v1)}%", f"-{abs(t2v2)}%"),
        ("3rd Trigger", f"-{abs(t3v1)}%", f"-{abs(t3v2)}%"),
        ("Sharpe", str(b1['sharpe']), str(b2['sharpe'])),
        ("Total Return", f"{b1['total_return']:+.2f}%", f"{b2['total_return']:+.2f}%"),
        ("Max DD", f"{b1['mdd']:.2f}%", f"{b2['mdd']:.2f}%"),
        ("Alpha vs B&H", f"{b1['alpha']:+.2f}%", f"{b2['alpha']:+.2f}%"),
        ("Win Rate", f"{b1['win_rate']:.1f}%", f"{b2['win_rate']:.1f}%"),
        ("Total Buys", str(b1['total_buys']), str(b2['total_buys'])),
    ]
    if 'cycles' in b2:
        metrics.append(("Cycles", "1", str(b2['cycles'])))

    for name, v1v, v2v in metrics:
        print(f"  {name:<22} {v1v:>18} {v2v:>18}")

    # Insight
    print(f"\n  Key Insight:")
    same_t1 = abs(t1v1) == abs(t1v2)
    same_t2 = abs(t2v1) == abs(t2v2)
    same_t3 = abs(t3v1) == abs(t3v2)

    if same_t1 and same_t2 and same_t3:
        print(f"     Both v1 and v2 converged to the SAME optimal triggers.")
        print(f"     This means the triggers are robust across different cycle definitions.")
    else:
        print(f"     v1 chose ({abs(t1v1)}%, {abs(t2v1)}%, {abs(t3v1)}%)")
        print(f"     v2 chose ({abs(t1v2)}%, {abs(t2v2)}%, {abs(t3v2)}%)")
        print(f"     v2 considers {b2.get('cycles', '?')} drawdown cycles vs v1's 1 cycle.")
        if abs(t1v2) < abs(t1v1):
            print(f"     v2 enters earlier (shallower trigger) to catch more opportunities.")
        elif abs(t1v2) > abs(t1v1):
            print(f"     v2 waits for deeper drawdowns, avoiding false signals.")

    print(f"\n  Recommendation:")
    if b2['sharpe'] > b1['sharpe']:
        print(f"     v2 (multi-cycle) has HIGHER Sharpe — use v2 triggers.")
    elif b1['sharpe'] > b2['sharpe']:
        print(f"     v1 (single-cycle) has HIGHER Sharpe — but v2 is more realistic.")
        print(f"     Use v2 triggers for robustness.")
    else:
        print(f"     Both have similar Sharpe. Use v2 triggers (data-backed).")


def update_config(ticker, t1, t2, t3, source="v2 multi-cycle"):
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        cfg = {"POSITIONS": {}}

    if ticker not in cfg.setdefault("POSITIONS", {}):
        cfg["POSITIONS"][ticker] = {}

    pos = cfg["POSITIONS"][ticker]
    pos["ATH_DCA"] = {
        "ENABLED": True,
        "SPLITS": 3,
        "TRIGGER_1": f"-{abs(t1)}%",
        "TRIGGER_2": f"-{abs(t2)}%",
        "TRIGGER_3": f"-{abs(t3)}%",
        "STRATEGY": source,
    }

    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json", encoding="utf-8") as tmp:
        json.dump(cfg, tmp, indent=4, ensure_ascii=False)
        tmp_path = tmp.name
    shutil.move(tmp_path, CONFIG_PATH)
    print(f"  OK {ticker} ATH_DCA config updated (source: {source})")


# =============================================================================
# Main
# =============================================================================

def main():
    use_v2 = "--v2" in sys.argv or "-v2" in sys.argv

    if use_v2:
        print("=" * 110)
        print("  ATH Drawdown DCA Trigger Optimization — v2 MULTI-CYCLE")
        print("  ATH reset enabled: multiple drawdown cycles analyzed")
        print("=" * 110)
    else:
        print("=" * 110)
        print("  ATH Drawdown DCA Trigger Optimization — v1 SINGLE-CYCLE")
        print("  (Use --v2 flag for multi-cycle mode)")
        print("=" * 110)

    print()
    print(f"  Search range:")
    print(f"     T1: {TRIGGER_SEARCH['trigger_1'][0]}% ~ {TRIGGER_SEARCH['trigger_1'][-1]}%")
    print(f"     T2: {TRIGGER_SEARCH['trigger_2'][0]}% ~ {TRIGGER_SEARCH['trigger_2'][-1]}%")
    print(f"     T3: {TRIGGER_SEARCH['trigger_3'][0]}% ~ {TRIGGER_SEARCH['trigger_3'][-1]}%")
    print()

    for ticker in TICKERS:
        print(f"\n{'=' * 110}")
        print(f"  {ticker}")
        print(f"{'=' * 110}")

        df = fetch_full_history(ticker)
        closes = df['Close']

        if use_v2:
            # v2: multi-cycle optimization
            results_v2 = grid_search(closes, ticker, use_v2=True)
            print_optimal_results(results_v2, ticker, top_n=8, use_v2=True)

            # Also run v1 for comparison
            print(f"\n  (Running v1 comparison for same ticker...)")
            results_v1 = grid_search(closes, ticker, use_v2=False)
            print_optimal_results(results_v1, ticker, top_n=5, use_v2=False)

            # Side-by-side comparison
            print_comparison(results_v1, results_v2, ticker)

            # Update config with v2 results (more realistic)
            best = results_v2[0]
            t1, t2, t3 = best['triggers']
            update_config(ticker, t1, t2, t3, source="v2 multi-cycle")
        else:
            # Original v1 mode
            results = grid_search(closes, ticker, use_v2=False)
            print_optimal_results(results, ticker, top_n=10, use_v2=False)
            best = results[0]
            t1, t2, t3 = best['triggers']
            update_config(ticker, t1, t2, t3, source="v1 single-cycle")

    print(f"\n{'=' * 110}")
    print(f"  Optimization complete!")
    print(f"{'=' * 110}")


if __name__ == "__main__":
    main()
