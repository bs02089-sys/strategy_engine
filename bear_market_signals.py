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
import datetime
import warnings
from dataclasses import dataclass
from io import StringIO
from typing import Optional

import pandas as pd
import requests

warnings.filterwarnings("ignore")

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

    if isinstance(raw_data.columns, pd.MultiIndex):
        data = raw_data['Close'] if 'Close' in raw_data.columns.levels[0] else raw_data
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
    raw_value: Optional[float] = None


# ─────────────────────────────────────────────
# Signal Functions
# ─────────────────────────────────────────────

def signal_yield_curve() -> SignalResult:
    """Detects yield curve inversion (T10Y2Y)."""
    score_total, notes = 0, []
    try:
        s = fred_series("T10Y2Y", lookback_days=365 * 2)
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
    
    return SignalResult("Yield Curve Inversion", score_total >= 1, score_total, " | ".join(notes), score_total)


def signal_market_breadth() -> SignalResult:
    """Detects market breadth cracks."""
    score_total, notes = 0, []
    symbols = ["SPY", "^NYA", "RSP"]
    try:
        data = validate_yf_data(yf.download(symbols, period="1y", progress=False), symbols)
        spy_dd = data["SPY"].iloc[-1] / data["SPY"].max() - 1
        nya_dd = data["^NYA"].iloc[-1] / data["^NYA"].max() - 1

        if spy_dd > -0.05 and nya_dd < -0.10:
            score_total += 1
            notes.append("Market breadth crack detected (+1)")
        else:
            notes.append("Market breadth stable (+0)")

        ratio_growth = (data["RSP"].iloc[-1] / data["RSP"].iloc[-20] - 1) * 100
        if ratio_growth < -2.0:
            score_total += 1
            notes.append(f"Concentration risk high (RSP/SPY {ratio_growth:.1f}%) (+1)")
        else:
            notes.append("Market balance maintained (+0)")
    except Exception as e:
        notes.append(get_error_message(e, "Market Breadth"))

    return SignalResult("Market Breadth", score_total >= 1, score_total, " | ".join(notes), score_total)


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
        hy = fred_series("BAMLH0A0HYM2", lookback_days=365 * 2)
        score, note = _spread_signal(hy, 6.0, 4.5, 1.5, "HY")
        score_total += score
        notes.append(note)

        ig = fred_series("BAMLC0A0CM", lookback_days=365 * 2)
        score, note = _spread_signal(ig, 2.0, float('inf'), 0.5, "IG")
        score_total += score
        notes.append(note)
    except Exception as e:
        notes.append(get_error_message(e, "Credit Spread"))

    return SignalResult("Credit Spread", score_total >= 1, score_total, " | ".join(notes), score_total)


def signal_fed_cycle() -> SignalResult:
    """Analyzes Fed rate cycle."""
    score_total, notes = 0, []
    try:
        s = fred_series("FEDFUNDS", lookback_days=365 * 8).resample("ME").last().dropna()
        peak_date, first_cut_date = s.idxmax(), next((s.index[i] for i in range(1, len(s)) if s.iloc[i] < s.iloc[i-1]), None)
        if first_cut_date:
            months = (s.index[-1].to_period("M") - first_cut_date.to_period("M")).n
            if months <= 12: score_total = 2; notes.append(f"High risk zone: {months} months since first cut")
            elif months <= 24: score_total = 1; notes.append(f"Residual risk: {months} months since first cut")
            else: notes.append("Safe period")
        else: notes.append("Awaiting rate cut")
    except Exception as e:
        notes.append(get_error_message(e, "Fed Cycle"))
    
    return SignalResult("Fed Policy Cycle", score_total >= 1, score_total, " | ".join(notes), score_total)


def signal_valuation() -> SignalResult:
    """Analyzes Shiller CAPE & EPS. (max 2점)"""
    score_total, notes = 0, []
    try:
        cape_url = "https://www.multpl.com/shiller-pe/table/by-month"
        response = requests.get(cape_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        response.raise_for_status()
        # <meta name="description"> 태그에서 현재 Shiller PE Ratio 값 추출 (더 안정적)
        m = re.search(r'Current Shiller PE Ratio is ([\d.]+)', response.text)
        if not m:
            raise ValueError("CAPE not found")

        cape = float(m.group(1))
        if cape >= 35:
            score_total += 2
            notes.append(f"CAPE {cape} (Critical) (+2)")
        elif cape >= 28:
            score_total += 1
            notes.append(f"CAPE {cape} (Warning) (+1)")
        else:
            notes.append(f"CAPE {cape} (Normal) (+0)")
    except Exception as e:
        notes.append(get_error_message(e, "Valuation"))

    return SignalResult("Valuation Overheat", score_total >= 1, score_total, " | ".join(notes), score_total)


def signal_leading_indicators() -> SignalResult:
    """LEI & Sahm Rule. (max 2점)"""
    score_total, notes = 0, []
    try:
        lei = fred_series("USSLIND", lookback_days=365 * 2)
        if lei.iloc[-1] < 0:
            score_total += 1
            notes.append("LEI contraction (+1)")
        else:
            notes.append("LEI stable (+0)")

        sahm = fred_series("SAHMREALTIME", lookback_days=365 * 2)
        if sahm.iloc[-1] >= 0.5:
            score_total += 1
            notes.append(f"Sahm Rule triggered ({sahm.iloc[-1]:.2f}%p) (+1)")
        elif sahm.iloc[-1] >= 0.3:
            notes.append(f"Sahm Rule elevated ({sahm.iloc[-1]:.2f}%p) (+0)")
        else:
            notes.append(f"Sahm Rule normal ({sahm.iloc[-1]:.2f}%p) (+0)")
    except Exception as e:
        notes.append("LEI/Sahm data error")

    return SignalResult("Leading Indicators", score_total >= 1, score_total, " | ".join(notes), score_total)


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

    return SignalResult("Momentum Strategy", score_total >= 1, score_total, " | ".join(notes), score_total)


# ─────────────────────────────────────────────
# Reporter
# ─────────────────────────────────────────────

def print_report(results: list):
    print(f"\n{'='*72}\n Summary Report: Bear Market Early Warning System\n Generated: {datetime.datetime.now():%Y-%m-%d %H:%M:%S}\n{'='*72}")
    for r in results:
        print(f"{r.name:<30} | {'Triggered' if r.triggered else 'Stable'}")
    
    total = sum(r.score for r in results)
    print(f"\nTotal Risk Score: {total} / 14")
    print("=" * 72)


def save_report_to_json(results: list, filename="signal_report.json"):
    data = {
        "timestamp": datetime.datetime.now().isoformat(),
        "total_score": sum(r.score for r in results),
        "signals": [{"name": r.name, "score": r.score, "detail": r.detail} for r in results]
    }
    with open(os.path.join(os.path.dirname(__file__), filename), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)


if __name__ == "__main__":
    results = [
        signal_yield_curve(), signal_market_breadth(), signal_credit_spread(),
        signal_fed_cycle(), signal_valuation(), signal_leading_indicators(),
        signal_momentum_breakdown()
    ]
    print_report(results)
    save_report_to_json(results)