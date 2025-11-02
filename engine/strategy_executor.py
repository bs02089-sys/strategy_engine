import warnings
warnings.simplefilter(action='ignore', category=FutureWarning)

import os, json
import pandas as pd
from datetime import datetime
from typing import Any, Dict, List, Tuple

from config.config import CONFIG, logger
from signalhub.signal_generator import check_2sd_buy_signal
from signalhub.notify import send_discord_alert
from signalhub.hedge import check_hedging
from engine.strategy_runner import adjust_weights_from_holdings

# ───────────── 전략 파라미터 로딩 ─────────────
def load_optimized_levels(path: str = "optimal_strategy.json") -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("params", {})
    except Exception:
        return {}

# ───────────── SGOV 이벤트 처리 ─────────────
def handle_sgov_event():
    holdings = CONFIG.get("current_holdings", [])
    updated_holdings = check_hedging(holdings, live_mode=True)
    current_weights, sgov_event = adjust_weights_from_holdings(updated_holdings)

    if sgov_event:
        print("\n🛡️ SGOV 이벤트 감지 → 전략적 비중 조정 결과")
        for ticker, weight in current_weights.items():
            print(f"{ticker}: {weight:.2%}")

        weights_msg = "\n".join([f"{ticker}: {weight:.2%}" for ticker, weight in current_weights.items()])
        message = f"🛡️ SGOV 이벤트 발생 → 전략적 피신 비중 조정\n조정된 비중:\n{weights_msg}"
        send_discord_alert(
            alert_type="sgov_event",
            ticker="SGOV",
            price=0.0,
            extra=message,
            live_mode=True
        )

# ───────────── 전략 판단 ─────────────
def check_buy_conditions(prices: pd.DataFrame, ticker: str, optimized_levels: Dict[str, Any]) -> Dict[str, Any]:
    series = prices.loc[ticker].dropna()
    if series.empty:
        return {"valid": False}

    signal = check_2sd_buy_signal(series, ticker)
    is_sd_buy = signal.iloc[-1] if not signal.empty else False

    peak = series.cummax()
    drawdown = (peak - series) / peak
    latest_dd = drawdown.iloc[-1]
    is_mdd_buy = latest_dd >= CONFIG["mdd_threshold"]
    current_price = series.iloc[-1]

    return {
        "valid": True,
        "ticker": ticker,
        "current_price": current_price,
        "latest_dd": latest_dd,
        "is_sd_buy": is_sd_buy,
        "is_mdd_buy": is_mdd_buy
    }

# ───────────── 메시지 생성 ─────────────
def build_strategy_message(ticker, current_price, latest_dd, sl_pct, tp_pct, sl_price_str, tp_price_str, timing_msg):
    emoji_map = {
        "BLOK": "🧠",
        "QQQM": "📈",
        "IAU": "🪙"
    }
    emoji = emoji_map.get(ticker, "📊")
    return (
        f"{emoji} {ticker}\n"
        f"현재가: {current_price:.2f} USD\n"
        f"MDD: {latest_dd:.2%}\n"
        f"SL: {sl_pct} ({sl_price_str}), TP: {tp_pct} ({tp_price_str})\n"
        f"{timing_msg}"
    )

# ───────────── 전략 실행 ─────────────
def execute_strategy(prices: pd.DataFrame):
    optimized_levels = load_optimized_levels()

    for ticker in CONFIG["strategy_tickers"]:
        if ticker not in prices.index:
            logger.warning(f"❌ {ticker} 가격 데이터 누락됨")
            continue

        result = check_buy_conditions(prices, ticker, optimized_levels)
        if not result.get("valid") or not result.get("is_sd_buy"):
            continue

        avg_price = CONFIG["entry_info"].get(ticker, {}).get("avg_price")
        sl_raw = optimized_levels.get(ticker, {}).get("SL")
        tp_raw = optimized_levels.get(ticker, {}).get("TP")

        current_price = result["current_price"]
        latest_dd = result["latest_dd"]

        sl_price = avg_price * (1 + sl_raw) if avg_price and sl_raw else None
        tp_price = avg_price * (1 + tp_raw) if avg_price and tp_raw else None

        sl_pct = f"{((sl_price - current_price) / current_price * 100):+.2f}%" if sl_price else "N/A"
        tp_pct = f"{((tp_price - current_price) / current_price * 100):+.2f}%" if tp_price else "N/A"
        sl_price_str = f"{sl_price:.2f} USD" if sl_price else "N/A"
        tp_price_str = f"{tp_price:.2f} USD" if tp_price else "N/A"

        timing_msg = ""
        if sl_price and current_price <= sl_price:
            timing_msg = "⚠️ 현재 SL 타임입니다."
        elif tp_price and current_price >= tp_price:
            timing_msg = "🎯 현재 TP 타임입니다."

        message = build_strategy_message(
            ticker, current_price, latest_dd,
            sl_pct, tp_pct, sl_price_str, tp_price_str, timing_msg
        )

        print("\n" + message)
        send_discord_alert(message)

# ───────────── 워치리스트 알림 ─────────────
def notify_watchlist(prices: pd.DataFrame):
    for ticker in CONFIG.get("watchlist_tickers", []):
        if ticker not in prices.index:
            continue
        signal = check_2sd_buy_signal(prices.loc[ticker], ticker)
        if not signal.empty and signal.iloc[-1]:
            send_discord_alert(f"📌 워치리스트 매수 시그널 발생: {ticker}")

# ───────────── 이벤트 기반 비중 조정 ─────────────
def adjust_weights_from_holdings(holdings: List[str]) -> Tuple[Dict[str, float], bool]:
    base_weights = {
        "QQQM": 0.272,
        "BLOK": 0.056,
        "IAU": 0.672
    }
    if "SGOV" in holdings:
        base_weights["QQQM"] = round(base_weights["QQQM"] * 0.8, 2)
        base_weights["BLOK"] = round(base_weights["BLOK"] * 0.8, 2)
        base_weights["IAU"] = round(1.0 - base_weights["QQQM"] - base_weights["BLOK"], 2)
        logger.info(f"🛡️ SGOV 피신 전략 적용 비중: {base_weights}")
        return base_weights, True
    else:
        logger.info(f"📊 SGOV 비포함, 기본 비중 유지: {base_weights}")
        return base_weights, False

# ───────────── 리밸런싱 (연초 1회만) ─────────────
def is_rebalancing_day() -> bool:
    today = datetime.today()
    return today.month == 1 and today.day == 1

def fallback_weights(tickers: List[str], reason: str) -> Dict[str, float]:
    logger.warning(f"⚠️ {reason} → 균등 비중으로 대체")
    equal_weight = round(1.0 / len(tickers), 2)
    return {ticker: equal_weight for ticker in tickers}

def calculate_target_weights(prices: pd.DataFrame) -> Dict[str, float]:
    strategy_tickers = CONFIG["strategy_tickers"]
    try:
        price_subset = prices.loc[strategy_tickers]
        if price_subset.empty or price_subset.dropna().empty:
            return fallback_weights(strategy_tickers, "가격 데이터 부족")

        returns = price_subset.pct_change().dropna()
        if returns.empty:
            return fallback_weights(strategy_tickers, "수익률 데이터 부족")

        vol = returns.std()
        inv_vol = 1 / vol
        weights = inv_vol / inv_vol.sum()
        target = {ticker: round(float(w), 2) for ticker, w in zip(vol.index, weights)}
        logger.info(f"🎯 Risk Parity 기반 target_weights 계산 완료: {target}")
        return target
    except Exception as e:
        logger.error(f"❌ 리밸런싱 계산 실패: {e}")
        return fallback_weights(strategy_tickers, "계산 오류")