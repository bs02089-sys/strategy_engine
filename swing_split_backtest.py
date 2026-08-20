#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
swing_split_backtest.py — 세븐 스플릿 하락 구간(스텝)/매도 목표 수익률 최적화 백테스트 (TQQQ 스윙 알리미용)
==========================================================================================================

현재 실전 설정 (swing_config.json):
  - 매수 구간: ATH 대비 -15% ~ -33%, 3% 스텝 7구간 (15/18/21/24/27/30/33)
  - 계좌 1~7 각 $500 (세븐 스플릿), 매도 목표: 매수가 대비 +40% (SWING_TARGET_PCT, 2026-08-15 전환)

이 백테스트의 질문:
  "첫 구간 -15%, 7분할 고정"에서 하락 스텝을 얼마로 잡아야 기회 비용(미투자 캐시)이
  적게 나는가?  — 예: 스텝 3% → -15/-18/-21/-24/-27/-30/-33 (-33% 종료),
                   스텝 5% → 기존 -15/-20/-25/-30/-35/-40/-45 (-45% 종료)

모델 규칙 (실전 엔진 swing_alerter.py 판정 규칙과 일치):
  - ATH = 원시 고가(High, 미조정) 기준 롤링 역대 최고가 (엔진 get_ath 와 동일 기준 — Google Finance high52와 동일)
  - 하락률 DD = 종가/ATH - 1, 구간 도달 판정은 **확정 종가** 기준 (엔진 동일 — 실시간 값 미사용)
  - 구간 도달(종가 ≤ 구간가) 시 해당 계좌가 $amount 매수 — 같은 날 여러 구간 동시 도달 가능
  - 매도: 종가 ≥ 매수가 × (1 + target%) → 전량 매도 (회수 현금으로 같은 날 신규 구간 매수 가능)
  - 계좌는 매도 후에만 같은 구간 재매수 가능 (엔진 bought 플래그와 동일 — 미매도 구간 중복 매수 없음)
  - 수수료: --fee (기본 0.1%, 매수/매도 각각)

부가 분석:
  - TQQQ 하락률 빈도표 — 각 깊이(-15/-20/.../-80%) 도달 일수/에피소드
  - ^IXIC(나스닥 종합) 동일 빈도표 — "최근 10년 나스닥 -30% 드묾" 주장을 TQQQ(3배 레버리지)와 대조

사용법:
  python3 swing_split_backtest.py                     # 스텝 1~6% 스윕 + 비교
  python3 swing_split_backtest.py --steps 3           # 특정 스텝만 스윕
  python3 swing_split_backtest.py --targets 10,15,20,25,30  # 매도 목표 수익률 스윕 (하락 스텝은 --step 고정)
  python3 swing_split_backtest.py --grid               # 2차원 그리드 — 스텝(--steps) × 목표(--targets) 동시 스윕
  python3 swing_split_backtest.py --grid --steps 2,3,4 --targets 30,40,50  # 원하는 조합만
  python3 swing_split_backtest.py --detail 3 5        # 원하는 스텝 상세 리포트
  python3 swing_split_backtest.py --all               # 상세에 전체 거래 로그 포함
  python3 swing_split_backtest.py --no-index          # 나스닥 비교 생략 (다운로드 절약)
"""
import argparse
from datetime import date, timedelta

import numpy as np
import pandas as pd
import yfinance as yf

DEFAULT_TICKER = "TQQQ"
INDEX_TICKER = "^IXIC"     # 나스닥 종합 — 유튜브 영상 주장(-30% 드묾) 검증용
AMOUNT = 500.0             # 계좌당 예산 (swing_config 기준)
DEFAULT_SINCE = (date.today() - timedelta(days=3650)).isoformat()   # 최근 10년


def fetch_closes(ticker: str) -> tuple[np.ndarray, pd.DatetimeIndex]:
    """배당 조정 종가 시계열 — 백테스트 전용 ATH/DD 판정 기준.
    엔진(swing_alerter.py)은 원시 고가(High) 기준이지만, 백테스트는 종가(Close) 기준으로
    일관된 내부 비교를 유지한다 (ARCHARLES 거래는 종가 기준)."""
    raw = yf.download(ticker, period="max", auto_adjust=True, progress=False)
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    s = raw["Close"].dropna()
    if s.index.tz is not None:
        s.index = s.index.tz_localize(None)
    s = s[~s.index.duplicated(keep="last")].sort_index()
    return s.to_numpy(dtype=float), s.index


def window_slice(closes: np.ndarray, dates: pd.DatetimeIndex, since: str) -> tuple[int, np.ndarray, pd.DatetimeIndex, float]:
    """백테스트 윈도우 시작 인덱스/슬라이스 + 윈도우 진입 시점 ATH."""
    ts0 = pd.Timestamp(since)
    idx0 = int(np.argmax(dates >= ts0)) if (dates >= ts0).any() else 0
    ath0 = float(closes[:idx0].max()) if idx0 > 0 else float(closes[0])
    return idx0, closes[idx0:], dates[idx0:], ath0


def simulate(closes: np.ndarray, dates: pd.DatetimeIndex, since: str,
             start_pct: float, step_pct: float, splits: int,
             target_pct: float, amount: float, fee_rate: float) -> dict:
    """하락 구간 래더 백테스트 1케이스. 지표 dict 반환."""
    zones = [start_pct + i * step_pct for i in range(splits)]
    idx0, cw, dw, ath = window_slice(closes, dates, since)
    n = len(cw)
    cash = amount * splits
    lots: list[dict] = []
    equity = np.empty(n)
    cash_series = np.empty(n)
    buy_log: list[tuple] = []
    sell_log: list[tuple] = []
    zone_buys = {z: 0 for z in zones}
    zone_px: dict[float, list[float]] = {z: [] for z in zones}
    zone_hold: dict[float, list[int]] = {z: [] for z in zones}
    for i in range(n):
        c = float(cw[i])
        if c > ath:
            ath = c
        # 1) 매도 먼저 — 회수 현금으로 같은 날 신규 구간 매수 가능 (자본 회전)
        keep = []
        for l in lots:
            if c >= l["bp"] * (1 + target_pct / 100.0) - 1e-9:
                proceeds = l["sh"] * c * (1 - fee_rate)
                cash += proceeds
                zone_hold[l["zone"]].append(i - l["i"])
                sell_log.append((dw[i], l["zone"], l["bp"], c, proceeds))
            else:
                keep.append(l)
        lots = keep
        # 2) 구간 매수 — 종가 ≤ 구간가 (같은 날 여러 구간 동시 도달 가능)
        open_zones = {l["zone"] for l in lots}
        for z in zones:
            if z in open_zones or cash < amount - 1e-9:
                continue
            if c <= ath * (1 - z / 100.0) + 1e-9:
                sh = amount * (1 - fee_rate) / c
                cash -= amount
                lots.append({"zone": z, "bp": c, "sh": sh, "i": i})
                zone_buys[z] += 1
                zone_px[z].append(c)
                buy_log.append((dw[i], z, c, amount))
        cash_series[i] = cash
        equity[i] = cash + sum(l["sh"] for l in lots) * c

    initial = amount * splits
    final_val = float(equity[-1])
    total_ret = (final_val / initial - 1) * 100
    peak = np.maximum.accumulate(equity)
    mdd = float(((equity - peak) / peak).min() * 100)
    daily_ret = equity[1:] / equity[:-1] - 1
    sd = float(daily_ret.std())
    sharpe = float(np.sqrt(252) * daily_ret.mean() / sd) if sd > 0 and n > 2 else 0.0
    avg_cash = float(cash_series.mean())
    avg_equity = float(equity.mean())
    holds = [h for hs in zone_hold.values() for h in hs]
    buys = sum(zone_buys.values())
    return {
        "step": step_pct, "target": target_pct, "zones": zones, "n": n,
        "window_start": dw[0].date(), "window_end": dw[-1].date(),
        "final_value": final_val, "total_return": total_ret, "mdd": mdd,
        "sharpe": sharpe, "buys": buys, "sells": len(sell_log),
        "avg_cash": avg_cash,
        "cash_ratio": avg_cash / avg_equity * 100 if avg_equity > 0 else 0.0,
        "avg_hold_days": float(np.mean(holds)) if holds else None,
        "total_invested": buys * amount,
        "zone_buys": zone_buys, "zone_px": zone_px, "zone_hold": zone_hold,
        "buy_log": buy_log, "sell_log": sell_log,
    }


def depth_frequency(closes: np.ndarray, dates: pd.DatetimeIndex, since: str,
                    depths: list[float]) -> tuple[list[dict], float]:
    """윈도우 내 하락률 깊이별 도달 일수/에피소드 + 최대 하락률."""
    idx0, cw, dw, ath0 = window_slice(closes, dates, since)
    aths = np.maximum.accumulate(np.concatenate(([ath0], cw)))[1:]
    dd = cw / aths - 1
    rows = []
    for d in depths:
        below = dd <= -d / 100.0
        days = int(below.sum())
        episodes = int((below[1:] & ~below[:-1]).sum()) + (1 if below[0] else 0)
        rows.append({"depth": d, "days": days, "pct": days / len(dd) * 100, "episodes": episodes})
    return rows, float(dd.min() * 100)


def print_depth_table(title: str, rows: list[dict], max_dd: float) -> None:
    print(f"\n  [하락률 빈도 — {title}]  최대 하락률 {max_dd:.1f}%")
    print(f"  {'깊이':>6}  {'도달 일수':>10}  {'비율':>7}  {'에피소드':>8}")
    for r in rows:
        print(f"  {r['depth']:>5.0f}%  {r['days']:>10,}  {r['pct']:>6.1f}%  {r['episodes']:>8}")


def print_detail_report(r: dict, args, header: str) -> None:
    """1케이스 상세 리포트 — 구간별 매수횟수/평균매수가/평균보유일 (+ --all 시 거래 로그)."""
    zs = r["zones"]
    print(f"\n{'═' * 76}")
    print(f"  상세 — {header} · {r['window_start']} ~ {r['window_end']}")
    print(f"{'═' * 76}")
    print(f"  최종가치 ${r['final_value']:,.0f} · 총수익률 {r['total_return']:+.1f}% · MDD {r['mdd']:.1f}% · "
          f"Sharpe {r['sharpe']:.2f}")
    print(f"  매수 {r['buys']}회 / 매도 {r['sells']}회 · 평균 보유 {r['avg_hold_days']:.0f}일 · "
          f"평균 현금 ${r['avg_cash']:,.0f} (캐시비율 {r['cash_ratio']:.1f}%)")
    print(f"  {'구간':>6} {'매수횟수':>8} {'평균매수가':>10} {'평균보유일':>10}")
    for z in zs:
        px = r["zone_px"][z]
        hd = r["zone_hold"][z]
        avg_px = float(np.mean(px)) if px else float("nan")
        avg_hd = float(np.mean(hd)) if hd else float("nan")
        print(f"  -{z:>4.0f}% {r['zone_buys'][z]:>8} {avg_px:>10,.2f} {avg_hd:>9.0f}일")
    if args.all:
        print("\n  -- 거래 로그 --")
        for (d, z, px, amt) in r["buy_log"]:
            print(f"  {d.date()}  BUY  -{z:.0f}%  @ ${px:,.2f}  ${amt:,.0f}")
        for (d, z, bp, px, pr) in r["sell_log"]:
            print(f"  {d.date()}  SELL -{z:.0f}%  @ ${px:,.2f}  (매수 ${bp:,.2f} → 회수 ${pr:,.2f})")


def run_step_sweep(closes: np.ndarray, dates: pd.DatetimeIndex, args,
                   steps: list[float], bh_final: float, bh_ret: float, bh_mdd: float) -> None:
    """하락 스텝 스윕 (기존) — 매도 목표는 --target 고정."""
    print(f"\n{'═' * 76}")
    print(f"  세븐 스플릿 하락 스텝 스윕 — {args.ticker} · {args.since} 이후")
    print(f"  첫 구간 -{args.start:.0f}% · {args.splits}분할 · 계좌당 ${args.amount:.0f} · 매도 목표 +{args.target:.0f}% · 수수료 {args.fee*100:.1f}%")
    print(f"{'═' * 76}")

    results = [simulate(closes, dates, args.since, args.start, s, args.splits,
                        args.target, args.amount, args.fee) for s in steps]

    hdr = (f"  {'스텝':>5} {'마지막구간':>10} {'최종가치':>10} {'총수익률':>9} {'MDD':>7} "
           f"{'Sharpe':>7} {'매수/매도':>9} {'평균현금':>9} {'캐시비율':>8}")
    print(hdr)
    print("  " + "-" * 74)
    for r in results:
        mark = " ◀현재" if r["step"] == 5.0 else (" ◀제안" if r["step"] == 3.0 else "")
        last_zone = -r["zones"][-1]
        print(f"  {r['step']:>4.0f}% {last_zone:>9.0f}% "
              f"{r['final_value']:>10,.0f} {r['total_return']:>+8.1f}% {r['mdd']:>6.1f}% "
              f"{r['sharpe']:>7.2f} {r['buys']:>4}/{r['sells']:<4} "
              f"${r['avg_cash']:>7,.0f} {r['cash_ratio']:>7.1f}%{mark}")
    best = max(results, key=lambda r: r["total_return"])
    print(f"\n  [참고] 전액 매수 후 보유: 최종 ${bh_final:,.0f} (수익 {bh_ret:+.1f}%) · MDD {bh_mdd:.1f}%")
    print(f"  → 총수익률 최고: 스텝 {best['step']:.0f}% (마지막 구간 -{best['zones'][-1]:.0f}%)")
    print(f"  → 캐시비율 = 평균현금/평균총자산 — 이 전략은 동시 최대 {args.splits}×${args.amount:.0f}만 주식 보유,"
          f"\n     사이클 수익은 현금으로 쌓이므로 캐시비율이 높을수록 기회 비용이 큼 (보유와 대조 필요)")

    # 상세 리포트 — 기본은 제안(3) + 현재(5), 총수익률 최고 스텝 자동 포함
    detail_steps = list(args.detail) if args.detail is not None else [3.0, 5.0]
    if best["step"] not in detail_steps:
        detail_steps.append(best["step"])
    for step in detail_steps:
        r = next((x for x in results if x["step"] == step), None)
        if r is None:
            continue
        header = f"스텝 {step:.0f}% (구간: " + "/".join(f"-{z:.0f}" for z in r["zones"]) + ")"
        print_detail_report(r, args, header)


def run_target_sweep(closes: np.ndarray, dates: pd.DatetimeIndex, args,
                     targets: list[float], bh_final: float, bh_ret: float, bh_mdd: float) -> None:
    """매도 목표 수익률 스윕 — 하락 스텝(--step) 고정, 목표 수익률만 변화."""
    step = args.step
    print(f"\n{'═' * 76}")
    print(f"  매도 목표 수익률 스윕 — {args.ticker} · {args.since} 이후 · 하락 스텝 {step:.0f}% 고정")
    print(f"  첫 구간 -{args.start:.0f}% · {args.splits}분할 · 계좌당 ${args.amount:.0f} · 수수료 {args.fee*100:.1f}%")
    print(f"{'═' * 76}")

    results = [simulate(closes, dates, args.since, args.start, step, args.splits,
                        t, args.amount, args.fee) for t in targets]

    hdr = (f"  {'목표':>5} {'최종가치':>10} {'총수익률':>9} {'MDD':>7} {'Sharpe':>7} "
           f"{'매수/매도':>9} {'평균보유':>7} {'캐시비율':>8}")
    print(hdr)
    print("  " + "-" * 72)
    best_ret = max(results, key=lambda r: r["total_return"])
    best_sharpe = max(results, key=lambda r: r["sharpe"])
    for r in results:
        t = r["target"]
        mark = ""
        if abs(t - args.target) < 1e-9:
            mark = " ◀현재"
        elif r is best_ret:
            mark = " ◀최적(수익률)"
        elif r is best_sharpe:
            mark = " ◀최적(Sharpe)"
        hold = f"{r['avg_hold_days']:>5.0f}일" if r["avg_hold_days"] is not None else f"{'미도달':>7}"
        print(f"  +{t:>4.0f}% {r['final_value']:>10,.0f} {r['total_return']:>+8.1f}% {r['mdd']:>6.1f}% "
              f"{r['sharpe']:>7.2f} {r['buys']:>4}/{r['sells']:<4} {hold} "
              f"{r['cash_ratio']:>7.1f}%{mark}")
    print(f"\n  [참고] 전액 매수 후 보유: 최종 ${bh_final:,.0f} (수익 {bh_ret:+.1f}%) · MDD {bh_mdd:.1f}%")
    print(f"  → 총수익률 최고: +{best_ret['target']:.0f}% (수익 {best_ret['total_return']:+.1f}% · MDD {best_ret['mdd']:.1f}% · "
          f"Sharpe {best_ret['sharpe']:.2f} · 매도 {best_ret['sells']}회)")
    print(f"  → Sharpe 최고: +{best_sharpe['target']:.0f}% (Sharpe {best_sharpe['sharpe']:.2f} · 수익 {best_sharpe['total_return']:+.1f}% · "
          f"MDD {best_sharpe['mdd']:.1f}% · 매도 {best_sharpe['sells']}회)")
    print(f"  → 목표↑ = 매도 도달 어려움(보유↑·미실현 위험) / 목표↓ = 회전↑(수수료·재진입 위험) — "
          f"단일 최고점은 과최적화 위험이 있으므로 주변 ±5%p 구간 중 취향 선택 권장")

    # 상세 리포트 — 기본: 현재(--target) + 수익률 최고 + Sharpe 최고 (중복 제외)
    detail_targets = [args.target]
    if args.detail is not None:
        detail_targets = [float(t) for t in args.detail]
    for r in (best_ret, best_sharpe):
        if r["target"] not in detail_targets:
            detail_targets.append(r["target"])
    for t in detail_targets:
        r = next((x for x in results if abs(x["target"] - t) < 1e-9), None)
        if r is None:
            continue
        header = f"목표 +{r['target']:.0f}% (스텝 {r['step']:.0f}% · 구간: " + "/".join(f"-{z:.0f}" for z in r["zones"]) + ")"
        print_detail_report(r, args, header)


def run_grid_sweep(closes: np.ndarray, dates: pd.DatetimeIndex, args,
                   steps: list[float], targets: list[float],
                   bh_final: float, bh_ret: float, bh_mdd: float) -> None:
    """2차원 그리드 — 하락 스텝 × 매도 목표 수익률 동시 스윕 (--grid).

    스텝/목표를 따로 최적화하면 상호작용(예: 좁은 스텝 + 높은 목표)을 놓친다.
    행 = 스텝, 열 = 목표, 셀 = 총수익률%/MDD% — 행 최고에 ★, 현재 실전 설정(3%×+40%)에 ◀.
    """
    print(f"\n{'═' * 76}")
    print(f"  2차원 그리드 — 하락 스텝 × 매도 목표 수익률 — {args.ticker} · {args.since} 이후")
    print(f"  첫 구간 -{args.start:.0f}% · {args.splits}분할 · 계좌당 ${args.amount:.0f} · 수수료 {args.fee*100:.1f}%")
    print(f"  행 = 하락 스텝, 열 = 매도 목표 — ★ 행 최고 · ◀ 현재 실전 설정(스텝 {args.step:.0f}% × +{args.target:.0f}%)")
    print(f"{'═' * 76}")

    grid: dict[tuple[float, float], dict] = {}
    for s in steps:
        for t in targets:
            grid[(s, t)] = simulate(closes, dates, args.since, args.start, s, args.splits,
                                    t, args.amount, args.fee)

    def _print_matrix(title: str, pick, fmt: str) -> None:
        """행별 최고값에 ★, 현재 실전 설정 셀에 ◀ 를 붙인 매트릭스 출력."""
        print(f"\n  [{title}]  (★ = 행 최고)")
        print(f"  {'스텝':>5}" + "".join(f"{f'+{t:.0f}%':>8}" for t in targets))
        for s in steps:
            vals = [pick(grid[(s, t)]) for t in targets]
            best = max(vals)
            cells = []
            for t, v in zip(targets, vals):
                is_cur = abs(s - args.step) < 1e-9 and abs(t - args.target) < 1e-9
                mark = "◀" if is_cur else ("★" if v == best else "")
                cells.append(f"{fmt.format(v=v):>7}" + mark)
            print(f"  {s:>5g}%" + "".join(cells))

    _print_matrix("총수익률 % (★ = 행에서 수익률 최고)", lambda r: r["total_return"], "{v:+.1f}")
    _print_matrix("MDD % (★ = 행에서 덜 하락)", lambda r: r["mdd"], "{v:.1f}")

    allr = list(grid.values())
    best_ret = max(allr, key=lambda r: r["total_return"])
    best_sharpe = max(allr, key=lambda r: r["sharpe"])
    best_mdd = max(allr, key=lambda r: r["mdd"])
    cur = grid.get((args.step, args.target))
    print(f"\n  [참고] 전액 매수 후 보유: 최종 ${bh_final:,.0f} (수익 {bh_ret:+.1f}%) · MDD {bh_mdd:.1f}%")
    print(f"  → 전체 최고 총수익률: 스텝 {best_ret['step']:g}% × 목표 +{best_ret['target']:g}% "
          f"(+{best_ret['total_return']:.1f}% · MDD {best_ret['mdd']:.1f}% · Sharpe {best_ret['sharpe']:.2f})")
    print(f"  → 전체 최고 Sharpe: 스텝 {best_sharpe['step']:g}% × 목표 +{best_sharpe['target']:g}% "
          f"(Sharpe {best_sharpe['sharpe']:.2f} · +{best_sharpe['total_return']:.1f}% · MDD {best_sharpe['mdd']:.1f}%)")
    print(f"  → 전체 최저 MDD(덜 하락): 스텝 {best_mdd['step']:g}% × 목표 +{best_mdd['target']:g}% "
          f"(MDD {best_mdd['mdd']:.1f}% · +{best_mdd['total_return']:.1f}% · Sharpe {best_mdd['sharpe']:.2f})")
    if cur is not None:
        n = len(allr)
        by_ret = sorted(allr, key=lambda r: -r["total_return"])
        by_sharpe = sorted(allr, key=lambda r: -r["sharpe"])
        by_mdd = sorted(allr, key=lambda r: -r["mdd"])
        rank_ret = next(i for i, r in enumerate(by_ret, 1) if r is cur)
        rank_sharpe = next(i for i, r in enumerate(by_sharpe, 1) if r is cur)
        rank_mdd = next(i for i, r in enumerate(by_mdd, 1) if r is cur)
        print(f"  → 현재 실전 설정(스텝 {args.step:g}% × +{args.target:g}%): "
              f"+{cur['total_return']:.1f}% · MDD {cur['mdd']:.1f}% · Sharpe {cur['sharpe']:.2f} "
              f"— 순위 {rank_ret}/{n}(수익률) · {rank_sharpe}/{n}(Sharpe) · {rank_mdd}/{n}(MDD)")

    # 플래토 판정 — 최고 수익률 대비 --plateau-pct(%p) 이내 AND MDD --plateau-mdd 이상(덜 하락)
    best_val = best_ret["total_return"]
    pl = sorted((r for r in allr if best_val - r["total_return"] <= args.plateau_pct
                 and r["mdd"] >= args.plateau_mdd), key=lambda r: -r["total_return"])
    if pl:
        pl_steps = sorted({r["step"] for r in pl})
        pl_targets = sorted({r["target"] for r in pl})
        print(f"  → 플래토 판정({len(pl)}/{len(allr)} 조합): 최고 수익률 대비 -{args.plateau_pct:g}%p 이내 "
              f"+ MDD {args.plateau_mdd:g}% 이상")
        print(f"    경계: 스텝 {pl_steps[0]:g}% ~ {pl_steps[-1]:g}% × 목표 +{pl_targets[0]:g}% ~ +{pl_targets[-1]:g}%")
        print(f"    조합: " + ", ".join(f"({r['step']:g}%·+{r['target']:g}%)" for r in pl))

    # 상세 — 전체 최고 수익률 조합 + 현재 실전 설정 (중복 제외)
    detail_rs = []
    if best_ret not in detail_rs:
        detail_rs.append(best_ret)
    if cur is not None and cur not in detail_rs:
        detail_rs.append(cur)
    for r in detail_rs:
        header = f"스텝 {r['step']:g}% × 목표 +{r['target']:g}% (구간: " + \
            "/".join(f"-{z:g}" for z in r["zones"]) + ")"
        print_detail_report(r, args, header)


def main() -> None:
    ap = argparse.ArgumentParser(description="세븐 스플릿 하락 구간(스텝)/매도 목표 수익률 최적화 백테스트 — TQQQ 스윙 알리미용")
    ap.add_argument("--ticker", default=DEFAULT_TICKER)
    ap.add_argument("--since", default=DEFAULT_SINCE, help=f"백테스트 시작일 (기본: 최근 10년 = {DEFAULT_SINCE})")
    ap.add_argument("--start", type=float, default=15.0, help="첫 구간 하락률 %% (기본 15 = -15%%)")
    ap.add_argument("--splits", type=int, default=7, help="분할 수 (기본 7 = 세븐 스플릿)")
    ap.add_argument("--target", type=float, default=40.0, help="매도 목표 수익률 %% (기본 40 = 현재 실전 설정)")
    ap.add_argument("--step", type=float, default=3.0, help="--targets 스윕 시 고정 하락 스텝 %% (기본 3 = 현재 실전 설정)")
    ap.add_argument("--amount", type=float, default=AMOUNT, help="계좌당 매수 금액 $ (기본 500)")
    ap.add_argument("--fee", type=float, default=0.001, help="왕복 수수료 (기본 0.001 = 0.1%%)")
    ap.add_argument("--steps", default="1,2,3,4,5,6", help="스윕할 스텝 목록 (기본 1,2,3,4,5,6)")
    ap.add_argument("--targets", default=None,
                    help="스윕할 매도 목표 수익률 목록 (예: 10,15,20,25,30 — 지정 시 하락 스텝은 --step 고정)")
    ap.add_argument("--grid", action="store_true",
                    help="2차원 그리드 — 하락 스텝(--steps) × 매도 목표(--targets) 동시 스윕"
                         " (--targets 미지정 시 기본 10,15,20,25,30,40,50)")
    ap.add_argument("--plateau-pct", type=float, default=5.0,
                    help="--grid 플래토 판정: 최고 수익률 대비 이만큼(%%p) 이내 조합만 (기본 5)")
    ap.add_argument("--plateau-mdd", type=float, default=-36.0,
                    help="--grid 플래토 판정: MDD 가 이 값 이상(덜 하락)인 조합만 (기본 -36%%)")
    ap.add_argument("--detail", type=float, nargs="*", default=None,
                    help="상세 리포트 값 (기본: 스텝 스윕은 3/5, 목표 스윕은 현재+최고)")
    ap.add_argument("--all", action="store_true", help="상세 리포트에 전체 거래 로그 포함")
    ap.add_argument("--no-index", action="store_true", help="나스닥(^IXIC) 비교 생략")
    args = ap.parse_args()

    steps = [float(s) for s in args.steps.split(",") if s.strip()]
    if not steps:
        print("❌ --steps 가 비어 있습니다.")
        return

    print(f"📥 {args.ticker} 데이터 다운로드 (배당 조정 종가, 최대 기간)...")
    closes, dates = fetch_closes(args.ticker)
    print(f"   데이터 범위: {dates[0].date()} ~ {dates[-1].date()} ({len(dates)} 거래일)")

    # 기준선: 전액 매수 후 보유 (참고용 — 스윕 차원과 무관)
    idx0, cw, dw, _ = window_slice(closes, dates, args.since)
    bh_shares = args.amount * args.splits * (1 - args.fee) / float(cw[0])
    bh_peak = np.maximum.accumulate(cw)
    bh_mdd = float(((cw - bh_peak) / bh_peak).min() * 100)
    bh_final = bh_shares * float(cw[-1])
    bh_ret = (bh_final / (args.amount * args.splits) - 1) * 100

    if args.grid:
        grid_targets = ([float(t) for t in args.targets.split(",") if t.strip()]
                        if args.targets is not None else [10.0, 15.0, 20.0, 25.0, 30.0, 40.0, 50.0])
        run_grid_sweep(closes, dates, args, steps, grid_targets, bh_final, bh_ret, bh_mdd)
    elif args.targets is not None:
        targets = [float(t) for t in args.targets.split(",") if t.strip()]
        if not targets:
            print("❌ --targets 가 비어 있습니다.")
            return
        run_target_sweep(closes, dates, args, targets, bh_final, bh_ret, bh_mdd)
    else:
        run_step_sweep(closes, dates, args, steps, bh_final, bh_ret, bh_mdd)

    # 깊이 빈도표 — TQQQ
    t_depths = [15, 18, 20, 21, 24, 27, 30, 33, 35, 40, 45, 50, 60, 70, 80]
    rows, max_dd = depth_frequency(closes, dates, args.since, t_depths)
    print_depth_table(f"{args.ticker} (3배 레버리지)", rows, max_dd)

    # 깊이 빈도표 — 나스닥 (영상 주장 검증)
    if not args.no_index:
        try:
            icloses, idates = fetch_closes(INDEX_TICKER)
            i_depths = [10, 15, 20, 25, 30, 35, 40, 45]
            irows, imax_dd = depth_frequency(icloses, idates, args.since, i_depths)
            print_depth_table(f"{INDEX_TICKER} (나스닥 종합)", irows, imax_dd)
            print("\n  ⚠️  참고: TQQQ 는 나스닥 3배 레버리지 — TQQQ -30% 는 나스닥 약 -11% 내외 하락과"
                  "\n       대응한다. '나스닥 -30% 는 드물다'는 주장을 TQQQ 구간 깊이에 그대로 적용하면"
                  "\n       안 된다 (TQQQ 는 -45% 이상 하락을 실제로 여러 번 경험 — 빈도표 확인).")
        except Exception as e:  # noqa: BLE001
            print(f"\n  ⚠️ {INDEX_TICKER} 다운로드 실패 — 나스닥 비교 생략: {e}")

    print()


if __name__ == "__main__":
    main()
