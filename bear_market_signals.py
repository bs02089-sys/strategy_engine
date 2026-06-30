# -*- coding: utf-8 -*-
"""
=============================================================
 베어마켓 7가지 조기 경보 시스템 (Bear Market 7 Early Warning Signals)
=============================================================

유튜브 영상 슬라이드 기반 구성:
  1. 장단기 금리 역전        - Yield Curve Inversion
  2. 시장 내부 균열          - Market Breadth Deterioration (A-D Line & 신고가/신저가)
  3. 신용 스프레드 확대      - Credit Spread Widening
  4. 연준 사이클 분석        - Fed Policy Cycle (첫 금리 인하의 역설)
  5. 밸류에이션 과열         - Shiller CAPE & 실적 피크
  6. 선행경제지수            - LEI & Sahm Rule
  7. 모멘텀 전략 전용 신호   - 섹터 로테이션 & SPX 200일 수익률

필요 라이브러리:
    pip install yfinance pandas requests beautifulsoup4 lxml

데이터 소스:
  - FRED (세인트루이스 연은) : API 키 없이 fredgraph.csv 엔드포인트로 직접 다운로드
  - yfinance                : 야후 파이낸스 (지수, ETF, 개별 종목)
  - multpl.com              : Shiller CAPE 테이블 스크래핑 (무료, API 키 불필요)

주의:
  - FRED, Yahoo Finance 접속은 사용자 PC(VS Code) 네트워크 환경에서 정상 동작해야 합니다.
  - 일부 사이트(multpl.com 등)는 구조가 바뀌면 스크래핑이 깨질 수 있습니다.
    그 경우 CAPE_FALLBACK 값을 수동으로 갱신해서 사용하세요.
  - 이 코드는 투자 자문이 아니며, 신호는 참고용입니다. 최종 판단은 본인 몫입니다.
"""

import sys
import datetime
import warnings
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd
import requests

warnings.filterwarnings("ignore")

try:
    import yfinance as yf
except ImportError:
    print("yfinance가 설치되어 있지 않습니다. `pip install yfinance` 후 다시 실행하세요.")
    sys.exit(1)


# =============================================================
# 공통 유틸 - FRED 데이터 무료 다운로드 (API 키 불필요)
# =============================================================

def fred_series(series_id: str, lookback_days: int = 365 * 5) -> pd.Series:
    """
    FRED의 fredgraph.csv 엔드포인트를 이용해 시계열을 가져온다.
    API 키가 필요 없는 공개 다운로드 경로다.
    """
    end = datetime.date.today()
    start = end - datetime.timedelta(days=lookback_days)
    url = (
        "https://fred.stlouisfed.org/graph/fredgraph.csv"
        f"?id={series_id}&cosd={start.isoformat()}&coed={end.isoformat()}"
    )
    try:
        df = pd.read_csv(url, na_values=".")
    except Exception as e:
        raise RuntimeError(f"[FRED:{series_id}] 다운로드 실패: {e}")

    df.columns = ["date", series_id]
    df["date"] = pd.to_datetime(df["date"])
    df = df.dropna().set_index("date")
    return df[series_id].astype(float)


def pct_change_window(series: pd.Series, days: int) -> float:
    if len(series) < 2:
        return float("nan")
    recent = series.iloc[-1]
    past_idx = series.index[-1] - pd.Timedelta(days=days)
    past_series = series[series.index <= past_idx]
    if past_series.empty:
        past = series.iloc[0]
    else:
        past = past_series.iloc[-1]
    if past == 0:
        return float("nan")
    return (recent - past) / abs(past) * 100


# =============================================================
# 신호별 결과를 담는 데이터 구조
# =============================================================

@dataclass
class SignalResult:
    name: str
    triggered: bool
    score: int  # 0~2 (0=정상, 1=주의, 2=경고)
    detail: str
    raw_value: Optional[float] = None


# =============================================================
# 1. 장단기 금리 역전 (Yield Curve Inversion)
# =============================================================

def signal_yield_curve() -> SignalResult:
    """
    FRED T10Y2Y (10년물 - 2년물 스프레드) 사용.
    - 마이너스(역전) 자체보다, '역전 이후 다시 플러스로 정상화(un-inversion)'되는
      구간이 침체 진입의 전형적 선행 신호로 알려져 있다.
    """
    try:
        s = fred_series("T10Y2Y", lookback_days=365 * 3)
    except RuntimeError as e:
        return SignalResult("장단기 금리 역전", False, 0, str(e))

    current = s.iloc[-1]
    min_recent = s.tail(252).min()  # 최근 1년 최저치
    was_inverted = min_recent < 0
    now_normalizing = was_inverted and current > 0

    if now_normalizing:
        score, triggered = 2, True
        detail = (
            f"최근 1년 내 역전(최저 {min_recent:.2f}%p) 이후 "
            f"현재 {current:.2f}%p로 '재정상화' 진행 중 -> 침체 선행 패턴과 유사"
        )
    elif current < 0:
        score, triggered = 1, True
        detail = f"현재 역전 상태 ({current:.2f}%p). 아직 재정상화 전 단계"
    else:
        score, triggered = 0, False
        detail = f"정상 우상향 ({current:.2f}%p), 최근 1년 역전 이력 없음"

    return SignalResult("장단기 금리 역전", triggered, score, detail, current)


# =============================================================
# 2. 시장 내부 균열 (Market Breadth Deterioration)
# =============================================================

def signal_market_breadth() -> SignalResult:
    """
    엄밀한 NYSE A-D Line(등락주선) 원자료는 무료 API로 구하기 까다로워,
    실전에서 흔히 쓰는 두 가지 프록시(proxy)로 대체한다.

      (a) S&P500(SPY) vs 동일가중 S&P500(RSP) 상대강도
          -> RSP가 SPY 대비 약세면 소수 대형주가 지수를 끌고 가는 중
             (= 내부 균열, breadth 약화)
      (b) S&P500 신고가 갱신 ETF 가격이 200일선 위/아래 여부로 추세 폭 확인
    """
    try:
        raw = yf.download(
            ["SPY", "RSP"], period="1y", interval="1d",
            progress=False, auto_adjust=True
        )
        if raw is None or raw.empty:
            return SignalResult("시장 내부 균열", False, 0, "데이터 다운로드 실패: 빈 응답")
        data = raw["Close"]
    except Exception as e:
        return SignalResult("시장 내부 균열", False, 0, f"데이터 다운로드 실패: {e}")

    if data.empty or "SPY" not in data or "RSP" not in data:
        return SignalResult("시장 내부 균열", False, 0, "SPY/RSP 데이터 없음")

    rel_strength = (data["RSP"] / data["SPY"])
    rel_strength_now = rel_strength.iloc[-1]
    rel_strength_3m_ago = rel_strength.iloc[-63] if len(rel_strength) > 63 else rel_strength.iloc[0]
    breadth_decline_pct = (rel_strength_now / rel_strength_3m_ago - 1) * 100

    spy_close = data["SPY"]
    spy_200dma = spy_close.rolling(200).mean()
    spy_above_200 = spy_close.iloc[-1] > spy_200dma.iloc[-1] if not spy_200dma.iloc[-1] != spy_200dma.iloc[-1] else None

    if breadth_decline_pct < -3:
        score, triggered = 2, True
        detail = (
            f"동일가중(RSP) 대비 시총가중(SPY) 상대강도 {breadth_decline_pct:.1f}% 약화 (3개월) "
            f"-> 소수 대형주 쏠림 심화, 내부 균열 신호"
        )
    elif breadth_decline_pct < -1:
        score, triggered = 1, True
        detail = f"RSP/SPY 상대강도 {breadth_decline_pct:.1f}% 약화 (3개월) -> 주의 관찰 구간"
    else:
        score, triggered = 0, False
        detail = f"RSP/SPY 상대강도 {breadth_decline_pct:.1f}% (3개월), 시장 폭 양호"

    return SignalResult("시장 내부 균열", triggered, score, detail, breadth_decline_pct)


# =============================================================
# 3. 신용 스프레드 확대 (Credit Spread Widening)
# =============================================================

def signal_credit_spread() -> SignalResult:
    """
    FRED BAMLH0A0HYM2 (ICE BofA US High Yield Index Option-Adjusted Spread) 사용.
    채권시장은 주식시장보다 먼저 경고하는 경향이 있다고 알려져 있다.
    """
    try:
        s = fred_series("BAMLH0A0HYM2", lookback_days=365 * 2)
    except RuntimeError as e:
        return SignalResult("신용 스프레드 확대", False, 0, str(e))

    current = s.iloc[-1]
    low_3m = s.tail(63).min()
    widen_from_low = current - low_3m  # %p

    if current > 6.0 or widen_from_low > 1.5:
        score, triggered = 2, True
        detail = (
            f"HY 스프레드 {current:.2f}%p, 최근 3개월 저점 대비 +{widen_from_low:.2f}%p 확대 "
            f"-> 신용시장 경고 신호 발동"
        )
    elif current > 4.5 or widen_from_low > 0.7:
        score, triggered = 1, True
        detail = f"HY 스프레드 {current:.2f}%p, 저점 대비 +{widen_from_low:.2f}%p -> 주의"
    else:
        score, triggered = 0, False
        detail = f"HY 스프레드 {current:.2f}%p, 안정적 수준"

    return SignalResult("신용 스프레드 확대", triggered, score, detail, current)


# =============================================================
# 4. 연준 사이클 분석 (Fed Policy Cycle - 첫 금리 인하의 역설)
# =============================================================

def signal_fed_cycle() -> SignalResult:
    """
    FRED FEDFUNDS (실효 기준금리) 사용.
    '첫 금리 인하의 역설': 연준이 금리를 올리다가 처음 내리기 시작하는 시점이
    오히려 침체/약세장 진입과 자주 겹친다는 경험적 패턴.
    """
    try:
        s = fred_series("FEDFUNDS", lookback_days=365 * 3)
    except RuntimeError as e:
        return SignalResult("연준 사이클 분석", False, 0, str(e))

    s = s.resample("ME").last().dropna()
    diffs = s.diff()

    # 최근 12개월 내 '인상 사이클(diffs>0 연속)' 이후 '첫 인하(diff<0)'가 있었는지 확인
    recent = diffs.tail(13)
    first_cut_idx = None
    was_hiking = False
    for i in range(1, len(recent)):
        if recent.iloc[i - 1] > 0:
            was_hiking = True
        if was_hiking and recent.iloc[i] < 0:
            first_cut_idx = recent.index[i]
            break

    if first_cut_idx is not None:
        months_since_cut = (s.index[-1].to_period("M") - first_cut_idx.to_period("M")).n
        score, triggered = 2, True
        detail = (
            f"인상 사이클 이후 첫 금리 인하 감지 ({first_cut_idx.date()}, "
            f"{months_since_cut}개월 경과) -> '첫 인하의 역설' 구간, 경계 필요"
        )
    else:
        current_trend = "인상" if diffs.iloc[-1] > 0 else ("인하" if diffs.iloc[-1] < 0 else "동결")
        score, triggered = 0, False
        detail = f"현재 기준금리 {s.iloc[-1]:.2f}%, 최근 추세: {current_trend}. 첫 인하 시그널 없음"

    return SignalResult("연준 사이클 분석", triggered, score, detail, s.iloc[-1])


# =============================================================
# 5. 밸류에이션 과열 (Shiller CAPE & 실적 피크)
# =============================================================

CAPE_FALLBACK = 35.0  # multpl.com 스크래핑 실패 시 수동 갱신용 폴백 값

def signal_valuation() -> SignalResult:
    """
    multpl.com의 Shiller PE(CAPE) 테이블을 스크래핑.
    역사적으로 CAPE 30 이상은 '과열' 구간, 35 이상은 '극단적 과열' 구간으로 흔히 언급된다.
    (역사적 평균 CAPE ≈ 17, 닷컴버블 정점 ≈ 44)
    """
    url = "https://www.multpl.com/shiller-pe/table/by-month"
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=10)
        try:
            tables = pd.read_html(resp.text, flavor="lxml")
        except (ImportError, ValueError):
            tables = pd.read_html(resp.text, flavor="bs4")
        df = tables[0]
        df.columns = ["date", "cape"]
        df["cape"] = df["cape"].astype(str).str.extract(r"([\d.]+)").astype(float)
        current_cape = df["cape"].iloc[0]
        source_note = "multpl.com 실시간 스크래핑"
    except Exception as e:
        current_cape = CAPE_FALLBACK
        source_note = f"스크래핑 실패({e}) -> 폴백 값 사용, CAPE_FALLBACK 수동 갱신 권장"

    if current_cape >= 35:
        score, triggered = 2, True
        detail = f"Shiller CAPE {current_cape:.1f} -> 역사적 극단 과열 구간 (닷컴버블 수준 근접) [{source_note}]"
    elif current_cape >= 28:
        score, triggered = 1, True
        detail = f"Shiller CAPE {current_cape:.1f} -> 과열 구간, 실적 피크 가능성 점검 필요 [{source_note}]"
    else:
        score, triggered = 0, False
        detail = f"Shiller CAPE {current_cape:.1f} -> 정상~중립 구간 [{source_note}]"

    return SignalResult("밸류에이션 과열", triggered, score, detail, current_cape)


# =============================================================
# 6. 선행경제지수 (LEI & Sahm Rule)
# =============================================================

def signal_leading_indicators() -> SignalResult:
    """
    - USSLIND (FRED, Leading Index for the United States) : 음전환 지속 여부 확인
    - SAHMREALTIME (FRED, Sahm Rule Recession Indicator)   : 0.5%p 이상이면 침체 신호 발동 규칙
    """
    notes = []
    score_total = 0

    try:
        lei = fred_series("USSLIND", lookback_days=365 * 3)
        lei_3m_chg = pct_change_window(lei, 90)
        if lei_3m_chg < -1.5:
            score_total += 2
            notes.append(f"선행지수(USSLIND) 3개월 {lei_3m_chg:.1f}% 하락 -> 강한 둔화 신호")
        elif lei_3m_chg < 0:
            score_total += 1
            notes.append(f"선행지수(USSLIND) 3개월 {lei_3m_chg:.1f}% 하락 -> 둔화 조짐")
        else:
            notes.append(f"선행지수(USSLIND) 3개월 {lei_3m_chg:.1f}% -> 안정")
    except RuntimeError as e:
        notes.append(f"USSLIND 조회 실패: {e}")

    try:
        sahm = fred_series("SAHMREALTIME", lookback_days=365 * 2)
        sahm_now = sahm.iloc[-1]
        if sahm_now >= 0.5:
            score_total += 2
            notes.append(f"Sahm Rule 지표 {sahm_now:.2f}%p (>=0.5 기준 충족) -> 침체 신호 발동 상태")
        elif sahm_now >= 0.3:
            score_total += 1
            notes.append(f"Sahm Rule 지표 {sahm_now:.2f}%p -> 기준선(0.5) 근접, 주의")
        else:
            notes.append(f"Sahm Rule 지표 {sahm_now:.2f}%p -> 안정")
    except RuntimeError as e:
        notes.append(f"SAHMREALTIME 조회 실패: {e}")

    score = min(score_total, 2)
    triggered = score >= 1
    detail = " | ".join(notes)

    return SignalResult("선행경제지수 (LEI & Sahm)", triggered, score, detail)


# =============================================================
# 7. 모멘텀 전략 전용 신호 (섹터 로테이션 & SPX 200일 수익률)
# =============================================================

SECTOR_ETFS = {
    "XLK": "기술", "XLF": "금융", "XLE": "에너지", "XLV": "헬스케어",
    "XLY": "임의소비재", "XLP": "필수소비재", "XLI": "산업재",
    "XLU": "유틸리티", "XLB": "소재", "XLRE": "리츠",
}

def signal_momentum_breakdown() -> SignalResult:
    """
    (a) SPY 200일 수익률(현재가 / 200일전 가격 - 1) -> 모멘텀 추세 강도
    (b) 경기방어 섹터(XLU, XLP, XLV) vs 경기민감 섹터(XLK, XLY, XLI) 상대 로테이션
        -> 방어주로의 자금 이동은 모멘텀 내부 붕괴(리스크 회피 전환) 신호로 해석
    """
    try:
        tickers = ["SPY"] + list(SECTOR_ETFS.keys())
        raw = yf.download(tickers, period="1y", interval="1d",
                           progress=False, auto_adjust=True)
        if raw is None or raw.empty:
            return SignalResult("모멘텀 전략 전용 신호", False, 0, "데이터 다운로드 실패: 빈 응답")
        data = raw["Close"]
    except Exception as e:
        return SignalResult("모멘텀 전략 전용 신호", False, 0, f"데이터 다운로드 실패: {e}")

    if data.empty or "SPY" not in data:
        return SignalResult("모멘텀 전략 전용 신호", False, 0, "SPY 데이터 없음")

    spy = data["SPY"].dropna()
    spx_200d_return = (spy.iloc[-1] / spy.iloc[0] - 1) * 100 if len(spy) > 0 else float("nan")

    defensive = ["XLU", "XLP", "XLV"]
    cyclical = ["XLK", "XLY", "XLI"]

    def basket_return(tks):
        rets = []
        for t in tks:
            if t in data and data[t].dropna().shape[0] > 1:
                series = data[t].dropna()
                rets.append(series.iloc[-1] / series.iloc[0] - 1)
        return sum(rets) / len(rets) * 100 if rets else float("nan")

    def_ret = basket_return(defensive)
    cyc_ret = basket_return(cyclical)
    rotation_gap = def_ret - cyc_ret  # 방어주가 경기민감주보다 더 많이 오르면 양수

    score = 0
    notes = [f"SPY 200일 수익률 {spx_200d_return:.1f}%",
             f"방어섹터(XLU/XLP/XLV) 1년 평균 {def_ret:.1f}% vs 경기민감(XLK/XLY/XLI) {cyc_ret:.1f}%"]

    if spx_200d_return < 0:
        score += 1
        notes.append("-> 200일 추세 자체가 마이너스, 모멘텀 붕괴 진행 중")
    if rotation_gap > 5:
        score += 1
        notes.append(f"-> 방어주 로테이션 +{rotation_gap:.1f}%p, 리스크 회피 전환 신호")

    score = min(score, 2)
    triggered = score >= 1
    detail = " | ".join(notes)

    return SignalResult("모멘텀 전략 전용 신호", triggered, score, detail, spx_200d_return)


# =============================================================
# 종합 실행 & 리포트
# =============================================================

def run_all_signals() -> list:
    signal_funcs = [
        signal_yield_curve,
        signal_market_breadth,
        signal_credit_spread,
        signal_fed_cycle,
        signal_valuation,
        signal_leading_indicators,
        signal_momentum_breakdown,
    ]
    results = []
    for i, fn in enumerate(signal_funcs, 1):
        print(f"[{i}/7] {fn.__name__} 계산 중...")
        try:
            results.append(fn())
        except Exception as e:
            results.append(SignalResult(fn.__name__, False, 0, f"오류 발생: {e}"))
    return results


def print_report(results: list):
    print("\n" + "=" * 70)
    print(" 베어마켓 7가지 조기 경보 시스템 - 종합 리포트")
    print(f" 생성 시각: {datetime.datetime.now():%Y-%m-%d %H:%M:%S}")
    print("=" * 70)

    total_score = 0
    max_score = len(results) * 2

    icons = {0: "🟢", 1: "🟡", 2: "🔴"}

    for idx, r in enumerate(results, 1):
        icon = icons.get(r.score, "⚪")
        print(f"\n{idx}. {icon} [{r.name}]  (점수 {r.score}/2)")
        print(f"   {r.detail}")
        total_score += r.score

    pct = total_score / max_score * 100
    print("\n" + "-" * 70)
    print(f" 종합 경보 점수: {total_score} / {max_score}  ({pct:.0f}%)")

    if pct >= 65:
        verdict = "🔴 고위험: 다수 지표 동시 경고 -> 리스크 관리(헷지/현금비중 확대) 검토 구간"
    elif pct >= 35:
        verdict = "🟡 주의: 일부 경고 신호 점등 -> 추가 모니터링 필요"
    else:
        verdict = "🟢 안정: 대부분 지표 정상 범위"
    print(f" 종합 판정: {verdict}")
    print("=" * 70)
    print("\n※ 본 결과는 투자 자문이 아니며, 참고용 정량 신호입니다.")
    print("※ 매매 결정은 본인의 추가 검증과 판단을 거쳐 진행하세요.\n")


def export_to_dict(results: list) -> dict:
    """Discord 알림 등 다른 모듈과 연동할 때 쓸 수 있는 dict 변환 함수"""
    return {
        "timestamp": datetime.datetime.now().isoformat(),
        "signals": [
            {"name": r.name, "triggered": r.triggered, "score": r.score, "detail": r.detail}
            for r in results
        ],
        "total_score": sum(r.score for r in results),
        "max_score": len(results) * 2,
    }


if __name__ == "__main__":
    results = run_all_signals()
    print_report(results)
