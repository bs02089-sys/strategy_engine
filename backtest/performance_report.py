# 📈 전략 성과 출력 모듈
from typing import Dict, Any
import numpy as np
from utils.format import format_percent as fmt


def safe_format(value: float, suffix: str = "") -> str:
    """
    NaN 또는 Inf 값을 'N/A'로 처리하고, 정상 값은 소수점 2자리로 포맷
    """
    return "N/A" if np.isnan(value) or np.isinf(value) else f"{value:.2f}{suffix}"


def print_performance(ticker: str, result: Dict[str, Any], strategy_name: str = "") -> None:
    """
    전략 성과를 콘솔에 출력합니다.

    Parameters:
    - ticker (str): 종목 코드
    - result (dict): 전략 실행 결과 (portfolio, params 포함)
    - strategy_name (str): 전략 이름 (선택)
    """
    pf = result.get("portfolio")
    params = result.get("params", {})

    if pf is None or pf.value().empty:
        print(f"⚠️ {ticker} 포트폴리오 없음 또는 값 비어 있음")
        return

    stats = pf.stats()
    total_return = float(pf.total_return())
    sharpe = stats.get("Sharpe Ratio", 0.0)
    mdd = stats.get("Max Drawdown [%]", 0.0)

    print(f"\n📈 {ticker} 전략 성과 ({strategy_name})")
    print("-" * 35)
    print(f"🔧 파라미터   | SL: {fmt(params.get('sl', 0.1))} | TP: {fmt(params.get('tp', 0.2))}")
    print(f"💰 수익률     | {fmt(total_return)}")
    print(f"📊 샤프지수   | {safe_format(sharpe)}")
    print(f"📉 최대낙폭   | {safe_format(abs(mdd), '%')}")
    print("-" * 35)