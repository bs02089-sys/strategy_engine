# -*- coding: utf-8 -*-
"""
=============================================================
 Bear Market 7 Early Warning Signals
=============================================================

System Overview:
  1. Yield Curve Inversion     - Inversion & Re-steepening (Most dangerous)
  2. Market Breadth            - A-D Line deviation & RSP/SPY relative strength
  3. Credit Spread Widening    - HY + IG spreads (Bond market signals)
  4. Fed Policy Cycle          - First rate cut + 6~12 months = Highest risk zone
  5. Valuation Overheat        - Shiller CAPE levels & S&P500 EPS growth slowing
  6. Leading Indicators        - USPHCI YoY (coincident activity) & Sahm Rule (0.5%p threshold)
     ⚠️ 원래 USSLIND(Philly Fed 선행지수)를 썼으나 그 시리즈가 2020-02 에 중단돼 6년간
        값이 동결(1.72) → 'LEI contraction' 이 구조적으로 발동 불가였다 (2026-09-20 교체)
  7. Momentum Strategy Signal  - SPX 200-day return & Sector rotation

Regime Assessment:
  Total Risk Score(0~14)를 시장 국면 판정에 사용해 'LOC_DCA / 스윙 중 유리한 매수 조건'을
  함께 출력한다. 7개 시그널을 두 그룹으로 나눈다:
    - 선행 그룹 (고점 경고, 0~6): Yield Curve · Fed Policy · Valuation(CAPE)
    - 확인 그룹 (하락 진행, 0~8): Breadth · Credit Spread · Leading Ind. · Momentum
  판정 규칙의 상세 내용은 `assess_regime()`의 docstring을 참고 (단일 출처로 관리,
  이 헤더에는 규칙을 중복 서술하지 않는다 — 과거 이중 관리로 인해 두 곳의 설명이
  어긋난 적이 있었음).

Dependencies:
    pip install yfinance pandas requests
    (선택) 브라우저 우회 경로를 쓰려면: pip install "scrapling[fetchers]"
           + python -m patchright install chromium — 없으면 ② 단계를 건너뛰고 사유를 리포트에 남긴다

Data Sources:
  - FRED    : Direct download via fredgraph.csv
  - yfinance: S&P500, Sector ETFs, NYSE A-D Line (^NYAD)
  - multpl.com: Shiller CAPE — ① requests → ② StealthyFetcher(Cloudflare 우회) → ③ 캐시 폴백.
    어느 단계로 내려갔는지(차단·실패 사유)가 리포트에 그대로 남는다 (2026-09-24)
"""

import json
import math
import os
import re
import sys
import shutil
import tempfile
import datetime
import warnings
from dataclasses import dataclass
from io import StringIO
from typing import Optional

import pandas as pd
import requests

warnings.filterwarnings("ignore")

# ─────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────

FRED_LOOKBACK_2Y = 365 * 2   # 2년 lookback
FRED_LOOKBACK_8Y = 365 * 8   # 8년 lookback (Fed cycle)
CAPE_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cape_cache.json")
CAPE_CACHE_MAX_AGE_DAYS = 7   # 캐시가 이보다 오래되면 신선하지 않다고 경고
ACTIVITY_MAX_AGE_DAYS = 120   # 동행지수(USPHCI) 허용 지연 — 월간 + 발표지연 ~2개월 (2026-09-20)

try:
    import yfinance as yf
except ImportError:
    print("yfinance is not installed. Please run: pip install yfinance")
    sys.exit(1)


# ─────────────────────────────────────────────
# Shared Utilities
# ─────────────────────────────────────────────

def fred_series(series_id: str, lookback_days: int = 365 * 5) -> pd.Series:
    """Download series from FRED via csv endpoint."""
    end = datetime.date.today()
    start = end - datetime.timedelta(days=lookback_days)
    url = (
        "https://fred.stlouisfed.org/graph/fredgraph.csv"
        f"?id={series_id}&cosd={start}&coed={end}"
    )
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    df = pd.read_csv(StringIO(resp.text), na_values=".")
    df.columns = ["date", series_id]
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    result: pd.Series = df.dropna().set_index("date")[series_id].astype(float)  # type: ignore[assignment]
    return result


def get_error_message(e: Exception, source_name: str) -> str:
    """Converts exceptions into user-friendly messages."""
    err_str = str(e).lower()
    if "timeout" in err_str:
        return f"{source_name} server response delayed"
    if "connection" in err_str:
        return f"{source_name} network connection failed"
    if "결측" in err_str:
        # _require_finite 가드 — 동기화 실패가 아니라 값이 불완전한 경우 (2026-09-20)
        return f"{source_name} 데이터 결측으로 판정 불가 (점수 미부여)"
    return f"{source_name} data sync error ({type(e).__name__}: {e})"


def _require_finite(*values: float) -> None:
    """결측(NaN)이면 예외 — 조용히 '정상(+0)' 으로 위장되는 것을 막는다 (2026-09-20).

    비교(<, >)만으로는 NaN 을 걸러낼 수 없다 — NaN 을 던지지 않고 오히려 모든 비교를
    False 로 만들어 else 가지('정상')로 떨어뜨린다. 이 헬퍼를 가드로 두면 NaN 이 들어온
    경우 그 신호는 except 로 빠져 data_ok=False (판정 불가) 가 된다.
    """
    if not all(math.isfinite(v) for v in values):
        raise ValueError("데이터 결측(NaN)으로 판정 불가")


def validate_yf_data(raw_data: Optional[pd.DataFrame], symbols: list) -> pd.DataFrame:
    """Validate yfinance input and extract 'Close' series.

    ⚠️ 심볼 공통 '완성' 행만 반환한다 (2026-09-20): yfinance 는 런타임에 심볼별로 당일 행이
    미확정(NaN)인 채 내려올 수 있다. 그대로 두면 호출부의 .iloc[-1] 이 NaN 을 집고, NaN 은
    예외를 던지지 않으면서 모든 비교(<, >)를 False 로 만들어 '정상(+0)' 으로 위장된다
    (실제 발생: Market Breadth RSP/SPY · Momentum 섹터 슬라이스가 +nan% 로 +0 보고 —
    최근 16회 리포트 중 14회). 마지막 완성 행까지만 남겨 .iloc[-1] 을 신뢰할 수 있게 한다.
    """
    if raw_data is None or raw_data.empty:
        raise ValueError("yfinance returned no data")

    cols = raw_data.columns
    if isinstance(cols, pd.MultiIndex):
        data = raw_data['Close'] if 'Close' in cols.levels[0] else raw_data
    else:
        data = raw_data

    if isinstance(data, pd.Series):
        data = data.to_frame()

    assert isinstance(data, pd.DataFrame)  # narrow type for pyright
    missing = [s for s in symbols if s not in data.columns]
    if missing:
        raise KeyError(f"Missing symbols: {missing}")

    # 심볼 공통 완성 행만 — 미완성 마지막 행(NaN)을 .iloc[-1] 이 집지 않도록 (2026-09-20)
    cleaned = data[list(symbols)].dropna()
    if cleaned.empty:
        raise ValueError("yfinance returned only incomplete rows (all NaN)")
    return cleaned  # type: ignore[return-type]


def atomic_write_json(path: str, data: dict) -> None:
    """JSON을 임시 파일에 쓰고 원자적으로 교체(rename)한다.

    임시 파일을 대상 파일과 '같은 디렉터리'에 만들어야 os.replace/shutil.move가
    같은 파일시스템 내 rename으로 처리되어 진짜 원자적 교체가 보장된다
    (시스템 기본 temp 디렉터리를 쓰면 파티션이 달라 copy+delete로 대체되며
    중간에 크래시가 나면 손상 위험이 생긴다).
    """
    target_dir = os.path.dirname(os.path.abspath(path)) or "."
    with tempfile.NamedTemporaryFile(
        "w", delete=False, suffix=".json", encoding="utf-8", dir=target_dir
    ) as tmp:
        json.dump(data, tmp, ensure_ascii=False, indent=2)
        tmp_path = tmp.name
    shutil.move(tmp_path, path)


@dataclass
class SignalResult:
    name: str
    triggered: bool
    score: int          # 0=Normal, 1=Caution, 2=Warning
    detail: str
    group: str = "confirm"   # "leading"=고점 경고 / "confirm"=하락 진행 (백테스트 연계용)
    data_ok: bool = True     # False = 데이터 결측/실패로 '판정 불가' — 점수 0 인 '정상'과 구분한다 (2026-09-20)


# ─────────────────────────────────────────────
# Signal Functions
# ─────────────────────────────────────────────

def signal_yield_curve() -> SignalResult:
    """Detects yield curve inversion (T10Y2Y)."""
    score_total, notes, data_ok = 0, [], True
    try:
        s = fred_series("T10Y2Y", lookback_days=FRED_LOOKBACK_2Y)
        current, min_2y = s.iloc[-1], s.min()
        _require_finite(current, min_2y)      # 결측이면 'Normal(+0)' 로 위장되므로 가드 (2026-09-20)
        was_inverted = min_2y < 0

        if was_inverted and current > 0:
            score_total = 2
            notes.append(f"Re-steepening in progress ({current:+.2f}%p) (+2)")
        elif current < 0:
            score_total = 1
            notes.append(f"Inverted ({current:.2f}%p) (+1)")
        else:
            notes.append(f"Normal ({current:+.2f}%p) (+0)")
    except Exception as e:
        data_ok = False
        notes.append(get_error_message(e, "Yield Curve"))

    return SignalResult("Yield Curve Inversion", score_total >= 1, score_total, " | ".join(notes),
                        group="leading", data_ok=data_ok)


def signal_market_breadth() -> SignalResult:
    """Detects market breadth cracks.

    Sub-signal 2 measures RSP(equal-weight S&P500) performance *relative to*
    SPY(cap-weight S&P500) — a falling ratio means mega-cap names are
    carrying the index while the average stock lags (concentration risk).
    NOTE: an earlier version computed RSP's own 20-day return in isolation,
    which mislabeled it as "RSP/SPY" without ever comparing to SPY. Fixed
    here to actually divide the two series.
    """
    score_total, notes, data_ok = 0, [], True
    symbols = ["SPY", "^NYA", "RSP"]
    try:
        data = validate_yf_data(yf.download(symbols, period="1y", progress=False), symbols)
        spy, nya, rsp = data["SPY"], data["^NYA"], data["RSP"]
        spy_dd = spy.iloc[-1] / spy.max() - 1
        nya_dd = nya.iloc[-1] / nya.max() - 1
        _require_finite(spy_dd, nya_dd)      # 'Market breadth stable(+0)' 로 위장 금지 (2026-09-20)

        if spy_dd > -0.05 and nya_dd < -0.10:
            score_total += 1
            notes.append("Market breadth crack detected (+1)")
        else:
            notes.append("Market breadth stable (+0)")

        # RSP/SPY 상대 강도(ratio)의 20일 변화율 — RSP 단독 수익률이 아니라
        # SPY 대비 상대적으로 얼마나 뒤처졌는지를 봐야 집중도 리스크를 잡는다.
        rsp_ratio_now = rsp.iloc[-1] / spy.iloc[-1]
        rsp_ratio_before = rsp.iloc[-20] / spy.iloc[-20]
        ratio_growth = (rsp_ratio_now / rsp_ratio_before - 1) * 100

        if not math.isfinite(ratio_growth):
            # NaN 비교는 항상 False → '정상'으로 위장되므로 결측을 명시적으로 알린다 (2026-09-20)
            data_ok = False
            notes.append("RSP/SPY 데이터 결측 — 이 하위신호 판정 불가 (+0)")
        elif ratio_growth < -2.0:
            score_total += 1
            notes.append(f"Concentration risk high (RSP/SPY ratio {ratio_growth:.1f}%) (+1)")
        else:
            notes.append(f"Market balance maintained (RSP/SPY ratio {ratio_growth:+.1f}%) (+0)")
    except Exception as e:
        data_ok = False
        notes.append(get_error_message(e, "Market Breadth"))

    return SignalResult("Market Breadth", score_total >= 1, score_total, " | ".join(notes),
                        group="confirm", data_ok=data_ok)


def _spread_signal(series: pd.Series, warn: float, caution: float, widen_warn: float, label: str) -> tuple[int, str]:
    """HY/IG 스프레드 평가.

    NOTE: "warning"과 "caution" 두 조건 모두 점수는 동일하게 +1이다 (각 스프레드는
    최대 1점만 기여하도록 설계됨 — HY 1점 + IG 1점 = signal_credit_spread 최대 2점).
    메시지 문구의 심각도 차이는 점수에는 반영되지 않으며, 사람이 보는 상세 로그용이다.
    """
    value = series.iloc[-1]
    widen = value - series.tail(min(63, len(series))).min()
    _require_finite(value, widen)      # NaN 이면 '{label} stable(0)' 로 위장되므로 가드 (2026-09-20)
    if value > warn or widen > widen_warn:
        return 1, f"{label} spread warning ({value:.2f}%)"
    if value > caution:
        return 1, f"{label} spread caution ({value:.2f}%)"
    return 0, f"{label} stable ({value:.2f}%)"


def signal_credit_spread() -> SignalResult:
    """Detects credit spread widening (HY & IG). Max 2점 (HY 1 + IG 1)."""
    score_total, notes, data_ok = 0, [], True
    try:
        hy = fred_series("BAMLH0A0HYM2", lookback_days=FRED_LOOKBACK_2Y)
        score, note = _spread_signal(hy, 6.0, 4.5, 1.5, "HY")
        score_total += score
        notes.append(note)

        ig = fred_series("BAMLC0A0CM", lookback_days=FRED_LOOKBACK_2Y)
        # IG: caution=inf → warning(widen) 조건으로만 점수 부여, 별도 caution 구간 없음
        score, note = _spread_signal(ig, 2.0, float('inf'), 0.5, "IG")
        score_total += score
        notes.append(note)
    except Exception as e:
        data_ok = False
        notes.append(get_error_message(e, "Credit Spread"))

    return SignalResult("Credit Spread", score_total >= 1, score_total, " | ".join(notes),
                        group="confirm", data_ok=data_ok)


def signal_fed_cycle() -> SignalResult:
    """Analyzes Fed rate cycle.

    Uses the MOST RECENT rate cut (iterates backward through the series)
    rather than the FIRST cut in the lookback window. This correctly
    handles multiple easing cycles (cut → hike → cut again): instead
    of measuring from the first cut of 2024, it measures from the most
    recent cut, so the risk score reflects the CURRENT easing cycle.
    """
    score_total, notes, data_ok = 0, [], True
    try:
        s = fred_series("FEDFUNDS", lookback_days=FRED_LOOKBACK_8Y).resample("ME").last().dropna()
        # Iterate BACKWARD to find the MOST RECENT rate cut (start of the
        # current easing cycle), not the first cut in the entire window.
        recent_cut_date = None
        for i in range(len(s) - 1, 0, -1):
            if s.iloc[i] < s.iloc[i - 1]:
                recent_cut_date = s.index[i]
                break
        if recent_cut_date:
            months = (s.index[-1].to_period("M") - recent_cut_date.to_period("M")).n
            if months <= 12:
                score_total = 2
                notes.append(f"High risk zone: {months}m since most recent cut ({recent_cut_date.strftime('%Y-%m')})")
            elif months <= 24:
                score_total = 1
                notes.append(f"Residual risk: {months}m since most recent cut ({recent_cut_date.strftime('%Y-%m')})")
            else:
                notes.append(f"Safe period ({months}m since most recent cut)")
        else:
            notes.append("Awaiting rate cut")
    except Exception as e:
        data_ok = False
        notes.append(get_error_message(e, "Fed Cycle"))

    return SignalResult("Fed Policy Cycle", score_total >= 1, score_total, " | ".join(notes),
                        group="leading", data_ok=data_ok)


# ─────────────────────────────────────────────
# CAPE 수집 — 차단 감지 + 3단계 폴백 (requests → 브라우저 우회 → 캐시)
# ─────────────────────────────────────────────
# multpl.com 조회는 **200 이라고 성공이 아니다.** Cloudflare 챌린지는 해결에 실패해도 예외를
# 던지지 않고 챌린지 페이지(200)를 그대로 돌려주므로, 상태코드만 보면 실패를 값으로 읽게 된다.
# 그래서 값을 파싱하기 전에 차단을 먼저 판정하고, 캐시로 폴백할 때는 그 **사유를 리포트에 남긴다**
# — 조용히 폴백하면 낡은 CAPE 가 '정상(+0)' 점수로 계속 쓰인다 (2026-09-20 33일 동결 사고와 같은 유형).
# detect_block 은 scrapling-project/main.py 에서 fixture 6건(차단 4 · 정상 2)으로 검증한 판정을
# 그대로 옮긴 순수 함수다 — 네트워크 없이 같은 fixture 로 재검증할 수 있다.
BLOCKED_STATUS_CODES = frozenset({401, 403, 407, 429, 444, 500, 502, 503, 504})
CHALLENGE_MARKERS = (
    "cType: '",                             # Turnstile/Interstitial 챌린지 스크립트
    "challenges.cloudflare.com/turnstile",  # 본문에 직접 박힌 Turnstile 위젯
    "Just a moment",                        # 인터스티셜 제목
)
# ⚠️ '__cf_chl' 은 마커로 쓰지 않는다 — 성공한 페이지(200)에도 들어 있어 정상 응답을 차단으로 오탐한다
CAPE_URL = "https://www.multpl.com/shiller-pe/table/by-month"
CAPE_SANE_RANGE = (5.0, 100.0)   # Shiller CAPE 실측 범위 — 밖이면 파싱 오류로 보고 무효 처리


def detect_block(status: int, html: str) -> Optional[str]:
    """차단/챌린지 페이지로 보이면 이유를, 아니면 None 을 돌려준다 (순수 함수)."""
    if status in BLOCKED_STATUS_CODES:
        return f"HTTP {status}"
    for marker in CHALLENGE_MARKERS:
        if marker in html:
            return f"Cloudflare 챌린지 페이지 (본문에 {marker!r})"
    return None


def _parse_cape(html: str) -> Optional[float]:
    """페이지 본문에서 CAPE 값 추출. 못 찾거나 상식 범위 밖이면 None (다른 숫자 오파싱 방지)."""
    m = re.search(r'Current Shiller PE Ratio is ([\d.]+)', html)
    if not m:
        return None
    value = float(m.group(1))
    return value if CAPE_SANE_RANGE[0] < value < CAPE_SANE_RANGE[1] else None


def fetch_cape_via_requests() -> tuple[Optional[float], Optional[str]]:
    """1차(브라우저 없음, 빠름): requests 로 조회 → (값 또는 None, 실패 사유)."""
    try:
        response = requests.get(CAPE_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
    except Exception as exc:
        return None, f"요청 실패 ({type(exc).__name__})"
    blocked = detect_block(response.status_code, response.text)
    if blocked:
        return None, f"차단 ({blocked})"
    if not response.ok:
        return None, f"HTTP {response.status_code}"
    value = _parse_cape(response.text)
    if value is None:
        return None, "값 파싱 실패 (페이지 개편 의심)"
    return value, None


def fetch_cape_via_browser() -> tuple[Optional[float], Optional[str]]:
    """2차(브라우저): StealthyFetcher 로 차단을 우회해 재시도 → (값 또는 None, 실패 사유).

    scrapling 은 **선택 의존성**이라 지연 임포트한다 — 미설치면 그 사실을 사유로 돌려주고
    캐시 폴백으로 내려간다 (스크립트가 죽지 않게).
    """
    try:
        from scrapling.fetchers import StealthyFetcher   # 브라우저 스택 필요
    except ImportError:
        return None, "scrapling 미설치 (우회 생략)"
    try:
        page = StealthyFetcher.fetch(
            CAPE_URL,
            headless=True,
            network_idle=True,
            solve_cloudflare=True,   # Turnstile/Interstitial 자동 해결
            timeout=90_000,          # Cloudflare 해결에는 60초 이상 권장 (밀리초)
        )
    except Exception as exc:
        return None, f"브라우저 실패 ({type(exc).__name__})"
    blocked = detect_block(page.status, page.html_content)
    if blocked:
        return None, f"차단 ({blocked})"
    value = _parse_cape(page.html_content)
    if value is None:
        return None, "값 파싱 실패 (페이지 개편 의심)"
    return value, None


def _save_cape_cache(cape: float) -> None:
    """성공적으로 조회된 CAPE 값을 캐시 파일에 원자적(atomic)으로 저장"""
    try:
        data = {"cape": cape, "date": datetime.datetime.now().strftime("%Y-%m-%d"), "source": "multpl.com"}
        atomic_write_json(CAPE_CACHE_PATH, data)
    except Exception:
        pass  # 캐시 저장 실패는 치명적이지 않음


def _load_cape_cache() -> Optional[tuple[float, int]]:
    """이전에 캐시된 (CAPE 값, 캐시 나이[일]) 로드. 없으면 None 반환."""
    try:
        if os.path.exists(CAPE_CACHE_PATH):
            with open(CAPE_CACHE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            cached_date = datetime.datetime.strptime(data["date"], "%Y-%m-%d").date()
            age_days = (datetime.date.today() - cached_date).days
            return float(data["cape"]), age_days
    except Exception:
        pass
    return None


def signal_valuation() -> SignalResult:
    """Analyzes Shiller CAPE & EPS. (max 2점)

    수집은 3단계 — ① requests → ② StealthyFetcher(차단 우회) → ③ 캐시 폴백.
    ②·③ 로 내려가면 그 **사유가 detail 에 그대로 남는다** (조용한 폴백 금지, 2026-09-24) —
    이전에는 조회 실패 이유가 사라지고 "캐시 사용" 한 줄만 남아, 차단인지 개편인지 구분할 수 없었다.
    """
    score_total, notes, data_ok = 0, [], True
    cape = None
    cache_age_days = None

    attempts: list[tuple[str, str]] = []
    for label, fetcher in (("requests", fetch_cape_via_requests), ("브라우저", fetch_cape_via_browser)):
        value, reason = fetcher()
        if value is not None:
            cape = value
            if attempts:
                notes.append(f"🚫 1차 조회 실패 ({attempts[0][1]}) → 브라우저 우회로 조회 성공")
            _save_cape_cache(cape)  # 성공 시 캐시 저장
            break
        attempts.append((label, reason))

    # 3차: 두 경로 모두 실패 → 캐시 (사유와 캐시 나이를 함께 남긴다)
    if cape is None:
        reasons = " / ".join(f"{label}: {reason}" for label, reason in attempts)
        cached = _load_cape_cache()
        if cached is None:
            notes.append(f"🚫 실시간 CAPE 조회 실패 ({reasons}) — 캐시도 없음")
            return SignalResult("Valuation Overheat", False, 0, " | ".join(notes),
                                group="leading", data_ok=False)
        cape, cache_age_days = cached
        notes.append(f"🚫 실시간 CAPE 조회 실패 ({reasons}) → {cache_age_days}일 전 캐시로 대체")

    # CAPE 값 평가 — 결측이면 'Normal(+0)' 로 위장하지 않는다 (캐시 파일은 외부 입력, 2026-09-20)
    if not math.isfinite(cape):
        notes.append("CAPE 값 결측 — 판정 불가")
        return SignalResult("Valuation Overheat", False, 0, " | ".join(notes),
                            group="leading", data_ok=False)

    source_tag = " (cached)" if cache_age_days is not None else ""   # 나이는 위 🚫 줄에 이미 적혀 있다
    if cape >= 35:
        score_total += 2
        notes.append(f"CAPE {cape} (Critical){source_tag} (+2)")
    elif cape >= 28:
        score_total += 1
        notes.append(f"CAPE {cape} (Warning){source_tag} (+1)")
    else:
        notes.append(f"CAPE {cape} (Normal){source_tag} (+0)")

    if cache_age_days is not None:
        if cache_age_days > CAPE_CACHE_MAX_AGE_DAYS:
            data_ok = False      # 신선도 기준 초과 = 값이 낡아 판정 근거로 쓸 수 없음 (2026-09-20)
            notes.append(
                f"⚠️ 캐시 신선도 초과 ({cache_age_days}일 > 기준 {CAPE_CACHE_MAX_AGE_DAYS}일) "
                f"— 값이 낡아 판정 근거로 쓸 수 없음 (판정 불가로 집계)"
            )

    return SignalResult("Valuation Overheat", score_total >= 1, score_total, " | ".join(notes),
                        group="leading", data_ok=data_ok)


def signal_leading_indicators() -> SignalResult:
    """경제활동(동행지수 YoY) & Sahm Rule. (max 2점)

    ⚠️ USSLIND(Philly Fed 선행지수)는 2020-02 에 중단된 시리즈라 `fredgraph.csv` 가 그 이후
    데이터를 주지 않아, 값이 1.72 로 6년 넘게 동결된 채 'LEI stable (+0)' 만 반복했다
    (2026-09-20 발견). 같은 발행처의 살아있는 동행지수(USPHCI)로 교체하고, 절대값(< 0) 대신
    전년 대비 감소(YoY < 0)를 위축으로 판정한다 — 실측 검증: 1990-91·2001·2008·2020 침체 구간
    포착, 플래그 발생률 8.2%, 최근 31개월 오경보 0회 (동행지수라 침체 '진행 중'에 반응 =
    확인 그룹의 목적과 부합).
    """
    score_total, notes, data_ok = 0, [], True
    try:
        phci = fred_series("USPHCI", lookback_days=FRED_LOOKBACK_2Y)
        if len(phci) < 13:
            raise ValueError("동행지수 이력 부족 (13개월 미만)")
        yoy = (phci.iloc[-1] / phci.iloc[-13] - 1) * 100          # 12개월 전 대비
        _require_finite(phci.iloc[-1], yoy)      # 'expanding(+0)' 로 위장 금지 (2026-09-20)
        age_days = (datetime.date.today() - phci.index[-1].date()).days
        if age_days > ACTIVITY_MAX_AGE_DAYS:
            # 중단·동결된 시리즈를 조용히 쓰지 않는다 (USSLIND 동결이 이 가드 없이 6년간 unnoticed)
            raise ValueError(f"동행지수 최신 관측이 {age_days}일 전 — 시리즈 중단/지연 의심")
        if yoy < 0:
            score_total += 1
            notes.append(f"경제활동 위축 (동행지수 YoY {yoy:+.1f}%) (+1)")
        else:
            notes.append(f"경제활동 확장 (동행지수 YoY {yoy:+.1f}%) (+0)")

        sahm = fred_series("SAHMREALTIME", lookback_days=FRED_LOOKBACK_2Y)
        _require_finite(sahm.iloc[-1])      # NaN 은 두 비교 모두 False → 'Sahm normal(+0)' 위장 (2026-09-20)
        if sahm.iloc[-1] >= 0.5:
            score_total += 1
            notes.append(f"Sahm Rule triggered ({sahm.iloc[-1]:.2f}%p) (+1)")
        elif sahm.iloc[-1] >= 0.3:
            notes.append(f"Sahm Rule elevated ({sahm.iloc[-1]:.2f}%p) (+0)")
        else:
            notes.append(f"Sahm Rule normal ({sahm.iloc[-1]:.2f}%p) (+0)")
    except Exception as e:
        data_ok = False
        notes.append(get_error_message(e, "Activity/Sahm"))

    return SignalResult("Leading Indicators", score_total >= 1, score_total, " | ".join(notes),
                        group="confirm", data_ok=data_ok)


def signal_momentum_breakdown() -> SignalResult:
    """Momentum & Sector Rotation. (max 2점)"""
    score_total, notes, data_ok = 0, [], True
    try:
        tickers = ["SPY", "XLU", "XLP", "XLV", "XLK", "XLY", "XLI"]
        data = validate_yf_data(yf.download(tickers, period="2y", progress=False), tickers)
        spy = data["SPY"].dropna()
        if len(spy) < 201:
            raise ValueError("Insufficient SPY history")

        ret_200d = (spy.iloc[-1] / spy.iloc[-201] - 1) * 100
        _require_finite(ret_200d)      # 'SPX momentum healthy(+0)' 로 위장 금지 (2026-09-20)
        if ret_200d < 0:
            score_total += 1
            notes.append(f"SPX 200D momentum negative ({ret_200d:.1f}%) (+1)")
        else:
            notes.append(f"SPX momentum healthy ({ret_200d:.1f}%) (+0)")

        # 섹터 로테이션: 방어주 vs 성장주 1개월 수익률 비교
        defensive = ["XLU", "XLP", "XLV"]
        growth = ["XLK", "XLY"]

        def_ret = (data[defensive].iloc[-1] / data[defensive].iloc[-22] - 1).mean() * 100
        grw_ret = (data[growth].iloc[-1] / data[growth].iloc[-22] - 1).mean() * 100

        if not (math.isfinite(def_ret) and math.isfinite(grw_ret)):
            # 결측을 '성장주 주도(+0)' 로 위장하지 않는다 (2026-09-20)
            data_ok = False
            notes.append("섹터 수익률 데이터 결측 — 이 하위신호 판정 불가 (+0)")
        elif def_ret > grw_ret:
            score_total += 1
            notes.append(f"Defensive sectors outperform growth ({def_ret - grw_ret:+.1f}% gap) (+1)")
        else:
            notes.append(f"Growth sectors lead ({grw_ret - def_ret:+.1f}% gap) (+0)")
    except Exception as e:
        data_ok = False
        notes.append(get_error_message(e, "Momentum"))

    return SignalResult("Momentum Strategy", score_total >= 1, score_total, " | ".join(notes),
                        group="confirm", data_ok=data_ok)


# ─────────────────────────────────────────────
# Reporter
# ─────────────────────────────────────────────

def assess_regime(results: list) -> dict:
    """시장 국면 판정 — 이 함수의 docstring이 판정 규칙의 단일 출처(source of truth)다.

    시그널을 두 그룹으로 나눈다:
      - 선행 그룹 (고점 경고, 0~6): Yield Curve / Fed Policy / Valuation(CAPE)
        → '고점 부근'을 알린다 (CAPE 과열·금리 인하 직후·커브 재급등)
      - 확인 그룹 (하락 진행, 0~8): Breadth / Credit Spread / Leading Ind. / Momentum
        → '하락이 실제 진행 중인지'를 확인한다

    판정 규칙:
      - 확인 0점 + 선행 ≥4 → '고점 + 강세장 지속' → LOC_DCA 유리 (2017-06 유형, 전환 모니터링)
      - 확인 0점 + 선행 <4 → '안정적 강세장'   → LOC_DCA 유리
      - 확인 1점 (관심·미세 조짐) → '고점 + 약세 조짐 관찰' → LOC_DCA/스윙 선택 (전환 아님)
      - 확인 2~4점 (하락 진행 조짐) → '고점 + 하락 전환' → 스윙 유리
      - 확인 5점 이상 (하락 진행 다수) → '하락 진행' → 스윙 유리 (2021-08 유형)

    ⭐ data_ok=False (데이터 결측/실패) 신호도 점수는 0 이라 '정상'과 구분되지 않는다 — 그래서
       판정 note 에 결측 신호 개수를 덧붙여 '낙관 쪽으로 기울었을 수 있음'을 함께 알린다 (2026-09-20).
    """
    leading = sum(r.score for r in results if r.group == "leading")
    confirm = sum(r.score for r in results if r.group == "confirm")
    degraded = [r.name for r in results if not r.data_ok]
    if confirm == 0:
        if leading >= 4:
            regime = "고점 + 강세장 지속"
            note = ("고점 경고(선행)가 최고치에 가깝고 하락 진행은 아직 없음 — LOC 즉시 투입이 유리하나, "
                    "2017-06 → 2021-08 전환 직전일 수 있어 확인 그룹(모멘텀·breadth·스프레드·경제활동) 매일 모니터링 필요")
        else:
            regime = "안정적 강세장"
            note = "고점 경고·하락 진행 모두 없음 — LOC 즉시 투입이 유리"
        favorite = "LOC_DCA"
    elif confirm == 1:
        # 확인 1점은 미세 조짐 — 전략 전환 근거로 불충분
        regime = "고점 + 약세 조짐 관찰"
        favorite = "선택"
        note = ("확인 그룹에서 미세 약세 신호 1개 발동 — 전략 전환 근거로는 불충분, "
                "추가 발동 시 스윙 전환 여부 결정")
    elif confirm <= 4:
        regime = "고점 + 하락 전환"
        favorite = "스윙"
        note = ("하락 진행 신호 발동 — LOC는 고점 부근에서 분할을 소진할 위험, "
                "스윙의 ATH 하락 구간 매수가 유리해짐")
    else:
        regime = "하락 진행"
        favorite = "스윙"
        note = "하락 진행 신호 다수 — 스윙의 ATH 하락 구간 매수가 유리 (2021-08 유형)"
    if degraded:
        # 점수 0 은 '정상'과 '판정 불가'가 같은 값 — 결측이 있으면 국면이 낙관 쪽으로 기울므로 함께 알린다
        note += (f" ⚠️ 데이터 결측/실패로 판정 불가인 신호 {len(degraded)}개"
                 f"({', '.join(degraded)}) — 점수가 과소집계됐을 수 있다.")
    return {"leading": leading, "confirm": confirm, "regime": regime,
            "favorite": favorite, "note": note, "degraded": degraded}


def print_report(results: list):
    print(f"\n{'='*72}\n Summary Report: Bear Market Early Warning System\n Generated: {datetime.datetime.now():%Y-%m-%d %H:%M:%S}\n{'='*72}")
    for r in results:
        status = 'Data Error' if not r.data_ok else ('Triggered' if r.triggered else 'Stable')
        print(f"{r.name:<30} | {r.score}/2 | {status}")

    total = sum(r.score for r in results)
    print(f"\nTotal Risk Score: {total} / 14")
    degraded = [r.name for r in results if not r.data_ok]
    if degraded:
        print(f"⚠️ Data Error {len(degraded)}개: {', '.join(degraded)}"
              f" — 점수 0(=정상)으로 잡히므로 실제보다 낮게 평가됐을 수 있음")
    print("=" * 72)

    # ── 국면 판정 (판정 규칙은 assess_regime()의 docstring 참고) ──
    reg = assess_regime(results)
    fav_text = f"{reg['favorite']} 매수 조건 유리" if reg['favorite'] != '선택' else f"{reg['favorite']} — 두 전략 모두 가능"
    print(f"\n [국면 판정] {reg['regime']} → {fav_text}")
    print(f"  선행(고점 경고) {reg['leading']}/6 : Yield Curve · Fed Policy · Valuation(CAPE)")
    print(f"  확인(하락 진행) {reg['confirm']}/8 : Breadth · Credit Spread · Leading Ind. · Momentum")
    print(f"  → {reg['note']}")
    print("=" * 72)


def save_report_to_json(results: list, filename="signal_report.json"):
    data = {
        "timestamp": datetime.datetime.now().isoformat(),
        "total_score": sum(r.score for r in results),
        "degraded_signals": [r.name for r in results if not r.data_ok],   # 판정 불가 신호 (2026-09-20)
        "signals": [{"name": r.name, "score": r.score, "detail": r.detail,
                      "group": r.group, "data_ok": r.data_ok} for r in results]   # group·data_ok: LOC 브리핑이 국면 판정 재현용
    }
    report_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    atomic_write_json(report_path, data)


if __name__ == "__main__":
    results = [
        signal_yield_curve(), signal_market_breadth(), signal_credit_spread(),
        signal_fed_cycle(), signal_valuation(), signal_leading_indicators(),
        signal_momentum_breakdown()
    ]
    print_report(results)
    save_report_to_json(results)