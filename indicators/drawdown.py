# 표준 라이브러리
from typing import Dict, Tuple

# 서드파티 라이브러리
import pandas as pd
import yfinance as yf

# 내부 모듈
from config.config import CONFIG


def get_mdd(series: pd.Series) -> Tuple[float, float, float]:
    """
    시계열에서 최대 낙폭(MDD) 계산.

    Returns:
        mdd (float): 최대 낙폭 (양수 기준)
        trough (float): 최저점
        peak (float): 최고점
    """
    peak = series.cummax()
    drawdown = (peak - series) / peak
    mdd = drawdown.max()
    trough = series[drawdown.idxmax()]
    peak_value = peak[drawdown.idxmax()]
    return mdd, trough, peak_value

def apply_conditional_hedge(
    prices: pd.DataFrame,
    live_mode: bool = True,
    reentry_threshold: float = 0.2
) -> Dict[str, float]:
    """
    S&P500 전고점 대비 20% 이상 하락 시 SGOV 헤지 적용.
    """
    try:
        hedge_ticker = CONFIG["hedge_ticker"]
        reentry_threshold = CONFIG.get("reentry_threshold", reentry_threshold)

        start = prices.index.min().strftime("%Y-%m-%d")
        end = prices.index.max().strftime("%Y-%m-%d")

        data = yf.download(CONFIG["sp500_ticker"], start=start, end=end, progress=False)
        if data is None or data.empty:
            raise ValueError("Failed to download S&P500 data")
        sp500 = data["Close"].dropna()
        drawdown, _, _ = get_mdd(sp500)

        hedge_on = drawdown >= 0.20
        hedge_ratio = reentry_threshold if hedge_on else 0.0

        optimized_ratios = {ticker: hedge_ratio for ticker in prices.columns}

        print(
            f"{'🚨' if hedge_on else '✅'} S&P500 하락률 {drawdown:.2%} → "
            f"{'헤지 적용' if hedge_on else '헤지 미적용'}"
        )
        return optimized_ratios

    except Exception as e:
        print(f"❌ 헤지 판단 실패: {type(e).__name__} → {e}")
        return {ticker: 0.0 for ticker in prices.columns}