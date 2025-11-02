from typing import Dict
import pandas as pd
from indicators.mdd import calculate_mdd

def run_backtest_for_ticker(price_series: pd.Series, signal_series: pd.Series) -> Dict[str, float]:
    """시그널 기반 전략의 누적 수익률과 MDD 계산."""
    if price_series.empty or signal_series.empty:
        print("⚠️ 백테스트 데이터 부족")
        return {"return": 0.0, "mdd": 0.0}

    shifted_signal = signal_series.shift(1).fillna(False)
    returns = price_series.pct_change().fillna(0.0)
    strategy_returns = returns.where(shifted_signal, 0.0)

    cumulative_return = strategy_returns.sum()
    mdd = calculate_mdd(strategy_returns)

    print(f"📊 백테스트 결과 → 수익률 {cumulative_return:.4f}, MDD {mdd:.4f}")
    return {"return": cumulative_return, "mdd": mdd}