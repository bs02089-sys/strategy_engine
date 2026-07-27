# -*- coding: utf-8 -*-
"""
=============================================================
 Bear Market 7 Early Warning Signals
=============================================================

System Overview:
  1. Yield Curve Inversion     - Inversion & Re-steepening (Most dangerous)
  2. Market Breadth            - A-D Line deviation & High/Low ratios
  3. Credit Spread Widening    - HY + IG spreads (Bond market signals)
  4. Fed Policy Cycle          - First rate cut + 6~12 months = Highest risk zone
  5. Valuation Overheat        - Shiller CAPE levels & S&P500 EPS growth slowing
  6. Leading Indicators        - USSLIND levels & Sahm Rule (0.5%p threshold)
  7. Momentum Strategy Signal  - SPX 200-day return & Sector rotation

Dependencies:
    pip install yfinance pandas requests

Data Sources:
  - FRED    : Direct download via fredgraph.csv
  - yfinance: S&P500, Sector ETFs, NYSE A-D Line (^NYAD)
  - multpl.com: Shiller CAPE (Direct regex parsing)
"""

import json
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
    return df.dropna().set_index("date")[series_id].astype(float)


def get_error_message(e: Exception, source_name: str) -> str:
    """Converts exceptions into user-friendly messages."""
    err_str = str(e).lower()
    if "timeout" in err_str:
        return f"{source_name} server response delayed"
    if "connection" in err_str:
        return f"{source_name} network connection failed"
    return f"{source_name} data sync error ({type(e).__name__})"


def validate_yf_data(raw_data: Optional[pd.DataFrame], symbols: list) -> pd.DataFrame:
    """Validate yfinance input and extract 'Close' series."""
    if raw_data is None or raw_data.empty:
        raise ValueError("yfinance returned no data")

    cols = raw_data.columns
    if isinstance(cols, pd.MultiIndex):
        data = raw_data['Close'] if 'Close' in cols.levels[0] else raw_data
    else:
        data = raw_data

    if isinstance(data, pd.Series):
        data = data.to_frame()

    missing = [s for s in symbols if s not in data.columns]
    if missing:
        raise KeyError(f"Missing symbols: {missing}")

    return data[symbols]


@dataclass
class SignalResult:
    name: str
    triggered: bool
    score: int          # 0=Normal, 1=Caution, 2=Warning
    detail: str


# ─────────────────────────────────────────────
# Signal Functions
# ─────────────────────────────────────────────

def signal_yield_curve() -> SignalResult:
    """Detects yield curve inversion (T10Y2Y)."""
    score_total, notes = 0, []
    try:
        s = fred_series("T10Y2Y", lookback_days=FRED_LOOKBACK_2Y)
        current, min_2y = s.iloc[-1], s.min()
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
        notes.append(get_error_message(e, "Yield Curve"))
    
    return SignalResult("Yield Curve Inversion", score_total >= 1, score_total, " | ".join(notes))


def signal_market_breadth() -> SignalResult:
    """Detects market breadth cracks."""
    score_total, notes = 0, []
    symbols = ["SPY", "^NYA", "RSP"]
    try:
        data = validate_yf_data(yf.download(symbols, period="1y", progress=False), symbols)
        spy, nya, rsp = data["SPY"], data["^NYA"], data["RSP"]
        spy_dd = spy.iloc[-1] / spy.max() - 1
        nya_dd = nya.iloc[-1] / nya.max() - 1

        if spy_dd > -0.05 and nya_dd < -0.10:
            score_total += 1
            notes.append("Market breadth crack detected (+1)")
        else:
            notes.append("Market breadth stable (+0)")

        ratio_growth = (rsp.iloc[-1] / rsp.iloc[-20] - 1) * 100
        if ratio_growth < -2.0:
            score_total += 1
            notes.append(f"Concentration risk high (RSP/SPY {ratio_growth:.1f}%) (+1)")
        else:
            notes.append("Market balance maintained (+0)")
    except Exception as e:
        notes.append(get_error_message(e, "Market Breadth"))

    return SignalResult("Market Breadth", score_total >= 1, score_total, " | ".join(notes))


def _spread_signal(series: pd.Series, warn: float, caution: float, widen_warn: float, label: str) -> tuple[int, str]:
    value = series.iloc[-1]
    widen = value - series.tail(min(63, len(series))).min()
    if value > warn or widen > widen_warn:
        return 1, f"{label} spread warning ({value:.2f}%)"
    if value > caution:
        return 1, f"{label} spread caution ({value:.2f}%)"
    return 0, f"{label} stable ({value:.2f}%)"


def signal_credit_spread() -> SignalResult:
    """Detects credit spread widening (HY & IG)."""
    score_total, notes = 0, []
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
        notes.append(get_error_message(e, "Credit Spread"))

    return SignalResult("Credit Spread", score_total >= 1, score_total, " | ".join(notes))


def signal_fed_cycle() -> SignalResult:
    """Analyzes Fed rate cycle.

    Uses the MOST RECENT rate cut (iterates backward through the series)
    rather than the FIRST cut in the lookback window.  This correctly
    handles multiple easing cycles (cut → hike → cut again): instead
    of measuring from the first cut of 2024, it measures from the most
    recent cut, so the risk score reflects the CURRENT easing cycle.
    """
    score_total, notes = 0, []
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
        notes.append(get_error_message(e, "Fed Cycle"))
    
    return SignalResult("Fed Policy Cycle", score_total >= 1, score_total, " | ".join(notes))


def _save_cape_cache(cape: float) -> None:
    """성공적으로 조회된 CAPE 값을 캐시 파일에 원자적(atomic)으로 저장"""
    try:
        data = {"cape": cape, "date": datetime.datetime.now().strftime("%Y-%m-%d"), "source": "multpl.com"}
        # Atomic write via tempfile + move — prevents corrupt cache on crash
        with tempfile.NamedTemporaryFile(
            "w", delete=False, suffix=".json", encoding="utf-8"
        ) as tmp:
            json.dump(data, tmp, ensure_ascii=False, indent=2)
            tmp_path = tmp.name
        shutil.move(tmp_path, CAPE_CACHE_PATH)
    except Exception:
        pass  # 캐시 저장 실패는 치명적이지 않음


def _load_cape_cache() -> Optional[float]:
    """이전에 캐시된 CAPE 값 로드. 없으면 None 반환."""
    try:
        if os.path.exists(CAPE_CACHE_PATH):
            with open(CAPE_CACHE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return float(data["cape"])
    except Exception:
        pass
    return None


def signal_valuation() -> SignalResult:
    """Analyzes Shiller CAPE & EPS. (max 2점)"""
    score_total, notes = 0, []
    cape = None
    from_cache = False

    # 1차 시도: multpl.com 실시간 조회
    try:
        cape_url = "https://www.multpl.com/shiller-pe/table/by-month"
        response = requests.get(cape_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        response.raise_for_status()
        # 페이지 내 "Current Shiller PE Ratio is X.XX" 텍스트에서 CAPE 값 추출 (meta description에 포함)
        m = re.search(r'Current Shiller PE Ratio is ([\d.]+)', response.text)
        if not m:
            raise ValueError("CAPE not found")

        cape = float(m.group(1))
        _save_cape_cache(cape)  # 성공 시 캐시 저장
    except Exception:
        pass

    # 2차: 실시간 실패 → 캐시 사용
    if cape is None:
        cached = _load_cape_cache()
        if cached is not None:
            cape = cached
            from_cache = True
        else:
            notes.append(get_error_message(Exception("No data from multpl.com or cache"), "Valuation"))
            return SignalResult("Valuation Overheat", False, 0, " | ".join(notes))

    # CAPE 값 평가
    source_tag = " (cached)" if from_cache else ""
    if cape >= 35:
        score_total += 2
        notes.append(f"CAPE {cape} (Critical){source_tag} (+2)")
    elif cape >= 28:
        score_total += 1
        notes.append(f"CAPE {cape} (Warning){source_tag} (+1)")
    else:
        notes.append(f"CAPE {cape} (Normal){source_tag} (+0)")
    if from_cache:
        notes.append("⚠️ 실시간 조회 실패, 캐시 데이터 사용")

    return SignalResult("Valuation Overheat", score_total >= 1, score_total, " | ".join(notes))


def signal_leading_indicators() -> SignalResult:
    """LEI & Sahm Rule. (max 2점)"""
    score_total, notes = 0, []
    try:
        lei = fred_series("USSLIND", lookback_days=FRED_LOOKBACK_2Y)
        if lei.iloc[-1] < 0:
            score_total += 1
            notes.append("LEI contraction (+1)")
        else:
            notes.append("LEI stable (+0)")

        sahm = fred_series("SAHMREALTIME", lookback_days=FRED_LOOKBACK_2Y)
        if sahm.iloc[-1] >= 0.5:
            score_total += 1
            notes.append(f"Sahm Rule triggered ({sahm.iloc[-1]:.2f}%p) (+1)")
        elif sahm.iloc[-1] >= 0.3:
            notes.append(f"Sahm Rule elevated ({sahm.iloc[-1]:.2f}%p) (+0)")
        else:
            notes.append(f"Sahm Rule normal ({sahm.iloc[-1]:.2f}%p) (+0)")
    except Exception as e:
        notes.append("LEI/Sahm data error")

    return SignalResult("Leading Indicators", score_total >= 1, score_total, " | ".join(notes))


def signal_momentum_breakdown() -> SignalResult:
    """Momentum & Sector Rotation. (max 2점)"""
    score_total, notes = 0, []
    try:
        tickers = ["SPY", "XLU", "XLP", "XLV", "XLK", "XLY", "XLI"]
        data = validate_yf_data(yf.download(tickers, period="2y", progress=False), tickers)
        spy = data["SPY"].dropna()
        if len(spy) < 201:
            raise ValueError("Insufficient SPY history")

        ret_200d = (spy.iloc[-1] / spy.iloc[-201] - 1) * 100
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

        if def_ret > grw_ret:
            score_total += 1
            notes.append(f"Defensive sectors outperform growth ({def_ret - grw_ret:+.1f}% gap) (+1)")
        else:
            notes.append(f"Growth sectors lead ({grw_ret - def_ret:+.1f}% gap) (+0)")
    except Exception as e:
        notes.append(get_error_message(e, "Momentum"))

    return SignalResult("Momentum Strategy", score_total >= 1, score_total, " | ".join(notes))


# ─────────────────────────────────────────────
# Reporter
# ─────────────────────────────────────────────

def print_report(results: list):
    print(f"\n{'='*72}\n Summary Report: Bear Market Early Warning System\n Generated: {datetime.datetime.now():%Y-%m-%d %H:%M:%S}\n{'='*72}")
    for r in results:
        status = 'Triggered' if r.triggered else 'Stable'
        print(f"{r.name:<30} | {r.score}/2 | {status}")
    
    total = sum(r.score for r in results)
    print(f"\nTotal Risk Score: {total} / 14")
    print("=" * 72)


def save_report_to_json(results: list, filename="signal_report.json"):
    data = {
        "timestamp": datetime.datetime.now().isoformat(),
        "total_score": sum(r.score for r in results),
        "signals": [{"name": r.name, "score": r.score, "detail": r.detail} for r in results]
    }
    # Atomic write — prevents corrupt signal_report.json on crash
    report_path = os.path.join(os.path.dirname(__file__), filename)
    with tempfile.NamedTemporaryFile(
        "w", delete=False, suffix=".json", encoding="utf-8"
    ) as tmp:
        json.dump(data, tmp, ensure_ascii=False, indent=4)
        tmp_path = tmp.name
    shutil.move(tmp_path, report_path)


if __name__ == "__main__":
    results = [
        signal_yield_curve(), signal_market_breadth(), signal_credit_spread(),
        signal_fed_cycle(), signal_valuation(), signal_leading_indicators(),
        signal_momentum_breakdown()
    ]
    print_report(results)
    save_report_to_json(results)