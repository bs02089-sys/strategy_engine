#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
loc_sweep_backtest.py — LOC 분할 수 스윕 백테스트 ('1년 매수 + 홀딩' 구조)
========================================================================

질문: 순수 LOC 지정가 5분할 DCA 에서 **1년차(매수 기간)에 몇 차 분할이 최적인가?**

모델:
  - 5년 윈도우, 1년차(매수 기간)에만 시그마 LOC 트리거로 최대 N 회 매수
    (회당 $budget/N), 2~5년차는 홀딩(매수 없음)
  - 분할 수가 많으면 회당 금액이 작아져 1년차에 트리거가 N 회 안 오면
    잔여 현금이 홀딩 기간 내내 논다 (평균현금비율↑)
  - 가격: **원시 종가(Close), 미조정** — 실전 판정(확정 종가)과 동일 계열
    (참고: LOC_DCA_strategy.py --backtest 는 배당 조정 종가를 쓰므로 절대 수치가 다름)

사용법:
  python3 loc_sweep_backtest.py                              # 기본: TQQQ, 5년 윈도우, 1,5,10,20,52분할
  python3 loc_sweep_backtest.py --buy-years 2                # 2년차까지 매수 후 홀딩
  python3 loc_sweep_backtest.py --sweep-counts 1,5,10,20,52   # 검사할 분할 수 목록
"""
import argparse
from datetime import date, timedelta

import numpy as np
import pandas as pd
import yfinance as yf

from LOC_DCA_strategy import (
    EWMA_LAMBDA, LOOKBACK_DAYS, TEST_END, VOL_METHOD,
    _calculate_loc_from_sigma, _calculate_volatility_from_closes,
    load_config,
)

DEFAULT_TICKER = "TQQQ"
DEFAULT_BUDGET = 50_000.0
DEFAULT_FEE = 0.001
DATA_START = "2013-12-01"   # LOC 엔진 DATA_START 와 동일 — 워밍업(σ 252일) 확보용


def load_ohlc(ticker: str, end: date) -> pd.DataFrame:
    """원시 Close 다운로드 — 실전 엔진과 동일 기준으로 검증한다.

    - Close(미조정): LOC 판정(마감가 체결) 기준 (실전은 확정 종가로 판정)
    - 배당 미반영 — LOC_DCA_strategy.py --backtest(배당 조정 종가)와는 절대 수치가 다르다
    TEST_START 필터만 제거해 윈도우 이전 워밍업 데이터를 확보한다.
    """
    raw = yf.download(ticker, start=DATA_START,
                      end=(end + timedelta(days=1)).isoformat(),
                      auto_adjust=False, progress=False)
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    df = raw[["Close"]].dropna().copy()
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df[df.index <= pd.Timestamp(end)]
    return df


def simulate_loc(df: pd.DataFrame, w0: int, budget: float, splits: int,
                 multiplier: float, fee_rate: float, wend: int | None = None,
                 buy_end: int | None = None) -> dict:
    """LOC 분할 DCA — LOC_DCA_strategy.backtest() 와 동일 논리, 윈도우 시작만 파라미터화.

    시뮬레이션은 df[w0:wend] (윈도우)에서만 진행, σ 계산은 직전 LOOKBACK_DAYS 를 워밍업으로 사용.
    buy_end: 매수 허용 마지막 인덱스 (기본 None = 윈도우 전체) — '1년 매수 후 홀딩' 모델용.
    """
    closes = df["Close"].to_numpy(dtype=float)
    dates = df.index
    n = wend if wend is not None else len(df)
    buy_limit = buy_end if buy_end is not None else n
    buy_amount = budget / splits

    cash = float(budget)
    shares = 0.0
    buys = 0
    total_spent = 0.0
    buy_log = []
    daily_values = []
    daily_cash: list[float] = []
    deployed_at: pd.Timestamp | None = None      # 분할 소진(매수 중단) 시점

    for i in range(w0, n):
        prev_close = float(closes[i - 1])
        today_close = float(closes[i])
        # 1e-9 엡실론: 마지막 회차가 부동소수점 오차(예: $50,000/3, /6)로 매수 누락되지 않게
        if i < buy_limit and cash >= buy_amount - 1e-9 and buys < splits:
            sigma, _ = _calculate_volatility_from_closes(
                pd.Series(closes[i - LOOKBACK_DAYS: i]), LOOKBACK_DAYS, VOL_METHOD, EWMA_LAMBDA
            )
            loc_price = _calculate_loc_from_sigma(prev_close, sigma, multiplier)
            if today_close <= loc_price:      # LOC: 장 마감가 ≤ 지정가일 때만 체결
                buy_price = today_close       # 체결가 = 마감가 (LOC_DCA_strategy.backtest 와 동일)
                amt = min(buy_amount, cash)
                shares += amt * (1 - fee_rate) / buy_price
                cash -= amt
                buys += 1
                total_spent += amt
                buy_log.append({"date": dates[i], "price": buy_price, "amount": amt})
                if buys == splits:
                    deployed_at = dates[i]
        daily_values.append(cash + shares * today_close)
        daily_cash.append(cash)

    return _metrics(daily_values, budget, closes[n - 1], dates[n - 1],
                    {"buys": buys, "total_spent": total_spent, "cash": cash,
                     "buy_log": buy_log, "deployed_at": deployed_at,
                     "cash_ratio": _cash_ratio(daily_cash, daily_values)})


def _cash_ratio(daily_cash: list[float], daily_values: list[float]) -> float:
    """기간 중 평균 현금 비율 = 평균 현금 / 평균 총자산 × 100 — 자금 유휴(노는 돈) 지표.

    0% = 항상 전액 투자, 높을수록 현금으로 오래 대기(기회 비용)했음을 뜻한다.
    """
    if not daily_values:
        return 0.0
    avg_cash = float(np.mean(daily_cash))
    avg_equity = float(np.mean(daily_values))
    return avg_cash / avg_equity * 100 if avg_equity > 0 else 0.0


def _metrics(daily_values: list[float], budget: float, last_close: float,
             last_date, extra: dict) -> dict:
    """LOC backtest() 와 동일 지표 공식 (total_return/MDD/Sharpe/Calmar)."""
    dv = np.array(daily_values, dtype=float)
    daily_ret = dv[1:] / dv[:-1] - 1
    ret_mean = float(daily_ret.mean())
    ret_std = float(daily_ret.std())
    sharpe = float(np.sqrt(252) * ret_mean / ret_std) if ret_std > 0 else 0.0
    peak = np.maximum.accumulate(dv)
    mdd = float(((dv - peak) / peak).min() * 100)
    final_val = float(dv[-1])
    total_ret = (final_val - budget) / budget * 100
    r = {
        "total_return": round(total_ret, 2),
        "final_value": round(final_val, 2),
        "mdd": round(mdd, 2),
        "sharpe": round(sharpe, 2),
        "calmar": round(total_ret / abs(mdd), 2) if mdd != 0 else 0.0,
        "window_end": last_date.date() if hasattr(last_date, "date") else last_date,
        "last_close": round(last_close, 2),
    }
    r.update(extra)
    return r


def _rolling_starts(df: pd.DataFrame, window_years: float, step_months: int) -> list[tuple[int, int]]:
    """롤링 윈도우 (w0, we) 목록 — 데이터 워밍업(252일) 이후부터 6개월 간격,
    각 윈도우는 정확히 window_years 년 (종료일이 데이터 끝보다 앞이어야 함)."""
    window_days = int(window_years * 365.25)
    last = df.index[-1]
    ts = df.index[LOOKBACK_DAYS + 1]
    out: list[tuple[int, int]] = []
    while ts <= last:
        w = int(np.argmax(df.index >= ts))
        end_target = df.index[w] + pd.Timedelta(days=window_days)
        mask = df.index >= end_target
        if not mask.any():          # 남은 데이터가 윈도우 길이보다 짧음 — 종료
            break
        we = int(np.argmax(mask))
        out.append((w, we))
        ts = ts + pd.DateOffset(months=step_months)
    return out


def run_split_sweep(df: pd.DataFrame, args, loc_cfg: dict) -> None:
    """LOC 분할 수 스윕 — '1년 매수 + 4년 홀딩' 구조에서 분할 수 N 의 최적값 판정.

    모델: 5년 윈도우, 1년차(매수 기간)에만 시그마 LOC 트리거로 최대 N 회 매수
    (회당 $budget/N), 2~5년차는 홀딩(매수 없음). 분할 수가 많으면 회당 금액이
    작아져 1년차에 트리거가 N 회 안 오면 잔여 현금이 홀딩 기간 내내 논다.
    """
    starts = _rolling_starts(df, float(args.rolling_window), int(args.rolling_step))
    counts = [int(x) for x in args.sweep_counts.split(",") if x.strip()]
    if not counts:
        print("❌ --sweep-counts 가 비어 있습니다.")
        return
    buy_days = int(float(args.buy_years) * 365.25)
    n_ref = int(loc_cfg["splits"])   # 사용자 현행 분할 수 (비교 기준 — portfolio_config.json 단일 소스)

    # 매수 기간 종료 인덱스 — 각 윈도우 공통 규칙 (w0 + buy_days 이후 매수 중단)
    buy_ends = [int(np.argmax(df.index >= df.index[w0] + pd.Timedelta(days=buy_days)))
                for w0, _ in starts]

    w0_main = int(np.argmax(df.index >= pd.Timestamp(args.end_default)))
    buy_end_main = int(np.argmax(df.index >= df.index[w0_main] + pd.Timedelta(days=buy_days)))

    print(f"\n{'═' * 100}")
    print(f"  LOC 분할 수 스윕 — {args.ticker} · 1년차 매수({buy_days}일) + 홀딩 · 롤링 {args.rolling_window:.0f}년 {len(starts)}개 윈도우")
    print(f"  모델: 1년차에만 시그마 LOC 트리거로 최대 N 회 매수(회당 ${args.budget:,.0f}/N), 이후 홀딩 · 수수료 {args.fee*100:.2f}%")
    print(f"  질문: 1년간 몇 차 분할이 최적인가? (현행 {n_ref}분할 ◀)")
    print(f"{'═' * 100}")
    win_head = f"{n_ref}분할승률"
    print(f"  {'분할수':>5} {'회당금액':>9} {'평균수익률':>10} {win_head:>10} {'평균MDD':>8} "
          f"{'평균현금비율':>10} {'평균투입률':>9} {'최근5년':>9}")
    print("  " + "-" * 86)

    # 각 분할 수별 윈도우 결과를 먼저 전부 계산 (현행 분할 대비 승률은 윈도우별 비교 필요)
    per_n: dict[int, list[dict]] = {}
    mains: dict[int, dict] = {}
    for n in counts:
        rs = []
        for (w0, we), be in zip(starts, buy_ends):
            rs.append(simulate_loc(df, w0, args.budget, n, loc_cfg["entry_multiplier"],
                                   args.fee, wend=we, buy_end=min(be, we)))
        per_n[n] = rs
        mains[n] = simulate_loc(df, w0_main, args.budget, n, loc_cfg["entry_multiplier"],
                                args.fee, buy_end=buy_end_main)

    ref_rets = [r["total_return"] for r in per_n.get(n_ref, [])]
    best_avg: tuple[float, int] | None = None
    for n in counts:
        rs = per_n[n]
        avg_ret = float(np.mean([r["total_return"] for r in rs]))
        if best_avg is None or avg_ret > best_avg[0]:
            best_avg = (avg_ret, n)
        win = (sum(1 for r, rr in zip(rs, ref_rets) if r["total_return"] > rr) / len(rs) * 100
               if ref_rets and n != n_ref else float("nan"))
        amt = args.budget / n
        mark = " ◀현행" if n == n_ref else (" ◀최적" if best_avg[1] == n else "")
        print(f"  {n:>5} ${amt:>8,.0f} {avg_ret:>+9.1f}% {win:>9.0f}% "
              f"{np.mean([r['mdd'] for r in rs]):>7.1f}% "
              f"{np.mean([r['cash_ratio'] for r in rs]):>9.1f}% "
              f"{np.mean([r['total_spent'] / args.budget for r in rs])*100:>8.0f}% "
              f"{mains[n]['total_return']:>+8.1f}%{mark}")
    print("  " + "-" * 86)
    print(f"\n  → 평균 수익률 최고: {best_avg[1]}분할 ({best_avg[0]:+.1f}%) · 현행 {n_ref}분할은 "
          f"{float(np.mean(ref_rets)):+.1f}%")
    print(f"  → 평균투입률 = 1년차에 실제 투입된 예산 비율 — 분할 수가 많아도 1년차 트리거가 "
          f"부족하면 잔여 현금이 홀딩 기간 내내 유휴(평균현금비율↑)")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="LOC 분할 수 스윕 백테스트 — '1년 매수 + 홀딩' 구조에서 몇 차 분할이 최적인지")
    ap.add_argument("--ticker", default=DEFAULT_TICKER)
    ap.add_argument("--end", default=None, help=f"종료일 (기본 {TEST_END})")
    ap.add_argument("--budget", type=float, default=DEFAULT_BUDGET,
                    help=f"총 예산 $ (기본 {DEFAULT_BUDGET:,.0f})")
    ap.add_argument("--fee", type=float, default=DEFAULT_FEE,
                    help=f"매수 수수료 (기본 {DEFAULT_FEE} = 0.1%%)")
    ap.add_argument("--buy-years", type=float, default=1.0,
                    help="매수 기간(년, 기본 1 — 이후 홀딩)")
    ap.add_argument("--sweep-counts", default=None,
                    help="스윕으로 검사할 분할 수 목록 (기본: 1,5,10,20,52 — 문서 수치와 동일한 세트)")
    ap.add_argument("--rolling-window", type=float, default=5.0,
                    help="윈도우 길이(년, 기본 5)")
    ap.add_argument("--rolling-step", type=int, default=6,
                    help="윈도우 시작 간격(개월, 기본 6)")
    args = ap.parse_args()

    end = date.fromisoformat(args.end) if args.end else TEST_END
    args.end_default = (end - timedelta(days=1826))   # 스윕의 '최근 5년' 윈도우 시작일

    print(f"📥 {args.ticker} 데이터 다운로드 ({DATA_START} → {end.isoformat()})...")
    df = load_ohlc(args.ticker, end)

    # ── 설정 로드 (단일 소스) ──
    loc_cfg = load_config(args.ticker)

    # ── LOC 분할 수 스윕 (1년 매수 + 홀딩) ──
    # 기본 1,5,10,20,52 — README 의 수치가 이 세트 기준이므로 재현성 유지
    args.sweep_counts = args.sweep_counts or "1,5,10,20,52"
    run_split_sweep(df, args, loc_cfg)


if __name__ == "__main__":
    main()
