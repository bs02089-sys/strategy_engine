import numpy as np
import pandas as pd
import vectorbt as vbt
from signalhub.signal_generator import generate_signals  # 시그널 생성 모듈
from config.config import logger

def optimize_strategy_vectorbt(
    ticker: str,
    prices: pd.DataFrame,
    n_iter: int = 500,
    method: str = "default"
) -> dict | None:
    """SL, TP, hedge_ratio 조합 기반 전략 최적화"""

    if ticker not in prices.columns:
        print(f"⚠️ {ticker} 데이터 누락 → 최적화 스킵")
        return None

    equity_series = prices[ticker].dropna()

    if "SGOV" in prices.columns:
        # reindex to equity index and forward-fill missing hedge prices
        hedge_series = prices["SGOV"].reindex(equity_series.index).ffill()
        # ensure numeric type (coerce non-numeric -> NaN then fill with 0.0)
        hedge_series = pd.to_numeric(hedge_series, errors="coerce").fillna(0.0)
    else:
        print(f"⚠️ SGOV 데이터 누락 → 헤지 비중 0으로 처리")
        hedge_series = pd.Series(0.0, index=equity_series.index)

    if len(equity_series) < 20 or len(hedge_series) < 20:
        print(f"⚠️ {ticker} 데이터 부족 → 최적화 스킵")
        return None

    entries, exits = generate_signals(equity_series)
    results = []
    failed_stats_count = 0
    inspected_pf_samples = 0

    if method == "monte_carlo":
        lower_band, current_price = evaluate_conditions(equity_series)
        print(f"📉 {ticker} 현재 가격: {current_price:.2f}")

    for _ in range(n_iter):
        sl = round(np.random.uniform(0.05, 0.15), 3)
        tp = round(np.random.uniform(0.10, 0.30), 3)
        hedge_ratio = round(np.random.uniform(0.0, 0.5), 2)

        equity_weight = 1.0 - hedge_ratio
        combined_series = equity_series * equity_weight + hedge_series * hedge_ratio

        pf = try_portfolio(combined_series, entries, exits, sl, tp)
        if pf is None:
            continue

        # Try to obtain stats; if unavailable or unexpected type, attempt fallback
        try:
            stats = pf.stats()
        except Exception:
            stats = None

        # Limited sample inspection to help debug unexpected pf/stats shapes
        if inspected_pf_samples < 3:
            try:
                logger.debug(f"[DEBUG] pf type: {type(pf)}, has value: {hasattr(pf, 'value')}")
                logger.debug(f"[DEBUG] stats type: {type(stats)}")
                inspected_pf_samples += 1
            except Exception:
                pass

        cagr = np.nan
        sharpe = np.nan

        valid_stats = False
        # Accept dict, Series, or DataFrame from pf.stats()
        if stats is not None:
            try:
                if isinstance(stats, dict):
                    stat_map = stats
                elif isinstance(stats, pd.Series):
                    stat_map = stats.to_dict()
                elif isinstance(stats, pd.DataFrame):
                    # take first row/column mapping
                    if not stats.empty:
                        stat_map = stats.iloc[0].to_dict()
                    else:
                        stat_map = {}
                else:
                    stat_map = {}

                # normalize to plain dict to satisfy type-checkers
                try:
                    stat_map = dict(stat_map)
                except Exception:
                    stat_map = {}

                cagr_pct = stat_map.get("CAGR [%]")
                sharpe = stat_map.get("Sharpe Ratio")

                # normalize Series-like values
                if isinstance(cagr_pct, pd.Series):
                    cagr_pct = cagr_pct.iloc[0] if not cagr_pct.empty else None
                if isinstance(sharpe, pd.Series):
                    sharpe = sharpe.iloc[0] if not sharpe.empty else None

                if cagr_pct is not None and pd.notna(cagr_pct):
                    cagr = float(cagr_pct) / 100.0
                    valid_stats = True
            except Exception:
                valid_stats = False

        # Fallback: compute simple CAGR and Sharpe from pf.value() if stats invalid
        if not valid_stats:
            failed_stats_count += 1
            try:
                if hasattr(pf, "value"):
                    val = pf.value()
                    if isinstance(val, (pd.Series, pd.DataFrame)):
                        # Use series of portfolio values
                        if isinstance(val, pd.DataFrame):
                            val_s = val.iloc[:, 0]
                        else:
                            val_s = val
                        if not val_s.empty:
                            start_v = float(val_s.iloc[0])
                            end_v = float(val_s.iloc[-1])
                            days = (val_s.index[-1] - val_s.index[0]).days or 1
                            years = days / 365.25
                            if start_v > 0 and end_v > 0 and years > 0:
                                cagr = (end_v / start_v) ** (1 / years) - 1
                                # simple sharpe proxy: mean(return)/std(return)
                                rets = val_s.pct_change().dropna()
                                sharpe = float(rets.mean() / (rets.std() + 1e-9)) if not rets.empty else np.nan
                                valid_stats = True
            except Exception:
                valid_stats = False

        if not valid_stats:
            # suppress per-iteration noisy log; we'll summarize later
            continue

        results.append({
            "sl": sl,
            "tp": tp,
            "hedge_ratio": hedge_ratio,
            "cagr": cagr,
            "sharpe": sharpe,
            "portfolio": pf
        })

    if not results:
        logger.warning(f"⚠️ {ticker} 시뮬레이션 실패 → 결과 없음 (failed_stats_count={failed_stats_count})")
        return None

    df = pd.DataFrame(results)
    best = df.sort_values(by="cagr", ascending=False).iloc[0]

    result = {
        "params": {
            "sl": best["sl"],
            "tp": best["tp"],
            "hedge_ratio": best["hedge_ratio"],
            "cagr": best["cagr"],
            "sharpe": best["sharpe"]
        },
        "portfolio": best["portfolio"],
        "distribution": df.drop(columns=["portfolio"])
    }

    if method == "monte_carlo":
        result["params"]["lower_band"] = lower_band
        result["price"] = current_price
        print(f"✅ {ticker} 최적화 완료 → SL: {best.sl*100:.1f}%, TP: {best.tp*100:.1f}%, 헤지: {best.hedge_ratio*100:.1f}%, CAGR: {best.cagr:.2f}, Sharpe: {best.sharpe:.2f}")

    return result

def evaluate_conditions(series: pd.Series) -> tuple[float, float]:
    mean = series.mean()
    std = series.std()
    lower_band = mean - 2.0 * std
    current_price = series.iloc[-1]
    return lower_band, current_price

def try_portfolio(
    price_series: pd.Series,
    entries: pd.Series,
    exits: pd.Series,
    sl: float,
    tp: float
) -> vbt.Portfolio | None:
    try:
        return vbt.Portfolio.from_signals(
            price_series,
            entries,
            exits,
            sl_stop=sl,
            tp_stop=tp,
            freq="1D"
        )
    except Exception as e:
        print(f"⚠️ 포트폴리오 생성 실패 → {type(e).__name__}: {e}")
        return None