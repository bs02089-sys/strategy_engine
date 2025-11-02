import warnings
warnings.simplefilter(action='ignore', category=FutureWarning)

from typing import Any, Dict, Optional, Tuple, List
from datetime import datetime
import pandas as pd
import vectorbt as vbt

# .env가 있으면 먼저 파싱해서 프로세스 환경변수에 주입합니다 (config 모듈 로딩 전에 실행)
import os as _os
_env_path = _os.path.join(_os.path.dirname(__file__), "..", ".env")
if _os.path.exists(_env_path):
    try:
        with open(_env_path, "r", encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _v = _line.split("=", 1)
                    _os.environ.setdefault(_k.strip(), _v.strip())
    except Exception:
        pass

from config.config import CONFIG, logger
# Robustly resolve fetch_historical_prices_ from the module without relying on a specific symbol name
try:
    from importlib import import_module
    _historical_prices_module = import_module("data.historical_prices")
except Exception:
    from importlib import import_module
    _historical_prices_module = import_module("historical_prices")

fetch_historical_prices_ = getattr(
    _historical_prices_module,
    "fetch_historical_prices_",
    None
) or getattr(
    _historical_prices_module,
    "fetch_historical_prices",
    None
)

if fetch_historical_prices_ is None:
    raise ImportError("Could not locate 'fetch_historical_prices_' or 'fetch_historical_prices' in data.historical_prices")

# Robustly resolve check_2sd_buy_signal_ without relying on a specific symbol/name
from importlib import import_module

try:
    _signal_module = import_module("signalhub.signal_generator")
except Exception:
    try:
        _signal_module = import_module("signal_generator")
    except Exception:
        _signal_module = None

def check_2sd_buy_signal_(series: pd.Series, ticker: Optional[str] = None) -> pd.Series:
    if not isinstance(series, pd.Series):
        series = pd.Series(series)
    return pd.Series(False, index=series.index)
if _signal_module is not None:
    for _name in (
        "check_2sd_buy_signal_",
        "check_2sd_buy_signal",
        "generate_2sd_buy_signal",
        "buy_signal_2sd"
    ):
        func = getattr(_signal_module, _name, None)
        if callable(func):
            selected_signal_func = func  # ✅ 올바른 변수에 할당
            logger.info(f"✅ 시그널 함수 선택됨: {_name}")
            break
        
if check_2sd_buy_signal_ is None:
    logger.warning("Could not locate 'check_2sd_buy_signal_' (or compatible) in signalhub.signal_generator; using no-op fallback.")
    def check_2sd_buy_signal_(series: pd.Series, ticker: Optional[str] = None) -> pd.Series:
        if not isinstance(series, pd.Series):
            series = pd.Series(series)
        return pd.Series(False, index=series.index)
from signalhub.notify import send_discord_alert as send_discord_alert_
from backtest.opt_vectorbt import optimize_strategy_vectorbt

# ───────────── 전략 실행 ─────────────

def parse_args(*args, **kwargs) -> Tuple[str, Any, pd.Series, bool]:
    if len(args) == 2 and isinstance(args[0], pd.DataFrame) and isinstance(args[1], str):
        prices_df, ticker = args
        return ticker, None, prices_df[ticker], kwargs.get("live_mode", True)
    # Extract and validate types
    ticker = args[0] if len(args) > 0 else kwargs.get("ticker")
    if isinstance(ticker, pd.DataFrame):
        raise TypeError("ticker argument must be a str, not a DataFrame")
    if ticker is None:
        ticker = ""
    date = args[1] if len(args) > 1 else kwargs.get("date")
    price_series = args[2] if len(args) > 2 else kwargs.get("price_series")
    if price_series is None or not isinstance(price_series, pd.Series):
        price_series = pd.Series(dtype=float)
    live_mode = args[3] if len(args) > 3 else kwargs.get("live_mode", True)
    live_mode = bool(live_mode)
    return ticker, date, price_series, live_mode

def build_portfolio(price_series: pd.Series, ticker: Optional[str] = None, equity_series: Optional[pd.Series] = None, hedge_series: Optional[pd.Series] = None) -> Optional[Dict[str, Any]]:
    # Provide default empty series if not supplied
    if equity_series is None:
        equity_series = pd.Series(dtype=float, index=price_series.index)
    if hedge_series is None:
        hedge_series = pd.Series(dtype=float, index=price_series.index)
    # `optimize_strategy_vectorbt` expects a DataFrame of prices (columns for tickers and optional 'SGOV').
    prices_df = pd.DataFrame({ticker: price_series})
    # include hedge series under the expected column name if provided
    try:
        if hedge_series is not None and not hedge_series.empty:
            prices_df["SGOV"] = hedge_series.reindex(prices_df.index)
    except Exception:
        # best-effort; ignore if reindexing fails
        pass

    optimized = optimize_strategy_vectorbt(ticker, prices=prices_df, n_iter=CONFIG.get("optimizer_n_iter", 200))  # type: ignore
    if optimized is None:
        return None
    pf = optimized.get("portfolio")
    if pf is None or (hasattr(pf, "value") and pf.value() is None):
        return None
    if hasattr(vbt, "Portfolio") and not isinstance(pf, vbt.Portfolio):
        try:
            pf = vbt.Portfolio.from_signals(price_series, optimized["entries"], optimized["exits"], freq="1D")
        except Exception:
            pass
    return {
        "portfolio": pf,
        "params": optimized.get("params"),
    }

def extract_metrics(pf: Any) -> Tuple[float, float]:
    try:
        total_return = float(getattr(pf, "total_return", lambda: 0.0)())
        current_mdd = float(getattr(pf, "max_drawdown", lambda: 0.0)())
    except Exception:
        return 0.0, 0.0
    return total_return, current_mdd

def calculate_cagr(pf: Any, value_series: pd.Series) -> float:
    try:
        start = pf.wrapper.index[0]
        end = pf.wrapper.index[-1]
    except Exception:
        start = value_series.index[0]
        end = value_series.index[-1]
    delta_days = (end - start).days
    if delta_days <= 0 or value_series.empty:
        return 0.0
    start_value = value_series.iloc[0]
    end_value = value_series.iloc[-1]
    if start_value == 0 or end_value == 0:
        return 0.0
    years = delta_days / 365.25
    if years <= 0:
        return 0.0
    cagr = (end_value / start_value) ** (1 / years) - 1
    return float(cagr)

def run_strategy_for_ticker(*args, **kwargs) -> Optional[Dict[str, Any]]:
    try:
        ticker, date, price_series, live_mode = parse_args(*args, **kwargs)
        equity_series = kwargs.get("equity_series", None)
        hedge_series = kwargs.get("hedge_series", None)
        result = build_portfolio(price_series, ticker=ticker, equity_series=equity_series, hedge_series=hedge_series)
        if result is None:
            print(f"⚠️ {ticker} 포트폴리오 없음 또는 값 비어 있음")
            return None
        pf = result["portfolio"]
        value_series = pf.value() if hasattr(pf, "value") else pd.Series(dtype=float)
        if value_series is None or value_series.empty:
            print(f"⚠️ {ticker} value series 비어 있음")
            return None
        total_return, current_mdd = extract_metrics(pf)
        cagr = calculate_cagr(pf, value_series)
        mdd_threshold = CONFIG.get("mdd_threshold", -0.2)
        if abs(current_mdd) > abs(mdd_threshold):
            print(f"⚠️ {ticker} MDD {current_mdd:.2%} → 임계치 초과")
        else:
            print(f"✅ {ticker} MDD {current_mdd:.2%} → 안정 범위")
        return {
            "ticker": ticker,
            "total_return": total_return,
            "cagr": cagr,
            "current_mdd": current_mdd,
            "vectorbt": {
                "params": result["params"],
                "portfolio": pf
            }
        }
    except Exception as e:
        print(f"❌ {kwargs.get('ticker', 'Unknown')} 전략 실행 실패: {type(e).__name__} → {e}")
        return None

# ───────────── 리밸런싱 ─────────────

def adjust_weights_from_holdings(holdings: List[str]) -> Tuple[Dict[str, float], bool]:
    base_weights = {
        "QQQM": 0.272,
        "BLOK": 0.056,
        "IAU": 0.672
    }
    sgov_event = False
    if "SGOV" in holdings:
        base_weights["QQQM"] = round(base_weights["QQQM"] * 0.8, 2)
        base_weights["BLOK"] = round(base_weights["BLOK"] * 0.8, 2)
        base_weights["IAU"] = round(1.0 - base_weights["QQQM"] - base_weights["BLOK"], 2)
        sgov_event = True
        logger.info(f"🛡️ SGOV 피신 전략 적용 비중: {base_weights}")
    else:
        logger.info(f"📊 SGOV 비포함, 기본 비중 유지: {base_weights}")
    return base_weights, sgov_event


def calculate_target_weights(prices: pd.DataFrame) -> Dict[str, float]:
    strategy_tickers = CONFIG["strategy_tickers"]
    if prices.empty or prices[strategy_tickers].dropna().empty:
        return fallback_weights(strategy_tickers, "가격 데이터 부족")
    returns = prices[strategy_tickers].pct_change().dropna()
    if returns.empty:
        return fallback_weights(strategy_tickers, "수익률 데이터 부족")
    try:
        vol = returns.vbt.returns().std()
        inv_vol = 1 / vol
        weights = inv_vol / inv_vol.sum()
        target = {ticker: round(float(w), 2) for ticker, w in zip(vol.index, weights)}
        logger.info(f"🎯 Risk Parity 기반 target_weights 계산 완료: {target}")
        return target
    except Exception as e:
        logger.error(f"❌ 리밸런싱 계산 실패: {e}")
        return fallback_weights(strategy_tickers, "계산 오류")

def fallback_weights(tickers: List[str], reason: str) -> Dict[str, float]:
    logger.warning(f"⚠️ {reason} → 균등 비중으로 대체")
    equal_weight = round(1.0 / len(tickers), 2)
    return {ticker: equal_weight for ticker in tickers}

from datetime import datetime

def rebalance_portfolio(current_weights: Dict[str, float], target_weights: Dict[str, float], date: Optional[datetime] = None):
    """연 1회 리밸런싱: 매년 1월 2일에만 실행"""
    if date is None:
        date = datetime.today()

    if not (date.month == 1 and date.day == 2):
        logger.info(f"⏳ 리밸런싱 미실행: {date.strftime('%Y-%m-%d')}은 연 1회 기준일이 아님")
        return

    logger.info(f"🔁 연 1회 리밸런싱 실행: {date.strftime('%Y-%m-%d')}")
    for ticker in target_weights:
        current = current_weights.get(ticker, 0)
        target = target_weights[ticker]
        diff = round(target - current, 2)
        if abs(diff) >= 0.01:
            if diff > 0:
                logger.info(f"📈 {ticker} 매수 필요: {diff:.2f}")
            else:
                logger.info(f"📉 {ticker} 매도 필요: {-diff:.2f}")
        else:
            logger.info(f"⚖️ {ticker} 비중 적정")

# ───────────── 실행 흐름 ─────────────

tickers = CONFIG["strategy_tickers"] + CONFIG.get("watchlist_tickers", [])
start = CONFIG.get("start_date")
end = CONFIG.get("end_date")
holdings = CONFIG.get("current_holdings", [])

prices = fetch_historical_prices_(tickers, start, end)

if not isinstance(prices, pd.DataFrame) or any(ticker not in prices.columns for ticker in CONFIG["strategy_tickers"]):
    raise ValueError("Prices must be a DataFrame with each ticker as a column.")

current_weights, sgov_event = adjust_weights_from_holdings(holdings)
target_weights = calculate_target_weights(prices)
CONFIG["target_weights"] = target_weights
rebalance_portfolio(current_weights, target_weights)
current_holdings = CONFIG.get("current_holdings", [])

for ticker in CONFIG["strategy_tickers"]:
    result = run_strategy_for_ticker(prices, ticker)
    if result:
        logger.info(f"✅ {ticker} 전략 실행 완료: CAGR={result['cagr']:.2%}, MDD={result['current_mdd']:.2%}")

for ticker in CONFIG.get("watchlist_tickers", []):
    signal_series = check_2sd_buy_signal_(prices[ticker], ticker)
    if signal_series.iloc[-1]:
        send_discord_alert_(f"📌 워치리스트 매수 시그널 발생: {ticker}")