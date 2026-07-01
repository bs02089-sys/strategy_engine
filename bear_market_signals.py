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

import json
import os
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


def get_error_message(e: Exception, source_name: str) -> str:
    """에러를 분석하여 사용자 친화적인 메시지로 변환합니다."""
    err_str = str(e).lower()
    if "timeout" in err_str:
        return f"{source_name} 서버 응답 지연 (인터넷/서버 확인)"
    if "connection" in err_str:
        return f"{source_name} 네트워크 연결 실패"
    return f"{source_name} 데이터 연동 오류 ({type(e).__name__})"


def validate_yf_data(raw_data: Optional[pd.DataFrame], symbols: list) -> pd.DataFrame:
    """yfinance 데이터를 검증하고 'Close' 데이터를 반환합니다."""
    if raw_data is None or raw_data.empty:
        raise ValueError("yfinance returned no data")
    
    # 1. 'Close' 컬럼 추출 (MultiIndex 대응)
    if isinstance(raw_data.columns, pd.MultiIndex):
        if "Close" in raw_data.columns.levels[0]:
            data = raw_data["Close"]
        else:
            # MultiIndex 구조가 다를 경우를 대비해 첫 번째 레벨을 사용
            data = raw_data.xs('Close', axis=1, level=0)
    else:
        data = raw_data

    # 2. 결과가 Series(티커가 1개일 때)라면 DataFrame으로 변환
    if isinstance(data, pd.Series):
        data = data.to_frame()
        
    # 3. 데이터가 비어 있는지 확인
    if data.empty:
        raise ValueError("Close price data is empty")
        
    # 4. 티커 검증
    missing = [s for s in symbols if s not in data.columns]
    if missing:
        # 에러 메시지에 실제 존재하는 컬럼을 명시하여 디버깅 용이하게 함
        raise KeyError(f"Missing symbols: {missing}. Available columns: {list(data.columns)}")
        
    return data[symbols]


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
    장단기 금리 역전 감지 (T10Y2Y)
    
    (a) 재정상화 구간: 역전 경험 후 금리차가 0%를 상회할 때 (침체 임박)
    (b) 역전 구간: 금리차가 0% 미만 (역전 지속)
    """
    score_total, notes = 0, []
    
    try:
        # 데이터 수집
        s = fred_series("T10Y2Y", lookback_days=365 * 2)
        current = s.iloc[-1]
        min_2y = s.min()
        was_inverted = min_2y < 0
        
        # 1. 재정상화 확인 (침체 경고 핵심 구간)
        if was_inverted and current > 0:
            score_total = 2
            notes.append(f"재정상화 진행 중 ({current:+.2f}%p) (+2점)")
            notes.append("→ 역사적 침체 진입 패턴과 일치")
            
        # 2. 역전 지속 확인
        elif current < 0:
            score_total = 1
            notes.append(f"현재 역전 상태 ({current:.2f}%p) (+1점)")
            notes.append("→ 재정상화 대기 중")
            
        # 3. 정상 구간
        else:
            score_total = 0
            notes.append(f"정상 우상향 ({current:+.2f}%p) (+0점)")
            notes.append("→ 최근 2년 역전 이력 없음")

    except Exception as e:
        notes.append(get_error_message(e, "장단기 금리차(T10Y2Y) 분석"))
        return SignalResult("장단기 금리 역전", False, 0, " | ".join(notes), 0)

    # 최종 결과 반환 (통일된 패턴)
    detail = f"{' | '.join(notes)} | 총점: {score_total}/2"
    return SignalResult("장단기 금리 역전", score_total >= 1, score_total, detail, score_total)


# ─────────────────────────────────────────────
# 2. 시장 내부 균열  (Market Breadth)
# ─────────────────────────────────────────────

def signal_market_breadth() -> SignalResult:
    """시장 내부 균열 감지 (Market Breadth)"""
    score_total, notes = 0, []
    # 딕셔너리로 티커 매핑
    tickers = {"SPY": "SPY", "NYA": "^NYA", "RSP": "RSP"}
    ticker_values = list(tickers.values())

    try:
        raw = yf.download(ticker_values, period="1y")
        data = validate_yf_data(raw, ticker_values)
        
        # 딕셔너리 키를 이용해 데이터에 접근 (데이터프레임 컬럼명이 ^NYA라면 그대로 참조)
        spy_col = tickers["SPY"]
        nya_col = tickers["NYA"]
        rsp_col = tickers["RSP"]
        
        # 1. 시장 폭 균열 분석 (SPY vs NYA)
        spy_dd = (data[spy_col].iloc[-1] / data[spy_col].max() - 1)
        nya_dd = (data[nya_col].iloc[-1] / data[nya_col].max() - 1)
        
        if spy_dd > -0.05 and nya_dd < -0.10: 
            score_total += 1
            notes.append(f"시장 폭 균열 발생: SPY 대비 NYSE 종합지수 상대적 약세 (+1점)")
        else:
            notes.append("시장 폭 양호: 종합지수 및 대형주 동반 흐름 (+0점)")

        # 2. 소수 종목 쏠림 분석 (RSP/SPY 상대강도)
        ratio = data[rsp_col] / data[spy_col]
        ratio_growth = (ratio.iloc[-1] / ratio.iloc[-20] - 1) * 100 
        
        if ratio_growth < -2.0:
            score_total += 1
            notes.append(f"종목 쏠림 심화: RSP/SPY 상대강도 급락 ({ratio_growth:.1f}%) (+1점)")
        else:
            notes.append(f"종목 균형 유지: RSP/SPY 상대강도 안정 ({ratio_growth:.1f}%) (+0점)")

    except Exception as e:
        notes.append(f"{get_error_message(e, '시장 내부 균열 분석')} (+0점)")
        return SignalResult("시장 내부 균열", False, 0, " | ".join(notes), 0)

    score = min(score_total, 2)
    detail = f"{' | '.join(notes)} | 총점: {score}/2"
    return SignalResult("시장 내부 균열", score >= 1, score, detail, score)


# ─────────────────────────────────────────────
# 3. 신용 스프레드 확대  (Credit Spread Widening)
# ─────────────────────────────────────────────

def signal_credit_spread() -> SignalResult:
    """
    신용 스프레드 확대 감지 (HY & IG)
    
    (a) HY(하이일드): 위험 선호 약화 (선행 지표)
    (b) IG(투자등급): 전면적 신용 위기로의 확산 (후행/확인 지표)
    """
    score_total, notes = 0, []

    # 1. HY 스프레드 분석
    try:
        hy = fred_series("BAMLH0A0HYM2", lookback_days=365 * 2)
        hy_now, hy_widen = hy.iloc[-1], hy.iloc[-1] - hy.tail(63).min()
        
        if hy_now > 6.0 or hy_widen > 1.5:
            score_total += 1
            notes.append(f"HY 확산: {hy_now:.2f}%p (+{hy_widen:.2f}%p) → 경고 (+1점)")
        elif hy_now > 4.5 or hy_widen > 0.7:
            score_total += 1
            notes.append(f"HY 주의: {hy_now:.2f}%p (+{hy_widen:.2f}%p) → 주의 (+1점)")
        else:
            notes.append(f"HY 안정: {hy_now:.2f}%p (+0점)")
    except Exception as e:
        notes.append(get_error_message(e, "HY 스프레드"))

    # 2. IG 스프레드 분석
    try:
        ig = fred_series("BAMLC0A0CM", lookback_days=365 * 2)
        ig_now, ig_widen = ig.iloc[-1], ig.iloc[-1] - ig.tail(63).min()
        
        if ig_now > 2.0 or ig_widen > 0.5:
            score_total += 1
            notes.append(f"IG 확산: {ig_now:.2f}%p (+{ig_widen:.2f}%p) → 위험 고조 (+1점)")
        else:
            notes.append(f"IG 안정: {ig_now:.2f}%p (+0점)")
    except Exception as e:
        notes.append(get_error_message(e, "IG 스프레드"))

    # 최종 결과 반환 (통일된 패턴)
    score = min(score_total, 2)
    detail = f"{' | '.join(notes)} | 총점: {score}/2"
    return SignalResult("신용 스프레드 확대", score >= 1, score, detail, score)


# ─────────────────────────────────────────────
# 4. 연준 사이클 분석  (Fed Policy Cycle)
# ─────────────────────────────────────────────

def signal_fed_cycle() -> SignalResult:
    """
    연준 금리 사이클 분석 (첫 인하 후 경과 기간)
    
    (a) 0~12개월: 침체 진입 고위험 구간 (침체 확률 ~70%)
    (b) 12~24개월: 후행 영향 잔존 가능 구간
    """
    score_total, notes = 0, []
    
    try:
        s = fred_series("FEDFUNDS", lookback_days=365 * 8).resample("ME").last().dropna()
        if len(s) < 6:
            raise ValueError("데이터 부족")

        peak_date, peak_value = s.idxmax(), float(s.max())
        current_rate = float(s.iloc[-1])

        # 첫 인하 시점 탐색
        after_peak = s.loc[peak_date:]
        first_cut_date = next((after_peak.index[i] for i in range(1, len(after_peak)) 
                               if after_peak.iloc[i] < after_peak.iloc[i - 1]), None)

        if first_cut_date is None:
            notes.append(f"고점 유지 단계: {peak_value:.2f}% ({peak_date:%Y-%m}) → 인하 전환 대기 (+0점)")
            score_total = 0
        else:
            months = (s.index[-1].to_period("M") - first_cut_date.to_period("M")).n
            if months <= 12:
                score_total = 2
                notes.append(f"최위험 구간: 첫 인하 {first_cut_date:%Y-%m} 후 {months}개월 경과 (+2점)")
            elif months <= 24:
                score_total = 1
                notes.append(f"잔존 위험: 첫 인하 {first_cut_date:%Y-%m} 후 {months}개월 경과 (+1점)")
            else:
                score_total = 0
                notes.append(f"위험 종료: 첫 인하 {first_cut_date:%Y-%m} 후 {months}개월 경과 (+0점)")
                
    except Exception as e:
        notes.append(get_error_message(e, "연준 기준금리(FEDFUNDS)"))
        return SignalResult("연준 사이클 분석", False, 0, " | ".join(notes), 0)

    # 최종 결과 반환 (통일된 패턴)
    detail = f"{' | '.join(notes)} | 총점: {score_total}/2"
    return SignalResult("연준 사이클 분석", score_total >= 1, score_total, detail, score_total)


# ─────────────────────────────────────────────
# 5. 밸류에이션 과열  (Shiller CAPE & 실적 피크)
# ─────────────────────────────────────────────

VALUATION_CONFIG = {
    "CAPE_URL": "https://www.multpl.com/shiller-pe/table/by-month",
    "EPS_URL": "https://www.multpl.com/s-p-500-earnings/table/by-year",
    "CAPE_THRESHOLDS": {"CRITICAL": 35.0, "WARNING": 28.0},
    "EPS_THRESHOLDS": {"PEAK_OUT": -5.0, "SLOWDOWN": 5.0},
    "CAPE_FALLBACK": 38.0
}

def signal_valuation() -> SignalResult:
    """Shiller CAPE & S&P 500 실적 분석"""
    score_total, notes = 0, []
    headers = {"User-Agent": "Mozilla/5.0"}
    row_pat = re.compile(
        r"<tr[^>]*>\s*<td>([^<]+)</td>\s*<td>\s*(?:&#x2002;|&nbsp;|\s)*([\d.]+)\s*</td>\s*</tr>",
        re.IGNORECASE,
    )

    # 1. Shiller CAPE 분석
    try:
        r = requests.get(VALUATION_CONFIG["CAPE_URL"], headers=headers, timeout=10)
        r.raise_for_status()
        m = row_pat.findall(r.text)
        cape_val = float(m[0][1]) if m else VALUATION_CONFIG["CAPE_FALLBACK"]
        cape_src = f"multpl.com ({m[0][0].strip()})" if m else "폴백값 사용"

        if cape_val >= VALUATION_CONFIG["CAPE_THRESHOLDS"]["CRITICAL"]:
            score_total += 1
            notes.append(f"CAPE {cape_val:.1f} → 극단 과열 [{cape_src}] (+1점)")
        elif cape_val >= VALUATION_CONFIG["CAPE_THRESHOLDS"]["WARNING"]:
            score_total += 1
            notes.append(f"CAPE {cape_val:.1f} → 과열 구간 [{cape_src}] (+1점)")
        else:
            notes.append(f"CAPE {cape_val:.1f} → 정상~중립 [{cape_src}] (+0점)")
    except Exception as e:
        notes.append(f"{get_error_message(e, 'Shiller CAPE')} (+0점)")
        cape_val = VALUATION_CONFIG["CAPE_FALLBACK"]

    # 2. S&P500 EPS 피크 분석
    try:
        r2 = requests.get(VALUATION_CONFIG["EPS_URL"], headers=headers, timeout=10)
        r2.raise_for_status()
        rows = row_pat.findall(r2.text)
        if len(rows) >= 2:
            growth = (float(rows[0][1]) / float(rows[1][1]) - 1) * 100
            if growth < VALUATION_CONFIG["EPS_THRESHOLDS"]["PEAK_OUT"]:
                score_total += 1
                notes.append(f"EPS 성장률 {growth:.1f}% → 피크아웃 위험 (+1점)")
            elif growth < VALUATION_CONFIG["EPS_THRESHOLDS"]["SLOWDOWN"]:
                score_total += 1
                notes.append(f"EPS 성장률 {growth:.1f}% → 성장 둔화 (+1점)")
            else:
                notes.append(f"EPS 성장률 {growth:.1f}% → 확장 지속 (+0점)")
        else:
            notes.append("EPS 데이터 부족 (+0점)")
    except Exception as e:
        notes.append(f"{get_error_message(e, 'S&P500 EPS')} (+0점)")

    # 최종 결과 반환 (통일된 패턴)
    score = min(score_total, 2)
    detail = f"{' | '.join(notes)} | 총점: {score}/2"
    return SignalResult("밸류에이션 과열", score >= 1, score, detail, score)


# ─────────────────────────────────────────────
# 6. 선행경제지수  (LEI & Sahm Rule)
# ─────────────────────────────────────────────

def signal_leading_indicators() -> SignalResult:
    """선행경제지수 (LEI & Sahm Rule) 분석"""
    score_total, notes = 0, []

    # 1. USSLIND 분석
    try:
        lei = fred_series("USSLIND", lookback_days=365 * 2)
        lei_now = lei.iloc[-1]
        lei_trend = lei_now - (lei.iloc[-3] if len(lei) > 3 else lei.iloc[0])
        
        if lei_now < 0:
            score_total += 1
            notes.append(f"LEI {lei_now:.2f}% (수축) (+1점)")
            if lei_trend < 0:
                score_total += 1
                notes.append(f"LEI 추세 하락 ({lei_trend:.2f}%p) (+1점)")
        else:
            notes.append(f"LEI {lei_now:.2f}% (확장) (+0점)")
    except Exception as e:
        notes.append(f"{get_error_message(e, 'LEI')} (+0점)")

    # 2. Sahm Rule 분석
    try:
        sahm = fred_series("SAHMREALTIME", lookback_days=365 * 2)
        sahm_now = sahm.iloc[-1]
        
        if sahm_now >= 0.5:
            score_total += 2
            notes.append(f"Sahm Rule {sahm_now:.2f}%p (침체 공식 발동) (+2점)")
        elif sahm_now >= 0.3:
            score_total += 1
            notes.append(f"Sahm Rule {sahm_now:.2f}%p (주의) (+1점)")
        else:
            notes.append(f"Sahm Rule {sahm_now:.2f}%p (안정) (+0점)")
    except Exception as e:
        notes.append(f"{get_error_message(e, 'Sahm Rule')} (+0점)")

    # 최종 결과 반환
    score = min(score_total, 2)
    detail = f"{' | '.join(notes)} | 총점: {score}/2"
    return SignalResult("선행경제지수", score >= 1, score, detail, score)


# ─────────────────────────────────────────────
# 7. 모멘텀 전략 전용 신호  (섹터 로테이션 & SPX 200R)
# ─────────────────────────────────────────────

def signal_momentum_breakdown() -> SignalResult:
    """섹터 로테이션 및 SPX 200일 모멘텀 붕괴 감지"""
    score_total, notes = 0, []
    tickers = ["SPY", "XLU", "XLP", "XLV", "XLK", "XLY", "XLI"]

    try:
        # 데이터 수집 및 검증
        raw = yf.download(tickers, period="2y")
        data = validate_yf_data(raw, tickers)
        
        # 1. SPX 200일 모멘텀 분석
        spy = data["SPY"].dropna()
        ret_200d = (spy.iloc[-1] / spy.iloc[-201] - 1) * 100 if len(spy) >= 201 else 0
        
        if ret_200d < 0:
            score_total += 1
            notes.append(f"SPX 200D 모멘텀 {ret_200d:.1f}% (하락) (+1점)")
        else:
            notes.append(f"SPX 200D 모멘텀 {ret_200d:.1f}% (양호) (+0점)")

        # 2. 섹터 로테이션 분석 (방어 vs 민감 3개월 수익률)
        def get_3m_ret(tk):
            col = data[tk].dropna()
            return (col.iloc[-1] / col.iloc[-64] - 1) * 100 if len(col) > 63 else 0

        def_ret = sum(get_3m_ret(t) for t in ["XLU", "XLP", "XLV"]) / 3
        cyc_ret = sum(get_3m_ret(t) for t in ["XLK", "XLY", "XLI"]) / 3
        gap = def_ret - cyc_ret

        if gap > 5:
            score_total += 1
            notes.append(f"방어/민감 갭 {gap:.1f}%p (스마트머니 회피) (+1점)")
        elif gap > 2:
            score_total += 1
            notes.append(f"방어/민감 갭 {gap:.1f}%p (로테이션 조짐) (+1점)")
        else:
            notes.append(f"방어/민감 갭 {gap:.1f}%p (성장 국면) (+0점)")

    except Exception as e:
        notes.append(f"{get_error_message(e, '모멘텀 분석')} (+0점)")
        return SignalResult("모멘텀 전략 전용 신호", False, 0, " | ".join(notes), 0)

    # 최종 결과 반환 (통일된 패턴)
    score = min(score_total, 2)
    detail = f"{' | '.join(notes)} | 총점: {score}/2"
    return SignalResult("모멘텀 전략 전용 신호", score >= 1, score, detail, score)


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


# 파일 상단에 배치
ICONS = {0: "🟢", 1: "🟡", 2: "🔴"}

def print_report(results: list):
    # 1. 헤더 출력
    print(f"\n{'='*72}\n 베어마켓 7가지 조기 경보 시스템 — 종합 리포트\n 생성 시각: {datetime.datetime.now():%Y-%m-%d %H:%M:%S}\n{'='*72}")

    # 2. 신호 요약 테이블
    print(f"{'지표명':<22} | {'상태':<5} | {'점수'}\n{'-'*38}")
    for r in results:
        print(f"{r.name[:20]:<22} | {ICONS.get(r.score, '⚪'):<5} | {r.score}/2")
    print("-" * 38)

    # 3. 상세 내용 출력 (분리된 detail을 순회)
    print("\n[상세 내역]")
    for idx, r in enumerate(results, 1):
        print(f"\n{idx}. {ICONS.get(r.score, '⚪')} {r.name}")
        # detail이 존재할 때만 분할하여 출력
        details = r.detail.split(" | ") if r.detail else ["데이터 없음"]
        for part in details:
            print(f"   - {part.strip()}")

    # 4. 종합 판정
    total = sum(r.score for r in results)
    max_score = len(results) * 2
    pct = total / max_score * 100
    
    print(f"\n{'─'*72}\n 종합 경보 점수: {total} / {max_score} ({pct:.0f}%)")
    if pct >= 65: verdict = "🔴 고위험 — 헷지/현금비중 확대 검토"
    elif pct >= 35: verdict = "🟡 주의 — 추가 모니터링 필요"
    else: verdict = "🟢 안정 — 정상 범위"

    print(f" 종합 판정: {verdict}\n{'='*72}\n※ 참고용 정량 신호입니다. 최종 판단은 본인의 몫입니다.\n")


def print_mobile_report(results: list):
    """모바일 메신저 가독성을 위한 요약 리포트"""
    total_score = sum(r.score for r in results)
    
    msg = "📊 [베어마켓 조기 경보]\n"
    msg += f"종합 점수: {total_score}/14\n"
    msg += f"판정: {'🟢 안정' if total_score < 6 else '🟡 주의' if total_score < 10 else '🔴 위험'}\n"
    msg += "-" * 20 + "\n"
    
    for r in results:
        status_icon = "🟢" if r.score == 0 else "🟡" if r.score == 1 else "🔴"
        msg += f"{status_icon} {r.name}: {r.score}점\n"
    
    msg += "-" * 20 + "\n"
    msg += "상세 확인 필요시 PC 로그를 확인하세요."
    print(msg)
    

def export_to_dict(results: list) -> dict:
    """Discord 알림 등 외부 모듈 연동용 dict 변환."""
    return {
        "timestamp":   datetime.datetime.now().isoformat(),
        "signals":     [{"name": r.name, "triggered": r.triggered,
                         "score": r.score, "detail": r.detail} for r in results],
        "total_score": sum(r.score for r in results),
        "max_score":   len(results) * 2,
    }


def save_report_to_json(results: list, filename="signal_report.json"):
    # 현재 스크립트 파일이 위치한 폴더를 기준으로 경로 지정
    base_dir = os.path.dirname(os.path.abspath(__file__))
    file_path = os.path.join(base_dir, filename)
    
    data = export_to_dict(results)
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
        
    print(f"✅ 파일이 다음 경로에 저장되었습니다: {file_path}")


if __name__ == "__main__":
    results = run_all_signals()
    print_report(results)
    save_report_to_json(results)  # 여기서 호출!