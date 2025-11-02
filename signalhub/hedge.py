import yfinance as yf
import pandas as pd
from typing import List

from config.config import CONFIG
from signalhub.notify import send_discord_alert


def check_hedging(holdings: List[str], live_mode: bool) -> List[str]:
    """S&P 500 하락 및 반등 여부에 따라 SGOV 헤지 판단 및 알림 전송."""

    ticker: str = CONFIG["sp500_ticker"]
    label: str = "S&P 500" if ticker == "^GSPC" else ticker

    try:
        sp500_data = yf.download(
            ticker,
            start=CONFIG["start_date"].strftime("%Y-%m-%d"),
            end=CONFIG["end_date"].strftime("%Y-%m-%d"),
            progress=False
        )
        if sp500_data is None or sp500_data.empty:
            if live_mode:
                send_discord_alert(
                    "error",
                    float(price), # type: ignore
                    "❌ S&P 500 데이터 수집 실패",
                    {"reason": "download_failed", "ticker": ticker}
                )
            return holdings
        sp500_series = sp500_data["Close"].dropna()
    except Exception as e:
        if live_mode:
            send_discord_alert(
                "error",
                label, # type: ignore
                f"❌ S&P 500 데이터 수집 실패: {e}",
                {"reason": "download_exception", "error": str(e), "ticker": ticker}
            )
        return holdings

    if sp500_series.empty:
        if live_mode:
            send_discord_alert(
                "warning",
                label, # type: ignore
                "⚠️ S&P 500 데이터 없음",
                {"reason": "no_data", "ticker": ticker}
            )
        return holdings

    sp500_lowest = float("inf")
    initial_price = float(sp500_series.iloc[0])

    for date, price in sp500_series.items():
        price = float(price)
        sp500_lowest = min(sp500_lowest, price)
        change = (price - initial_price) / initial_price

        def should_hedge(change: float, threshold: float) -> bool:
            """change가 threshold 이하로 떨어졌을 때 헤지 조건을 만족하는지 판단."""
            return change <= threshold

        if should_hedge(change, CONFIG["hedge_threshold"]):
            message = f"{date}: {label} 하락률 {change:.2%} → SGOV 매수"
            if live_mode:
                send_discord_alert(
                    "hedge",
                    float(price),
                    message,
                    {"label": label, "change": change, "date": str(date)}
                )
            if "SGOV" not in holdings:
                holdings.append("SGOV")

        def has_rebounded(current_price: float, lowest_price: float, threshold: float) -> bool:
            """현재 가격이 최저점 대비 threshold 이상 반등했는지 판단."""
            if lowest_price == float("inf"):
                return False
            rebound = (current_price - lowest_price) / lowest_price
            return rebound >= threshold

        if has_rebounded(price, sp500_lowest, CONFIG["reentry_threshold"]):
            rebound = (price - sp500_lowest) / sp500_lowest
            message = f"{date}: {label} 반등률 {rebound:.2%} → SGOV 매도 및 BLOK/NVDX 재매수"
            if live_mode:
                send_discord_alert(
                    "hedge",
                    float(price),
                    message,
                    {"price": price, "rebound": rebound, "lowest": sp500_lowest, "date": str(date)}
                )
            if "SGOV" in holdings:
                holdings.remove("SGOV")

    return holdings