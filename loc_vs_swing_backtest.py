#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
loc_vs_swing_backtest.py — 장기 축적형 매수 조건 비교: LOC_DCA(시그마) vs 스윙(ATH 하락 구간)
==============================================================================================

질문: "스윙 알리미의 세븐 스플릿 매수 조건(ATH 대비 -15%~-33% 3% 단위 7구간)이
      LOC_DCA(전일 종가 × (1−σ×승수))보다 장기 적립 방식으로 우월한가?"

모델 (2026-08-17 사용자 결정):
  - ⚠️ 스윙의 매도 목표(+40%, SWING_TARGET_PCT)는 **제외** — 두 전략 모두 무매도 순수 적립
  - 총 예산 동일화: --budget (기본 $50,000 — LOC 실전 예산과 동일)
  - 기간: 최근 5년 (기본 2021-08-02 ~ 2026-08-02, LOC TEST_END 기준 고정 — 재현성)
  - 스윙 재투입: 새 ATH 사이클(+1% 리셋, swing_alerter.py _handle_ath_cycle_reset 와 동일 규칙)
    마다 구간이 재무장되어 같은 구간을 다시 매수 (무매도 축적 — 실전의 '매도 후 재매수'가
    아닌 '보유 중 추가 매수' 가정). 기본은 총 예산(--budget) 내에서만 재투입 → 예산 소진 시
    중단. --swing-per-cycle $N 을 주면 사이클마다 $N 신규 자금을 추가 투입 (실전 스윙의
    사이클당 $3,500 규모로 다중 사이클 축적 확인 가능)

전략 규칙 (실전 엔진과 동일):
  - LOC: 매일 loc = 전일 종가 × (1−σ×승수), σ = EWMA(λ=0.94, 252일 로그수익률).
         당일 저가 ≤ loc → 매수 (체결가 = min(종가, loc)), $budget/splits × 최대 splits 회
  - 스윙: ATH = 배당 조정 종가 롤링 역대 최고가. 종가 ≤ ATH × (1−구간%) 도달 시 매수
         (확정 종가 기준 — 실시간 값 미사용, 엔진 동일), 구간당 $budget/7.
         구간은 사이클당 1회 매수, ATH +1% 갱신 시 새 사이클 → 전 구간 재무장·재매수.
  - 수수료: --fee (기본 0.1%, 매수 시 적용 — 두 전략 동일)
  - 기준선: 전액 일시 매수 후 보유 (Buy & Hold) 참고

사용법:
  python3 loc_vs_swing_backtest.py                      # 기본: TQQQ, 최근 5년, $50,000, 수수료 0.1%
  python3 loc_vs_swing_backtest.py --since 2016-08-02   # 10년 기간 비교 (LOC 문서 기준 윈도우)
  python3 loc_vs_swing_backtest.py --fee 0              # 수수료 0% 비교
  python3 loc_vs_swing_backtest.py --budget 3500        # 스윙 실전 예산 규모로 비교
  python3 loc_vs_swing_backtest.py --swing-per-cycle 3500  # 사이클마다 $3,500 신규 투입 (다중 사이클 축적)
"""
import argparse
import json
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
SWING_CONFIG_PATH = "swing_config.json"
DEFAULT_BUDGET = 50_000.0
DEFAULT_FEE = 0.001
DATA_START = "2013-12-01"   # LOC 엔진 DATA_START 와 동일 — 워밍업(σ 252일 + ATH) 확보용


def load_ohlc(ticker: str, end: date) -> pd.DataFrame:
    """OHLC 다운로드 — LOC 엔진 load_data 와 동일 규칙(TEST_START 필터만 제거해
    윈도우 이전 워밍업 데이터를 확보). Close/Low 로 시뮬레이션한다."""
    raw = yf.download(ticker, start=DATA_START,
                      end=(end + timedelta(days=1)).isoformat(),
                      auto_adjust=True, progress=False)
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    df = raw[["Close", "Low"]].dropna().copy()
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df[df.index <= pd.Timestamp(end)]
    return df


def load_swing_zones(count: int | None = None) -> list[int]:
    """swing_config.json 의 MDD 구간 설정 읽기 — 단일 소스 (기본 -15~-33, 3% 스텝 7구간).

    count 지정 시 스타트/스텝은 설정을 유지하고 구간 수만 확장한다
    (예: count=20 → -15% ~ -72%, 3% 스텝 — 20분할 래더).
    """
    try:
        with open(SWING_CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        start = int(cfg.get("MDD_START_PCT", 15))
        end = int(cfg.get("MDD_END_PCT", 33))
        step = int(cfg.get("MDD_STEP_PCT", 3))
        if count is not None:
            end = start + step * (count - 1)
        return list(range(start, end + 1, step))
    except Exception:
        base = [15, 18, 21, 24, 27, 30, 33]
        if count is not None:
            return [15 + 3 * k for k in range(count)]
        return base


def simulate_loc(df: pd.DataFrame, w0: int, budget: float, splits: int,
                 multiplier: float, fee_rate: float, wend: int | None = None,
                 buy_end: int | None = None) -> dict:
    """LOC 분할 DCA — LOC_DCA_strategy.backtest() 와 동일 논리, 윈도우 시작만 파라미터화.

    시뮬레이션은 df[w0:wend] (윈도우)에서만 진행, σ 계산은 직전 LOOKBACK_DAYS 를 워밍업으로 사용.
    buy_end: 매수 허용 마지막 인덱스 (기본 None = 윈도우 전체) — '1년 매수 후 홀딩' 모델용.
    """
    closes = df["Close"].to_numpy(dtype=float)
    lows = df["Low"].to_numpy(dtype=float)
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
        today_low = float(lows[i])
        today_close = float(closes[i])
        # 1e-9 엡실론: 마지막 회차가 부동소수점 오차(예: $50,000/3, /6)로 매수 누락되지 않게
        if i < buy_limit and cash >= buy_amount - 1e-9 and buys < splits:
            sigma, _ = _calculate_volatility_from_closes(
                pd.Series(closes[i - LOOKBACK_DAYS: i]), LOOKBACK_DAYS, VOL_METHOD, EWMA_LAMBDA
            )
            loc_price = _calculate_loc_from_sigma(prev_close, sigma, multiplier)
            if today_low <= loc_price:
                buy_price = min(today_close, loc_price)
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


def simulate_swing(df: pd.DataFrame, w0: int, budget: float, zones: list[int],
                   fee_rate: float, per_cycle: float = 0.0,
                   wend: int | None = None) -> dict:
    """세븐 스플릿 매수 조건 무매도 축적 — ATH 하락 구간 + 사이클(+1% 리셋) 재투입.

    - ATH = 배당 조정 종가 롤링 역대 최고가 (윈도우 진입 시점 ATH = 이전 데이터 최고가)
    - 구간 도달 판정: 종가 ≤ ATH × (1−구간%) — 확정 종가 기준 (엔진 동일)
    - 사이클: ATH 가 사이클 기준 대비 +1% 초과 갱신 시 새 사이클 → 전 구간 재무장 (재매수 가능)
    - 매수: 구간당 사이클 1회, $budget/len(zones) — 잔여 예산 소진 시 중단
    - per_cycle > 0: 새 사이클 시작 시 $per_cycle 신규 자금 추가 (다중 사이클 축적)
    - wend: 윈도우 종료 인덱스 (기본 = 데이터 끝) — 롤링 검증용
    """
    closes = df["Close"].to_numpy(dtype=float)
    dates = df.index
    n = wend if wend is not None else len(df)
    per_zone = budget / len(zones)

    ath = float(closes[:w0].max()) if w0 > 0 else float(closes[0])   # 윈도우 진입 ATH
    cycle_base = ath
    cycle = 1
    last_bought_cycle = {z: 0 for z in zones}   # 구간별 마지막 매수 사이클 (사이클당 1회)

    cash = float(budget)
    shares = 0.0
    buys = 0
    total_spent = 0.0
    buy_log = []
    cycle_buys = {1: 0}
    daily_values = []
    daily_cash: list[float] = []
    deployed_at: pd.Timestamp | None = None      # 예산 완전 소진(매수 중단) 시점

    for i in range(w0, n):
        c = float(closes[i])
        if c > ath:
            ath = c
        # 사이클 리셋 — ATH_CYCLE_BASE 대비 +1% 초과 시 (엔진 _handle_ath_cycle_reset 동일 규칙)
        if ath > cycle_base * 1.01:
            cycle += 1
            cycle_base = ath
            cycle_buys.setdefault(cycle, 0)
            if per_cycle > 0:
                cash += per_cycle                # 신규 자금 투입 (재투입 모델)
        # 구간 매수 — 종가 ≤ 구간가 (같은 날 여러 구간 동시 도달 가능)
        for z in zones:
            if last_bought_cycle[z] >= cycle or cash < per_zone - 1e-9:
                continue
            if c <= ath * (1 - z / 100.0) + 1e-9:
                amt = min(per_zone, cash)
                shares += amt * (1 - fee_rate) / c
                cash -= amt
                buys += 1
                total_spent += amt
                last_bought_cycle[z] = cycle
                cycle_buys[cycle] = cycle_buys.get(cycle, 0) + 1
                buy_log.append({"date": dates[i], "zone": z, "price": c, "cycle": cycle})
        # 예산 완전 소진(매수 중단) 시점 — 일시 예산(per_cycle=0) 모드에서만 의미 있음
        # (재투입 모드는 다음 사이클에 현금이 다시 채워져 '소진'이 아니므로 기록하지 않음)
        if deployed_at is None and per_cycle == 0 and buys > 0 and cash < per_zone - 1e-9:
            deployed_at = dates[i]
        daily_values.append(cash + shares * c)
        daily_cash.append(cash)

    return _metrics(daily_values, budget, closes[n - 1], dates[n - 1],
                    {"buys": buys, "total_spent": total_spent, "cash": cash,
                     "buy_log": buy_log, "cycles": cycle, "cycle_buys": cycle_buys,
                     "zones": zones, "ath_end": ath, "deployed_at": deployed_at,
                     "initial_budget": budget, "per_cycle": per_cycle,
                     "cash_ratio": _cash_ratio(daily_cash, daily_values)})


def simulate_buyhold(df: pd.DataFrame, w0: int, budget: float, fee_rate: float,
                     wend: int | None = None) -> dict:
    """기준선 — 윈도우 시작 전액 일시 매수 후 보유."""
    closes = df["Close"].to_numpy(dtype=float)
    dates = df.index
    n = wend if wend is not None else len(df)
    c0 = float(closes[w0])
    shares = budget * (1 - fee_rate) / c0
    daily_values = [shares * float(closes[i]) for i in range(w0, n)]
    return _metrics(daily_values, budget, float(closes[n - 1]), dates[n - 1],
                    {"buys": 1, "total_spent": budget, "cash": 0.0, "buy_log": []})


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


def print_zone_detail(r: dict) -> None:
    """스윙 구간별 매수 통계 — 구간별 매수횟수/평균 매수가/최초·최후 매수일."""
    log = r["buy_log"]
    reinvest_note = f" + 사이클당 ${r['per_cycle']:,.0f} 신규 투입" if r.get("per_cycle") else " (예산 내 재투입)"
    print(f"  ── 스윙 구간별 매수 상세 (총 {len(log)}회 · ATH +1% 리셋 {r['cycles']}사이클{reinvest_note}) ──")
    print(f"  {'구간':>6} {'매수횟수':>8} {'평균매수가':>10} {'최초매수':>12} {'최후매수':>12}")
    for z in r["zones"]:
        zl = [b for b in log if b["zone"] == z]
        if not zl:
            # 미매수 = 가격 도달 여부와 무관하게 매수 없음 (예산 소진으로 매수 불가였을 수 있음)
            print(f"  -{z:>4.0f}% {'미매수':>8}")
            continue
        avg = float(np.mean([b["price"] for b in zl]))
        first = zl[0]["date"].date()
        last = zl[-1]["date"].date()
        print(f"  -{z:>4.0f}% {len(zl):>8} {avg:>10,.2f} {str(first):>12} {str(last):>12}")
    active = {c: n for c, n in r["cycle_buys"].items() if n > 0}
    if active:
        cycle_str = ", ".join(f"{c}사이클:{n}회" for c, n in sorted(active.items()))
        print(f"  ── 매수 발생 사이클: {cycle_str} ──")
    if r.get("deployed_at") is not None:
        print(f"  ⏳ 예산 완전 소진(매수 중단) 시점: {r['deployed_at'].date()}")
    if log:
        avg_all = float(np.mean([b["price"] for b in log]))
        print(f"  평균 매수가 ${avg_all:,.2f} (전 구간 통합) — 최종 종가 ${r['last_close']:,.2f}")


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


def run_zone_sweep(df: pd.DataFrame, args, loc_cfg: dict) -> None:
    """스윙 구간 수 스윕 — '몇 차까지 나눠야 자금이 안 노는가'를 판정.

    각 구간 수 N 에 대해: 14개 롤링 5년 윈도우의 평균 총수익률 · LOC 대비 승률 ·
    평균 MDD · 평균 자금 투입률(매수 회수/구간 수 — 낮을수록 유휴 현금)을 비교하고,
    최근 5년(기본 윈도우) 수익률도 함께 보여준다. LOC(항상 전액 투입)가 기준선.
    """
    starts = _rolling_starts(df, float(args.rolling_window), int(args.rolling_step))
    counts = [int(x) for x in args.sweep_counts.split(",") if x.strip()]
    if not counts:
        print("❌ --sweep-counts 가 비어 있습니다.")
        return

    # LOC 결과 — 구간 수와 무관하므로 1회만 계산 (캐시)
    loc_roll = [simulate_loc(df, w0, args.budget, loc_cfg["splits"],
                             loc_cfg["entry_multiplier"], args.fee, wend=we)
                for w0, we in starts]
    loc_avg = float(np.mean([r["total_return"] for r in loc_roll]))
    loc_mdd = float(np.mean([r["mdd"] for r in loc_roll]))
    loc_cash = float(np.mean([r["cash_ratio"] for r in loc_roll]))

    # 최근 5년 (기본 윈도우) — 각 구간 수 공통
    w0_main = int(np.argmax(df.index >= pd.Timestamp(args.end_default)))
    loc_main = simulate_loc(df, w0_main, args.budget, loc_cfg["splits"],
                            loc_cfg["entry_multiplier"], args.fee)

    print(f"\n{'═' * 100}")
    print(f"  스윙 구간 수 스윕 — {args.ticker} · 롤링 {args.rolling_window:.0f}년 {len(starts)}개 윈도우 · "
          f"예산 ${args.budget:,.0f} · 수수료 {args.fee*100:.2f}%")
    print(f"  질문: 구간을 몇 차까지 나누면 자금이 안 노는가? (투입률↓ = 유휴 현금 ↑ = 기회 비용)")
    print(f"{'═' * 100}")
    print(f"  {'구간수':>5} {'범위':>13} {'평균수익률':>10} {'LOC승률':>8} {'평균MDD':>8} "
          f"{'평균현금비율':>10} {'최근5년':>9}")
    print("  " + "-" * 80)

    best_avg: tuple[float, int] | None = None
    for n in counts:
        zones = load_swing_zones(n)
        rets, mdds, cashes = [], [], []
        for (w0, we), rloc in zip(starts, loc_roll):
            r = simulate_swing(df, w0, args.budget, zones, args.fee, wend=we)
            rets.append(r["total_return"])
            mdds.append(r["mdd"])
            cashes.append(r["cash_ratio"])
        avg_ret = float(np.mean(rets))
        win_rate = sum(1 for ret, rloc in zip(rets, loc_roll) if ret > rloc["total_return"]) / len(rets)
        avg_mdd = float(np.mean(mdds))
        avg_cash = float(np.mean(cashes))
        r_main = simulate_swing(df, w0_main, args.budget, zones, args.fee)
        mark = " ◀최적(평균)" if best_avg is None or avg_ret > best_avg[0] else ""
        if best_avg is None or avg_ret > best_avg[0]:
            best_avg = (avg_ret, n)
        print(f"  {n:>5} {-zones[-1]:>5.0f}%%~-{zones[0]:>2.0f}%  {avg_ret:>+9.1f}% {win_rate*100:>7.0f}% "
              f"{avg_mdd:>7.1f}% {avg_cash:>9.1f}% {r_main['total_return']:>+8.1f}%{mark}")
    print("  " + "-" * 80)
    print(f"  {'LOC':>5} {'시그마':>13} {loc_avg:>+9.1f}% {'—':>8} {loc_mdd:>7.1f}% "
          f"{loc_cash:>9.1f}% {loc_main['total_return']:>+8.1f}%  (기준선 — 시그마 {loc_cfg['splits']}분할)")
    print(f"\n  → 평균 수익률 최고: {best_avg[1]}구간 (범위 -15%%~-{15 + 3 * (best_avg[1] - 1)}%)")
    print(f"  → 평균현금비율 = 기간 중 평균 현금/총자산 — 높을수록 자금이 오래 놀았음(기회 비용)."
          f"\n     깊은 구간(많은 분할)일수록 심한 하락이 오기 전까지 현금 대기 → 비율 상승.")


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


def run_rolling(df: pd.DataFrame, args, loc_cfg: dict, zones: list[int]) -> None:
    """롤링 윈도우 강건성 검증 — 시작 시점을 바꿔가며 5년 윈도우 반복 비교.

    목적: '최근 5년에서 스윙 우위'가 전략의 내재적 우월함인지, 아니면 윈도우 시작
    시점(고점/저점)에 좌우되는 우연인지 판별. 시작 시점의 ATH 대비 하락률도 함께
    출력해 '고점 부근 시작 → 스윙 유리 / 저점 부근 시작 → LOC 유리' 메커니즘을 확인한다.
    """
    window_years = float(args.rolling_window)
    step_months = int(args.rolling_step)
    starts = _rolling_starts(df, window_years, step_months)

    rows = []
    for w0, we in starts:
        start_close = float(df["Close"].iloc[w0])
        ath_before = float(df["Close"].iloc[:w0].max())
        start_dd = (start_close / ath_before - 1) * 100   # 시작 시점 ATH 대비 하락률 (음수 = 저점 부근)
        loc = simulate_loc(df, w0, args.budget, loc_cfg["splits"],
                           loc_cfg["entry_multiplier"], args.fee, wend=we)
        swing = simulate_swing(df, w0, args.budget, zones, args.fee, wend=we)
        bh = simulate_buyhold(df, w0, args.budget, args.fee, wend=we)
        rows.append({"start": df.index[w0].date(), "end": df.index[we - 1].date(),
                     "dd": start_dd,
                     "loc": loc["total_return"], "swing": swing["total_return"],
                     "bh": bh["total_return"],
                     "loc_mdd": loc["mdd"], "swing_mdd": swing["mdd"]})

    print(f"\n{'═' * 96}")
    print(f"  롤링 {window_years:.0f}년 윈도우 강건성 검증 — 시작 시점 {step_months}개월 간격 스윕")
    print(f"  {args.ticker} · 예산 ${args.budget:,.0f} · 수수료 {args.fee*100:.2f}% · 40% 매도 제외 무매도 축적")
    print(f"  dd = 윈도우 시작 시점의 ATH 대비 하락률 (0 부근 = 고점 시작, -20% = 저점 시작)")
    print(f"{'═' * 96}")
    print(f"  {'시작일':>12} {'종료일':>12} {'시작dd':>7} {'LOC':>9} {'스윙':>9} {'B&H':>9} {'승자':>6} {'LOC MDD':>8} {'스윙 MDD':>8}")
    print("  " + "-" * 92)
    loc_wins = swing_wins = 0
    for r in rows:
        winner = "LOC" if r["loc"] > r["swing"] else "스윙"
        if winner == "LOC":
            loc_wins += 1
        else:
            swing_wins += 1
        print(f"  {str(r['start']):>12} {str(r['end']):>12} {r['dd']:>6.1f}% {r['loc']:>+8.1f}% "
              f"{r['swing']:>+8.1f}% {r['bh']:>+8.1f}% {winner:>6} {r['loc_mdd']:>7.1f}% {r['swing_mdd']:>7.1f}%")
    print("  " + "-" * 84)
    avg_loc = float(np.mean([r["loc"] for r in rows]))
    avg_swing = float(np.mean([r["swing"] for r in rows]))
    avg_bh = float(np.mean([r["bh"] for r in rows]))
    print(f"  → 스윙 우위 {swing_wins}/{len(rows)} 윈도우 · LOC 우위 {loc_wins}/{len(rows)} 윈도우")
    print(f"  → 평균 총수익률: 스윙 {avg_swing:+.1f}% vs LOC {avg_loc:+.1f}% vs B&H {avg_bh:+.1f}%")
    print(f"  → MDD 평균: 스윙 {np.mean([r['swing_mdd'] for r in rows]):.1f}% vs LOC {np.mean([r['loc_mdd'] for r in rows]):.1f}%")
    print(f"\n  ⚠️ 해석: 스윙 우위가 특정 시작 시점(예: 고점 부근)에만 나타나면 윈도우 선택 효과(운)일 뿐."
          f"\n     시작 dd 와 승자 패턴을 함께 보라 — 고점 시작( dd≈0 ) → 스윙, 저점 시작( dd 음수 큼 ) → LOC.")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="장기 축적형 매수 조건 비교: LOC_DCA(시그마) vs 스윙(ATH 하락 구간) — 40% 매도 제외")
    ap.add_argument("--ticker", default=DEFAULT_TICKER)
    ap.add_argument("--since", default=None,
                    help=f"시뮬레이션 시작일 (기본: 최근 5년 = {TEST_END - pd.Timedelta(days=1826)} ~ {TEST_END})")
    ap.add_argument("--end", default=None, help=f"종료일 (기본 {TEST_END})")
    ap.add_argument("--budget", type=float, default=DEFAULT_BUDGET,
                    help=f"총 예산 $ (기본 {DEFAULT_BUDGET:,.0f} — 두 전략 동일)")
    ap.add_argument("--fee", type=float, default=DEFAULT_FEE,
                    help=f"매수 수수료 (기본 {DEFAULT_FEE} = 0.1%% — 두 전략 동일)")
    ap.add_argument("--swing-per-cycle", type=float, default=0.0,
                    help="스윙: 새 ATH 사이클마다 신규 자금 $N 투입 (기본 0 = 총 예산 내 재투입, 예산 소진 시 중단)")
    ap.add_argument("--swing-zones", type=int, default=None,
                    help="스윙 구간 수 (기본 = swing_config 설정의 7구간). 예: 20 → -15%%~-72%% 3%% 스텝 20분할 래더")
    ap.add_argument("--rolling", action="store_true",
                    help="롤링 윈도우 강건성 검증 — 시작 시점을 바꿔가며 반복 비교 (--rolling-window/--rolling-step)")
    ap.add_argument("--rolling-window", type=float, default=5.0,
                    help="--rolling 윈도우 길이(년, 기본 5)")
    ap.add_argument("--rolling-step", type=int, default=6,
                    help="--rolling 윈도우 시작 간격(개월, 기본 6)")
    ap.add_argument("--sweep-zones", action="store_true",
                    help="스윙 구간 수 스윕 — 몇 차까지 나눠야 자금이 안 노는지 판정 (--sweep-counts)")
    ap.add_argument("--sweep-counts", default=None,
                    help="스윕으로 검사할 구간/분할 수 목록 (기본: --sweep-zones 3,5,7,10,12,15,20 · "
                         "--sweep-splits 1,5,10,20,52 — 문서 수치와 동일한 세트)")
    ap.add_argument("--sweep-splits", action="store_true",
                    help="LOC 분할 수 스윕 — 1년 매수 + 홀딩 구조에서 몇 차 분할이 최적인지 (--buy-years/--sweep-counts)")
    ap.add_argument("--buy-years", type=float, default=1.0,
                    help="--sweep-splits 매수 기간(년, 기본 1 — 이후 홀딩)")
    args = ap.parse_args()

    end = date.fromisoformat(args.end) if args.end else TEST_END
    since = date.fromisoformat(args.since) if args.since else (end - timedelta(days=1826))
    args.end_default = (end - timedelta(days=1826))   # 스윕의 '최근 5년' 윈도우 시작일

    print(f"📥 {args.ticker} 데이터 다운로드 ({DATA_START} → {end.isoformat()})...")
    df = load_ohlc(args.ticker, end)

    # ── 설정 로드 (단일 소스) ──
    loc_cfg = load_config(args.ticker)
    zones = load_swing_zones(args.swing_zones)
    if args.swing_zones:
        print(f"🔧 스윙 구간 {len(zones)}개로 확장: -{zones[0]}% ~ -{zones[-1]}% (스텝 {zones[1]-zones[0]}%)")

    # ── LOC 분할 수 스윕 모드 (1년 매수 + 홀딩) ──
    if args.sweep_splits:
        # 기본 1,5,10,20,52 — README 의 수치(1분할 +438% … 52분할 +345%)가 이 세트 기준이므로 재현성 유지
        args.sweep_counts = args.sweep_counts or "1,5,10,20,52"
        run_split_sweep(df, args, loc_cfg)
        return

    # ── 스윙 구간 수 스윕 모드 (자금 유휴 문제 해결) ──
    if args.sweep_zones:
        args.sweep_counts = args.sweep_counts or "3,5,7,10,12,15,20"
        run_zone_sweep(df, args, loc_cfg)
        return

    # ── 롤링 강건성 검증 모드 ──
    if args.rolling:
        run_rolling(df, args, loc_cfg, zones)
        return

    w0 = int(np.argmax(df.index >= pd.Timestamp(since)))
    if w0 < LOOKBACK_DAYS:
        print(f"❌ --since {since} 가 데이터 워밍업({LOOKBACK_DAYS}일)보다 앞입니다.")
        return
    wstart = df.index[w0].date()
    wend = df.index[-1].date()
    years = (df.index[-1] - df.index[w0]).days / 365.25

    # ── LOC (portfolio_config.json 단일 소스 — 승수/분할) ──
    loc = simulate_loc(df, w0, args.budget, loc_cfg["splits"],
                       loc_cfg["entry_multiplier"], args.fee)

    # ── 스윙 (swing_config.json 단일 소스 — 구간) ──
    swing = simulate_swing(df, w0, args.budget, zones, args.fee, args.swing_per_cycle)

    # ── 기준선 ──
    bh = simulate_buyhold(df, w0, args.budget, args.fee)

    print(f"\n{'═' * 88}")
    print(f"  매수 조건 비교 — {args.ticker} · {wstart} ~ {wend} ({years:.1f}년) · 예산 ${args.budget:,.0f} · 수수료 {args.fee*100:.2f}%")
    reinvest_desc = (f" + 사이클당 ${args.swing_per_cycle:,.0f} 신규 투입" if args.swing_per_cycle > 0
                     else " (총 예산 내 재투입 — 예산 소진 시 중단)")
    print(f"  모델: 40% 매도 제외 무매도 축적 | 스윙 = ATH -{zones[0]}~-{zones[-1]}% {len(zones)}구간 "
          f"(3% 래더) 사이클(+1% ATH 리셋) 재투입{reinvest_desc}")
    print(f"{'═' * 88}")
    print(f"  {'전략':<28} {'총수익률':>9} {'MDD':>8} {'Sharpe':>7} {'Calmar':>7} {'매수':>5} "
          f"{'총투입':>9} {'잔여현금':>9} {'최종가치':>11}")
    print("  " + "-" * 84)

    def row(name: str, r: dict) -> None:
        print(f"  {name:<28} {r['total_return']:>+8.1f}% {r['mdd']:>7.1f}% {r['sharpe']:>7.2f} "
              f"{r['calmar']:>7.1f} {r['buys']:>5} ${r['total_spent']:>7,.0f} "
              f"${r['cash']:>8,.0f} ${r['final_value']:>9,.0f}")

    row(f"LOC_DCA (시그마 ×{loc_cfg['entry_multiplier']}, {loc_cfg['splits']}분할)", loc)
    row(f"스윙 (ATH 구간, {len(zones)}구간 × 사이클 재투입)", swing)
    row("Buy & Hold (기준선)", bh)
    print("  " + "-" * 84)

    winner = "스윙" if swing["total_return"] > loc["total_return"] else "LOC_DCA"
    print(f"\n  → 총수익률: {'스윙' if swing['total_return'] > loc['total_return'] else 'LOC_DCA'} 우위 "
          f"(스윙 {swing['total_return']:+.1f}% vs LOC {loc['total_return']:+.1f}%)")
    print(f"  → MDD: {'스윙' if swing['mdd'] > loc['mdd'] else 'LOC_DCA'} 우위 "
          f"(스윙 {swing['mdd']:.1f}% vs LOC {loc['mdd']:.1f}%)")
    print(f"  → Sharpe: {'스윙' if swing['sharpe'] > loc['sharpe'] else 'LOC_DCA'} 우위 "
          f"(스윙 {swing['sharpe']:.2f} vs LOC {loc['sharpe']:.2f})")
    print(f"\n  📌 매수 조건 판정: {winner} 매수 조건이 이 기간 장기 적립에 더 유리")

    # ── 상세 ──
    print_zone_detail(swing)
    loc_log = loc["buy_log"]
    if loc_log:
        avg_loc = float(np.mean([b["price"] for b in loc_log]))
        dep = f" · 분할 소진 {loc['deployed_at'].date()}" if loc.get("deployed_at") is not None else ""
        print(f"\n  ── LOC 매수 상세 ({len(loc_log)}회) ──")
        print(f"  최초 {loc_log[0]['date'].date()} @ ${loc_log[0]['price']:,.2f} · "
              f"최후 {loc_log[-1]['date'].date()} @ ${loc_log[-1]['price']:,.2f} · "
              f"평균 ${avg_loc:,.2f}{dep} — 최종 종가 ${loc['last_close']:,.2f}")
    print(f"\n  ⚠️ 참고: 스윙은 종가 도달 기준(확정 종가), LOC는 당일 저가 기준(장중 지정가) — "
          f"각 실전 엔진의 판정 규칙을 그대로 사용했습니다.\n")


if __name__ == "__main__":
    main()
