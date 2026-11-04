"""SOXX 닷컴 버블 재현 — SOXL 낙폭 스트레스 테스트 (일회성 분석 도구).

질문: SOXX 가 2000-03~2002-10 닷컴 붕괴(-84%)를 다시 그리면 SOXL 은 어디까지 가는가.

SOXL 은 2010-03 상장이라 그 구간 데이터가 없다 → **^SOX(SOXX 의 모지수, 1994~) 일간 수익률에
3배 일간 리셋**을 적용해 합성한다. 모델은 먼저 SOXL 실측(2010~)으로 검증하고, 총수익률 격차만큼
연간 비용(drag: 보수 + 3배 금융비용)을 실측에 맞춰 보정한다 — 낙폭 비교가 목적이라 차입 비용이
빠진 '무비용' 수치도 함께 출력해 두 값 사이를 신뢰 구간으로 본다.

같은 경로에 ① 가격만(SOXL 이 어디까지 가나) ② 2σ LOC 를 계속 걸었다면(신호가 몇 번 뜨고
기체결 3회 + 추가 체결의 평단·손실은) 을 겹쳐 본다. σ·발동 정의는 알림과 같은
`soxl_tqqq_2sigma_count.signal_frame()` 하나만 쓴다 — 여기서 다시 구현하지 않는다.

⚠️ 전제는 "오늘부터 폭락이 시작된다"(= 오늘이 닷컴 고점)는 가정이다. 실제 SOXX 는 고점 대비
-29% 빠진 자리라 하락 여지가 그만큼 줄었다고도, 버블 붕괴는 -84% 가 바닥이라 더 남았다고도
볼 수 있다. 답은 '가정'이지 예측이 아니다.

상태(기체결 3회)는 상수 하나로 둔다 — 포지션이 바뀌면 ROUNDS_DONE 만 고친다.
사용법: `python3 soxl_dotcom_stress.py`

ponytail: 조회·계산만 하는 일회성 도구 — 알림·워크플로우에 연결하지 않는다.
"""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from soxl_tqqq_2sigma_count import frequency_stats, iso, load_closes, load_settings, signal_frame

LEV = 3.0            # 3배 일간 리셋
ROUNDS_DONE = 3      # 사용자 상태: SOXL·TQQQ 각 3회 매수 완료 (2026-11)
TRADING_DAYS = 252
BUBBLE_SEARCH = ("1999-06-01", "2001-12-31")  # 고점 탐색 창
BUBBLE_END = "2003-12-31"                     # 저점 탐색 창 끝
# 시나리오 표에 찍을 SOXX 환산 낙폭 체크포인트 (저점은 따로)
CHECKPOINTS = (0.20, 0.40, 0.60, 0.80)


# ==================== 데이터·모델 ====================
def money(x: float) -> str:
    """1달러 미만은 소수 4자리 — 저점에서는 센트 표기로는 0.01 이 되어 정보가 사라진다."""
    return f"${x:,.4f}" if x < 1 else f"${x:,.2f}"


def fetch(ticker: str, start: tuple[int, int, int]) -> pd.Series:
    try:
        return load_closes(ticker, datetime(*start))
    except RuntimeError as e:
        raise SystemExit(f"❌ {e}")


def bubble_path(proxy: pd.Series) -> tuple[pd.Timestamp, pd.Timestamp, pd.Series]:
    """프록시 지수의 닷컴 고점·저점과 **고점 다음 날부터의 일간 수익률**."""
    peak = proxy.loc[BUBBLE_SEARCH[0]:BUBBLE_SEARCH[1]].idxmax()
    trough = proxy.loc[peak:BUBBLE_END].idxmin()
    ret = proxy.loc[peak:trough].pct_change(fill_method=None).dropna()
    return peak, trough, ret


def levered(ret: pd.Series, drag: float = 0.0, nav0: float = 1.0) -> pd.Series:
    """3배 일간 리셋 합성 — 매일 `1 + 3r − 연비용/252` 을 곱한다 (복리·변동성 감쇠 포함)."""
    return nav0 * (1.0 + LEV * ret - drag / TRADING_DAYS).cumprod()


def max_drawdown(series: pd.Series) -> float:
    return float((series / series.cummax() - 1.0).min())


def fit_drag(proxy: pd.Series, etf: pd.Series) -> tuple[float, float, float]:
    """실측 ETF 수명 구간으로 합성 3배를 검증·보정.

    반환 = (연간 비용, 일간 수익률 상관, 검증 구간 년수).
    비용은 합성 총성장률과 실측 총성장률의 **연율 로그 격차** — 배당·보수·금융비용이 전부 여기로 모인다.
    """
    ret = proxy.loc[etf.index[0]:etf.index[-1]].pct_change(fill_method=None).dropna()
    model = levered(ret)
    years = len(ret) / TRADING_DAYS
    drag = math.log(float(model.iloc[-1]) / float(etf.iloc[-1] / etf.iloc[0])) / years
    etf_ret = etf.pct_change(fill_method=None).reindex(ret.index).dropna()
    corr = float(np.corrcoef(etf_ret, (ret * LEV).reindex(etf_ret.index))[0, 1])
    return drag, corr, years


def forward_closes(nav0: float, ret: pd.Series, drag: float) -> pd.Series:
    """오늘 종가에서 닷컴 경로를 적용한 합성 종가 — 인덱스는 **오늘 이후 영업일**(표시용)."""
    nav = levered(ret, drag, nav0=nav0)
    index = pd.bdate_range((datetime.now() + timedelta(days=1)).date(), periods=len(nav))
    return pd.Series(nav.to_numpy(), index=index)


# ==================== 2σ LOC 오버레이 ====================
@dataclass
class LOCResult:
    fills: list[tuple[pd.Timestamp, float, float]]  # (시나리오 날짜, 체결가, 그날 2σ 폭)
    executed: list[tuple[pd.Timestamp, float, float]]
    skipped: int                   # 예산 소진 뒤 건너뛴 신호 수
    avg_cost: float                # 기체결 + 추가 체결 평단
    value_at_trough: float         # 저점 평가액 (1회 = 1단위 기준)
    cost: float
    sigma_at_trough: float
    worst_day: float
    year_used: dict[int, int]      # 시나리오 연도별 체결 수 (연간 예산은 해마다 리셋)


def loc_overlay(etf: pd.Series, scenario: pd.Series, window: int, mult: float,
                budget_rounds: int, avg_cost: float, trough_price: float) -> LOCResult:
    """오늘까지의 실제 SOXL 이력으로 σ 를 시드하고, 같은 2σ 규칙으로 시나리오 경로를 판정한다.

    기체결 `ROUNDS_DONE` 회 + 신규 체결은 **회당 같은 금액(1단위)** 으로 계산 — 평단·손실률이
    실제와 같은 구조가 된다(체결가는 LOC 규칙대로 그날 종가).
    예산은 **해마다 리셋**된다 (연간 예산 ÷ 10회 = 회당 금액) — 첫 해만 기체결 3회를 뺀 만큼 쓴다.
    """
    seed = etf.iloc[-(window + 2):]  # 첫 시나리오 날의 σ(20일)가 바로 유효하도록
    frame = signal_frame(pd.concat([seed, scenario]), window, mult)
    fwd = frame.loc[scenario.index]
    fills = [(d, float(fwd.loc[d, "close"]), float(fwd.loc[d, "sigma"]) * mult)
             for d, hit in zip(fwd.index, fwd["fill_hit"]) if hit]
    first_year = scenario.index[0].year
    year_used: dict[int, int] = {}
    executed: list[tuple[pd.Timestamp, float, float]] = []
    for d, price, width in fills:
        room = budget_rounds - ROUNDS_DONE if d.year == first_year else budget_rounds
        if year_used.get(d.year, 0) < room:
            year_used[d.year] = year_used.get(d.year, 0) + 1
            executed.append((d, price, width))

    prices = [avg_cost] * ROUNDS_DONE + [p for _, p, _ in executed]
    shares = sum(1.0 / p for p in prices)
    cost = float(len(prices))
    return LOCResult(
        fills=fills,
        executed=executed,
        skipped=len(fills) - len(executed),
        avg_cost=cost / shares,
        value_at_trough=shares * trough_price,
        cost=cost,
        sigma_at_trough=float(fwd["sigma"].iloc[-1]) * mult,
        worst_day=float(fwd["ret"].min()),
        year_used=year_used,
    )


# ==================== 리포트 ====================
def report(name: str, proxy_name: str, proxy: pd.Series, etf: pd.Series, underlying: pd.Series,
           underlying_name: str, window: int, mult: float) -> None:
    print("=" * 74)
    print(f"[{name}] {underlying_name} 닷컴 붕괴 재현")
    peak, trough, ret = bubble_path(proxy)
    peak_px, trough_px = float(proxy.loc[peak]), float(proxy.loc[trough])
    drop = trough_px / peak_px - 1.0
    days = (trough - peak).days
    last, last_under = float(etf.iloc[-1]), float(underlying.iloc[-1])
    under_dd = last_under / float(underlying.max()) - 1.0
    print(f"  기준        : {iso(etf.index[-1])} {name} ${last:,.2f} · {underlying_name} ${last_under:,.2f} "
          f"(고점 대비 {under_dd * 100:.0f}%)")
    print(f"  프록시 경로 : {proxy_name} {iso(peak)} {peak_px:,.2f} → {iso(trough)} {trough_px:,.2f} "
          f"({drop * 100:.0f}%, {days}일)")

    drag, corr, years = fit_drag(proxy, etf)
    validation_ret = proxy.loc[etf.index[0]:etf.index[-1]].pct_change(fill_method=None).dropna()
    print(f"  모델 검증   : 실측 {years:.1f}년 일간 수익률 상관 {corr:.4f} · "
          f"최대낙폭 합성 {max_drawdown(levered(validation_ret)) * 100:.1f}% "
          f"vs 실측 {max_drawdown(etf) * 100:.1f}%")
    print(f"  비용 보정   : 연 {drag * 100:.1f}% (보수 + 3배 금융비용 + 합성 잔차 — 실측 총수익률에 맞춤)")

    # ---- ① 가격 경로
    print("  " + "-" * 70)
    print(f"  가격 경로 — {underlying_name} 가 오늘부터 닷컴 낙폭을 그대로 반복한다고 가정")
    est_under = last_under * (1.0 + ret).cumprod()          # 지수 경로는 선형(1배)
    est_net = forward_closes(last, ret, drag)               # 3배 일간 리셋 + 비용
    est_raw = forward_closes(last, ret, 0.0)                # 3배 일간 리셋, 무비용

    def row(label: str, i: int) -> None:
        px = float(est_net.iloc[i])
        print(f"    {label:<12}({iso(ret.index[i])}) {underlying_name} {money(float(est_under.iloc[i]))} "
              f"→ {name} {money(px)}  ({px / last - 1.0:+.2%})")

    print(f"    {'오늘':<12}({iso(etf.index[-1])}) {underlying_name} {money(last_under)} "
          f"→ {name} {money(last)}")
    hit: set[int] = set()
    for mark in CHECKPOINTS:
        pos = next((i for i in range(len(est_under)) if float(est_under.iloc[i]) / last_under - 1.0 <= -mark),
                   None)
        if pos is not None and pos not in hit and pos > 0:
            hit.add(pos)
            row(f"{underlying_name} -{mark * 100:.0f}%", pos)
    row(f"저점 {drop * 100:.0f}%", len(ret) - 1)
    print(f"\n  → {name} 저점 {money(float(est_net.iloc[-1]))} = 오늘 대비 "
          f"{float(est_net.iloc[-1]) / last - 1.0:+.2%} "
          f"(무비용 3배 {money(float(est_raw.iloc[-1]))}, "
          f"{float(est_raw.iloc[-1]) / last - 1.0:+.2%})")
    print(f"    최악의 하루 {float(est_net.pct_change().min()) * 100:.1f}% · "
          f"반등 없이 {days}일 (기준 {iso(etf.index[-1])} → 닷컴 {iso(trough)})")

    # ---- ② 2σ LOC
    try:
        budget = int(frequency_stats(name, window, mult, close=etf)["median"])
    except RuntimeError:
        budget = 10
    frame = signal_frame(etf, window, mult)
    real_fills = [(iso(d), float(frame.loc[d, "close"]))
                  for d, h in zip(frame.index, frame["fill_hit"]) if h][-ROUNDS_DONE:]
    real_avg = sum(p for _, p in real_fills) / len(real_fills)
    res = loc_overlay(etf, est_net, window, mult, budget, real_avg, float(est_net.iloc[-1]))

    print("  " + "-" * 70)
    print(f"  2σ LOC 를 계속 걸었다면 (예산 = 1년 {budget}회, 기체결 {ROUNDS_DONE}회)")
    print(f"  · 기체결 {ROUNDS_DONE}회: " + " · ".join(f"{d} ${p:,.2f}" for d, p in real_fills)
          + f"  → 평단 ${real_avg:,.2f} (신호일 종가 기준 근사)")
    first_year = int(est_net.index[0].year)
    allowance = " · ".join(
        f"{y} {n}/{budget - ROUNDS_DONE if y == first_year else budget}회"
        for y, n in sorted(res.year_used.items()))
    print(f"  · 경로 내 신호 {len(res.fills)}회 → {len(res.executed)}회 체결"
          + (f", {res.skipped}회 건너뜀(연간 예산 소진)" if res.skipped else "")
          + f" · 연도별 {allowance}")
    for d, p, width in res.executed:
        print(f"      - {iso(d)} {money(p)} (그날 2σ 폭 {width * 100:.1f}%)")
    print(f"  · 기체결 3회만 보유 시: 평단 ${real_avg:,.2f} → 저점 평가 "
          f"{float(est_net.iloc[-1]) / real_avg - 1.0:+.2%}")
    print(f"  · 예산 소진 후(기체결 + 신규 체결): 평단 {money(res.avg_cost)} · 저점 평가액 "
          f"{money(res.value_at_trough)} / 투입 {res.cost:.0f}단위 = "
          f"{(res.value_at_trough / res.cost - 1.0) * 100:.2f}%")
    print(f"  · 저점 2σ 폭 {res.sigma_at_trough * 100:.1f}% (하루 {res.sigma_at_trough * 100:.1f}% "
          f"급락이어야 트리거) · 최악의 하루 {res.worst_day * 100:.1f}%")
    print()


def main() -> None:
    s = load_settings()
    print(f"\n설정: {s.window}거래일 롤링 σ × {s.multiplier:g} · 3배 일간 리셋 합성 · "
          f"기체결 {ROUNDS_DONE}회 (SOXL·TQQQ 각각)\n")

    sox = fetch("^SOX", (1994, 5, 1))
    ndx = fetch("^NDX", (1994, 5, 1))
    soxl = fetch("SOXL", (2010, 3, 1))
    tqqq = fetch("TQQQ", (2010, 3, 1))
    soxx = fetch("SOXX", (2010, 3, 1))
    qqq = fetch("QQQ", (2010, 3, 1))

    report("SOXL", "^SOX", sox, soxl, soxx, "SOXX", s.window, s.multiplier)
    report("TQQQ", "^NDX", ndx, tqqq, qqq, "QQQ", s.window, s.multiplier)


if __name__ == "__main__":
    main()
