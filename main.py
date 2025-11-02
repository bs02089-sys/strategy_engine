import warnings
warnings.simplefilter(action='ignore', category=FutureWarning)

import os
import json
# .env가 있으면 먼저 파싱해서 프로세스 환경변수에 주입합니다 (config 모듈 로딩 전에 실행)
env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(env_path):
    try:
        with open(env_path, "r", encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _v = _line.split("=", 1)
                    os.environ.setdefault(_k.strip(), _v.strip())
    except Exception:
        pass
import pandas as pd
from datetime import datetime, timedelta
from typing import Dict, Any
from config.config import CONFIG, logger
from data.historical_prices import fetch_historical_prices
from data.live_prices import live_prices
from signalhub.notify import send_ping, send_discord_alert

# track tickers we've already warned about missing avg_price to avoid repeating warnings
_missing_avg_warned: set = set()

# ✅ 전략 최적화 결과 로딩
def load_optimized_levels(path: str = "optimal_strategy.json") -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("params", {})
    except Exception:
        return {}

# ✅ 2SD 매수 조건 판단
def check_2sd_buy_signal(prices, ticker, lookback=20, threshold=2.0) -> pd.Series:
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

# ✅ 매수 조건 판단 및 알림
def check_buy_conditions(prices: pd.DataFrame, ticker: str, optimized_levels: Dict[str, Any]) -> None:
    sd_signal = check_2sd_buy_signal(prices, ticker)
    is_sd_buy = sd_signal.iloc[-1] if not sd_signal.empty else False

    series = prices[ticker].dropna()
    if series.empty:
        print(f"❌ {ticker} 데이터 없음")
        return

    peak = series.cummax()
    drawdown = (peak - series) / peak
    latest_dd = drawdown.iloc[-1]
    is_mdd_buy = latest_dd >= CONFIG["mdd_threshold"]

    current_price = series.iloc[-1]

    # ✅ SL/TP 비율 → 가격 환산 → 백분율 계산
    params = optimized_levels.get(ticker, {})
    sl_raw = params.get("SL")
    tp_raw = params.get("TP")

    # 기존: current_price 기준 환산. 사용자 요청: SL/TP는 '내 평균가격'을 기준점으로 계산
    avg_price = CONFIG.get("entry_info", {}).get(ticker, {}).get("avg_price")
    # If avg_price exists in CONFIG, be silent; otherwise warn once per ticker so user can fill it
    if isinstance(avg_price, (int, float)):
        logger.debug(f"입력 평균가격 확인: {ticker} avg_price={avg_price}")
    else:
        if ticker not in _missing_avg_warned:
            logger.warning(f"⚠️ {ticker}의 평균매수가가 CONFIG에 없습니다. entry_info.{ticker}.avg_price를 설정하세요.")
            _missing_avg_warned.add(ticker)

    sl_price_current = current_price * (1 - sl_raw) if isinstance(sl_raw, (int, float)) and sl_raw < 1 else sl_raw
    tp_price_current = current_price * (1 + tp_raw) if isinstance(tp_raw, (int, float)) and tp_raw < 1 else tp_raw

    # avg 기반 가격 (사용자 요청). avg_price가 없으면 current 기준 가격을 사용
    if isinstance(avg_price, (int, float)):
        sl_price_from_avg = avg_price * (1 - sl_raw) if isinstance(sl_raw, (int, float)) and sl_raw < 1 else sl_raw
        tp_price_from_avg = avg_price * (1 + tp_raw) if isinstance(tp_raw, (int, float)) and tp_raw < 1 else tp_raw
    else:
        sl_price_from_avg = sl_price_current
        tp_price_from_avg = tp_price_current

    # Percent shown relative to current price (keeps previous behavior); parentheses show avg-based price
    sl_pct = f"{((sl_price_current - current_price) / current_price * 100):+.2f}%" if isinstance(sl_price_current, (int, float)) else "N/A"
    tp_pct = f"{((tp_price_current - current_price) / current_price * 100):+.2f}%" if isinstance(tp_price_current, (int, float)) else "N/A"

    # Controlled verbosity: use DETAILED_OUTPUT env var to toggle
    detailed = os.getenv("DETAILED_OUTPUT", "0").lower() in ("1", "true", "yes")
    if detailed:
        print(f"\n📊 {ticker} 매수 조건 판단")
        print(f"    - 현재가: ${current_price:,.2f}   (MDD: {latest_dd:.2%} 하락)")
        print(f"    - 2SD 매수 조건: {'✅ 충족' if is_sd_buy else '❌ 미충족'}")
        print(f"    - SL: {sl_pct} (${sl_price_from_avg:,.2f}), TP: {tp_pct} (${tp_price_from_avg:,.2f})")
    else:
        logger.info(f"{ticker}: 현재가 ${current_price:,.2f} | MDD {latest_dd:.2%} | 2SD={'Y' if is_sd_buy else 'N'} | SL={sl_pct} (${sl_price_from_avg:,.2f}), TP={tp_pct} (${tp_price_from_avg:,.2f})")

    # ✅ 조건 충족 시 디스코드 알림 (상세 메시지는 기존 포맷 유지)
    if is_sd_buy or is_mdd_buy:
        emoji_map = {
            "BLOK": "🧠",
            "QQQM": "📈",
            "IAU": "🪙"
        }
        emoji = emoji_map.get(ticker, "📊")

        # SL/TP 가격 표시 (avg 기반)
        sl_price_str = f"${sl_price_from_avg:,.2f}" if isinstance(sl_price_from_avg, (int, float)) else "N/A"
        tp_price_str = f"${tp_price_from_avg:,.2f}" if isinstance(tp_price_from_avg, (int, float)) else "N/A"

        # SL/TP 타이밍 판단 (현재가와 avg 기반 가격 비교)
        sl_hit = isinstance(sl_price_from_avg, (int, float)) and current_price <= sl_price_from_avg
        tp_hit = isinstance(tp_price_from_avg, (int, float)) and current_price >= tp_price_from_avg

        timing_msg = ""
        if sl_hit:
            timing_msg = "⚠️ 현재 SL 타임입니다."
        elif tp_hit:
            timing_msg = "🎯 현재 TP 타임입니다."

        message = (
            f"{emoji} {ticker}\n"
            f"현재가: ${current_price:,.2f}\n"
            f"MDD: {latest_dd:.2%}\n"
            f"SL: {sl_pct} ({sl_price_str}), TP: {tp_pct} ({tp_price_str})\n"
            f"{timing_msg}"
        )

        send_discord_alert(
            ticker=ticker,
            price=current_price,
            extra=message,
            live_mode=True
        )
        
# ✅ 전략 실행 흐름
def main():
    send_ping(live_mode=True)

    try:
        tickers = ["BLOK", "QQQM", "IAU"]
        end_dt = datetime.now()
        start_dt = end_dt - timedelta(days=365)

        prices_hist = fetch_historical_prices(tickers, start_dt, end_dt)
        live_map = live_prices(tickers)

        if isinstance(prices_hist, pd.DataFrame) and not prices_hist.empty:
            last_idx = prices_hist.index[-1]
            for t in tickers:
                if t in prices_hist.columns and t in live_map and live_map[t] is not None:
                    try:
                        prices_hist.at[last_idx, t] = float(live_map[t])
                    except Exception:
                        pass
            prices = prices_hist
        else:
            now_idx = pd.Timestamp.now()
            data = {t: [float(live_map.get(t, float('nan')))] for t in tickers}
            prices = pd.DataFrame(data, index=[now_idx])
    except Exception as e:
        print(f"❌ 가격 데이터 조회 실패: {e}")
        return

    if prices.empty:
        print("❌ 실시간 가격 데이터 없음")
        return

    optimized_levels = load_optimized_levels()
    for ticker in prices.columns:
        check_buy_conditions(prices, ticker, optimized_levels)

if __name__ == "__main__":
    main()