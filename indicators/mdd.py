from typing import Tuple
import pandas as pd

# 📉 MDD 계산 함수
def calculate_mdd(series: pd.Series) -> float:
    """
    주어진 수익률 시계열에서 최대 낙폭(MDD)을 계산
    """
    if series.empty or len(series) < 2:
        print("⚠️ 수익률 데이터 부족")
        return 0.0

    cumulative = series.cumsum()
    peak = cumulative.cummax()
    drawdown = cumulative - peak
    mdd = drawdown.min()

    print(f"📉 MDD 계산 완료 → {mdd:.4f}")
    return abs(mdd)