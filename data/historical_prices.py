import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from datetime import datetime
from typing import List
import yfinance as yf
import pandas as pd
from config.config import logger


# 📥 가격 데이터 수집
def fetch_historical_prices(tickers: List[str], start: datetime, end: datetime):
    """여러 티커의 가격 데이터를 수집하고 공통 인덱스로 정렬."""
    price_data = {}

    for ticker in tickers:
        try:
            df = yf.Ticker(ticker).history(
                start=start.strftime("%Y-%m-%d"),
                end=end.strftime("%Y-%m-%d")
            )
            if df.empty:
                logger.warning(f"⚠️ {ticker} 데이터 없음")
                continue

            close_col = "Adj Close" if "Adj Close" in df.columns else "Close"
            price_data[ticker] = df[close_col].dropna()
            logger.info(f"✅ {ticker} 데이터 확보")

        except Exception as e:
            logger.error(f"❌ {ticker} 데이터 수집 실패: {e}")

    if not price_data:
        logger.warning("⚠️ 수집된 가격 데이터가 없습니다.")
        return pd.DataFrame()

    # 공통 인덱스로 정렬
    common_index = sorted(set.intersection(*(set(s.index) for s in price_data.values())))
    aligned_data = {
        ticker: series.reindex(common_index)
        for ticker, series in price_data.items()
    }

    return pd.DataFrame(aligned_data)