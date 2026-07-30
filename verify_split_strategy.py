#!/usr/bin/env python3
"""
═══════════════════════════════════════════════════════════════
  🧪 YouTube 전략 검증 — 분할 매수법 최적화 스윕
═══════════════════════════════════════════════════════════════

두 가지 모드:
  1. 기본 모드 (default)    : 3분할 vs 10분할 비교 (원본 유튜브 검증)
  2. 스윕 모드 (--sweep)    : 3,5,10,15,20분할 전수 비교 → 국면별 최적 분할 수 도출

Usage:
  python3 verify_split_strategy.py              # 3분할 vs 10분할
  python3 verify_split_strategy.py --sweep      # 전체 스윕
"""

import sys
import os
import numpy as np
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta

# ══════════════════════════════════════════════
# Configuration
# ══════════════════════════════════════════════
TOTAL_INVESTMENT = 50_000      # 총 투자 금액 ($)

# 시장 국면 분류 임계값 (SPY 200일 이동평균 기준)
BULL_THRESHOLD  = 0.03   # 200MA +3% 이상 → 상승장
BEAR_THRESHOLD  = -0.03  # 200MA -3% 이하 → 하락장

# 최소 분석 기간 (거래일)
MIN_PERIOD_DAYS = 80

# 분석 대상
MARKET_INDEX = "SPY"
TICKERS = ["TQQQ", "SOXL"]

# 스윕할 분할 수 리스트
SWEEP_SPLITS = [3, 5, 10, 15, 20]

REGIME_LABELS = {
    1:  "🔥 상승장 (Bull)",
    -1: "📉 하락장 (Bear)",
    0:  "➡️  횡보장 (Sideways)",
}

REGIME_COLORS = {
    1:  "\033[91m",   # Red (bull - hot)
    -1: "\033[94m",   # Blue (bear - cold)
    0:  "\033[93m",   # Yellow (sideways)
}
RESET_COLOR = "\033[0m"


# ══════════════════════════════════════════════
# Data Fetching
# ══════════════════════════════════════════════

def fetch_data(ticker: str, years: int = 10) -> pd.DataFrame:
    """Download long-term historical OHLCV data."""
    end_date = datetime.now()
    start_date = end_date - timedelta(days=years * 365 + 60)

    print(f"  📥 Downloading {ticker} ({start_date.date()} → {end_date.date()})...", end=" ")
    stock = yf.Ticker(ticker)
    hist = stock.history(start=start_date, end=end_date, interval="1d", auto_adjust=True)

    if hist.empty:
        raise RuntimeError(f"{ticker} returned no data.")

    df = hist[['Close']].copy()
    df.columns = ['Close']
    df = df.dropna()
    print(f"✅ {len(df)} trading days")
    return df


# ══════════════════════════════════════════════
# Market Regime Classification
# ══════════════════════════════════════════════

def classify_regime(df: pd.DataFrame) -> pd.DataFrame:
    """
    Classify each trading day's market regime using SPY's 200-day MA.
    Returns df with 'regime' column:
      1  = Bull  (SPY > MA200 × (1 + BULL_THRESHOLD))
      -1 = Bear  (SPY < MA200 × (1 + BEAR_THRESHOLD))
      0  = Sideways (between threshold)
    """
    result = df.copy()
    result['ma200'] = result['Close'].rolling(window=200).mean()
    result['ma200_ratio'] = result['Close'] / result['ma200'] - 1

    conditions = [
        result['ma200_ratio'] >= BULL_THRESHOLD,
        result['ma200_ratio'] <= BEAR_THRESHOLD,
    ]
    choices = [1, -1]
    result['regime'] = np.select(conditions, choices, default=0)
    result['regime_name'] = result['regime'].map(REGIME_LABELS)
    return result


# ══════════════════════════════════════════════
# N-Split DCA Simulator
# ══════════════════════════════════════════════

def simulate_n_split(price_series: pd.Series, n_splits: int,
                     total_amount: float = TOTAL_INVESTMENT) -> dict | None:
    """
    Simulate N-split DCA over a price window.
    Divides total_amount into n_splits equal portions bought at evenly
    spaced intervals.
    Returns metrics dict or None if period is too short.
    """
    if len(price_series) < n_splits:
        return None

    # Evenly spaced buy indices
    buy_indices = np.linspace(0, len(price_series) - 1, n_splits, dtype=int)

    portion  = total_amount / n_splits
    shares   = 0.0
    invested = 0.0
    buy_prices = []

    for idx in buy_indices:
        price = float(price_series.iloc[idx])
        sh    = portion / price
        shares   += sh
        invested += portion
        buy_prices.append(price)

    final_price  = float(price_series.iloc[-1])
    final_value  = shares * final_price
    total_return = (final_value - invested) / invested * 100

    start_price = float(price_series.iloc[0])
    bh_shares   = total_amount / start_price
    bh_value    = bh_shares * final_price
    bh_return   = (bh_value - total_amount) / total_amount * 100

    avg_price = np.mean(buy_prices)
    wins      = sum(1 for p in buy_prices if final_price > p)
    win_rate  = wins / n_splits * 100

    return {
        'n_splits':     n_splits,
        'total_return': round(total_return, 2),
        'final_value':  round(final_value, 2),
        'bh_return':    round(bh_return, 2),
        'alpha':        round(total_return - bh_return, 2),
        'avg_price':    round(avg_price, 2),
        'start_price':  round(start_price, 2),
        'final_price':  round(final_price, 2),
        'win_rate':     round(win_rate, 1),
        'invested':     round(invested, 2),
        'shares':       round(shares, 4),
    }


# ══════════════════════════════════════════════
# Period-based Analysis
# ══════════════════════════════════════════════

def find_regime_periods(spy_df: pd.DataFrame, regime: int) -> list[tuple[int, int]]:
    """Find all contiguous blocks of a given regime. Returns (start_idx, end_idx) pairs."""
    arr = (spy_df['regime'].values == regime)
    periods = []
    i = 0
    n = len(arr)
    while i < n:
        if arr[i]:
            start = i
            while i < n and arr[i]:
                i += 1
            periods.append((start, i))
        else:
            i += 1
    return periods


def get_aligned_dataframes(spy_df: pd.DataFrame, target_df: pd.DataFrame):
    """Align SPY and target dataframes by common dates, preserving row order."""
    common = spy_df.index.intersection(target_df.index)
    s = spy_df.loc[spy_df.index.isin(common)]
    t = target_df.loc[target_df.index.isin(common)]
    return s, t


def analyze_regime_period(spy_s: pd.DataFrame, target_s: pd.DataFrame,
                          start_idx: int, end_idx: int,
                          split_counts: list[int]) -> dict | None:
    """
    Analyze a single regime period across multiple split counts.
    Returns dict with results for each split count, or None if period too short.
    """
    length = end_idx - start_idx
    if length < MIN_PERIOD_DAYS:
        return None

    prices = target_s['Close'].iloc[start_idx:end_idx]

    splits_results = {}
    best_return = -float('inf')
    best_n = None

    for n in split_counts:
        r = simulate_n_split(prices, n)
        if r is None:
            continue
        splits_results[n] = r
        if r['total_return'] > best_return:
            best_return = r['total_return']
            best_n = n

    if not splits_results:
        return None

    start_date = spy_s.index[start_idx].strftime('%Y-%m-%d')
    end_date   = spy_s.index[end_idx - 1].strftime('%Y-%m-%d')

    spy_ret = round(
        (float(spy_s['Close'].iloc[end_idx - 1]) / float(spy_s['Close'].iloc[start_idx]) - 1) * 100, 2
    )

    return {
        'start':         start_date,
        'end':           end_date,
        'days':          length,
        'spy_return':    spy_ret,
        'best_n':        best_n,
        'best_return':   round(best_return, 2),
        'splits':        splits_results,
    }


# ══════════════════════════════════════════════
# Full Analysis — Sweep Mode
# ══════════════════════════════════════════════

def analyze_ticker_sweep(ticker: str, spy_df: pd.DataFrame, target_df: pd.DataFrame,
                         split_counts: list[int] = SWEEP_SPLITS,
                         verbose: bool = True) -> dict:
    """
    For each regime, test all split counts across all qualifying periods.
    Returns { regime: [period_results, ...] } where each period_result contains
    results for all split counts.
    """
    s, t = get_aligned_dataframes(spy_df, target_df)

    results = {1: [], -1: [], 0: []}

    if verbose:
        print(f"\n{'=' * 110}")
        print(f"  📈 {ticker} — {len(split_counts)}개 분할 수 스윕 분석")
        print(f"     분할 수: {', '.join(f'{n}분할' for n in split_counts)}")
        print(f"{'=' * 110}")

    for regime in [1, -1, 0]:
        periods = find_regime_periods(s, regime)
        label = REGIME_LABELS[regime]
        color = REGIME_COLORS[regime]

        if verbose:
            print(f"\n  {color}{label}{RESET_COLOR} — {len(periods)}개 구간 발견")

        qualifying = 0
        for start_idx, end_idx in periods:
            period_result = analyze_regime_period(s, t, start_idx, end_idx, split_counts)
            if period_result is None:
                continue
            results[regime].append(period_result)
            qualifying += 1

            if verbose:
                best_label = f"{period_result['best_n']}분할 🏆"
                print(f"    {period_result['start']} → {period_result['end']} "
                      f"({period_result['days']:3d}일) "
                      f"SPY {period_result['spy_return']:+.1f}% | "
                      f"최적: {best_label} (수익률 {period_result['best_return']:+.2f}%)")

        if verbose:
            print(f"     → {qualifying}개 구간 분석 완료 (≥{MIN_PERIOD_DAYS}일 필터)")

    return results


# ══════════════════════════════════════════════
# Sweep Reporting
# ══════════════════════════════════════════════

def build_regime_rankings(results: dict, split_counts: list[int]) -> dict:
    """
    For a given regime's period results, compute average return per split count
    and rank them.
    Returns { regime: [(avg_return, n_split), ...] } sorted by avg_return desc.
    """
    rankings = {}

    for regime in [1, -1, 0]:
        items = results.get(regime, [])
        if not items:
            rankings[regime] = []
            continue

        # Collect returns for each split count across all periods
        perf = {n: [] for n in split_counts}
        for period in items:
            for n, r in period['splits'].items():
                if n in perf:
                    perf[n].append(r['total_return'])

        # Compute average return per split count
        avg_perf = {}
        for n, returns in perf.items():
            if returns:
                avg_perf[n] = float(np.mean(returns))

        # Rank: higher average return = better
        ranked = sorted(avg_perf.items(), key=lambda x: -x[1])
        rankings[regime] = ranked

    return rankings


def print_sweep_header(split_counts: list[int]):
    """Print the sweep analysis header."""
    print(f"{'═' * 110}")
    print(f"  🔬 분할 수 최적화 스윕 분석")
    print(f"  🎯 3, 5, 10, 15, 20분할 전수 비교 → 국면별 최적 분할 수 도출")
    print(f"{'═' * 110}")
    print()
    print(f"  📊 분석 설정:")
    print(f"     • 시장 국면 분류: SPY 200일 이동평균선 기준")
    print(f"     • 상승장 기준: SPY > MA200 × ({1+BULL_THRESHOLD:.0%})")
    print(f"     • 하락장 기준: SPY < MA200 × ({1+BEAR_THRESHOLD:.0%})")
    print(f"     • 투자 금액: ${TOTAL_INVESTMENT:,} (N분할 균등 매수)")
    print(f"     • 최소 분석 기간: {MIN_PERIOD_DAYS} 거래일")
    print(f"     • 분석 대상: {', '.join(TICKERS)}")
    print(f"     • 비교 분할 수: {', '.join(f'{n}분할' for n in split_counts)}")
    print()


def print_sweep_report(ticker: str, results: dict, split_counts: list[int]):
    """Print detailed per-regime sweep results for one ticker."""
    rankings = build_regime_rankings(results, split_counts)

    print(f"\n{'═' * 110}")
    print(f"  📊 {ticker} — 국면별 분할 수 성능 랭킹")
    print(f"{'═' * 110}")

    for regime in [1, -1, 0]:
        items = results.get(regime, [])
        ranked = rankings.get(regime, [])
        label = REGIME_LABELS[regime]
        color = REGIME_COLORS[regime]

        if not items:
            print(f"\n  {color}{label}{RESET_COLOR}: 데이터 부족 (≥{MIN_PERIOD_DAYS}일 구간 없음)")
            continue

        n_periods = len(items)
        print(f"\n  {color}{label}{RESET_COLOR} (총 {n_periods}개 구간 분석)")
        print(f"  {'─' * 100}")

        # ── Ranking table ───────────────────────────────────────────
        print(f"  {'순위':<6} {'분할 수':>8} {'평균 수익률':>14} {'vs 1위':>12} {'최고승':>8} {'평균승률':>10}")
        print(f"  {'─'*6} {'─'*8} {'─'*14} {'─'*12} {'─'*8} {'─'*10}")

        if not ranked:
            print(f"  {'데이터 부족':^60}")
            continue

        best_avg = ranked[0][1]  # Best average return

        for rank, (n, avg_ret) in enumerate(ranked, 1):
            # Count how many times this split count was the best
            best_count = sum(1 for p in items if p.get('best_n') == n)

            # Average win rate across periods
            win_rates = []
            for p in items:
                sr = p['splits'].get(n)
                if sr:
                    win_rates.append(sr['win_rate'])
            avg_win = float(np.mean(win_rates)) if win_rates else 0

            vs_best = avg_ret - best_avg
            medal = "🥇" if rank == 1 else ("🥈" if rank == 2 else ("🥉" if rank == 3 else ""))
            print(f"  {rank:<3} {medal:<3} {n:>4}분할  {avg_ret:>+11.2f}%  "
                  f"{vs_best:>+9.2f}%p  {best_count:>3d}회  {avg_win:>7.1f}%")

        # ── Per-period detailed view ────────────────────────────────
        print(f"\n  📋 구간별 상세 성능:")
        print(f"  {'기간':<26} {'':>4} ", end="")
        for n in split_counts:
            print(f"{n:>8}분할", end=" ")
        print(f" {'🏆최적':>8}")
        print(f"  {'─'*26} {'':>4} ", end="")
        for _ in split_counts:
            print(f"{'─'*10}", end=" ")
        print(f" {'─'*10}")

        for p in items:
            period_label = f"{p['start']} → {p['end']}"
            print(f"  {period_label:<26} ({p['days']:3d}일)", end=" ")
            for n in split_counts:
                sr = p['splits'].get(n)
                if sr:
                    ret = sr['total_return']
                    # Highlight best
                    if n == p['best_n']:
                        print(f" {ret:>+7.2f}%🏆", end=" ")
                    else:
                        print(f" {ret:>+7.2f}% ", end=" ")
                else:
                    print(f" {'N/A':>8} ", end=" ")
            print(f" {p['best_n']:>4}분할")

        # ── Summary ────────────────────────────────────────────────
        winner = ranked[0]
        runner_up = ranked[1] if len(ranked) > 1 else None
        gap = winner[1] - (runner_up[1] if runner_up else 0)
        gap_str = f" {gap:+.2f}%p 차이"

        # Count how many periods each split count wins
        win_counts = {}
        for p in items:
            bn = p['best_n']
            win_counts[bn] = win_counts.get(bn, 0) + 1

        wins_str = ", ".join(f"{n}분할 {c}회" for n, c in sorted(win_counts.items(), key=lambda x: -x[1]))

        print(f"\n  🏆 **최적 분할 수: {winner[0]}분할** (평균 {winner[1]:+.2f}%, {gap_str})")
        print(f"     • 구간별 최적 등장 횟수: {wins_str}")
        print(f"     • 상위 3개: {' > '.join(f'{n}분할({ret:+.2f}%)' for n, ret in ranked[:3])}")


def print_sweep_verdict(all_results: dict, split_counts: list[int]):
    """Print overall optimal strategy recommendation across all tickers."""
    print(f"\n{'═' * 110}")
    print(f"  🏆 최종 최적 분할 전략 — 국면별 추천")
    print(f"{'═' * 110}")

    overall = {}  # regime -> { n_split -> [returns] }

    for ticker, results in all_results.items():
        rankings = build_regime_rankings(results, split_counts)

        for regime in [1, -1, 0]:
            if regime not in overall:
                overall[regime] = {n: [] for n in split_counts}

            items = results.get(regime, [])
            for p in items:
                for n, sr in p['splits'].items():
                    overall[regime][n].append(sr['total_return'])

    for regime in [1, -1, 0]:
        label = REGIME_LABELS[regime]
        color = REGIME_COLORS[regime]

        perf = overall.get(regime, {})
        total_periods = max((len(v) for v in perf.values()), default=0)

        if total_periods == 0:
            print(f"\n  {color}{label}{RESET_COLOR}: 데이터 부족")
            continue

        # Average across all tickers
        avg_ret = {}
        for n, returns in perf.items():
            if returns:
                avg_ret[n] = float(np.mean(returns))

        ranked = sorted(avg_ret.items(), key=lambda x: -x[1])

        print(f"\n  {color}{label}{RESET_COLOR} (총 {total_periods}개 구간 × 2개 ETF)")
        print(f"  {'─' * 80}")
        print(f"  {'순위':<6} {'분할 수':>8} {'평균 수익률':>14} {'vs 1위':>12}")
        print(f"  {'─'*6} {'─'*8} {'─'*14} {'─'*12}")

        best_avg = ranked[0][1]
        for rank, (n, ret) in enumerate(ranked, 1):
            vs_best = ret - best_avg
            medal = "🥇" if rank == 1 else ("🥈" if rank == 2 else ("🥉" if rank == 3 else ""))
            print(f"  {rank:<3} {medal:<3} {n:>4}분할  {ret:>+11.2f}%  {vs_best:>+9.2f}%p")

        winner = ranked[0]
        print(f"\n  🏆 **추천: {winner[0]}분할 매수법** (평균 {winner[1]:+.2f}%)")

        # Rule of thumb confirmation
        if regime == 1:  # Bull
            if winner[0] <= 5:
                print(f"     ✅ 유튜브 주장(상승장=적은분할)과 일치 — 적은 분할 수가 유리")
            else:
                print(f"     ⚠️ 유튜브 주장(상승장=적은분할)과 다름 — 많은 분할이 더 유리")
        elif regime == -1:  # Bear
            if winner[0] >= 10:
                print(f"     ✅ 유튜브 주장(하락장=많은분할)과 일치 — 많은 분할 수가 유리")
            else:
                print(f"     ⚠️ 유튜브 주장(하락장=많은분할)과 다름 — 적은 분할이 더 유리")
        elif regime == 0:  # Sideways
            if winner[0] >= 10:
                print(f"     ✅ 유튜브 주장(횡보장=많은분할)과 일치 — 많은 분할 수가 유리")
            else:
                print(f"     ⚠️ 유튜브 주장(횡보장=많은분할)과 다름 — 적은 분할이 더 유리")

    print(f"\n{'═' * 110}")
    print(f"  ✅ 스윕 분석 완료!")
    print(f"{'═' * 110}")


# ══════════════════════════════════════════════
# Original Analysis — 3 vs 10 (kept for backward compat)
# ══════════════════════════════════════════════

def analyze_ticker_3v10(ticker: str, spy_df: pd.DataFrame, target_df: pd.DataFrame) -> dict:
    """
    Original analysis: 3-split vs 10-split comparison.
    Returns { regime: [period_comparisons] }
    """
    s, t = get_aligned_dataframes(spy_df, target_df)

    results = {1: [], -1: [], 0: []}

    print(f"\n{'=' * 110}")
    print(f"  📈 {ticker} 분석 (3분할 vs 10분할)")
    print(f"{'=' * 110}")

    for regime in [1, -1, 0]:
        periods = find_regime_periods(s, regime)
        label = REGIME_LABELS[regime]
        color = REGIME_COLORS[regime]

        print(f"\n  {color}{label}{RESET_COLOR} — {len(periods)}개 구간 발견")
        print(f"     (필터: ≥{MIN_PERIOD_DAYS}일, SPY regime counts: "
              f"Bull={int((s['regime']==1).sum())} "
              f"Bear={int((s['regime']==-1).sum())} "
              f"Sideways={int((s['regime']==0).sum())})")

        for start_idx, end_idx in periods:
            length = end_idx - start_idx
            if length < MIN_PERIOD_DAYS:
                continue

            start_date = s.index[start_idx].strftime('%Y-%m-%d')
            end_date = s.index[end_idx - 1].strftime('%Y-%m-%d')
            prices = t['Close'].iloc[start_idx:end_idx]

            r3  = simulate_n_split(prices, 3)
            r10 = simulate_n_split(prices, 10)
            if r3 is None or r10 is None:
                continue

            gap = r3['total_return'] - r10['total_return']
            if gap > 0.5:
                winner = "3분할 ✅"
            elif gap < -0.5:
                winner = "10분할 ✅"
            else:
                winner = "비슷함"

            spy_ret = round(
                (float(s['Close'].iloc[end_idx - 1]) / float(s['Close'].iloc[start_idx]) - 1) * 100, 2
            )

            results[regime].append({
                'start':      start_date,
                'end':        end_date,
                'days':       length,
                'ticker':     ticker,
                'r3_return':  r3['total_return'],
                'r10_return': r10['total_return'],
                'r3_avg':     r3['avg_price'],
                'r10_avg':    r10['avg_price'],
                'r3_win':     r3['win_rate'],
                'r10_win':    r10['win_rate'],
                'bh_return':  r3['bh_return'],
                'winner':     winner,
                'gap':        round(abs(gap), 2),
                'spy_return': spy_ret,
            })

            print(f"    {start_date} → {end_date} ({length:3d}일) "
                  f"SPY {spy_ret:+.1f}% | "
                  f"3분할 {r3['total_return']:+.2f}% | "
                  f"10분할 {r10['total_return']:+.2f}% | "
                  f"{winner}")

    return results


def print_3v10_summary(results: dict, ticker: str):
    """Print aggregated summary per market regime (original mode)."""
    print(f"\n{'═' * 110}")
    print(f"  📊 {ticker} — 시장 국면별 분할 매수 전략 종합")
    print(f"{'═' * 110}")

    for regime in [1, -1, 0]:
        items = results.get(regime, [])
        if not items:
            print(f"\n  {REGIME_LABELS[regime]}: 데이터 부족")
            continue

        avg_3   = float(np.mean([r['r3_return'] for r in items]))
        avg_10  = float(np.mean([r['r10_return'] for r in items]))
        w3      = sum(1 for r in items if '3분할' in r['winner'])
        w10     = sum(1 for r in items if '10분할' in r['winner'])
        wtie    = sum(1 for r in items if r['winner'] == '비슷함')
        total   = len(items)
        label   = REGIME_LABELS[regime]
        color   = REGIME_COLORS[regime]

        spread = avg_3 - avg_10
        better = "3분할" if spread > 0.5 else ("10분할" if spread < -0.5 else "비슷")

        print(f"\n  {color}{label}{RESET_COLOR} (총 {total}개 구간)")
        print(f"  {'─' * 100}")
        print(f"    {'구분':<15} {'3분할':>14} {'10분할':>14} {'차이':>10} {'비고':<20}")
        print(f"    {'─'*15} {'─'*14} {'─'*14} {'─'*10} {'─'*20}")

        for r in items:
            diff = r['r3_return'] - r['r10_return']
            print(f"    {r['start']:<15} {r['r3_return']:>+12.2f}% {r['r10_return']:>+12.2f}% "
                  f"{diff:>+8.2f}%p {r['winner']:<20}")

        print(f"    {'─'*15} {'─'*14} {'─'*14} {'─'*10} {'─'*20}")
        print(f"    {'평균':<15} {avg_3:>+12.2f}% {avg_10:>+12.2f}% "
              f"{spread:>+8.2f}%p {better:<20}")
        print(f"\n    📊 승리 횟수: 3분할 {w3}승 / 10분할 {w10}승 / 비슷 {wtie}회 (총 {total}개 구간)")
        if total > 0:
            print(f"       → 3분할 승률: {w3/total*100:.0f}%  |  10분할 승률: {w10/total*100:.0f}%")
        if abs(spread) >= 0.5:
            print(f"       → 🏆 {better} 매수법이 평균 {abs(spread):.2f}%p 더 높은 수익률 기록")
        else:
            print(f"       → 🤝 두 전략 간 유의미한 차이 없음 ({abs(spread):.2f}%p)")


def print_3v10_verdict(results_per_ticker: dict):
    """Final verdict on the YouTube claim (original mode)."""
    print(f"\n{'═' * 110}")
    print(f"  🏁 최종 검증 결과")
    print(f"{'═' * 110}")

    for ticker, results in results_per_ticker.items():
        print(f"\n  📌 {ticker}:")

        for regime in [1, -1, 0]:
            items = results.get(regime, [])
            if not items:
                print(f"     {REGIME_LABELS[regime]}: 데이터 부족")
                continue

            avg_3  = float(np.mean([r['r3_return'] for r in items]))
            avg_10 = float(np.mean([r['r10_return'] for r in items]))
            w3     = sum(1 for r in items if '3분할' in r['winner'])
            w10    = sum(1 for r in items if '10분할' in r['winner'])
            total  = len(items)

            if avg_3 > avg_10:
                judgement = f"✅ **3분할 우세** (평균 {avg_3:+.2f}% vs {avg_10:+.2f}%)"
            elif avg_10 > avg_3:
                judgement = f"✅ **10분할 우세** (평균 {avg_10:+.2f}% vs {avg_3:+.2f}%)"
            else:
                judgement = "🤝 동률"
            print(f"     {REGIME_LABELS[regime]}: {judgement}")
            print(f"       └ 승률: 3분할 {w3}/{total} ({w3/total*100:.0f}%)  "
                  f"10분할 {w10}/{total} ({w10/total*100:.0f}%)")

    print(f"\n{'═' * 110}")
    print(f"  🧪 유튜브 주장 검증: 대세 상승기 3분할 / 대세 하락기·횡보장 10분할")
    print(f"{'═' * 110}")

    for ticker, results in results_per_ticker.items():
        print(f"\n  [ {ticker} ]")

        bull_ok = bear_ok = side_ok = None
        bull_items = results.get(1, [])
        bear_items = results.get(-1, [])
        side_items = results.get(0, [])

        if bull_items:
            a3 = float(np.mean([r['r3_return'] for r in bull_items]))
            a10 = float(np.mean([r['r10_return'] for r in bull_items]))
            bull_ok = bool(a3 >= a10)
            print(f"    🔥 상승장: 유튜브 주장=3분할우세 → {'✅ 일치' if bull_ok else '❌ 불일치'} "
                  f"(3분할 {a3:+.2f}% vs 10분할 {a10:+.2f}%)")
        else:
            print(f"    🔥 상승장: 데이터 부족")

        if bear_items:
            a3 = float(np.mean([r['r3_return'] for r in bear_items]))
            a10 = float(np.mean([r['r10_return'] for r in bear_items]))
            bear_ok = bool(a10 >= a3)
            print(f"    📉 하락장: 유튜브 주장=10분할우세 → {'✅ 일치' if bear_ok else '❌ 불일치'} "
                  f"(10분할 {a10:+.2f}% vs 3분할 {a3:+.2f}%)")
        else:
            print(f"    📉 하락장: 데이터 부족")

        if side_items:
            a3 = float(np.mean([r['r3_return'] for r in side_items]))
            a10 = float(np.mean([r['r10_return'] for r in side_items]))
            side_ok = bool(a10 >= a3)
            print(f"    ➡️  횡보장: 유튜브 주장=10분할우세 → {'✅ 일치' if side_ok else '❌ 불일치'} "
                  f"(10분할 {a10:+.2f}% vs 3분할 {a3:+.2f}%)")
        else:
            print(f"    ➡️  횡보장: 데이터 부족")

        verdicts = [bull_ok, bear_ok, side_ok]
        passed = sum(1 for v in verdicts if v == True)
        failed = sum(1 for v in verdicts if v == False)
        nodata = sum(1 for v in verdicts if v is None)

        if passed > 0 and failed == 0:
            print(f"\n    🎉 **유튜브 전략 검증 통과!** ({passed}/{3-nodata}개 국면 일치)")
        elif failed > 0:
            print(f"\n    ⚠️ **일부 국면에서 유튜브 주장과 다른 결과** "
                  f"(일치 {passed}개 / 불일치 {failed}개)")
        else:
            print(f"\n    ⏳ 데이터 부족으로 판단 불가")

    print(f"\n{'═' * 110}")
    print(f"  ✅ 검증 완료!")
    print(f"{'═' * 110}")


# ══════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════

def main():
    sweep_mode = "--sweep" in sys.argv
    split_counts = SWEEP_SPLITS if sweep_mode else [3, 10]

    # ── Header ──────────────────────────────────────────────────────
    if sweep_mode:
        print_sweep_header(split_counts)
    else:
        print(f"{'═' * 110}")
        print(f"  🧪 유튜브 전략 과학적 검증")
        print(f"  🔬 3분할 vs 10분할 매수법 — 시장 국면별 성능 비교")
        print(f"{'═' * 110}")
        print()
        print(f"  📌 가설:")
        print(f"     • 🔥 대세 상승기 (Bull)       → 3분할 매수법 우세")
        print(f"     • 📉 대세 하락기 (Bear)        → 10분할 매수법 우세")
        print(f"     • ➡️  횡보장 (Sideways)        → 10분할 매수법 우세")
        print()
        print(f"  📊 분석 설정:")
        print(f"     • 시장 국면 분류: SPY 200일 이동평균선 기준")
        print(f"     • 상승장 기준: SPY > MA200 × ({1+BULL_THRESHOLD:.0%})")
        print(f"     • 하락장 기준: SPY < MA200 × ({1+BEAR_THRESHOLD:.0%})")
        print(f"     • 투자 금액: ${TOTAL_INVESTMENT:,} (N분할 균등 매수)")
        print(f"     • 최소 분석 기간: {MIN_PERIOD_DAYS} 거래일")
        print(f"     • 분석 대상: {', '.join(TICKERS)} (레버리지 ETF)")
        print()

    # ── Step 1: Fetch SPY for regime classification ─────────────────
    print(f"📡 [1/3] 시장 데이터 다운로드...")
    spy_df = fetch_data(MARKET_INDEX)
    spy_df = classify_regime(spy_df)

    # Print regime distribution
    drop_start = 200
    spy_clean = spy_df.iloc[drop_start:]
    regime_counts = spy_clean['regime'].value_counts().sort_index()
    total_days = len(spy_clean)
    print(f"\n  📊 시장 국면 분포 (SPY, {total_days} 거래일 ≈ {total_days//252:.1f}년):")
    for regime, cnt in regime_counts.items():
        pct = cnt / total_days * 100
        print(f"     {REGIME_LABELS[regime]}: {cnt:>5}일 ({pct:>5.1f}%)")

    # ── Step 2 & 3: Analyze each ticker ─────────────────────────────
    all_results = {}

    for ticker in TICKERS:
        print(f"\n📡 [2/3] {ticker} 데이터 다운로드...")
        target_df = fetch_data(ticker)

        print(f"🔬 [3/3] {ticker} 분석 중...")
        if sweep_mode:
            results = analyze_ticker_sweep(ticker, spy_df, target_df, split_counts)
            all_results[ticker] = results
            print_sweep_report(ticker, results, split_counts)
        else:
            results = analyze_ticker_3v10(ticker, spy_df, target_df)
            all_results[ticker] = results
            print_3v10_summary(results, ticker)

    # ── Step 4: Final output ────────────────────────────────────────
    if sweep_mode:
        print_sweep_verdict(all_results, split_counts)
    else:
        print_3v10_verdict(all_results)


if __name__ == "__main__":
    main()
