import pandas as pd
import json
from typing import List, Dict, Union

from config.config import CONFIG, logger
from signalhub.notify import send_discord_alert


# ─────────────────────────────────────────────
# 📡 유틸리티 함수: 웹훅 로딩
# ─────────────────────────────────────────────

def load_webhook(path: str = "discord_webhook.json") -> str:
    """디스코드 웹훅 URL을 JSON 파일에서 불러옴"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("discord_webhook", "")
    except Exception as e:
        logger.warning(f"❌ 웹훅 로드 실패: {e}")
        return ""

# ─────────────────────────────────────────────
# 📡 SL/TP 도달 알림 전송
# ─────────────────────────────────────────────

def send_sl_tp_alert(
    ticker: str,
    current_price: float,
    avg_price: float,
    sl_ratio: float,
    tp_ratio: float,
    live_mode: bool = True
) -> None:
    """평균 매입가 기준 SL/TP 도달 시 디스코드 알림 전송"""
    sl_price = avg_price * (1 - sl_ratio)
    tp_price = avg_price * (1 + tp_ratio)

    sl_hit = current_price <= sl_price
    tp_hit = current_price >= tp_price

    if sl_hit or tp_hit:
        signal = "손절 도달" if sl_hit else "익절 도달"
        message = (
            f"📡 {ticker} {signal} 알림\n"
            f"현재가: {current_price:.2f} USD\n"
            f"평균 매입가: {avg_price:.2f} USD\n"
            f"SL 목표가: {sl_price:.2f}, TP 목표가: {tp_price:.2f}"
        )
        webhook_url = load_webhook()
        if webhook_url:
            send_discord_alert(
                alert_type="price_alert",
                ticker=ticker,
                price=current_price,
                extra=message,
                live_mode=live_mode,
                webhook_url=webhook_url
            )

# ─────────────────────────────────────────────
# ⚠️ MDD 하락 감지 및 알림
# ─────────────────────────────────────────────

def check_mdd(prices: pd.DataFrame, live_mode: bool = True) -> None:
    warned_tickers = set()

    for ticker in prices.columns:
        series = prices[ticker].dropna()
        if series.empty:
            continue

        peak = series.cummax()
        drawdown = (peak - series) / peak
        latest_dd = drawdown.iloc[-1]

        if latest_dd >= CONFIG["mdd_threshold"] and ticker not in warned_tickers:
            warned_tickers.add(ticker)
            current_price = series.iloc[-1]

            print(f"⚠️ {ticker} MDD {latest_dd:.2%} → 손실 위험 감지")

            message = f"{ticker} MDD 20% 이상 하락 ({latest_dd:.2%}) → 손실 위험. 여유자금으로 매수 실행!"
            send_discord_alert(
                alert_type="mdd_buy",
                ticker=ticker,
                price=current_price,
                extra=message,
                live_mode=live_mode
            )

            cash_amount = CONFIG["initial_capital"] * 0.1
            shares_to_buy = cash_amount / current_price
            logger.info(f"💰 {ticker} 매수 시뮬: {shares_to_buy:.0f}주 ({cash_amount:.2f} USD)")

# ─────────────────────────────────────────────
# 📊 2표준편차 매수 시그널 생성 및 알림
# ─────────────────────────────────────────────

def generate_2sd_signals(prices: pd.DataFrame) -> Dict[str, pd.Series]:
    window = CONFIG.get("signal_window", 20)
    signals = {}

    for ticker in prices.columns:
        series = prices[ticker]
        rolling_stats = series.rolling(window=window)
        lower_band = rolling_stats.mean() - 2 * rolling_stats.std()

        signal_series = pd.Series("", index=series.index, dtype="string")
        signal_series.loc[series < lower_band] = "2표준편차 매수"
        signals[ticker] = signal_series

    return signals

def check_2sd_buy_signal(
    prices: Union[pd.Series, pd.DataFrame],
    ticker: str,
    lookback: int = 20,
    threshold: float = 2.0
) -> pd.Series:
    if isinstance(prices, pd.Series):
        series = prices
    elif isinstance(prices, pd.DataFrame):
        if ticker not in prices.columns:
            raise ValueError(f"티커 '{ticker}'가 가격 데이터에 없습니다.")
        series = prices[ticker]
    else:
        raise TypeError("prices는 Series 또는 DataFrame이어야 합니다.")

    rolling_mean = series.rolling(window=lookback).mean()
    rolling_std = series.rolling(window=lookback).std()
    z_scores = (series - rolling_mean) / rolling_std

    signal = (z_scores <= -threshold).astype(bool)
    return signal.reindex(series.index, fill_value=False)

def notify_2sd_buy(prices: pd.DataFrame, tickers: List[str], live_mode: bool = True) -> None:
    for ticker in tickers:
        signal = check_2sd_buy_signal(prices, ticker)
        if not signal.empty and signal.iloc[-1]:
            current_price = prices[ticker].iloc[-1]
            message = f"{ticker} 2표준편차 매수 신호 발생. 손실 위험 감지 → 전략 매수 실행!"

            send_discord_alert(
                alert_type="sd_buy",
                ticker=ticker,
                price=current_price,
                extra=message,
                live_mode=live_mode
            )

# ─────────────────────────────────────────────
# 🧠 전략 보조 함수
# ─────────────────────────────────────────────

def should_hedge(change: float, threshold: float) -> bool:
    return change <= -threshold

def has_rebounded(current_price: Union[float, pd.Series], lowest_price: float, threshold: float) -> bool:
    if isinstance(current_price, pd.Series):
        current_price = current_price.iloc[-1]
    if lowest_price == 0:
        return False
    assert not isinstance(current_price, pd.Series)
    curr = float(current_price)
    return (curr - lowest_price) / lowest_price > threshold

def generate_signals(price_series: pd.Series) -> tuple[pd.Series, pd.Series]:
    """단순 이동평균 교차 기반 진입/청산 시그널 생성"""
    ma_short = price_series.rolling(window=10).mean()
    ma_long = price_series.rolling(window=20).mean()
    entries = (ma_short > ma_long).shift(1).fillna(False).astype(bool)
    exits = (ma_short < ma_long).shift(1).fillna(False).astype(bool)
    return entries, exits

# ─────────────────────────────────────────────
# 🛡️ SGOV 이벤트 기반 비중 조정 알림
# ─────────────────────────────────────────────

def notify_sgov_event(holdings: List[str], live_mode: bool = True) -> None:
    if "SGOV" in holdings:
        weights = {
            "QQQM": round(0.272 * 0.8, 2),
            "BLOK": round(0.056 * 0.8, 2),
            "IAU": round(1.0 - 0.272 * 0.8 - 0.056 * 0.8, 2)
        }
        message = (
            f"🛡️ SGOV 이벤트 발생 → 전략적 피신 비중 조정\n"
            f"조정된 비중:\n"
            f"QQQM: {weights['QQQM']}, BLOK: {weights['BLOK']}, IAU: {weights['IAU']}"
        )
        send_discord_alert(
            alert_type="sgov_event",
            ticker="SGOV",
            price=0.0,
            extra=message,
            live_mode=live_mode
        )