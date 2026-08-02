#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
═══════════════════════════════════════════════════════════════
  TQQQ 단기/장기 이동평균 교차(Golden/Death Cross) 그리드 탐색 백테스트
═══════════════════════════════════════════════════════════════

유튜브 전략: 단기 MA가 장기 MA를 상향 돌파(Golden Cross)하면 전액 매수,
하향 돌파(Death Cross)하면 전액 매도 (현금 보유).

이 스크립트는 $50,000 투자금, 최근 10년(2016-08-02 ~ 2026-07-31) 구간에서
단기 MA(1~40일) × 장기 MA(20~250일) 전체 조합을 탐색하여

  1. 총수익률 최고 조합
  2. MDD(최대 낙폭) 최고 조합
  3. 수익률 대비 MDD가 균형 잡힌 조합
  4. 각 조합의 연간 매수/매도 빈도

를 찾아내는 툴입니다.

가정:
  - 신호 발생일 종가(Close)에 매수/매도 체결
  - 거래 수수료/슬리피지 없음 (0)
  - auto_adjust=True (분할/배당 조정) 종가 사용
  - 현금 보유 시 이자 없음
  - $50,000 초기 자본, 전액 진입/전액 청산 (이진 포지션)

Usage:
  python3 tqqq_ma_crossover_backtest.py            # 전체 그리드 탐색
  python3 tqqq_ma_crossover_backtest.py 6 107      # 특정 조합만 상세 리포트
  python3 tqqq_ma_crossover_backtest.py --hybrid   # 순수 MA 교차 vs 하이브리드 비교
  python3 tqqq_ma_crossover_backtest.py --hybrid 7 104   # 해당 조합 하이브리드 파라미터 스윕

── 하이브리드 모드 ─────────────────────────────────────────────
MA 교차(골든크로스 진입/데드크로스 전량 청산) + 급락 분할 매수:
  - 진입 시 전액이 아닌 base_pct만 투자, 나머지는 예비금 보유
  - 보유 중 진입 후 고점 대비 dip% 하락 시 tranches회 × tranche_pct 추가 매수
  - 데드크로스 시 전량 매도 → 다음 골든크로스에서 base_pct 재진입
"""

import sys
import numpy as np
import pandas as pd
import yfinance as yf
from datetime import date, timedelta

# ══════════════════════════════════════════════
# Configuration
# ══════════════════════════════════════════════
TICKER       = "TQQQ"
INITIAL_CASH = 50_000.0
DATA_START   = "2013-12-01"        # 장기 MA 워밍업용 추가 데이터
TEST_START   = date(2016, 8, 2)    # 최근 10년 테스트 시작
TEST_END     = date(2026, 8, 2)    # 테스트 종료(포함)
SHORT_RANGE  = range(1, 41)        # 단기 MA 후보: 1~40일
LONG_RANGE   = range(20, 251)      # 장기 MA 후보: 20~250일
TOP_N        = 10                  # 상위 리포트 개수

# 유튜버 기준 조합 (비교용)
YT_SHORT, YT_LONG = 6, 107


# ══════════════════════════════════════════════
# Data
# ══════════════════════════════════════════════
def load_data() -> tuple[pd.Series, pd.DatetimeIndex]:
    print(f"📥 {TICKER} 데이터 다운로드 ({DATA_START} → {TEST_END.isoformat()})...")
    df = yf.download(TICKER, start=DATA_START,
                     end=(TEST_END + timedelta(days=1)).isoformat(),
                     auto_adjust=True, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    close = df["Close"].dropna()
    if close.index.tz is not None:
        close.index = close.index.tz_localize(None)
    close = close[~close.index.duplicated(keep="last")].sort_index()
    print(f"   → 전체 {len(close)} 거래일")
    return close, close.index


def test_window(close: pd.Series) -> pd.DatetimeIndex:
    mask = (close.index >= pd.Timestamp(TEST_START)) & (close.index <= pd.Timestamp(TEST_END))
    return close.index[mask]


# ══════════════════════════════════════════════
# Backtest Engine
# ══════════════════════════════════════════════
def backtest(sma_s: np.ndarray, sma_l: np.ndarray,
             close_vals: np.ndarray, initial: float = INITIAL_CASH) -> dict:
    """
    MA 교차 시뮬레이션 (벡터화).
      - 신호: 단기 MA > 장기 MA → 보유, 반대 → 현금
      - 체결: 신호 발생일 종가
      - 포지션은 교차 시점에만 변경 (첫 날 미보유 시작)
    """
    n = len(close_vals)
    valid = ~(np.isnan(sma_s) | np.isnan(sma_l))
    above = np.zeros(n, dtype=bool)
    above[valid] = sma_s[valid] > sma_l[valid]

    prev_above = np.zeros(n, dtype=bool)
    prev_above[1:] = above[:-1]
    prev_valid = np.zeros(n, dtype=bool)
    prev_valid[1:] = valid[:-1]

    cross_up = (~prev_above) & above & valid & prev_valid   # 상향 돌파 → 매수
    cross_dn = prev_above & (~above) & valid & prev_valid   # 하향 돌파 → 매도

    # 포지션 상태 (교차 시점에만 변경)
    hold = np.zeros(n, dtype=bool)
    h = False
    for i in range(n):
        if cross_up[i]:
            h = True
        elif cross_dn[i]:
            h = False
        hold[i] = h

    # 일간 포트폴리오 수익률: t일 보유분이 t일 자산 수익률을 받음
    asset_ret = np.zeros(n)
    if n > 1:
        asset_ret[1:] = close_vals[1:] / close_vals[:-1] - 1.0
    port_ret = np.zeros(n)
    if n > 1:
        port_ret[1:] = hold[:-1] * asset_ret[1:]

    value = initial * np.cumprod(1.0 + port_ret)

    total_ret = value[-1] / initial - 1.0
    peak = np.maximum.accumulate(value)
    dd = (value - peak) / peak
    mdd = float(dd.min() * 100)

    # Sharpe: 현금 보유일(수익률 0) 포함 전체 일별 수익률 기준 (표준 연환산)
    if len(port_ret) > 1 and port_ret.std(ddof=1) > 0:
        sharpe = float(port_ret.mean() / port_ret.std(ddof=1) * np.sqrt(252))
    else:
        sharpe = 0.0

    buys = int(cross_up.sum())
    sells = int(cross_dn.sum())

    return {
        "total_return": total_ret * 100,
        "final_value": float(value[-1]),
        "mdd": mdd,
        "sharpe": sharpe,
        "buys": buys,
        "sells": sells,
        "trades": buys + sells,
        "time_in_market": float(hold.sum()) / n * 100,
        "days_in_market": int(hold.sum()),
    }


def run_pair(short: int, long_: int, close: pd.Series, window: pd.DatetimeIndex,
             initial: float = INITIAL_CASH) -> dict:
    """특정 (단기, 장기) 조합의 백테스트 결과."""
    sma_s = close.rolling(short).mean()
    sma_l = close.rolling(long_).mean()
    s = sma_s.reindex(window).to_numpy(dtype=float)
    l = sma_l.reindex(window).to_numpy(dtype=float)
    c = close.reindex(window).to_numpy(dtype=float)
    r = backtest(s, l, c, initial)
    r["short_ma"] = short
    r["long_ma"] = long_
    return r


# ══════════════════════════════════════════════
# Hybrid Engine — MA 교차 + 급락 분할 매수
# ══════════════════════════════════════════════
def hybrid(close: pd.Series, win: pd.DatetimeIndex,
           short: int, long_: int,
           base_pct: float = 0.70, dip: float = 0.10,
           tranches: int = 3, tranche_pct: float = 0.10,
           initial: float = INITIAL_CASH) -> dict:
    """MA 교차(진입/청산) + 급락 분할 매수 하이브리드 시뮬레이션.

    - 골든크로스 진입: 자산의 base_pct만 투자, 나머지 예비금 보유
    - 보유 중 진입 후 고점 대비 dip% 하락 시 tranches회 × tranche_pct 추가 매수
    - 데드크로스 청산: 전량 매도 → 다음 골든크로스에서 base_pct 재진입
    - 신호일 종가 체결, 수수료 0
    """
    sma_s = close.rolling(short).mean().reindex(win).to_numpy(dtype=float)
    sma_l = close.rolling(long_).mean().reindex(win).to_numpy(dtype=float)
    c = close.reindex(win).to_numpy(dtype=float)
    n = len(c)

    valid = ~(np.isnan(sma_s) | np.isnan(sma_l))
    above = np.zeros(n, dtype=bool)
    above[valid] = sma_s[valid] > sma_l[valid]
    prev_above = np.zeros(n, dtype=bool)
    prev_above[1:] = above[:-1]
    prev_valid = np.zeros(n, dtype=bool)
    prev_valid[1:] = valid[:-1]
    cross_up = (~prev_above) & above & valid & prev_valid   # 진입
    cross_dn = prev_above & (~above) & valid & prev_valid   # 청산

    cash = float(initial)
    shares = 0.0
    in_mkt = False
    peak = 0.0
    used = 0
    entries = 0
    exits = 0
    dip_buys = 0
    in_mkt_days = 0
    values = []

    for i in range(n):
        # 골든크로스 진입: 자산의 base_pct만 투자, 나머지 예비금
        if cross_up[i] and not in_mkt:
            eq = cash + shares * c[i]
            amt = eq * base_pct
            shares += amt / c[i]
            cash -= amt
            in_mkt = True
            peak = c[i]
            used = 0
            entries += 1

        # 데드크로스 청산: 전량 매도
        if cross_dn[i] and in_mkt:
            cash += shares * c[i]
            shares = 0.0
            in_mkt = False
            peak = 0.0
            used = 0
            exits += 1

        # 보유 중 급락 분할 매수 (진입 후 고점 대비 dip% 하락)
        if in_mkt:
            in_mkt_days += 1
            if c[i] > peak:
                peak = c[i]
            if c[i] <= peak * (1.0 - dip) and cash > 1.0 and used < tranches:
                amt = min(initial * tranche_pct, cash)
                if amt > 1.0:
                    shares += amt / c[i]
                    cash -= amt
                    used += 1
                    dip_buys += 1

        values.append(cash + shares * c[i])

    v = np.array(values, dtype=float)
    total_ret = (v[-1] / initial - 1.0) * 100
    peak_v = np.maximum.accumulate(v)
    mdd = ((v - peak_v) / peak_v).min() * 100
    pr = v[1:] / v[:-1] - 1.0
    sharpe = float(pr.mean() / pr.std(ddof=1) * np.sqrt(252)) if pr.std(ddof=1) > 0 else 0.0
    years = (win[-1] - win[0]).days / 365.25

    return {
        "short_ma": short, "long_ma": long_,
        "base_pct": base_pct, "dip": dip,
        "tranches": tranches, "tranche_pct": tranche_pct,
        "total_return": total_ret,
        "final_value": float(v[-1]),
        "mdd": mdd,
        "sharpe": sharpe,
        "entries": entries, "exits": exits, "dip_buys": dip_buys,
        "trades": entries + exits + dip_buys,
        "annual_trades": (entries + exits + dip_buys) / years,
        "time_in_market": in_mkt_days / n * 100,
    }


def _hybrid_label(r: dict) -> str:
    if r.get("base_pct") is None:
        return f"[순수] {int(r['short_ma'])}/{int(r['long_ma'])} 전액"
    return (f"[하이브리드] {int(r['short_ma'])}/{int(r['long_ma'])}"
            f" base{int(r['base_pct']*100)}% dip{int(r['dip']*100)}% x{int(r['tranches'])}")


def print_hybrid_table(rows: list[dict], years: float):
    print(f"  {'전략':<40} {'총수익률':>9} {'MDD':>8} {'Sharpe':>7} {'최종가치':>11} {'진입/청산':>9} {'분할':>5} {'연간':>6} {'노출':>6}")
    print("  " + "─" * 106)
    for r in rows:
        print(f"  {_hybrid_label(r):<40} {r['total_return']:>+8.1f}% {r['mdd']:>7.1f}% "
              f"{r['sharpe']:>7.2f} ${r['final_value']:>9,.0f} "
              f"{int(r.get('entries', 0)):>4}/{int(r.get('exits', 0)):<4}"
              f" {int(r.get('dip_buys', 0)):>5} {r['annual_trades']:>5.1f}회"
              f" {r.get('time_in_market', 0.0):>5.1f}%")


def _pure_row(r: dict, years: float) -> dict:
    row = dict(r)
    row.update({"base_pct": None, "dip": None, "tranches": None,
                "entries": r["buys"], "exits": r["sells"], "dip_buys": 0,
                "annual_trades": r["trades"] / years})
    return row


def _hybrid_compare(close: pd.Series, window: pd.DatetimeIndex):
    """기본 비교: 대표 조합의 순수 MA 교차 vs 하이브리드(기본 설정)."""
    years = (window[-1] - window[0]).days / 365.25
    rows = []
    for s, l in [(7, 104), (6, 107), (1, 20), (13, 26)]:
        rows.append(_pure_row(run_pair(s, l, close, window), years))
        rows.append(hybrid(close, window, s, l, 0.70, 0.10, 3, 0.10))
    print("\n  📋 순수 MA 교차 vs 하이브리드 (기본: base70% / dip10% / x3)")
    print_hybrid_table(rows, years)


def _hybrid_sweep(close: pd.Series, window: pd.DatetimeIndex, short: int, long_: int):
    """특정 조합의 하이브리드 파라미터 스윕 (base × dip × tranches)."""
    years = (window[-1] - window[0]).days / 365.25
    print(f"\n  🔎 하이브리드 파라미터 스윕 — 단기 {short}일 / 장기 {long_}일")
    print("     base_pct: 60/70/80% × dip: 5/8/10/15/20% × tranches: 1/2/3")

    rows = [_pure_row(run_pair(short, long_, close, window), years)]
    for bp in (0.60, 0.70, 0.80):
        for dip in (0.05, 0.08, 0.10, 0.15, 0.20):
            for tr in (1, 2, 3):
                rows.append(hybrid(close, window, short, long_, bp, dip, tr, 0.10))

    df = pd.DataFrame(rows)
    df.to_csv("tqqq_hybrid_sweep_results.csv", index=False)
    print("   → 스윕 결과 저장: tqqq_hybrid_sweep_results.csv")

    print("\n  📋 전체 비교 (순수 + 하이브리드 45종)")
    print_hybrid_table(rows, years)

    hyb = df[df["base_pct"].notna()].copy()
    if len(hyb):
        hyb["calmar"] = hyb["total_return"] / hyb["mdd"].abs()
        prof = hyb[hyb["total_return"] > 0]
        print("\n  🏆 수익률 TOP 5 (하이브리드)")
        print_hybrid_table(hyb.nlargest(5, "total_return").to_dict("records"), years)
        print("\n  🛡️ MDD 최소 TOP 5 (하이브리드, 수익률>0)")
        print_hybrid_table(prof.nlargest(5, "mdd").to_dict("records"), years)
        print("\n  ⚖️ Calmar TOP 5 (하이브리드)")
        print_hybrid_table(hyb.nlargest(5, "calmar").to_dict("records"), years)


def run_hybrid_mode(close: pd.Series, window: pd.DatetimeIndex,
                    short: int | None = None, long_: int | None = None):
    """--hybrid CLI 진입점: 비교 모드 또는 특정 조합 스윕 모드."""
    years = (window[-1] - window[0]).days / 365.25
    print("\n" + "═" * 108)
    print("  📊 TQQQ 하이브리드 (MA 교차 + 급락 분할 매수)  |  10년, $50,000")
    print("═" * 108)
    print(f"  구간: {window[0].date()} ~ {window[-1].date()} ({years:.1f}년)")
    print("  설계: 골든크로스 시 base% 투자 + 보유 중 고점 대비 dip% 하락 시 분할 추가 매수")
    print("        + 데드크로스 시 전량 청산 → 다음 골든크로스에서 base% 재진입")
    print("─" * 108)

    c_win = close.reindex(window).to_numpy(float)
    bh_ret = (c_win[-1] / c_win[0] - 1) * 100
    bh_v = INITIAL_CASH * c_win / c_win[0]
    bh_mdd = ((bh_v - np.maximum.accumulate(bh_v)) / np.maximum.accumulate(bh_v)).min() * 100
    print(f"\n  📌 참고 — Buy & Hold: {bh_ret:+.1f}% | MDD {bh_mdd:.1f}%")

    if short is not None and long_ is not None:
        _hybrid_sweep(close, window, short, long_)
    else:
        _hybrid_compare(close, window)
    print("\n" + "═" * 108)
    print("  ✅ Hybrid Backtest Complete")
    print("═" * 108)


# ══════════════════════════════════════════════
# Grid Search
# ══════════════════════════════════════════════
def grid_search(close: pd.Series, window: pd.DatetimeIndex) -> pd.DataFrame:
    print(f"🔎 그리드 탐색: 단기 MA {SHORT_RANGE.start}~{SHORT_RANGE.stop-1}일 × "
          f"장기 MA {LONG_RANGE.start}~{LONG_RANGE.stop-1}일 ...")

    close_full = close
    c_win = close_full.reindex(window).to_numpy(dtype=float)

    # rolling MA 사전 계산 (반복 재사용)
    periods = sorted(set(SHORT_RANGE) | set(LONG_RANGE))
    rolling = {p: close_full.rolling(p).mean() for p in periods}

    rows = []
    for s in SHORT_RANGE:
        sma_s_win = rolling[s].reindex(window).to_numpy(dtype=float)
        for l in LONG_RANGE:
            if l <= s:
                continue
            sma_l_win = rolling[l].reindex(window).to_numpy(dtype=float)
            r = backtest(sma_s_win, sma_l_win, c_win)
            r["short_ma"] = s
            r["long_ma"] = l
            rows.append(r)

    df = pd.DataFrame(rows)
    print(f"   → {len(df):,}개 조합 평가 완료")
    return df


# ══════════════════════════════════════════════
# Reporting
# ══════════════════════════════════════════════
def fmt_pct(x: float) -> str:
    return f"{x:+.2f}%"


def print_table(title: str, df: pd.DataFrame):
    print(f"\n  {title}")
    print(f"  {'단기MA':>6} {'장기MA':>6} {'총수익률':>10} {'MDD':>9} {'Sharpe':>7} "
          f"{'매수':>5} {'매도':>5} {'연간매매':>8} {'시장노출':>8} {'최종가치':>12}")
    print("  " + "─" * 100)
    for _, r in df.iterrows():
        years = (window[-1] - window[0]).days / 365.25
        annual = r["trades"] / years
        print(f"  {int(r['short_ma']):>6} {int(r['long_ma']):>6} "
              f"{fmt_pct(r['total_return']):>10} {r['mdd']:>8.2f}% {r['sharpe']:>7.2f} "
              f"{int(r['buys']):>5} {int(r['sells']):>5} {annual:>7.2f}회 {r['time_in_market']:>7.1f}% "
              f"${r['final_value']:>10,.0f}")


def trade_dates(short: int, long_: int, close: pd.Series, window: pd.DatetimeIndex):
    """특정 조합의 매수/매도 발생일(교차일) 목록을 반환."""
    sma_s = close.rolling(short).mean().reindex(window).to_numpy(dtype=float)
    sma_l = close.rolling(long_).mean().reindex(window).to_numpy(dtype=float)
    n = len(window)
    valid = ~(np.isnan(sma_s) | np.isnan(sma_l))
    above = np.zeros(n, dtype=bool)
    above[valid] = sma_s[valid] > sma_l[valid]
    prev_above = np.zeros(n, dtype=bool)
    prev_above[1:] = above[:-1]
    prev_valid = np.zeros(n, dtype=bool)
    prev_valid[1:] = valid[:-1]
    cross_up = (~prev_above) & above & valid & prev_valid
    cross_dn = prev_above & (~above) & valid & prev_valid
    buy_dates = window[cross_up]
    sell_dates = window[cross_dn]
    return buy_dates, sell_dates


def print_trade_frequency(close: pd.Series, window: pd.DatetimeIndex, short: int, long_: int):
    """연도별 매수/매도 빈도 표 출력."""
    buy_dates, sell_dates = trade_dates(short, long_, close, window)
    buys_s = pd.Series(1, index=buy_dates.year, dtype=int)
    sells_s = pd.Series(1, index=sell_dates.year, dtype=int)
    yearly = pd.DataFrame({"매수": buys_s.groupby(buys_s.index).sum(),
                           "매도": sells_s.groupby(sells_s.index).sum()}).fillna(0)
    years = (window[-1] - window[0]).days / 365.25
    print(f"  📅 연도별 매매 빈도 — 단기 {short}일 / 장기 {long_}일 (연 평균 매수 {buy_dates.size/years:.2f}회, 매도 {sell_dates.size/years:.2f}회)")
    print(f"  {'연도':>6} {'매수':>6} {'매도':>6}")
    print("  " + "─" * 22)
    for y, row in yearly.iterrows():
        print(f"  {int(y):>6} {int(row['매수']):>6} {int(row['매도']):>6}")
    print(f"  {'합계':>6} {int(buys_s.sum()):>6} {int(sells_s.sum()):>6}")


def robustness_check(close: pd.Series, window: pd.DatetimeIndex, combos: list[tuple[int, int]]) -> pd.DataFrame:
    """전반기 / 후반기 안정성 확인 (과최적화 과잉적합 여부)."""
    mid = window[len(window) // 2]
    half1 = window[window < mid]
    half2 = window[window >= mid]

    rows = []
    for s, l in combos:
        sma_s = close.rolling(s).mean()
        sma_l = close.rolling(l).mean()
        row = {"short_ma": s, "long_ma": l}
        for label, win in (("전반기(16-21)", half1), ("후반기(21-26)", half2)):
            r = backtest(sma_s.reindex(win).to_numpy(float),
                         sma_l.reindex(win).to_numpy(float),
                         close.reindex(win).to_numpy(float))
            row[f"{label}수익"] = r["total_return"]
            row[f"{label}MDD"] = r["mdd"]
        rows.append(row)
    return pd.DataFrame(rows)


# ══════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════
if __name__ == "__main__":
    close, idx = load_data()
    window = test_window(close)
    years = (window[-1] - window[0]).days / 365.25
    print(f"   → 테스트 구간 {window[0].date()} ~ {window[-1].date()} "
          f"({len(window)} 거래일, 약 {years:.1f}년)")

    # 하이브리드 모드 (--hybrid [short long])
    if "--hybrid" in sys.argv:
        args = sys.argv[sys.argv.index("--hybrid") + 1:]
        s, l = None, None
        if len(args) >= 2 and args[0].isdigit() and args[1].isdigit():
            s, l = int(args[0]), int(args[1])
        run_hybrid_mode(close, window, s, l)
        sys.exit(0)

    # 단일 조합 모드
    if len(sys.argv) == 3 and sys.argv[1].isdigit() and sys.argv[2].isdigit():
        s, l = int(sys.argv[1]), int(sys.argv[2])
        res = run_pair(s, l, close, window)
        print(f"\n📊 단일 조합 상세 리포트: 단기 {s}일 / 장기 {l}일")
        print(f"   총수익률    : {res['total_return']:+.2f}%")
        print(f"   최종가치    : ${res['final_value']:,.2f}")
        print(f"   MDD         : {res['mdd']:.2f}%")
        print(f"   Sharpe      : {res['sharpe']:.2f}")
        print(f"   매수/매도   : {res['buys']} / {res['sells']}회")
        print(f"   연간 매매   : {res['trades']/years:.2f}회/년")
        print(f"   시장 노출   : {res['time_in_market']:.1f}%")
        sys.exit(0)

    df = grid_search(close, window)
    csv_path = "tqqq_ma_grid_results.csv"
    df.to_csv(csv_path, index=False)
    print(f"   → 전체 그리드 결과 저장: {csv_path}")

    # 매매 0회(무포지션) 제외
    traded = df[df["trades"] > 0].copy()

    print("\n" + "═" * 104)
    print("  📊 TQQQ MA 교차 그리드 탐색 리포트 (10년, $50,000)")
    print("═" * 104)
    print(f"  구간: {window[0].date()} ~ {window[-1].date()} ({years:.1f}년)")
    print(f"  체결: 신호일 종가 | 수수료/슬리피지: 0 | auto_adjust 종가")
    print("─" * 104)

    # Buy & Hold
    c_win = close.reindex(window).to_numpy(float)
    bh_ret = (c_win[-1] / c_win[0] - 1) * 100
    bh_peak = np.maximum.accumulate(c_win)
    bh_mdd = ((c_win - bh_peak) / bh_peak).min() * 100
    print(f"\n  📌 참고 — Buy & Hold (TQQQ)")
    print(f"     총수익률 {bh_ret:+.2f}% | MDD {bh_mdd:.2f}% | 최종 ${INITIAL_CASH*(1+bh_ret/100):,.0f}")

    # 유튜버 기준 조합 (6/107)
    yt = run_pair(YT_SHORT, YT_LONG, close, window)
    print(f"\n  📌 참고 — 유튜버 기준 (단기 {YT_SHORT}일 / 장기 {YT_LONG}일)")
    print(f"     총수익률 {yt['total_return']:+.2f}% | MDD {yt['mdd']:.2f}% | Sharpe {yt['sharpe']:.2f} "
          f"| 매수 {yt['buys']}회/매도 {yt['sells']}회 (연 {yt['trades']/years:.1f}회) "
          f"| 최종 ${yt['final_value']:,.0f}")

    # 1) 총수익률 Top N
    top_ret = traded.nlargest(TOP_N, "total_return")[
        ["short_ma", "long_ma", "total_return", "mdd", "sharpe",
         "buys", "sells", "trades", "time_in_market", "final_value"]]
    print_table(f"🏆 총수익률 TOP {TOP_N}", top_ret)

    # 2) MDD Top N (수익률 > 0, 매매 1회 이상) — 낙폭이 가장 작은(0에 가까운) 조합
    prof = traded[traded["total_return"] > 0]
    top_mdd = prof.nlargest(TOP_N, "mdd")[
        ["short_ma", "long_ma", "total_return", "mdd", "sharpe",
         "buys", "sells", "trades", "time_in_market", "final_value"]]
    print_table(f"🛡️ MDD(낙폭) 최소 TOP {TOP_N} (수익률>0 필터)", top_mdd)

    # 2b) MDD 제한 내 최고 수익 (MDD > -45% 필터)
    dd_limited = traded[traded["mdd"] > -45.0].nlargest(TOP_N, "total_return")
    if len(dd_limited):
        print_table(f"🎯 MDD -45% 이상(안전) 유지 + 최고 수익 TOP {TOP_N}", dd_limited[
            ["short_ma", "long_ma", "total_return", "mdd", "sharpe",
             "buys", "sells", "trades", "time_in_market", "final_value"]])

    # 3) Calmar (수익률/|MDD|) Top N
    traded_c = traded.copy()
    traded_c["calmar"] = traded_c["total_return"] / traded_c["mdd"].abs()
    top_calmar = traded_c.nlargest(TOP_N, "calmar")[
        ["short_ma", "long_ma", "total_return", "mdd", "sharpe",
         "buys", "sells", "trades", "time_in_market", "final_value"]]
    print_table(f"⚖️ Calmar(수익/MDD) TOP {TOP_N}", top_calmar)

    # 4) MDD 제한 내 최고 수익 (MDD > -35% 필터)
    dd_limited = traded[traded["mdd"] > -35.0].nlargest(TOP_N, "total_return")
    if len(dd_limited):
        print_table(f"🎯 MDD -35% 이상 유지 + 최고 수익 TOP {TOP_N}", dd_limited[
            ["short_ma", "long_ma", "total_return", "mdd", "sharpe",
             "buys", "sells", "trades", "time_in_market", "final_value"]])

    # 5) 로버스트니스: 상위 조합 전반기/후반기
    featured = list(zip(top_ret["short_ma"], top_ret["long_ma"]))
    rob = robustness_check(close, window, featured)
    print("\n  🔎 로버스트니스 체크 — 총수익 TOP 조합의 전반기/후반기 성과")
    print(f"  {'단기':>4} {'장기':>4} {'전반기수익':>10} {'전반기MDD':>9} {'후반기수익':>10} {'후반기MDD':>9}")
    print("  " + "─" * 56)
    for _, r in rob.iterrows():
        print(f"  {int(r['short_ma']):>4} {int(r['long_ma']):>4} "
              f"{r['전반기(16-21)수익']:>+9.1f}% {r['전반기(16-21)MDD']:>8.1f}% "
              f"{r['후반기(21-26)수익']:>+9.1f}% {r['후반기(21-26)MDD']:>8.1f}%")

    # 6) 유튜버 조합 그리드 내 순위 (1위 = 최고)
    yt_row = df[(df["short_ma"] == YT_SHORT) & (df["long_ma"] == YT_LONG)].iloc[0]
    ret_rank = int((df["total_return"] > yt_row["total_return"]).sum()) + 1
    mdd_rank = int((df["mdd"] > yt_row["mdd"]).sum()) + 1
    print(f"\n  📍 유튜버 조합({YT_SHORT}/{YT_LONG}) 순위: 수익률 {ret_rank}/{len(df)}위, "
          f"MDD(작을수록 좋음) {mdd_rank}/{len(df)}위")

    # 7) 대표 조합 연도별 매매 빈도
    best_calmar = top_calmar.iloc[0]
    yt2 = run_pair(YT_SHORT, YT_LONG, close, window)
    print(f"\n  🔢 대표 조합 — 최고 Calmar({int(best_calmar['short_ma'])}/{int(best_calmar['long_ma'])})")
    print_trade_frequency(close, window, int(best_calmar["short_ma"]), int(best_calmar["long_ma"]))
    print(f"\n  🔢 대표 조합 — 유튜버 기준({YT_SHORT}/{YT_LONG})")
    print_trade_frequency(close, window, YT_SHORT, YT_LONG)
    print("\n" + "═" * 104)
    print("  ✅ Backtest Complete")
    print("═" * 104)
