# 📊 전략 시각화: 누적 수익률 & MDD
import matplotlib.pyplot as plt
import pandas as pd

def plot_mdd_distribution(returns: pd.Series, title: str = "MDD 분포") -> None:
    """
    수익률 시계열의 누적 수익률과 MDD를 시각화합니다.
    
    Parameters:
    - returns (pd.Series): 수익률 시계열 데이터
    - title (str): 그래프 제목
    """
    if returns.empty:
        print("⚠️ 시각화할 수익률 데이터 없음")
        return

    # 누적 수익률 및 MDD 계산
    cumulative_returns = returns.cumsum()
    peak_returns = cumulative_returns.cummax()
    drawdowns = cumulative_returns - peak_returns

    # 시각화
    fig, ax = plt.subplots(figsize=(10, 6))
    ax.plot(cumulative_returns, label="누적 수익률", color="blue")
    ax.plot(drawdowns, label="Drawdown", color="red")
    ax.set_title(title)
    ax.set_xlabel("기간")
    ax.set_ylabel("수익률")
    ax.legend()
    ax.grid(True)
    fig.tight_layout()
    plt.show()