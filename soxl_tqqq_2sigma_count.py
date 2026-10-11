"""SOXL / TQQQ 롤링 2σ 매수 신호 빈도 집계 — 자금 배분 계획용.

설정은 `soxl_tqqq_config.json` 공용 (알림 스크립트 `soxl_2sigma_alert.py` 와 같은 파일).
- σ 창: `rolling_window`(거래일) × `sigma_multiplier` — 지표 정의는 이것뿐.
- 집계 창: `count_years`(년, 기본 1) — 실행 시점부터 거슬러 올라가는 기간.
- ⚠️ `lookback_days`(60) 는 알림 스크립트가 데이터를 **받아오는** 기간이라 여기서는 쓰지 않는다
  (σ 는 최근 20거래일만 보므로 다운로드 창을 늘려도 신호는 달라지지 않는다).

두 기준을 함께 센다 — 둘 다 같은 부등식이지만 "언제 알 수 있는 σ 인가"가 다르다:
- 알림 판정: 당일 수익률 < −(당일까지의 σ × 배수) → `soxl_2sigma_alert.py` 가 매일 찍는 신호.
- 체결(LOC): 당일 종가 ≤ 전일 종가 × (1 − 배수 × 전일까지의 σ) → 주문을 미리 걸어둘 때 실제로 체결되는 날.

롤링 창이 채워지지 않는 첫 ~20거래일은 σ 가 없어 집계에서 빠진다(집계 구간을 함께 출력).

사용법: `python3 soxl_tqqq_2sigma_count.py [집계 년수]` — 인자를 주면 설정값을 덮어쓴다
(예: `... 5` 로 장기 연환산 빈도 확인 — 1년 표본은 사건이 3~8회뿐이라 흔들린다).

ponytail: 조회·집계만 하는 단발 도구 — 알림/워크플로우에 연결하지 않는다.
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import yfinance as yf

ALERT_BASIS = "알림 판정 (당일 σ 포함)"
FILL_BASIS = "체결 기준 (전일까지 σ · 룩어헤드 없음)"  # 자금 배분 비중은 이 기준으로 계산


def load_config() -> dict:
    config_path = Path(__file__).parent / "soxl_tqqq_config.json"
    if not config_path.exists():
        return {}
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def count_signals(ticker: str, days: int, window: int, multiplier: float) -> dict:
    end = datetime.now()
    start = end - timedelta(days=days)
    df = yf.download(
        ticker,
        start=start.strftime("%Y-%m-%d"),
        end=end.strftime("%Y-%m-%d"),
        progress=False,
        multi_level_index=False,  # 단일 종목 → df["Close"] 가 항상 Series (컬럼 구조 변동 차단)
    )
    if df is None or df.empty:
        raise RuntimeError(f"[{ticker}] 데이터를 가져오지 못했습니다.")

    close = df["Close"].dropna()
    ret = close.pct_change()
    sigma = ret.rolling(window).std()

    variants = {ALERT_BASIS: sigma, FILL_BASIS: sigma.shift(1)}
    signals = {}
    for label, sig in variants.items():
        hit = (ret < -sig * multiplier) & sig.notna()
        # ponytail: index[bool Series] 대신 items() 순회 — 부울 인덱싱은 타입 스텁에서 오류로 뜬다
        signals[label] = [d.strftime("%Y-%m-%d") for d, h in hit.items() if h]

    covered = sigma.dropna()  # 롤링 창이 채워진 구간 (앞 ~20거래일 제외)
    if covered.empty:
        raise RuntimeError(f"[{ticker}] 롤링 {window}일 σ 계산에 필요한 데이터가 부족합니다.")
    return {
        "ticker": ticker,
        "rows": len(close),
        # ponytail: .strftime() 대신 f-string 포맷 — Index.min()/max() 는 스텁상 넓은 유니온이라
        # 속성 호출이 편집기에서 오류로 뜨고, 포맷 지정은 어떤 타입에도 통과한다
        "covered": (f"{covered.index.min():%Y-%m-%d}", f"{close.index.max():%Y-%m-%d}"),
        "covered_days": len(covered),
        "avg_sigma_pct": float(covered.mean() * 100),
        "signals": signals,
    }


def print_ticker(r: dict, years: float, window: int, multiplier: float) -> None:
    print("=" * 62)
    print(f"[{r['ticker']}] {r['rows']}거래일 수신 · 집계 구간 {r['covered'][0]} ~ {r['covered'][1]}")
    print(f"평균 {window}일 롤링 σ: {r['avg_sigma_pct']:.2f}% "
          f"(임계 −{r['avg_sigma_pct'] * multiplier:.2f}% 수준, σ는 매일 변동)")
    for label, dates in r["signals"].items():
        rate = len(dates) / r["covered_days"] * 100 if r["covered_days"] else 0.0
        print(f"  · {label}: {len(dates)}회 / {r['covered_days']}거래일 ({rate:.1f}%) "
              f"→ 연 {len(dates) / years:.1f}회")
        for d in dates:
            print(f"      - {d}")
    print("=" * 62 + "\n")


def print_allocation(results: list[dict], years: float) -> None:
    counts = {r["ticker"]: len(r["signals"][FILL_BASIS]) for r in results}
    total = sum(counts.values())
    print("── 자금 배분 참고 (체결 기준 = 전일 σ 로 미리 걸어둔 LOC) ──")
    if total == 0:
        print("  발동 0회 — 비중을 정할 근거가 없다 (count_years 를 늘려 재실행)\n")
        return

    # 차수당 금액이 같다면 필요한 예산은 발동 횟수에 비례한다
    for ticker, n in counts.items():
        print(f"  {ticker}: 연 {n / years:.1f}회 → 비중 {n / total * 100:.0f}%")
    lowest = min(counts.values())
    print("  비율 " + " : ".join(f"{t} {n / lowest:.1f}" for t, n in counts.items()))

    common = set.intersection(*(set(r["signals"][FILL_BASIS]) for r in results))
    if common:
        print(f"  ⚠️ 같은 날 동시 발동 {len(common)}일 ({', '.join(sorted(common))}) — "
              "그날은 주문이 겹치므로 종목별 예산을 따로 잡거나 우선순위를 정해야 한다")
    print(f"  ※ 과거 {years:g}년 빈도는 미래 보장이 아니다 — count_years 를 바꿔 민감도를 확인할 것\n")


def main() -> None:
    config = load_config()
    tickers = config.get("tickers") or [config["ticker"]]
    window = int(config.get("rolling_window", 20))
    multiplier = float(config.get("sigma_multiplier", 2.0))
    # 인자로 집계 창을 덮어쓸 수 있다 (설정 파일을 고치지 않고 민감도 확인용)
    years = float(sys.argv[1]) if len(sys.argv) > 1 else float(config.get("count_years", 1))
    days = int(365 * years)

    print(f"설정: {window}거래일 롤링 수익률 σ × {multiplier} · 집계 창 {years:g}년({days}일) "
          f"· 실행 {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"      (설정 lookback_days={config.get('lookback_days', 60)} 는 알림용 다운로드 창 — 여기선 미사용)\n")

    results = []
    for ticker in tickers:
        try:
            r = count_signals(ticker, days, window, multiplier)
        except RuntimeError as e:
            print(f"❌ {e}\n")
            continue
        results.append(r)
        print_ticker(r, years, window, multiplier)

    if len(results) > 1:
        print_allocation(results, years)


if __name__ == "__main__":
    main()