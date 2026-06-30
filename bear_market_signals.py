# -*- coding: utf-8 -*-
"""
=============================================================
 베어마켓 7가지 조기 경보 시스템 (Bear Market 7 Early Warning Signals)
=============================================================

유튜브 영상 슬라이드 기반 구성:
  1. 장단기 금리 역전        - Yield Curve Inversion (역전 후 재정상화 = 가장 위험)
  2. 시장 내부 균열          - A-D Line 추세 이탈 & 신고가/신저가 비율
  3. 신용 스프레드 확대      - HY + IG 스프레드 (채권시장이 먼저 경고한다)
  4. 연준 사이클 분석        - 첫 금리 인하 후 6~12개월 = 최위험 구간
  5. 밸류에이션 과열         - Shiller CAPE 수준 + S&P500 실적 성장 둔화 감지
  6. 선행경제지수            - USSLIND 레벨 & Sahm Rule (0.5%p 룰)
  7. 모멘텀 전략 전용 신호   - SPX 정확히 200거래일 수익률 + 섹터 로테이션 방향

필요 라이브러리:
    pip install yfinance pandas requests

데이터 소스:
  - FRED  : API 키 없이 fredgraph.csv 엔드포인트로 직접 다운로드
  - yfinance : S&P500, 섹터 ETF, NYSE A-D Line(^NYAD)
  - multpl.com : Shiller CAPE (정규식 직접 파싱, lxml 불필요)
"""

import re
import sys
import datetime
import warnings
from dataclasses import dataclass
from typing import Optional

import pandas as pd
import requests

warnings.filterwarnings("ignore")

try:
    import yfinance as yf
except ImportError:
    print("yfinance가 설치되어 있지 않습니다.  pip install yfinance  후 다시 실행하세요.")
    sys.exit(1)


# ─────────────────────────────────────────────
# 공통 유틸
# ─────────────────────────────────────────────

def fred_series(series_id: str, lookback_days: int = 365 * 5) -> pd.Series:
    """FRED fredgraph.csv 무료 엔드포인트 (API 키 불필요)."""
    end   = datetime.date.today()
    start = end - datetime.timedelta(days=lookback_days)
    url   = (
        "https://fred.stlouisfed.org/graph/fredgraph.csv"
        f"?id={series_id}&cosd={start}&coed={end}"
    )
    df = pd.read_csv(url, na_values=".")
    df.columns = ["date", series_id]
    df["date"] = pd.to_datetime(df["date"])
    df = df.dropna().set_index("date")
    return df[series_id].astype(float)


def yf_close(tickers: list, period: str = "2y") -> pd.DataFrame:
    """yfinance 종가 다운로드. 반환값이 None이거나 빈 경우 예외 발생."""
    raw = yf.download(tickers, period=period, interval="1d",
                      progress=False, auto_adjust=True)
    if raw is None or raw.empty:
        raise RuntimeError("yfinance 빈 응답")
    close = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) else raw
    return pd.DataFrame(close)


@dataclass
class SignalResult:
    name: str
    triggered: bool
    score: int          # 0=정상  1=주의  2=경고
    detail: str
    raw_value: Optional[float] = None


# ─────────────────────────────────────────────
# 1. 장단기 금리 역전  (Yield Curve Inversion)
# ─────────────────────────────────────────────

def signal_yield_curve() -> SignalResult:
    """
    FRED T10Y2Y (10년 - 2년 스프레드).

    영상 핵심: "역전 자체보다 역전 이후 재정상화(un-inversion) 구간이 실제 침체 진입과 겹친다."
      - 현재 역전 중                   → 주의 (score 1)
      - 최근 2년 내 역전 경험 + 현재 플러스로 회복 → 경고 (score 2) ← 가장 위험
      - 역전 이력 없고 플러스           → 정상 (score 0)
    """
    try:
        s = fred_series("T10Y2Y", lookback_days=365 * 2)
    except Exception as e:
        return SignalResult("장단기 금리 역전", False, 0, f"데이터 오류: {e}")

    current      = s.iloc[-1]
    min_2y       = s.min()           # 최근 2년 최저치
    was_inverted = min_2y < 0        # 역전 경험 여부
    re_normalized = was_inverted and current > 0   # 역전 후 재정상화

    if re_normalized:
        # 역전이 얼마나 깊었는지 함께 표시
        deepest = min_2y
        score, triggered = 2, True
        detail = (
            f"역전 후 재정상화 진행 중 (현재 {current:+.2f}%p, "
            f"최근 2년 최저 {deepest:.2f}%p) "
            f"→ 역사적으로 침체 진입과 가장 자주 겹치는 패턴"
        )
    elif current < 0:
        score, triggered = 1, True
        detail = f"현재 역전 상태 ({current:.2f}%p) → 아직 재정상화 전, 추세 주시"
    else:
        score, triggered = 0, False
        detail = f"정상 우상향 ({current:+.2f}%p), 최근 2년 역전 이력 없음"

    return SignalResult("장단기 금리 역전", triggered, score, detail, current)


# ─────────────────────────────────────────────
# 2. 시장 내부 균열  (Market Breadth)
# ─────────────────────────────────────────────

def signal_market_breadth() -> SignalResult:
    """
    영상 슬라이드: "A-D Line & 신고가/신저가"

    (a) NYSE A-D Line (^NYAD, yfinance) — 추세 이탈 여부
        지수가 신고가인데 A-D Line이 고점을 갱신 못하면 '다이버전스 = 내부 균열'
    (b) RSP(동일가중) / SPY(시총가중) 상대강도 — 소수 대형주 쏠림 감지
        RSP 약세 = 소수 종목이 지수를 견인 중 = breadth 붕괴

    두 항목 각 1점, 합산 0~2점.
    """
    score_total = 0
    notes = []

    # (a) NYSE 종합지수(^NYA) vs SPY 다이버전스
    # ^NYAD는 야후 파이낸스에서 상장폐지됨.
    # ^NYA(NYSE Composite, 약 2,000개 종목)를 A-D Line 대체 지표로 사용.
    # SPY가 신고가 근접인데 ^NYA가 고점을 못 넘으면 → 소수 대형주만 끌고 가는 내부 균열.
    MIN_BARS = 61
    try:
        raw = yf_close(["^NYA", "SPY"], period="1y")
        has_nya = "^NYA" in raw.columns
        has_spy = "SPY"  in raw.columns

        if has_nya and has_spy:
            nya = raw["^NYA"].dropna()
            spy = raw["SPY"].dropna()

            if len(nya) < MIN_BARS or len(spy) < MIN_BARS:
                notes.append(f"^NYA 데이터 부족 ({len(nya)}행 < {MIN_BARS}) → breadth 분석 생략")
            else:
                spy_60_max = float(spy.tail(60).max())
                nya_60_max = float(nya.tail(60).max())
                spy_last   = float(spy.iloc[-1])
                nya_last   = float(nya.iloc[-1])

                spy_new_high = spy_last >= spy_60_max * 0.99
                nya_new_high = nya_last >= nya_60_max * 0.99

                if spy_new_high and not nya_new_high:
                    score_total += 1
                    nya_vs_peak = (nya_last / nya_60_max - 1) * 100
                    notes.append(
                        f"SPY 신고가 근접 + NYSE종합(^NYA) 고점 대비 {nya_vs_peak:.1f}% 미달 "
                        f"→ 대형주 쏠림 다이버전스 (breadth 붕괴 전조)"
                    )
                else:
                    notes.append(
                        f"NYA 다이버전스 없음 "
                        f"(SPY {spy_last:,.1f} / NYA {nya_last:,.1f}, 동행 중)"
                    )
        else:
            notes.append("^NYA 데이터 없음 → breadth 분석 생략")
    except Exception as e:
        notes.append(f"^NYA 조회 실패: {e}")

    # (b) RSP/SPY 상대강도 (3개월 변화)
    try:
        raw2 = yf_close(["SPY", "RSP"], period="1y")
        if "SPY" in raw2.columns and "RSP" in raw2.columns:
            rel = raw2["RSP"] / raw2["SPY"]
            chg_3m = (rel.iloc[-1] / rel.iloc[-63] - 1) * 100 if len(rel) > 63 else float("nan")
            if chg_3m < -3:
                score_total += 1
                notes.append(
                    f"동일가중(RSP)/시총가중(SPY) 3개월 {chg_3m:.1f}% 약화 "
                    f"→ 소수 대형주 쏠림, 시장 폭 심각하게 좁아짐"
                )
            elif chg_3m < -1:
                score_total += 1
                notes.append(f"RSP/SPY 3개월 {chg_3m:.1f}% 약화 → 쏠림 진행 중, 주의")
            else:
                notes.append(f"RSP/SPY 3개월 {chg_3m:.1f}% → 시장 폭 양호")
        else:
            notes.append("RSP/SPY 데이터 없음")
    except Exception as e:
        notes.append(f"RSP/SPY 조회 실패: {e}")

    score     = min(score_total, 2)
    triggered = score >= 1
    return SignalResult("시장 내부 균열", triggered, score, " | ".join(notes))


# ─────────────────────────────────────────────
# 3. 신용 스프레드 확대  (Credit Spread Widening)
# ─────────────────────────────────────────────

def signal_credit_spread() -> SignalResult:
    """
    영상 슬라이드: "채권 시장이 먼저 경고한다"

    HY(하이일드)와 IG(투자등급) 두 스프레드를 모두 확인.
    - HY 스프레드가 먼저 벌어지고 (위험 선호 약화)
    - IG 스프레드까지 확대되면 → 전면적 신용 위기로 번지는 전조

    FRED:
      BAMLH0A0HYM2 : ICE BofA US HY OAS (하이일드)
      BAMLC0A0CM   : ICE BofA US IG OAS (투자등급)
    """
    score_total = 0
    notes = []

    # HY 스프레드
    try:
        hy = fred_series("BAMLH0A0HYM2", lookback_days=365 * 2)
        hy_now      = hy.iloc[-1]
        hy_low_3m   = hy.tail(63).min()
        hy_widen    = hy_now - hy_low_3m
        if hy_now > 6.0 or hy_widen > 1.5:
            score_total += 1
            notes.append(f"HY 스프레드 {hy_now:.2f}%p (3개월 저점比 +{hy_widen:.2f}%p) → 경고")
        elif hy_now > 4.5 or hy_widen > 0.7:
            score_total += 1
            notes.append(f"HY 스프레드 {hy_now:.2f}%p (+{hy_widen:.2f}%p) → 주의")
        else:
            notes.append(f"HY 스프레드 {hy_now:.2f}%p → 안정")
    except Exception as e:
        notes.append(f"HY 스프레드 조회 실패: {e}")

    # IG 스프레드
    try:
        ig = fred_series("BAMLC0A0CM", lookback_days=365 * 2)
        ig_now    = ig.iloc[-1]
        ig_low_3m = ig.tail(63).min()
        ig_widen  = ig_now - ig_low_3m
        if ig_now > 2.0 or ig_widen > 0.5:
            score_total += 1
            notes.append(
                f"IG 스프레드 {ig_now:.2f}%p (+{ig_widen:.2f}%p) → 투자등급까지 확산, 위험 고조"
            )
        else:
            notes.append(f"IG 스프레드 {ig_now:.2f}%p → 안정 (HY만 벌어진 초기 단계)")
    except Exception as e:
        notes.append(f"IG 스프레드 조회 실패: {e}")

    score     = min(score_total, 2)
    triggered = score >= 1
    return SignalResult("신용 스프레드 확대", triggered, score, " | ".join(notes))


# ─────────────────────────────────────────────
# 4. 연준 사이클 분석  (Fed Policy Cycle)
# ─────────────────────────────────────────────

def signal_fed_cycle() -> SignalResult:
    """
    영상 슬라이드 핵심:
      인상 시작 → 고점 유지(경제 둔화) → 첫 금리 인하(실제 침체 신호) → 침체/베어마켓

    '첫 금리 인하의 역설' : 인하 = 시장 호재가 아닌 연준의 경기 악화 공식 인정.
    역사적으로 첫 인하 후 6~12개월 내 침체 확률 ~70% (2001-01, 2007-09 사례).

    로직:
      - 최근 8년 FEDFUNDS에서 사이클 고점을 찾고
      - 고점 이후 최초 인하 시점을 탐색
      - 현재가 그 시점으로부터 몇 개월째인지 계산
        0~6개월   → score 2 (위험 진입)
        6~12개월  → score 2 (최위험 구간, ~70% 침체 확률)
        12~24개월 → score 1 (후행 영향 잔존 가능)
        24개월 초과 → score 0 (윈도우 경과)
    """
    try:
        s = fred_series("FEDFUNDS", lookback_days=365 * 8)
    except Exception as e:
        return SignalResult("연준 사이클 분석", False, 0, f"데이터 오류: {e}")

    s = s.resample("ME").last().dropna()
    if len(s) < 6:
        return SignalResult("연준 사이클 분석", False, 0, "데이터 부족")

    # 사이클 고점: 최근 8년 최고치 — idxmax()로 라벨 직접 취득 (positional index 불필요)
    peak_date  = s.idxmax()
    peak_value = float(s.max())
    current_rate = float(s.iloc[-1])

    # 고점 이후 첫 인하 탐색
    after_peak     = s.loc[peak_date:]
    first_cut_date = None
    for i in range(1, len(after_peak)):
        if after_peak.iloc[i] < after_peak.iloc[i - 1]:
            first_cut_date = after_peak.index[i]
            break

    if first_cut_date is None:
        score, triggered = 0, False
        detail = (
            f"현재 기준금리 {current_rate:.2f}% | "
            f"사이클 고점 {peak_value:.2f}% ({peak_date:%Y-%m}) | "
            f"아직 첫 인하 없음 → 고점 유지 단계, 인하 전환 시점 모니터링"
        )
        return SignalResult("연준 사이클 분석", triggered, score, detail, current_rate)

    months = (s.index[-1].to_period("M") - first_cut_date.to_period("M")).n

    if months <= 12:
        score, triggered = 2, True
        zone = "최위험(6~12개월)" if months >= 6 else "위험 진입(0~6개월)"
        detail = (
            f"첫 금리 인하 {first_cut_date:%Y-%m} | 경과 {months}개월 → {zone} | "
            f"역사적 침체 확률 ~70% (2001·2007 유사 패턴) | 현재 {current_rate:.2f}%"
        )
    elif months <= 24:
        score, triggered = 1, True
        detail = (
            f"첫 인하 {first_cut_date:%Y-%m} | {months}개월 경과 → "
            f"12개월 위험 윈도우 통과, 후행 영향 잔존 가능 | 현재 {current_rate:.2f}%"
        )
    else:
        score, triggered = 0, False
        detail = (
            f"첫 인하 {first_cut_date:%Y-%m} | {months}개월 경과 → "
            f"위험 윈도우(24개월) 종료 | 다음 사이클 고점 재형성 여부 관찰"
        )

    return SignalResult("연준 사이클 분석", triggered, score, detail, current_rate)


# ─────────────────────────────────────────────
# 5. 밸류에이션 과열  (Shiller CAPE & 실적 피크)
# ─────────────────────────────────────────────

CAPE_FALLBACK = 38.0   # 스크래핑 실패 시 수동 갱신

def signal_valuation() -> SignalResult:
    """
    영상 슬라이드: "Shiller CAPE & 실적 피크 — 거품의 언어"

    (a) Shiller CAPE  : multpl.com 정규식 파싱
        역사 평균 ≈17, 위험권 ≥30, 극단 과열 ≥35 (닷컴 정점 44)
    (b) 실적 피크 감지 : S&P500 12개월 trailing EPS 성장률 둔화
        multpl.com의 S&P500 Earnings 페이지에서 파싱.
        EPS가 전년比 감소 또는 성장률이 급격히 둔화되면 '실적 피크' 신호로 해석.

    각 항목 1점, 합산 0~2점.
    """
    score_total = 0
    notes = []
    row_pat = re.compile(
        r"<tr[^>]*>\s*<td>([^<]+)</td>\s*<td>\s*(?:&#x2002;|&nbsp;|\s)*([\d.]+)\s*</td>\s*</tr>",
        re.IGNORECASE,
    )
    hdrs = {"User-Agent": "Mozilla/5.0"}

    # (a) Shiller CAPE
    cape_val = CAPE_FALLBACK
    cape_note = f"폴백 값({CAPE_FALLBACK}) 사용 — CAPE_FALLBACK 수동 갱신 권장"
    try:
        r = requests.get("https://www.multpl.com/shiller-pe/table/by-month",
                         headers=hdrs, timeout=10)
        r.raise_for_status()
        m = row_pat.findall(r.text)
        if m:
            cape_val  = float(m[0][1])
            cape_note = f"multpl.com ({m[0][0].strip()})"
    except Exception as e:
        cape_note = f"스크래핑 실패({e}) → {cape_note}"

    if cape_val >= 35:
        score_total += 1
        notes.append(f"Shiller CAPE {cape_val:.1f} → 극단 과열 (닷컴버블 근접) [{cape_note}]")
    elif cape_val >= 28:
        score_total += 1
        notes.append(f"Shiller CAPE {cape_val:.1f} → 과열 구간 [{cape_note}]")
    else:
        notes.append(f"Shiller CAPE {cape_val:.1f} → 정상~중립 [{cape_note}]")

    # (b) S&P500 실적 피크 — trailing EPS YoY 성장률
    try:
        r2 = requests.get("https://www.multpl.com/s-p-500-earnings/table/by-year",
                          headers=hdrs, timeout=10)
        r2.raise_for_status()
        rows = row_pat.findall(r2.text)
        # 연간 테이블: 최신 2개 연도의 EPS 비교
        eps_vals = []
        for date_str, val_str in rows[:4]:
            try:
                eps_vals.append((date_str.strip(), float(val_str)))
            except ValueError:
                continue
        if len(eps_vals) >= 2:
            latest_eps  = eps_vals[0][1]
            prev_eps    = eps_vals[1][1]
            eps_growth  = (latest_eps / prev_eps - 1) * 100
            if eps_growth < -5:
                score_total += 1
                notes.append(
                    f"S&P500 EPS {eps_vals[0][0]} {latest_eps:.1f} "
                    f"(전년比 {eps_growth:.1f}%) → 실적 역성장, 피크아웃 신호"
                )
            elif eps_growth < 5:
                score_total += 1
                notes.append(
                    f"S&P500 EPS 성장률 {eps_growth:.1f}% (둔화 구간) "
                    f"→ 고밸류에이션 + 실적 정체 = 복합 위험"
                )
            else:
                notes.append(f"S&P500 EPS 성장률 {eps_growth:.1f}% → 실적 확장 지속 중")
        else:
            notes.append("EPS 연간 데이터 파싱 실패 (최소 2개 연도 필요)")
    except Exception as e:
        notes.append(f"EPS 데이터 조회 실패: {e}")

    score     = min(score_total, 2)
    triggered = score >= 1
    return SignalResult("밸류에이션 과열", triggered, score, " | ".join(notes), cape_val)


# ─────────────────────────────────────────────
# 6. 선행경제지수  (LEI & Sahm Rule)
# ─────────────────────────────────────────────

def signal_leading_indicators() -> SignalResult:
    """
    영상 슬라이드: "LEI & Sahm Rule — 경기의 체온계"

    (a) FRED USSLIND (Philadelphia Fed Leading Index)
        이 시리즈는 이미 '연율화 성장률 전망치(%)'로 표현된 값이다.
        → % 변화율을 다시 계산하는 것이 아니라 레벨(값 자체)과 방향을 본다.
          현재값 < 0         → 경기 수축 예고  (score +1)
          현재값 < 0 + 하락 추세 → 더 강한 위험  (score +1)

    (b) FRED SAHMREALTIME (Sahm Rule Recession Indicator)
        실업률 3개월 이동평균 - 최근 12개월 최저치.
        ≥ 0.5%p → 과거 모든 침체에서 발동, 높은 신뢰도의 침체 신호.
    """
    score_total = 0
    notes = []

    # (a) USSLIND 레벨 기반 판단
    try:
        lei = fred_series("USSLIND", lookback_days=365 * 2)
        lei_now   = lei.iloc[-1]
        lei_3m_ago = lei.iloc[-3] if len(lei) > 3 else lei.iloc[0]
        lei_trend = lei_now - lei_3m_ago   # 3개월 방향

        if lei_now < 0:
            score_total += 1
            direction = "하락 지속" if lei_trend < 0 else "소폭 반등 중"
            notes.append(
                f"선행지수(USSLIND) {lei_now:.2f}% (마이너스 = 경기 수축 예고) "
                f"| 3개월 추세: {direction} → 경기 둔화 신호"
            )
            if lei_trend < 0:
                score_total += 1   # 방향까지 악화 중이면 추가 1점
        else:
            notes.append(
                f"선행지수(USSLIND) {lei_now:.2f}% (플러스 = 경기 확장 예고) "
                f"| 3개월 방향: {'+' if lei_trend >= 0 else ''}{lei_trend:.2f}%p"
            )
    except Exception as e:
        notes.append(f"USSLIND 조회 실패: {e}")

    # (b) Sahm Rule
    try:
        sahm = fred_series("SAHMREALTIME", lookback_days=365 * 2)
        sahm_now = sahm.iloc[-1]
        if sahm_now >= 0.5:
            score_total += 2
            notes.append(
                f"Sahm Rule {sahm_now:.2f}%p ≥ 0.5 → 침체 신호 공식 발동 "
                f"(과거 모든 침체에서 적중)"
            )
        elif sahm_now >= 0.3:
            score_total += 1
            notes.append(f"Sahm Rule {sahm_now:.2f}%p → 기준선(0.5) 근접, 주의")
        else:
            notes.append(f"Sahm Rule {sahm_now:.2f}%p → 안정")
    except Exception as e:
        notes.append(f"SAHMREALTIME 조회 실패: {e}")

    score     = min(score_total, 2)
    triggered = score >= 1
    return SignalResult("선행경제지수 (LEI & Sahm)", triggered, score, " | ".join(notes))


# ─────────────────────────────────────────────
# 7. 모멘텀 전략 전용 신호  (섹터 로테이션 & SPX 200R)
# ─────────────────────────────────────────────

def signal_momentum_breakdown() -> SignalResult:
    """
    영상 슬라이드: "섹터 로테이션 & SPX 200R — 모멘텀 내부 붕괴 감지"

    (a) SPX 200일 수익률 (SPX 200R)
        정확히 200거래일 전 가격 대비 현재 수익률.
        마이너스 → 중기 추세 자체가 하락, 모멘텀 전략의 기본 전제 붕괴.

    (b) 섹터 로테이션 방향
        방어섹터(XLU/XLP/XLV) vs 경기민감섹터(XLK/XLY/XLI) 3개월 상대수익률.
        방어섹터 아웃퍼폼 → 스마트머니가 리스크 회피로 이동 중 = 내부 붕괴 선행 신호.

    각 항목 1점, 합산 0~2점.
    """
    score_total = 0
    notes = []

    tickers = ["SPY", "XLU", "XLP", "XLV", "XLK", "XLY", "XLI"]
    try:
        raw = yf_close(tickers, period="2y")
        if raw is None or raw.empty:
            return SignalResult("모멘텀 전략 전용 신호", False, 0, "데이터 없음")
    except Exception as e:
        return SignalResult("모멘텀 전략 전용 신호", False, 0, f"다운로드 실패: {e}")

    ret_200d: float = float("nan")
    # (a) SPX 200거래일 수익률 — iloc[-201] ~ iloc[-1]
    try:
        spy: pd.Series = (
            raw["SPY"].dropna() if "SPY" in raw.columns
            else raw.iloc[:, 0].dropna()   # 첫 번째 컬럼을 Series로 명시 추출
        )
        if len(spy) >= 201:
            ret_200d = (float(spy.iloc[-1]) / float(spy.iloc[-201]) - 1) * 100
        else:
            ret_200d = (float(spy.iloc[-1]) / float(spy.iloc[0]) - 1) * 100

        if ret_200d < 0:
            score_total += 1
            notes.append(
                f"SPX 200거래일 수익률 {ret_200d:.1f}% (마이너스) "
                f"→ 중기 추세 붕괴, 모멘텀 전략 기본 전제 훼손"
            )
        else:
            notes.append(f"SPX 200거래일 수익률 {ret_200d:.1f}% → 중기 추세 양호")
    except Exception as e:
        notes.append(f"SPX 200R 계산 실패: {e}")
        ret_200d = float("nan")

    # (b) 섹터 로테이션: 3개월(63거래일) 수익률 비교
    def basket_ret_3m(tks):
        rets = []
        for t in tks:
            col = raw[t] if t in raw.columns else None
            if col is not None:
                col = col.dropna()
                if len(col) > 63:
                    rets.append(col.iloc[-1] / col.iloc[-64] - 1)
        return sum(rets) / len(rets) * 100 if rets else float("nan")

    try:
        def_ret = basket_ret_3m(["XLU", "XLP", "XLV"])
        cyc_ret = basket_ret_3m(["XLK", "XLY", "XLI"])
        gap = def_ret - cyc_ret

        if gap > 5:
            score_total += 1
            notes.append(
                f"방어섹터 3개월 {def_ret:.1f}% vs 경기민감 {cyc_ret:.1f}% "
                f"(+{gap:.1f}%p 방어 우위) → 스마트머니 리스크 회피 이동, 내부 붕괴 신호"
            )
        elif gap > 2:
            score_total += 1
            notes.append(
                f"방어섹터 {def_ret:.1f}% vs 경기민감 {cyc_ret:.1f}% "
                f"(+{gap:.1f}%p) → 방어주 로테이션 시작 조짐, 주의"
            )
        else:
            notes.append(
                f"방어섹터 {def_ret:.1f}% vs 경기민감 {cyc_ret:.1f}% "
                f"({gap:+.1f}%p) → 경기민감 우위, 정상 성장 국면"
            )
    except Exception as e:
        notes.append(f"섹터 로테이션 계산 실패: {e}")

    score     = min(score_total, 2)
    triggered = score >= 1
    return SignalResult("모멘텀 전략 전용 신호", triggered, score, " | ".join(notes), ret_200d)


# ─────────────────────────────────────────────
# 종합 실행 & 리포트
# ─────────────────────────────────────────────

def run_all_signals() -> list:
    funcs = [
        signal_yield_curve,
        signal_market_breadth,
        signal_credit_spread,
        signal_fed_cycle,
        signal_valuation,
        signal_leading_indicators,
        signal_momentum_breakdown,
    ]
    results = []
    for i, fn in enumerate(funcs, 1):
        print(f"[{i}/7] {fn.__name__} 계산 중...")
        try:
            results.append(fn())
        except Exception as e:
            results.append(SignalResult(fn.__name__, False, 0, f"오류: {e}"))
    return results


def print_report(results: list):
    icons = {0: "🟢", 1: "🟡", 2: "🔴"}
    print("\n" + "=" * 72)
    print(" 베어마켓 7가지 조기 경보 시스템 — 종합 리포트")
    print(f" 생성 시각: {datetime.datetime.now():%Y-%m-%d %H:%M:%S}")
    print("=" * 72)

    total = 0
    for idx, r in enumerate(results, 1):
        icon = icons.get(r.score, "⚪")
        print(f"\n{idx}. {icon} [{r.name}]  (점수 {r.score}/2)")
        # detail이 길면 파이프로 구분된 항목을 줄 바꿔 출력
        for part in r.detail.split(" | "):
            print(f"   {part.strip()}")
        total += r.score

    pct = total / (len(results) * 2) * 100
    print("\n" + "─" * 72)
    print(f" 종합 경보 점수: {total} / {len(results) * 2}  ({pct:.0f}%)")

    if   pct >= 65: verdict = "🔴 고위험 — 다수 지표 동시 경고 → 헷지/현금비중 확대 검토"
    elif pct >= 35: verdict = "🟡 주의   — 일부 경고 점등 → 추가 모니터링 필요"
    else:           verdict = "🟢 안정   — 대부분 지표 정상 범위"

    print(f" 종합 판정: {verdict}")
    print("=" * 72)
    print("\n※ 본 결과는 투자 자문이 아니며 참고용 정량 신호입니다.")
    print("※ 최종 매매 결정은 본인의 추가 검증과 판단을 거쳐 진행하세요.\n")


def export_to_dict(results: list) -> dict:
    """Discord 알림 등 외부 모듈 연동용 dict 변환."""
    return {
        "timestamp":   datetime.datetime.now().isoformat(),
        "signals":     [{"name": r.name, "triggered": r.triggered,
                         "score": r.score, "detail": r.detail} for r in results],
        "total_score": sum(r.score for r in results),
        "max_score":   len(results) * 2,
    }


if __name__ == "__main__":
    results = run_all_signals()
    print_report(results)
