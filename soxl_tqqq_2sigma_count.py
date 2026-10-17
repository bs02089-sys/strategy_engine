"""SOXL / TQQQ 롤링 2σ 매수 신호 빈도 집계 — 연간 체결 횟수 파악용.

설정은 `soxl_tqqq_config.json` 공용 (알림 스크립트 `soxl_2sigma_alert.py` 와 같은 파일).
- σ 창: `rolling_window`(거래일) × `sigma_multiplier` — 지표 정의는 이것뿐.
- 집계 창: `count_years`(년, 기본 1) — 실행 시점부터 거슬러 올라가는 기간.
- ⚠️ `lookback_days`(60) 는 알림 스크립트가 데이터를 **받아오는** 기간이라 여기서는 쓰지 않는다
  (σ 는 최근 20거래일만 보므로 다운로드 창을 늘려도 신호는 달라지지 않는다).

두 기준을 함께 센다 — 둘 다 같은 부등식이지만 "언제 알 수 있는 σ 인가"가 다르다:
- 알림 판정: 당일 수익률 < −(당일까지의 σ × 배수) → `soxl_2sigma_alert.py` 가 매일 찍는 신호.
- 체결(LOC): 당일 종가 ≤ 전일 종가 × (1 − 배수 × 전일까지의 σ) → 주문을 미리 걸어둘 때 실제로 체결되는 날.

`frequency_stats()` 는 알림 스크립트가 import 해 쓰는 공용 계산이다 — 연간 예상 발동 횟수(장기
중앙값)와 최근 1년 실적을 돌려준다. 나눗셈(연간 예산 ÷ 발동 횟수)의 분모가 이 값이다.

롤링 창이 채워지지 않는 첫 ~20거래일은 σ 가 없어 집계에서 빠진다(집계 구간을 함께 출력).

다운로드 창의 끝은 `download_end()` — **마지막으로 마감된 미국 세션**이다. `datetime.now()` 를 그대로
쓰면 러너 시계(UTC)와 로컬 시계(KST)에 따라 다른 세션이 잘리거나 진행 중인 봉이 섞인다.
`_hits()` 와 `download_end()` 가 이 모듈의 공용 정의다 — 알림 스크립트가 import 해 쓴다.

사용법: `python3 soxl_tqqq_2sigma_count.py [집계 년수]` — 인자를 주면 설정값을 덮어쓴다
(예: `... 5` 로 장기 연환산 빈도 확인 — 1년 표본은 사건이 3~8회뿐이라 흔들린다).

ponytail: 조회·집계만 하는 도구 — 알림/워크플로우에 연결하지 않는다(알림은 이걸 import 해서 쓴다).
"""

import json
import math
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

import yfinance as yf

ALERT_BASIS = "알림 판정 (당일 σ 포함)"
FILL_BASIS = "체결 기준 (전일까지 σ · 룩어헤드 없음)"  # 주문을 미리 걸어둘 때 실제 체결되는 날

STATS_DAYS = 365 * 16  # 장기 중앙값용 기간 — SOXL/TQQQ 상장(2010-02) 이후 전체를 덮는 최소 길이
YEAR_ROWS = 252  # 1년 = 252거래일
_WARNED: set[str] = set()  # 결측 경고는 종목당 1회만 (한 실행에서 같은 다운로드를 여러 번 한다)


def load_config() -> dict:
    config_path = Path(__file__).parent / "soxl_tqqq_config.json"
    if not config_path.exists():
        return {}
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def download_end() -> str:
    """yfinance `end`(**배타적**) 인자용 날짜 — **마지막으로 마감된 미국 세션 + 1일**.

    ⚠️ `datetime.now()` 를 그대로 쓰면 한 세션이 어긋난다 (2026-10-16 수정 — CI 만 한 세션 낡았다):
    ① GHA 러너는 UTC 라 22:30 UTC(=18:30 ET, 마감 후) 실행 시 UTC 날짜가 **그날 마감된 세션**과
       같아지고, `end` 가 배타적이라 그 세션이 잘려 직전 세션 값이 나온다 (로컬 KST 실행과 하루 차),
    ② KST 로컬 실행은 반대로 장중(11:00 ET 이후)에 **진행 중인 부분 봉**을 마감가로 집는다.
    ET 정규장 마감(16:00) 을 기준으로 '마지막 마감 세션'을 정하면 두 경로가 같은 세션을 쓴다.
    휴장일·주말이면 그 이전 거래일 봉이 그대로 마지막 행이 된다.
    """
    et = datetime.now(timezone.utc).astimezone(ZoneInfo("America/New_York"))
    last_session = et.date() if et.time() >= time(16, 0) else et.date() - timedelta(days=1)
    return (last_session + timedelta(days=1)).strftime("%Y-%m-%d")


def load_closes(ticker: str, start: datetime):
    """다운로드 결과에서 종가 Series 추출 (multi_level_index=False 로 단일 컬럼 고정).

    창 끝은 늘 `download_end()`(마지막 마감 세션) — 호출마다 같은 규칙을 쓴다.
    """
    df = yf.download(
        ticker,
        start=start.strftime("%Y-%m-%d"),
        end=download_end(),
        progress=False,
        multi_level_index=False,  # 단일 종목 → df["Close"] 가 항상 Series (컬럼 구조 변동 차단)
    )
    if df is None or df.empty:
        raise RuntimeError(f"[{ticker}] 데이터를 가져오지 못했습니다.")
    close = df["Close"].dropna()
    # ⚠️ 마지막 봉의 종가가 결측이면 dropna 가 조용히 지운다 — 집계 구간이 왜 하루 낡았는지 남긴다
    # (2026-10-17 실측: 야후가 10-16 일봉을 Open/High/Low/Volume 만 주고 Close 는 NaN 으로 반환).
    if len(close) and ticker not in _WARNED and math.isnan(float(df["Close"].iloc[-1])):
        _WARNED.add(ticker)
        print(f"⚠️ [{ticker}] 마지막 봉({str(df.index[-1])[:10]}) 종가 결측 — 제외하고 계산 "
              f"(확정 세션: {str(close.index[-1])[:10]})")
    return close


def _hits(close, window: int, multiplier: float, prev_sigma: bool):
    """발동 마스크 — σ·판정 정의의 단일 소스. prev_sigma=True 면 체결 기준(전일까지의 σ)."""
    ret = close.pct_change()
    sigma = ret.rolling(window).std()
    ref = sigma.shift(1) if prev_sigma else sigma
    return (ret < -ref * multiplier) & ref.notna()


def signal_dates(close, window: int, multiplier: float, prev_sigma: bool) -> list[str]:
    """발동일 목록(ISO 날짜 문자열). prev_sigma=True → 체결 기준."""
    hits = _hits(close, window, multiplier, prev_sigma)
    # ponytail: index[bool Series] 대신 items() 순회 — 부울 인덱싱은 타입 스텁에서 오류로 뜬다
    return [d.strftime("%Y-%m-%d") for d, h in hits.items() if h]


def frequency_stats(ticker: str, window: int, multiplier: float) -> dict:
    """연간 예상 발동 횟수(장기 중앙값) + 최근 1년 실적 — 알림 스크립트 공용 계산."""
    now = datetime.now()
    close = load_closes(ticker, now - timedelta(days=STATS_DAYS))
    hits = signal_dates(close, window, multiplier, prev_sigma=True)

    # 날짜 사전순 = 시간순이라 문자열 비교만으로 창 계산이 된다
    # (Index 위치 인덱싱을 피하려고 ISO 문자열로 다룬다 — 타입 스텁 이슈 회피)
    # ponytail: 날짜 라벨은 str()[:10] 로 뽑는다 — .strftime() 은 스텁상 라벨 타입이
    # Hashable 로 잡혀 편집기에서 오류로 뜨고, ISO 문자열 앞 10자가 곪 날짜다
    index_dates = [str(d)[:10] for d, _ in close.items()]
    position = {d: i for i, d in enumerate(index_dates)}
    hit_pos = sorted(position[d] for d in hits)

    # 1년(252거래일) 롤링 창마다 발동 횟수 → 그 중앙값을 분모로 쓴다
    counts = [sum(1 for p in hit_pos if i - YEAR_ROWS <= p <= i)
              for i in range(YEAR_ROWS, len(index_dates))]
    if not counts:
        raise RuntimeError(f"[{ticker}] 중앙값 계산에 필요한 이력이 부족합니다.")

    recent_cut = (now - timedelta(days=365)).strftime("%Y-%m-%d")
    return {
        "ticker": ticker,
        "median": median(counts),
        "mean": sum(counts) / len(counts),
        "windows": len(counts),
        "recent": sum(1 for d in hits if d >= recent_cut),
        "history": (index_dates[0], index_dates[-1]),
    }


def count_signals(ticker: str, days: int, window: int, multiplier: float) -> dict:
    close = load_closes(ticker, datetime.now() - timedelta(days=days))

    sigma = close.pct_change().rolling(window).std()
    covered = sigma.dropna()  # 롤링 창이 채워진 구간 (앞 ~20거래일 제외)
    if covered.empty:
        raise RuntimeError(f"[{ticker}] 롤링 {window}일 σ 계산에 필요한 데이터가 부족합니다.")

    # ponytail: 날짜 라벨은 str()[:10] 로 뽑는다 — .strftime() 은 스텁상 라벨 타입이
    # Hashable 로 잡혀 편집기에서 오류로 뜨고, ISO 문자열 앞 10자가 곪 날짜다
    index_dates = [str(d)[:10] for d, _ in close.items()]
    first_covered = next(d for d, ok in zip(index_dates, sigma.notna()) if ok)
    return {
        "ticker": ticker,
        "rows": len(close),
        "covered": (first_covered, index_dates[-1]),
        "covered_days": len(covered),
        "avg_sigma_pct": float(covered.mean() * 100),
        "signals": {
            ALERT_BASIS: signal_dates(close, window, multiplier, prev_sigma=False),
            FILL_BASIS: signal_dates(close, window, multiplier, prev_sigma=True),
        },
    }


def print_ticker(r: dict, years: float, window: int, multiplier: float, stats: dict | None) -> None:
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
    if stats:
        print(f"  · 장기({stats['history'][0]}~) 1년 발동 중앙값: {stats['median']:g}회 "
              f"(평균 {stats['mean']:.1f}회, 1년 창 {stats['windows']}개) · 최근 1년 실제 {stats['recent']}회"
              f"  ← 연간 예산 ÷ {stats['median']:g}")
    print("=" * 62 + "\n")


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

    for ticker in tickers:
        try:
            r = count_signals(ticker, days, window, multiplier)
        except RuntimeError as e:
            print(f"❌ {e}\n")
            continue
        try:
            stats = frequency_stats(ticker, window, multiplier)
        except RuntimeError as e:
            print(f"⚠️ {e} — 중앙값 생략\n")
            stats = None
        print_ticker(r, years, window, multiplier, stats)


if __name__ == "__main__":
    main()
