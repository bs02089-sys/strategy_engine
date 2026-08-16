#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dollar_split_backtest.py — 달러(USD/KRW) '매직 스플릿' 매매 전략 백테스트
================================================================================

질문 출처: 박성현 『매직 스플릿』 인용 —
  "1년 동안 달러가 0.3%~0.7% 오르면 매수하고 0.3%~0.5% 하락하면 매도하여
   97% 수익률을 달성했다. 달러를 사서 평균 0.5% 수익이 모여 복리효과가
   더해지면 100% 이상의 수익도 가능해진다."

이 백테스트의 질문:
  1) 위 규칙을 USD/KRW(원달러) 일봉에 그대로 적용하면 97% 연수익이 실제로 나오는가?
  2) 은행 환전 스프레드(수수료)를 반영하면 얼마나 깎이는가?
  3) 바이앤홀드(달러 보유)와 비교하면 어떠한가?

모델 (일봉 OHLC 기반 — 장중 경로는 알 수 없으므로 아래처럼 근사):
  - 기본 해석 (--mode intraday, 풀백 매도 — '평균 0.5% 수익' 주장과 정합):
      * 기준가 ref = 전일 종가
      * 매수: 당일 고가가 ref 대비 +0.3% 이상 오르고(0.3~0.7% 밴드,
              --no-band 로 상한 필터 해제) → ref×1.003 에서 매수
              (단, 당일 시가가 이미 그 위면 시가에서 체결 — 더 높은 가격이 현실적)
      * 매도: 보유 중 신고가(peak) 대비 -0.3% 하락하면 peak×0.997 에서 매도
              (0.3~0.5% 풀백 — '매수 후 작은 되돌림에서 수익 확정' 구조)
  - 대안 해석 (--mode close): 일별 종가 등락률 기준
      * --direction up    (인용 그대로): +0.3~0.7% 상승일 종가 매수 / -0.3~0.5% 하락일 종가 매도
      * --direction down  (세븐 스플릿 정통 — 환율 하락 시 분할매수):
                            -0.3~0.5% 하락일 종가 매수 / +0.3~0.7% 상승일 종가 매도

비용:
  - --spread: 왕복 환전 스프레드 (기본 0% = 나무 멤버스 환전 우대 100%, 직접 환전 — 2026-08-17
    사용자 앱 확인). 매수/매도 각각 절반씩 적용. 95% 우대(왕복 0.1%) 비교는 --spread 0.1 로.
    ※ 나무증권 스프레드는 기준환율의 1% (편도) — 95% 우대 시 편도 0.05%, 100% 우대 시 0.

데이터:
  - yfinance USDKRW=X 일봉 (2003-12-01 ~ 오늘, 야후 원달러 시장환율).
    ※ 은행 매매기준율과 소폭 다를 수 있음 (마감 시점/스프레드 정책 차이).

실전 유의점 (백테스트 결과와 무관하게):
  - 한국 개인은 외환거래 연간 한도(5만 달러 수준)가 적용될 수 있음 — 은행 확인 필요.
  - 외환시장은 주말에도 열려 yfinance 일봉에 토/일 바가 포함될 수 있음 (미국 장 마감 기준).
  - 은행 현찰 환전은 전신환보다 스프레드가 큼 — 전신환/증권사 환전 우대 계좌 권장.

사용법:
  python3 dollar_split_backtest.py                      # 기본: 인트라데이 그리드 + 스프레드 스윕 + 연도별 표
  python3 dollar_split_backtest.py --since 2004-01-01   # 전체 20년+ 검증
  python3 dollar_split_backtest.py --grid               # 2차원 그리드 — 매수 하락률 × 익절 상승률 최적화
  python3 dollar_split_backtest.py --grid --drops 0.2,0.3,0.5 --targets 0.5,1.0,1.5  # 원하는 조합만
  python3 dollar_split_backtest.py --split              # 분할 진입(래더) × 로트별 익절 비교 (단일 vs 3분할)
  python3 dollar_split_backtest.py --mode close         # 종가 기준 해석 비교
  python3 dollar_split_backtest.py --mode close --direction down  # 세븐 스플릿 정통(하락 매수)
  python3 dollar_split_backtest.py --all                # 기본 설정 거래 로그 포함
"""
import argparse
from datetime import date, timedelta

import numpy as np
import pandas as pd
import yfinance as yf

TICKER = "USDKRW=X"
DEFAULT_SINCE = (date.today() - timedelta(days=3650)).isoformat()   # 최근 10년 (프로젝트 기본)
INITIAL = 1_000_000.0   # 가상 초기 자산(원) — 비율 기준이라 금액 무관
SPREAD_DEFAULT = 0.0    # 왕복 0% = 나무 멤버스 환전 우대 100% (직접 환전 — 2026-08-17 사용자 앱 확인)
                        # 비교: 95% 우대(왕복 0.1%)는 --spread 0.1 로 확인 (CAGR +4.1% · 회당 +0.40%)


def fetch_ohlc(ticker: str) -> pd.DataFrame:
    """USD/KRW 일봉 OHLC — tz 제거 + 중복 제거 + 정렬."""
    raw = yf.download(ticker, period="max", auto_adjust=True, progress=False)
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    df = raw[["Open", "High", "Low", "Close"]].dropna()
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    # 데이터 글리치 방어 (2026-08-17 확인) — yfinance USDKRW=X 에 고가 오류 바 1건:
    # 2008-03-17 High=21,353 (실제 ~988). 고저비 >15% 는 σ 0.6% 환율 일봉에서 비현실적
    # → High 를 max(Open, Close) 로 보정. 익절/LOC 판정이 고가에 의존하므로 허위 체결 방지.
    # (기존 실전 설정 0.3%/0.3% 결과에는 영향 없음 — 392회·CAGR +5.2% 동일 확인)
    bad = df["High"] / df["Low"] > 1.15
    if bad.any():
        df.loc[bad, "High"] = np.maximum(df.loc[bad, "Open"], df.loc[bad, "Close"])
    return df


def window_slice(df: pd.DataFrame, since: str) -> pd.DataFrame:
    """백테스트 윈도우 — since 이후만."""
    ts0 = pd.Timestamp(since)
    idx0 = int(np.argmax(df.index >= ts0)) if (df.index >= ts0).any() else 0
    return df.iloc[idx0:]


def simulate_intraday(df: pd.DataFrame, direction: str, band_lo: float, band_hi: float,
                      exit_pct: float, spread: float, band: bool,
                      initial: float = INITIAL) -> dict:
    """인트라데이 근사 — 일봉 OHLC로 장중 트리거를 흉내 낸다.

    direction 'up'   (인용 그대로): 기준가(ref=전일 종가) 대비 +band_lo~+band_hi 상승 시
                      매수 → 보유 중 신고가(peak) 대비 -exit_pct 풀백 시 매도.
    direction 'down' (세븐 스플릿 정통 — 환율 하락 시 분할매수): ref 대비 -band_lo~-band_hi
                      하락 시 매수 → 매수가 대비 +exit_pct 상승(익절) 시 매도.

    체결 가격은 보수적으로 — 진입은 시가가 이미 트리거 위(아래)면 시가, 매도는 지정가(트리거가).
    """
    closes = df["Close"].to_numpy(float)
    highs = df["High"].to_numpy(float)
    lows = df["Low"].to_numpy(float)
    opens = df["Open"].to_numpy(float)
    dates = df.index
    n = len(df)
    cash = initial
    units = 0.0
    peak = 0.0
    entry_px = 0.0
    entry_i = -1
    trades: list[tuple] = []        # (매도일, 매수가, 매도가, 순수익률, 보유일)
    equity = np.empty(n)
    for i in range(n):
        o, h, l, c = opens[i], highs[i], lows[i], closes[i]
        ref = closes[i - 1] if i > 0 else c
        if units == 0:
            if direction == "up":
                hit = h >= ref * (1 + band_lo) and (not band or h <= ref * (1 + band_hi))
                buy_px = max(o, ref * (1 + band_lo))
            else:
                hit = l <= ref * (1 - band_lo) and (not band or l >= ref * (1 - band_hi))
                buy_px = min(o, ref * (1 - band_lo))
            if hit:
                units = cash / (buy_px * (1 + spread / 2))
                cash = 0.0
                entry_px = buy_px
                peak = buy_px
                entry_i = i
        else:
            if direction == "up":
                peak = max(peak, h)
                exit_px = peak * (1 - exit_pct)
                hit = l <= exit_px
            else:
                exit_px = entry_px * (1 + exit_pct)
                hit = h >= exit_px
            if hit:
                proceeds = units * exit_px * (1 - spread / 2)
                cash = proceeds
                net = (exit_px * (1 - spread / 2)) / (entry_px * (1 + spread / 2)) - 1
                trades.append((dates[i], entry_px, exit_px, net, i - entry_i))
                units = 0.0
        equity[i] = cash + units * c
    open_trade = (dates[entry_i], entry_px, float(closes[-1]),
                  units * float(closes[-1]) * (1 - spread / 2)) if units > 0 else None
    return {"equity": equity, "dates": dates, "trades": trades,
            "open_trade": open_trade, "params": dict(direction=direction,
                                                      band_lo=band_lo, exit_pct=exit_pct)}


def simulate_close(df: pd.DataFrame, direction: str, spread: float,
                   initial: float = INITIAL) -> dict:
    """종가 기준 해석 — direction 'up' = 인용 그대로(상승 매수/하락 매도),
    'down' = 세븐 스플릿 정통(하락 매수/상승 매도). 등락률 밴드는 인용 수치 고정."""
    buy_lo, buy_hi = 0.003, 0.007     # 상승 밴드
    sell_lo, sell_hi = 0.003, 0.005   # 하락 밴드
    closes = df["Close"].to_numpy(float)
    dates = df.index
    n = len(df)
    cash = initial
    units = 0.0
    entry_px = 0.0
    entry_i = -1
    trades: list[tuple] = []
    equity = np.empty(n)
    for i in range(n):
        c = closes[i]
        ref = closes[i - 1] if i > 0 else c
        ret = c / ref - 1 if ref > 0 else 0.0
        if units == 0:
            if direction == "up":
                hit = buy_lo <= ret <= buy_hi
            else:
                hit = -sell_hi <= ret <= -sell_lo
            if hit:
                units = cash / (c * (1 + spread / 2))
                cash = 0.0
                entry_px = c
                entry_i = i
        else:
            if direction == "up":
                hit = -sell_hi <= ret <= -sell_lo
            else:
                hit = buy_lo <= ret <= buy_hi
            if hit:
                proceeds = units * c * (1 - spread / 2)
                cash = proceeds
                net = (c * (1 - spread / 2)) / (entry_px * (1 + spread / 2)) - 1
                trades.append((dates[i], entry_px, c, net, i - entry_i))
                units = 0.0
        equity[i] = cash + units * c
    open_trade = (dates[entry_i], entry_px, float(closes[-1]),
                  units * float(closes[-1]) * (1 - spread / 2)) if units > 0 else None
    return {"equity": equity, "dates": dates, "trades": trades,
            "open_trade": open_trade, "params": dict(direction=direction)}


def summarize(res: dict, initial: float = INITIAL) -> dict:
    """자산 곡선 → 성과 지표."""
    eq = res["equity"]
    dates = res["dates"]
    trades = res["trades"]
    n = len(eq)
    final = float(eq[-1])
    years = max((dates[-1] - dates[0]).days / 365.25, 1e-9)
    cagr = ((final / initial) ** (1 / years) - 1) * 100
    total_ret = (final / initial - 1) * 100
    peak = np.maximum.accumulate(eq)
    mdd = float(((eq - peak) / peak).min() * 100)
    rets = eq[1:] / eq[:-1] - 1
    sd = float(rets.std())
    sharpe = float(np.sqrt(252) * rets.mean() / sd) if sd > 0 and n > 2 else 0.0
    wins = [t for t in trades if t[3] > 0]
    win_rate = len(wins) / len(trades) * 100 if trades else 0.0
    avg_profit = float(np.mean([t[3] for t in trades]) * 100) if trades else 0.0
    avg_hold = float(np.mean([t[4] for t in trades])) if trades else 0.0
    return {"final": final, "total_ret": total_ret, "cagr": cagr, "mdd": mdd,
            "sharpe": sharpe, "trades": len(trades), "win_rate": win_rate,
            "avg_profit": avg_profit, "avg_hold": avg_hold, "open": res["open_trade"]}


def annual_returns(res: dict) -> pd.Series:
    """연도별 수익률(%) — 연말 자산 기준."""
    s = pd.Series(res["equity"], index=res["dates"])
    yr = s.resample("YE").last().dropna()
    return yr.pct_change().dropna() * 100


def buy_and_hold(df: pd.DataFrame, initial: float = INITIAL) -> dict:
    """기준선 — 전액 달러 보유 (환전 수수료 1회성 스프레드 반영)."""
    c = df["Close"].to_numpy(float)
    n = len(c)
    final = initial / (c[0] * (1 + SPREAD_DEFAULT / 2)) * (c[-1] * (1 - SPREAD_DEFAULT / 2))
    years = max((df.index[-1] - df.index[0]).days / 365.25, 1e-9)
    cagr = ((final / initial) ** (1 / years) - 1) * 100
    total_ret = (final / initial - 1) * 100
    peak = np.maximum.accumulate(c)
    mdd = float(((c - peak) / peak).min() * 100)
    rets = c[1:] / c[:-1] - 1
    sd = float(rets.std())
    sharpe = float(np.sqrt(252) * rets.mean() / sd) if sd > 0 else 0.0
    return {"final": final, "total_ret": total_ret, "cagr": cagr, "mdd": mdd, "sharpe": sharpe}


def fmt_pct(x: float) -> str:
    return f"{x:+.1f}%"


def print_matrix(title: str, rows: list[float], cols: list[float],
                 grid: dict, pick, fmt: str, mark_key: tuple | None = None,
                 col_label: str = "매도 풀백") -> None:
    """진입(행) × 매도(열) 매트릭스 — 행 최고 ★, 기본 설정 ◀."""
    print(f"\n  [{title}]  (★ = 행 최고)")
    print("  " + "".join(f"{f'{c:.1f}%':>10}" for c in cols))
    for r in rows:
        vals = [pick(grid[(r, c)]) for c in cols]
        best = max(vals)
        cells = []
        for c, v in zip(cols, vals):
            mark = ""
            if mark_key is not None and abs(r - mark_key[0]) < 1e-9 and abs(c - mark_key[1]) < 1e-9:
                mark = " ◀기본"
            elif v == best:
                mark = " ★"
            cells.append(f"{fmt.format(v=v):>8}{mark}")
        print(f"  {r:.1f}% 진입" + "".join(cells))
    print(f"  {'':>9}" + "".join(f"{col_label:>10}" for _ in cols))


def print_year_table(yearly: pd.Series, bh_yearly: pd.Series, threshold: float = 97.0) -> None:
    """연도별 전략/바이앤홀드 수익률 표 — threshold(97%) 이상 연도 강조."""
    print(f"\n  [연도별 수익률]  (★ = {threshold:.0f}% 이상 달성 연도)")
    print(f"  {'연도':>6}  {'전략':>9}  {'바이앤홀드':>10}")
    for yr, v in yearly.items():
        bh = bh_yearly.get(yr, float("nan"))
        mark = " ★" if v >= threshold else ""
        print(f"  {yr.year:>6}  {v:>+8.1f}%  {bh:>+9.1f}%{mark}")


def run_intraday_block(w: pd.DataFrame, direction: str, rows: list[float], cols: list[float],
                       band_hi_pct: float, base_lo: float, base_exit: float,
                       spread: float, band_on: bool, bh: dict, args, title: str,
                       exit_label: str) -> None:
    """방향별 인트라데이 블록 — 진입×매도 그리드 + 스프레드 스윕 + 연도별 표 + 결론.

    rows/cols/base_lo/base_exit/band_hi_pct 는 모두 %% 단위 (예: 0.3 = 0.3%%).
    direction 'up'   = 인용 그대로(상승 매수 / 풀백 매도)
    direction 'down' = 세븐 스플릿 정통(하락 매수 / 익절 매도)
    """
    band_desc = "밴드 필터 적용" if band_on else "밴드 필터 해제(--no-band)"
    print(f"\n  ── {title} ──")
    print(f"  진입 밴드 {rows[0]:.1f}~{band_hi_pct:.1f}% · 매도 {exit_label} {base_exit:.1f}% · "
          f"스프레드 {args.spread:.2f}% · {band_desc}")

    grid = {(lo, ex): summarize(simulate_intraday(w, direction, lo / 100.0, band_hi_pct / 100.0,
                                                  ex / 100.0, spread, band=band_on))
            for lo in rows for ex in cols}
    col_label = "매도 풀백" if direction == "up" else "익절 상승"
    print_matrix("총수익률 % (★ = 행 최고)", rows, cols, grid,
                 lambda r: r["total_ret"], "{v:+.1f}", (base_lo, base_exit), col_label)
    print_matrix("MDD % (★ = 행에서 덜 하락)", rows, cols, grid,
                 lambda r: r["mdd"], "{v:.1f}", (base_lo, base_exit), col_label)

    print(f"\n  [스프레드 스윕] 기본 설정(진입 {base_lo:.1f}% · {exit_label} {base_exit:.1f}%) 고정 — "
          f"왕복 스프레드별 성과 (바이앤홀드 CAGR {bh['cagr']:+.1f}%)")
    print(f"  {'스프레드':>8}  {'총수익률':>9}  {'CAGR':>7}  {'MDD':>7}  {'거래수':>7}  {'승률':>6}  {'회당평균':>8}")
    for sp in [0.0, 0.05, 0.1, 0.5, 1.0]:
        s = summarize(simulate_intraday(w, direction, base_lo / 100.0, band_hi_pct / 100.0,
                                        base_exit / 100.0, sp / 100.0, band=band_on))
        mark = " ◀기본" if abs(sp - args.spread) < 1e-9 else ""
        print(f"  {sp:>7.2f}%  {s['total_ret']:>+8.1f}%  {s['cagr']:>+6.1f}%  {s['mdd']:>6.1f}%  "
              f"{s['trades']:>7}  {s['win_rate']:>5.1f}%  {s['avg_profit']:>+7.2f}%{mark}")

    base = simulate_intraday(w, direction, base_lo / 100.0, band_hi_pct / 100.0,
                             base_exit / 100.0, spread, band=band_on)
    s = summarize(base)
    print(f"\n  [기본 설정 상세] {title} · 스프레드 {args.spread:.2f}%")
    print(f"  총수익률 {s['total_ret']:+.1f}% · CAGR {s['cagr']:+.1f}% · MDD {s['mdd']:.1f}% · "
          f"Sharpe {s['sharpe']:.2f}")
    print(f"  거래 {s['trades']}회 · 승률 {s['win_rate']:.1f}% · 회당 평균 {s['avg_profit']:+.2f}% · "
          f"평균 보유 {s['avg_hold']:.0f}일")
    if s["open"]:
        d0, bp, cp, val = s["open"]
        print(f"  ⚠️ 미청산 포지션: {d0.date()} 매수 @{bp:,.2f}원 → 현재 {cp:,.2f}원 (평가 {val:,.0f}원)")

    yearly = annual_returns(base)
    bh_yr = pd.Series(w["Close"].to_numpy(float), index=w.index).resample("YE").last().dropna()
    print_year_table(yearly, bh_yr.pct_change().dropna() * 100)
    best_years = yearly[yearly >= 97.0]
    n97 = len(best_years)
    if n97:
        print(f"  [결론] 97% 이상 연도: {n97}개 — "
              + ", ".join(f"{yr.year}년 {v:+.1f}%" for yr, v in best_years.items()))
    else:
        print(f"  [결론] 97% 이상 연도: 0개 — 97% 수익률 주장 재현 실패")
    verdict = "전략 우위" if s["cagr"] > bh["cagr"] else "바이앤홀드 우위"
    print(f"  전략 CAGR {s['cagr']:+.1f}% vs 바이앤홀드 CAGR {bh['cagr']:+.1f}% → {verdict}")
    if s["avg_profit"] >= 0.35:
        match = "'평균 0.5%' 주장에 근접 (스프레드 0%면 정확히 +0.50%)"
    else:
        match = "'평균 0.5%' 주장과 거리 있음"
    print(f"  ⚠️ 회당 평균 {s['avg_profit']:+.2f}% — {match} (97%는 특정 해의 큰 스윙·낮은 스프레드에 의존)")

    if args.all:
        print("\n  -- 거래 로그 --")
        for (d, bp, spx, net, hold) in base["trades"]:
            print(f"  {d.date()}  SELL  매수 {bp:,.2f} → 매도 {spx:,.2f}  ({net*100:+.2f}% · {hold}일)")


def simulate_ladder(df: pd.DataFrame, buy_levels: list[float], sell_levels: list[float],
                    spread: float, initial: float = INITIAL) -> dict:
    """분할 진입(래더) + 로트별 익절 시뮬레이터.

    - buy_levels: 전일 종가 대비 하락% 목록 (오름차순, 예: [0.3, 0.6, 0.9]).
      각 레벨에서 예산 1/N 씩 지정가(전일종가×(1−레벨))로 진입 — 당일 저가가 레벨
      이하로 내려가면 체결. 단, 기존 밴드(급락 제외)와 동일하게 레벨+0.2%p 보다 깊은
      급락일은 그 레벨을 스킵 (예: -1% 급락일엔 -0.3/-0.6 레벨 스킵, -0.9~-1.1 구간만
      체결) — 급락일 칼날 매수 방지 (simulate_intraday 의 band 와 동일 원칙).
    - sell_levels: 로트별 익절 상승% (각 로트는 자기 매수가 대비 이 % 도달 시 개별 매도).
      길이가 buy_levels 보다 짧으면 마지막 값으로 나머지 채움 (swing LOTS 구조와 동일).
    - spread: 왕복 스프레드 (매수/매도 각 절반).
    전 로트 청산(사이클 완료) 시 재무장 — 다음 진입부터 새 사이클.
    """
    closes = df["Close"].to_numpy(float)
    highs = df["High"].to_numpy(float)
    lows = df["Low"].to_numpy(float)
    opens = df["Open"].to_numpy(float)
    dates = df.index
    n = len(df)
    nl = len(buy_levels)
    sell_lv = (sell_levels + [sell_levels[-1]] * nl)[:nl]
    cash = initial
    lots: list[dict] = []          # 체결 순서대로 {units, entry, date}
    trades: list[tuple] = []       # (매도일, 매수가, 매도가, 순수익률, 보유일)
    next_lv = 0                    # 다음 진입 레벨 (로트 부분 매도 후에도 재사용 금지)
    cycle_budget = initial / nl    # 사이클 예산 = 사이클 시작 시점 현금 ÷ 분할 수 (수익 재투자)
    equity = np.empty(n)
    for i in range(n):
        o, h, l, c = opens[i], highs[i], lows[i], closes[i]
        ref = closes[i - 1] if i > 0 else c
        # 0) 새 사이클 시작(전 로트 미보유) → 예산 갱신 (복리: 이전 사이클 수익 포함)
        if not lots and next_lv == 0:
            cycle_budget = cash / nl
        # ① 진입 — next_lv 부터, 당일 저가가 레벨 트리거 이하이고 밴드(레벨+0.2%p) 이상인 레벨
        #    (급락일엔 깊은 레벨만 체결 — 칼날 매수 방지, simulate_intraday 의 band 와 동일 원칙)
        j = next_lv
        while j < nl and l <= ref * (1 - buy_levels[j] / 100.0) \
                and l >= ref * (1 - (buy_levels[j] + 0.2) / 100.0):
            px = min(o, ref * (1 - buy_levels[j] / 100.0))
            lots.append({"units": cycle_budget / (px * (1 + spread / 2)),
                         "entry": px, "date": i})
            cash -= cycle_budget
            next_lv += 1
            j += 1
        # ② 익절 — 각 로트가 자기 목표(매수가 대비 +sell_lv) 도달 시 개별 매도
        #    (당일 매수분은 제외 — 다음 날부터 익절 체크, simulate_intraday 와 동일 보수성)
        remaining: list[dict] = []
        for idx, lot in enumerate(lots):
            if lot["date"] == i:
                remaining.append(lot)
                continue
            tgt = lot["entry"] * (1 + sell_lv[idx] / 100.0)
            if h >= tgt:
                proceeds = lot["units"] * tgt * (1 - spread / 2)
                cash += proceeds
                net = (tgt * (1 - spread / 2)) / (lot["entry"] * (1 + spread / 2)) - 1
                trades.append((dates[i], lot["entry"], tgt, net, i - lot["date"]))
            else:
                remaining.append(lot)
        lots = remaining
        # ③ 전 로트 청산(사이클 완료) → 재무장
        if not lots:
            next_lv = 0
        equity[i] = cash + sum(lot["units"] * c for lot in lots)
    open_val = sum(lot["units"] * float(closes[-1]) * (1 - spread / 2) for lot in lots)
    open_trade = (dates[lots[0]["date"]], lots[0]["entry"], float(closes[-1]),
                  open_val) if lots else None
    return {"equity": equity, "dates": dates, "trades": trades,
            "open_trade": open_trade, "params": dict(buy=buy_levels, sell=sell_lv)}


def run_ladder_compare(w: pd.DataFrame, spread: float, bh: dict, args) -> None:
    """분할 진입/분할 청산 비교 — 단일 vs 3분할 래더 (--split)."""
    print(f"\n{'═' * 78}")
    print(f"  분할 진입 × 로트별 익절 비교 — {w.index[0].date()} ~ {w.index[-1].date()} · 스프레드 {args.spread:.2f}%")
    print(f"  단일(전액 1회) vs 3분할 래더(1/3씩) — 각 로트는 자기 매수가 기준 익절")
    print(f"{'═' * 78}")
    configs = [
        ("단일 진입 × +0.3% 익절 (현재 실전)", [0.3], [0.3]),
        ("단일 진입 × +0.5% 익절 (구 실전)", [0.3], [0.5]),
        ("3분할 진입 × 로트별 +0.3%", [0.3, 0.6, 0.9], [0.3]),
        ("3분할 진입 × 계단 +0.3/+0.5/+0.7%", [0.3, 0.6, 0.9], [0.3, 0.5, 0.7]),
        ("3분할 진입 × 로트별 +0.5%", [0.3, 0.6, 0.9], [0.5]),
    ]
    print(f"  {'설정':<34}  {'총수익률':>8}  {'CAGR':>6}  {'MDD':>6}  {'Sharpe':>6}  {'거래':>5}  {'평균보유':>7}  {'미청산':>5}")
    for name, buys, sells in configs:
        s = summarize(simulate_ladder(w, buys, sells, spread))
        open_n = "O" if s["open"] else "-"
        print(f"  {name:<34}  {s['total_ret']:>+7.1f}%  {s['cagr']:>+5.1f}%  {s['mdd']:>5.1f}%  "
              f"{s['sharpe']:>5.2f}  {s['trades']:>5}  {s['avg_hold']:>6.0f}일  {open_n:>5}")
    print(f"\n  [참고] 바이앤홀드: CAGR {bh['cagr']:+.1f}% · MDD {bh['mdd']:.1f}%")


def run_dollar_grid(w: pd.DataFrame, spread: float, bh: dict, args, drops: list[float],
                    targets: list[float]) -> None:
    """2차원 그리드 — 매수 하락률 × 익절 상승률 동시 스윕 (--grid).

    박성현 숫자(0.3%/0.5%) 전제 없이 전 구간을 탐색한다. 방향은 하락 매수/익절 매도
    고정, 밴드(급락 제외) 상한 = 트리거 + 0.2%p. 현재 실전 설정(0.3% × +0.3%)이
    그리드 셀로 포함된다. 셀 = 총수익률/MDD/Sharpe/평균 보유일.
    """
    print(f"\n{'═' * 78}")
    print(f"  2차원 그리드 — 매수 하락률 × 익절 상승률 — {w.index[0].date()} ~ {w.index[-1].date()}")
    print(f"  스프레드 {args.spread:.2f}% · 밴드(급락 제외) = 트리거 +0.2%p · 방향: 하락 매수 / 익절 매도")
    print(f"  행 = 매수 하락%, 열 = 익절 상승% — ★ 행 최고 · ◀ 현재 실전 설정(0.3% × +0.3%)")
    print(f"{'═' * 78}")

    grid: dict[tuple[float, float], dict] = {}
    for d in drops:
        for t in targets:
            res = simulate_intraday(w, "down", d / 100.0, (d + 0.2) / 100.0,
                                    t / 100.0, spread, band=True)
            s = summarize(res)
            holds = [tr[4] for tr in res["trades"]]
            s["median_hold"] = float(np.median(holds)) if holds else 0.0
            s["quick3"] = sum(1 for h in holds if h <= 3) / len(holds) * 100 if holds else 0.0
            s["calmar"] = s["cagr"] / abs(s["mdd"]) if s["mdd"] < 0 else 0.0
            s["drop"], s["target"] = d, t
            grid[(d, t)] = s

    def _print_matrix(title: str, pick, fmt: str, low_best: bool = False) -> None:
        """행별 최고값에 ★, 현재 실전 설정 셀에 ◀ 를 붙인 매트릭스 출력."""
        print(f"\n  [{title}]  (★ = 행 최고)")
        print(f"  {'하락':>5}" + "".join(f"{f'+{t:.1f}%':>8}" for t in targets))
        for d in drops:
            vals = [pick(grid[(d, t)]) for t in targets]
            best = min(vals) if low_best else max(vals)
            cells = []
            for t, v in zip(targets, vals):
                is_cur = abs(d - 0.3) < 1e-9 and abs(t - 0.3) < 1e-9
                mark = "◀" if is_cur else ("★" if v == best else "")
                cells.append(f"{fmt.format(v=v):>7}" + mark)
            print(f"  {d:>5g}%" + "".join(cells))

    _print_matrix("총수익률 % (★ = 행 최고)", lambda s: s["total_ret"], "{v:+.1f}")
    _print_matrix("MDD % (★ = 행에서 덜 하락)", lambda s: s["mdd"], "{v:.1f}")
    _print_matrix("Sharpe (★ = 행 최고)", lambda s: s["sharpe"], "{v:.2f}")
    _print_matrix("평균 보유 일수 (★ = 행에서 최단)", lambda s: s["avg_hold"], "{v:.0f}", low_best=True)

    allr = list(grid.values())
    best_ret = max(allr, key=lambda r: r["total_ret"])
    best_sharpe = max(allr, key=lambda r: r["sharpe"])
    best_calmar = max(allr, key=lambda r: r["calmar"])
    best_mdd = max(allr, key=lambda r: r["mdd"])
    best_short = min(allr, key=lambda r: r["avg_hold"])
    cur = grid.get((0.3, 0.3))
    print(f"\n  [참고] 바이앤홀드: CAGR {bh['cagr']:+.1f}% · MDD {bh['mdd']:.1f}%")
    print(f"  → 최고 총수익률 : -{best_ret['drop']:g}% × +{best_ret['target']:g}% "
          f"(+{best_ret['total_ret']:.1f}% · MDD {best_ret['mdd']:.1f}% · Sharpe {best_ret['sharpe']:.2f} · 평균 {best_ret['avg_hold']:.0f}일)")
    print(f"  → 최고 Sharpe   : -{best_sharpe['drop']:g}% × +{best_sharpe['target']:g}% "
          f"(Sharpe {best_sharpe['sharpe']:.2f} · +{best_sharpe['total_ret']:.1f}% · MDD {best_sharpe['mdd']:.1f}% · 평균 {best_sharpe['avg_hold']:.0f}일)")
    print(f"  → 최고 Calmar   : -{best_calmar['drop']:g}% × +{best_calmar['target']:g}% "
          f"(Calmar {best_calmar['calmar']:.2f} · +{best_calmar['total_ret']:.1f}% · MDD {best_calmar['mdd']:.1f}% · 평균 {best_calmar['avg_hold']:.0f}일)")
    print(f"  → 최저 MDD      : -{best_mdd['drop']:g}% × +{best_mdd['target']:g}% "
          f"(MDD {best_mdd['mdd']:.1f}% · +{best_mdd['total_ret']:.1f}% · Sharpe {best_mdd['sharpe']:.2f} · 평균 {best_mdd['avg_hold']:.0f}일)")
    print(f"  → 최단 평균 보유: -{best_short['drop']:g}% × +{best_short['target']:g}% "
          f"(평균 {best_short['avg_hold']:.0f}일 · 중앙값 {best_short['median_hold']:.0f}일 · +{best_short['total_ret']:.1f}% · MDD {best_short['mdd']:.1f}%)")
    if cur is not None:
        n = len(allr)
        by_ret = sorted(allr, key=lambda r: -r["total_ret"])
        by_sharpe = sorted(allr, key=lambda r: -r["sharpe"])
        by_hold = sorted(allr, key=lambda r: r["avg_hold"])
        rank_ret = next(i for i, r in enumerate(by_ret, 1) if r is cur)
        rank_sharpe = next(i for i, r in enumerate(by_sharpe, 1) if r is cur)
        rank_hold = next(i for i, r in enumerate(by_hold, 1) if r is cur)
        print(f"  → 현재 실전 설정(0.3% × +0.3%): +{cur['total_ret']:.1f}% · MDD {cur['mdd']:.1f}% · "
              f"Sharpe {cur['sharpe']:.2f} · 평균 보유 {cur['avg_hold']:.0f}일 — "
              f"순위 {rank_ret}/{n}(수익률) · {rank_sharpe}/{n}(Sharpe) · {rank_hold}/{n}(최단보유)")

    # 빠른 회전 균형 — 평균 보유 7일 이하 중 최고 Sharpe ("못 파는 케이스" 회피 관점)
    quick = [r for r in allr if r["avg_hold"] <= 7]
    if quick:
        best_q = max(quick, key=lambda r: r["sharpe"])
        print(f"  → 빠른 회전(평균 보유 ≤7일) 중 최고 Sharpe: -{best_q['drop']:g}% × +{best_q['target']:g}% "
              f"(Sharpe {best_q['sharpe']:.2f} · +{best_q['total_ret']:.1f}% · MDD {best_q['mdd']:.1f}% · "
              f"평균 {best_q['avg_hold']:.0f}일 · 3일 내 청산 {best_q['quick3']:.0f}%)")


def main() -> None:
    ap = argparse.ArgumentParser(description="달러(USD/KRW) '매직 스플릿' 매매 전략 백테스트 — 97% 수익률 주장 검증")
    ap.add_argument("--since", default=DEFAULT_SINCE, help=f"백테스트 시작일 (기본: 최근 10년 = {DEFAULT_SINCE})")
    ap.add_argument("--grid", action="store_true", help="2차원 그리드 — 매수 하락률 × 익절 상승률 동시 스윕 (박성현 숫자 전제 없음)")
    ap.add_argument("--drops", default="0.1,0.2,0.3,0.4,0.5,0.7,1.0,1.5,2.0",
                    help="--grid 매수 하락률 목록 %% (기본: 0.1~2.0)")
    ap.add_argument("--targets", default="0.3,0.5,0.7,1.0,1.5,2.0,3.0",
                    help="--grid 익절 상승률 목록 %% (기본: 0.3~3.0)")
    ap.add_argument("--split", action="store_true",
                    help="분할 진입(래더) × 로트별 익절 비교 — 단일 vs 3분할")
    ap.add_argument("--mode", choices=["intraday", "close"], default="intraday",
                    help="해석 모드 — intraday(기본: 장중 트리거 근사) / close(일별 종가 기준)")
    ap.add_argument("--direction", choices=["up", "down"], default="up",
                    help="--mode close 에서만 사용 — up(인용 그대로: 상승 매수/하락 매도) / down(세븐 스플릿 정통: 하락 매수/상승 매도)")
    ap.add_argument("--entry-lo", type=float, default=0.3, help="인용 그대로(up) 블록의 진입 트리거 하한 %% (기본 0.3)")
    ap.add_argument("--entry-hi", type=float, default=0.7, help="인용 그대로(up) 블록의 진입 밴드 상한 %% (기본 0.7)")
    ap.add_argument("--exit", type=float, default=0.3, help="인용 그대로(up) 블록의 매도 풀백 %% (기본 0.3)")
    ap.add_argument("--spread", type=float, default=SPREAD_DEFAULT * 100,
                    help="왕복 환전 스프레드 %% (기본 0 = 나무 멤버스 100%% 우대 — 비교: 95%% 우대는 0.1)")
    ap.add_argument("--no-band", action="store_true", help="진입 밴드 상한 필터 해제 — 밴드 밖 급등/급락일에도 매수")
    ap.add_argument("--all", action="store_true", help="기본 설정의 거래 로그 포함")
    args = ap.parse_args()

    spread = args.spread / 100.0

    print(f"📥 {TICKER} 데이터 다운로드 (일봉 OHLC, 최대 기간)...")
    df = fetch_ohlc(TICKER)
    w = window_slice(df, args.since)
    print(f"   데이터 범위: {w.index[0].date()} ~ {w.index[-1].date()} ({len(w)} 거래일)")

    # 기준선: 바이앤홀드
    bh = buy_and_hold(w)
    print(f"\n{'═' * 78}")
    print(f"  USD/KRW '매직 스플릿' 백테스트 — {w.index[0].date()} ~ {w.index[-1].date()}")
    print(f"  [참고] 바이앤홀드(달러 보유, 스프레드 1회성 반영): {bh['total_ret']:+.1f}% · CAGR {bh['cagr']:+.1f}% · "
          f"MDD {bh['mdd']:.1f}% · Sharpe {bh['sharpe']:.2f}")
    print(f"{'═' * 78}")

    if args.grid:
        # ── 그리드 최적화: 매수 하락률 × 익절 상승률 (박성현 숫자 전제 없음) ──
        drops = [float(x) for x in args.drops.split(",") if x.strip()]
        targets = [float(x) for x in args.targets.split(",") if x.strip()]
        run_dollar_grid(w, spread, bh, args, drops, targets)
        print()
        return

    if args.split:
        # ── 분할 진입 × 로트별 익절 비교 ──
        run_ladder_compare(w, spread, bh, args)
        print()
        return

    if args.mode == "intraday":
        # ① 인용 그대로: +0.3~0.7% 상승 매수 / -0.3~0.5% 풀백 매도
        run_intraday_block(w, "up", [0.3, 0.5], [0.3, 0.4, 0.5],
                           args.entry_hi, args.entry_lo, args.exit,
                           spread, not args.no_band, bh, args,
                           "인용 그대로: +0.3~0.7% 상승 매수 / -0.3~0.5% 풀백 매도",
                           "풀백")
        # ② 세븐 스플릿 정통: -0.3~0.5% 하락 매수 / +0.3~0.7% 상승 익절 (기본 0.3 — 그리드 최적)
        run_intraday_block(w, "down", [0.3, 0.4], [0.3, 0.5, 0.7],
                           0.5, 0.3, 0.3,
                           spread, not args.no_band, bh, args,
                           "세븐 스플릿 정통: -0.3~0.5% 하락 매수 / +0.3~0.7% 상승 익절",
                           "익절")
    else:
        # ── 종가 기준 해석 비교 (--mode close) ──
        print(f"\n  [모드] 종가 기준 — direction '{args.direction}'"
              f" ({'인용 그대로: 상승 매수/하락 매도' if args.direction == 'up' else '세븐 스플릿 정통: 하락 매수/상승 매도'})"
              f" · 스프레드 {args.spread:.2f}%")
        print(f"  {'방향':>10}  {'총수익률':>9}  {'CAGR':>7}  {'MDD':>7}  {'거래수':>7}  {'승률':>6}  {'회당평균':>8}")
        for direction in (["up", "down"] if args.direction == "up" else ["down", "up"]):
            r = simulate_close(w, direction, spread)
            s = summarize(r)
            mark = " ◀선택" if direction == args.direction else ""
            print(f"  {direction:>10}  {s['total_ret']:>+8.1f}%  {s['cagr']:>+6.1f}%  {s['mdd']:>6.1f}%  "
                  f"{s['trades']:>7}  {s['win_rate']:>5.1f}%  {s['avg_profit']:>+7.2f}%{mark}")
        print(f"\n  [참고] 바이앤홀드 CAGR {bh['cagr']:+.1f}% · MDD {bh['mdd']:.1f}%")
        r = simulate_close(w, args.direction, spread)
        yearly = annual_returns(r)
        bh_yr = pd.Series(w["Close"].to_numpy(float), index=w.index).resample("YE").last().dropna()
        print_year_table(yearly, bh_yr.pct_change().dropna() * 100)

    print()


if __name__ == "__main__":
    main()
