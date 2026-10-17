"""SOXL / TQQQ 롤링 σ 매수 신호 — 공용 정의 + 연간 발동 횟수 집계.

설정은 `soxl_tqqq_config.json` 공용 (알림 스크립트 `soxl_2sigma_alert.py` 와 같은 파일).
- σ 창: `rolling_window`(거래일) × `sigma_multiplier` — 지표 정의는 이것뿐.
- 집계 창: `count_years`(년, 기본 1) — 실행 시점부터 거슬러 올라가는 기간.

이 모듈이 알림 스크립트와 공유하는 **단일 소스**:
- `load_settings()` — 설정 파일 파싱·검증 (종목, σ 창, 배수, 집계 년수)
- `download_daily()` — yfinance 다운로드 (예외를 RuntimeError 로 통일, 옵션 명시)
- `signal_frame()` — Return · σ · 두 가지 발동 마스크 계산 (σ·판정 정의는 여기 한 곳)
- `expected_session()` — 지금 기준으로 있어야 할 마지막 마감 세션 (봉 지연 검출용)
- `is_trading_day_today()` — 오늘(ET)이 NYSE 거래일인지 (휴장일 중복 알림 방지용, 캘린더 없으면 None)
- `download_end()` — 다운로드 창의 끝 (마지막으로 마감된 미국 세션 + 1일)
- `frequency_stats()` — 연간 예상 발동 횟수(장기 중앙값) + 최근 1년 실적

두 기준을 함께 센다 — 둘 다 같은 부등식이지만 "언제 알 수 있는 σ 인가"가 다르다:
- 체결(LOC): 당일 종가 ≤ 전일 종가 × (1 − 배수 × 전일까지의 σ) → 주문을 미리 걸어둘 때 실제로
  체결되는 날. **알림 스크립트도 이 기준으로 '신호'를 판정한다** (빈도 통계와 같은 정의).
- 알림 판정(참고): 당일 수익률 < −(당일까지의 σ × 배수) → 당일 급락이 σ 를 키우는 자기 포함
  편향이 있어 실제 체결과 어긋날 수 있다. 비교용으로만 출력한다.

롤링 창이 채워지지 않는 첫 ~20거래일은 σ 가 없어 집계에서 빠진다(집계 구간을 함께 출력).

다운로드 창의 끝은 `download_end()` — **마지막으로 마감된 미국 세션**이다. `datetime.now()` 를 그대로
쓰면 러너 시계(UTC)와 로컬 시계(KST)에 따라 다른 세션이 잘리거나 진행 중인 봉이 섞인다.

사용법: `python3 soxl_tqqq_2sigma_count.py [집계 년수]` — 인자를 주면 설정값을 덮어쓴다
(예: `... 5` 로 장기 연환산 빈도 확인 — 1년 표본은 사건이 3~8회뿐이라 흔들린다).

선택 의존성: `pip install exchange-calendars` 를 깔면 `expected_session()` 이 NYSE 휴장일까지
반영한다(없으면 None → 알림의 '봉 미수신' 검사만 꺼진다). Windows 는 `pip install tzdata` 도 필요.

ponytail: 조회·집계만 하는 도구 — 알림/워크플로우에 연결하지 않는다(알림은 이걸 import 해서 쓴다).
"""

import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import yfinance as yf

try:  # 선택 의존성 — 없으면 휴장일 반영 검사만 건너뛴다
    import exchange_calendars as _xcals

    _NYSE = _xcals.get_calendar("XNYS")
except Exception:
    _NYSE = None

CONFIG_NAME = "soxl_tqqq_config.json"
ALERT_BASIS = "알림 판정 (당일 σ 포함)"
FILL_BASIS = "체결 기준 (전일까지 σ · 룩어헤드 없음)"  # 주문을 미리 걸어둘 때 실제 체결되는 날

STATS_DAYS = 365 * 16  # 장기 중앙값용 기간 — SOXL/TQQQ 상장(2010-02) 이후 전체를 덮는 최소 길이
YEAR_ROWS = 252  # 1년 = 252거래일
_WARNED: set[str] = set()  # 결측 경고는 종목당 1회만


# ==================== 설정 ====================
@dataclass(frozen=True)
class Settings:
    tickers: list[str]
    window: int
    multiplier: float
    count_years: float


def load_settings() -> Settings:
    """설정 파일을 읽어 검증한다 — 알림·집계 스크립트가 같은 파싱을 쓴다."""
    path = Path(__file__).parent / CONFIG_NAME
    config: dict = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    raw = config.get("tickers") or ([config["ticker"]] if config.get("ticker") else [])
    tickers = [str(t) for t in raw]
    if not tickers:
        raise ValueError(f"설정 파일({CONFIG_NAME})에 'ticker' 또는 'tickers'가 설정되지 않았습니다.")
    return Settings(
        tickers=tickers,
        window=int(config.get("rolling_window", 20)),
        multiplier=float(config.get("sigma_multiplier", 2.0)),
        count_years=float(config.get("count_years", 1)),
    )


def iso(label) -> str:
    """인덱스 라벨 → 'YYYY-MM-DD' (.strftime 은 타입 스텁에서 오류로 뜨므로 문자열 앞 10자)."""
    return str(label)[:10]


# ==================== 세션·다운로드 ====================
def _last_closed_et_date():
    """ET 정규장 마감(16:00) 기준 '마지막으로 마감된 날짜' (휴장일·주말 미반영)."""
    et = datetime.now(timezone.utc).astimezone(ZoneInfo("America/New_York"))
    return et.date() if et.time() >= time(16, 0) else et.date() - timedelta(days=1)


def download_end() -> str:
    """yfinance `end`(**배타적**) 인자용 날짜 — **마지막으로 마감된 미국 세션 + 1일**.

    ⚠️ `datetime.now()` 를 그대로 쓰면 한 세션이 어긋난다 (2026-10-16 수정 — CI 만 한 세션 낡았다):
    ① GHA 러너는 UTC 라 22:30 UTC(=18:30 ET, 마감 후) 실행 시 UTC 날짜가 **그날 마감된 세션**과
       같아지고, `end` 가 배타적이라 그 세션이 잘려 직전 세션 값이 나온다 (로컬 KST 실행과 하루 차),
    ② KST 로컬 실행은 반대로 장중(11:00 ET 이후)에 **진행 중인 부분 봉**을 마감가로 집는다.
    ET 정규장 마감(16:00) 을 기준으로 '마지막 마감 세션'을 정하면 두 경로가 같은 세션을 쓴다.
    휴장일·주말이면 그 이전 거래일 봉이 그대로 마지막 행이 된다.
    """
    return (_last_closed_et_date() + timedelta(days=1)).strftime("%Y-%m-%d")


def expected_session() -> str | None:
    """지금 시점에 야후가 줘야 할 마지막 마감 세션('YYYY-MM-DD'). 휴장 캘린더가 없으면 None.

    최신 봉 날짜가 이보다 이르면 '봉이 아직 안 올라온 것' — 결측(NaN)과 달리 봉 자체가 없어
    종가 NaN 검사로는 잡히지 않는다. 휴장일은 캘린더가 직전 세션으로 되돌려 주므로 오탐이 없다.
    """
    if _NYSE is None:
        return None
    try:
        day = pd.Timestamp(_last_closed_et_date())
        return iso(_NYSE.date_to_session(day, direction="previous"))
    except Exception:
        return None


def is_trading_day_today() -> bool | None:
    """오늘(ET 날짜)이 NYSE 거래일인지. 휴장 캘린더가 없거나 판단에 실패하면 None.

    알림 CI 가 휴장일에 같은 세션을 또 알리지 않도록 스케줄 실행에서만 쓴다. 판단이 불가(None)하면
    호출부는 '알림을 보내는 쪽'으로 진행해야 한다 — 조용히 건너뛰어 알림을 놓치는 쪽이 더 나쁘다.
    """
    if _NYSE is None:
        return None
    try:
        et_today = datetime.now(timezone.utc).astimezone(ZoneInfo("America/New_York")).date()
        return bool(_NYSE.is_session(pd.Timestamp(et_today)))
    except Exception:
        return None


def download_daily(ticker: str, start: datetime) -> pd.DataFrame:
    """일봉 원본 DataFrame (마지막 봉의 Close 가 NaN 일 수 있다 — 호출부가 판단).

    실패(예외·빈 결과)는 모두 RuntimeError 로 통일한다 — 종목 단위로 격리해 처리하기 위해.
    예외 메시지엔 타입만 남긴다(요청 URL 등이 로그에 섞이는 것을 막는다).
    """
    try:
        df = yf.download(
            ticker,
            start=start.strftime("%Y-%m-%d"),
            end=download_end(),
            progress=False,
            auto_adjust=True,  # 기본값이 버전마다 바뀐 적이 있어 명시
            multi_level_index=False,  # 단일 종목 → df["Close"] 가 항상 Series
        )
    except Exception as e:
        raise RuntimeError(f"{ticker} 데이터 수신 예외 ({type(e).__name__})") from e
    if df is None or df.empty:
        raise RuntimeError(f"{ticker} 데이터 수신 실패 (yfinance)")
    return df


def load_closes(ticker: str, start: datetime) -> pd.Series:
    """종가 Series (결측 봉은 제거)."""
    df = download_daily(ticker, start)
    close = df["Close"].dropna()
    # ⚠️ 마지막 봉의 종가가 결측이면 dropna 가 조용히 지운다 — 집계 구간이 왜 하루 낡았는지 남긴다
    # (2026-10-17 실측: 야후가 10-16 일봉을 Open/High/Low/Volume 만 주고 Close 는 NaN 으로 반환).
    if len(close) and ticker not in _WARNED and math.isnan(float(df["Close"].iloc[-1])):
        _WARNED.add(ticker)
        print(f"⚠️ [{ticker}] 마지막 봉({iso(df.index[-1])}) 종가 결측 — 제외하고 계산 "
              f"(확정 세션: {iso(close.index[-1])})")
    return close


# ==================== 신호 정의 (단일 소스) ====================
def signal_frame(close: pd.Series, window: int, multiplier: float) -> pd.DataFrame:
    """종가 → 컬럼 close · ret · sigma · alert_hit · fill_hit 의 DataFrame.

    - sigma: ret 의 `window` 거래일 롤링 표준편차 (배수는 곱하지 않은 원본 σ)
    - alert_hit: ret < −σ[t]·배수      (당일 σ 포함)
    - fill_hit : ret < −σ[t−1]·배수    (전일까지 σ — 실제 LOC 체결 기준, 알림·빈도 통계가 쓰는 기준)
    `fill_method=None` 으로 pandas 버전별 기본값 차이(결측 앞값 채움)를 막는다.
    """
    ret = close.pct_change(fill_method=None)
    sigma = ret.rolling(window).std()
    prev = sigma.shift(1)
    return pd.DataFrame({
        "close": close,
        "ret": ret,
        "sigma": sigma,
        "alert_hit": (ret < -sigma * multiplier) & sigma.notna(),
        "fill_hit": (ret < -prev * multiplier) & prev.notna(),
    })


def _dates(mask: pd.Series) -> list[str]:
    # ponytail: index[bool Series] 대신 items() 순회 — 부울 인덱싱은 타입 스텁에서 오류로 뜬다
    return [iso(d) for d, h in mask.items() if h]


# ==================== 집계 ====================
def frequency_stats(ticker: str, window: int, multiplier: float,
                    close: pd.Series | None = None) -> dict:
    """연간 예상 발동 횟수(장기 중앙값) + 최근 1년 실적 — 알림 스크립트 공용 계산.

    `close` 를 넘기면 다시 받지 않는다(알림이 이미 받은 장기 이력을 재사용).
    """
    if close is None:
        close = load_closes(ticker, datetime.now() - timedelta(days=STATS_DAYS))
    frame = signal_frame(close, window, multiplier)
    n = len(frame)
    if n <= YEAR_ROWS:
        raise RuntimeError(f"[{ticker}] 중앙값 계산에 필요한 이력이 부족합니다.")

    # 1년(252거래일) 롤링 창마다 발동 횟수(누적합 차) → 그 중앙값을 분모로 쓴다
    cum = np.concatenate([[0], np.cumsum(frame["fill_hit"].to_numpy(dtype=int))])
    counts = [int(cum[i + 1] - cum[i - YEAR_ROWS]) for i in range(YEAR_ROWS, n)]

    # 최근 1년 = 최신 봉 날짜 − 365일 (실행 시계가 아니라 데이터 기준 — CI·로컬 동일)
    last_day = datetime.strptime(iso(frame.index[-1]), "%Y-%m-%d")
    recent_cut = (last_day - timedelta(days=365)).strftime("%Y-%m-%d")
    return {
        "ticker": ticker,
        "median": median(counts),
        "mean": sum(counts) / len(counts),
        "windows": len(counts),
        "recent": sum(1 for d in _dates(frame["fill_hit"]) if d >= recent_cut),
        "history": (iso(frame.index[0]), iso(frame.index[-1])),
    }


def count_signals(ticker: str, days: int, window: int, multiplier: float,
                  close: pd.Series | None = None) -> dict:
    """최근 `days` 달력일 구간의 발동일 (두 기준). `close` 를 넘기면 다시 받지 않는다."""
    now = datetime.now()
    if close is None:
        close = load_closes(ticker, now - timedelta(days=days))
    frame = signal_frame(close, window, multiplier)  # σ 는 전체 이력으로 계산 → 구간 첫날부터 유효

    cut = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    start = next((i for i, d in enumerate(frame.index) if iso(d) >= cut), len(frame))
    part = frame.iloc[start:]
    covered = part["sigma"].dropna()  # 롤링 창이 채워진 구간
    if covered.empty:
        raise RuntimeError(f"[{ticker}] 롤링 {window}일 σ 계산에 필요한 데이터가 부족합니다.")
    return {
        "ticker": ticker,
        "rows": len(part),
        "covered": (iso(covered.index[0]), iso(part.index[-1])),
        "covered_days": len(covered),
        "avg_sigma_pct": float(covered.mean() * 100),
        "signals": {
            ALERT_BASIS: _dates(part["alert_hit"]),
            FILL_BASIS: _dates(part["fill_hit"]),
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
    s = load_settings()
    # 인자로 집계 창을 덮어쓸 수 있다 (설정 파일을 고치지 않고 민감도 확인용)
    years = float(sys.argv[1]) if len(sys.argv) > 1 else s.count_years
    days = int(365 * years)

    print(f"설정: {s.window}거래일 롤링 수익률 σ × {s.multiplier:g} · 집계 창 {years:g}년({days}일) "
          f"· 실행 {datetime.now():%Y-%m-%d %H:%M:%S}\n")

    for ticker in s.tickers:
        try:
            # 종목당 한 번만 받는다 — 집계와 장기 중앙값이 같은 데이터를 쓴다
            close = load_closes(ticker, datetime.now() - timedelta(days=max(STATS_DAYS, days)))
            r = count_signals(ticker, days, s.window, s.multiplier, close=close)
        except RuntimeError as e:
            print(f"❌ {e}\n")
            continue
        try:
            stats = frequency_stats(ticker, s.window, s.multiplier, close=close)
        except RuntimeError as e:
            print(f"⚠️ {e} — 중앙값 생략\n")
            stats = None
        print_ticker(r, years, s.window, s.multiplier, stats)


if __name__ == "__main__":
    main()