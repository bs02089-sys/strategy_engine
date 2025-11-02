import warnings
warnings.simplefilter(action='ignore', category=FutureWarning)
import os
import json
import logging
from typing import Dict, Any
import pandas as pd
import numpy as np
from config.config import CONFIG
# Import optimizer implementation from backtest package
from backtest.opt_vectorbt import optimize_strategy_vectorbt


logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

STRATEGY_JSON_PATH = "optimal_strategy.json"

# 전략 저장소 확보
def ensure_strategy_json():
        if not os.path.exists(STRATEGY_JSON_PATH):
                empty_data = {"params": {}, "best": {}}
                with open(STRATEGY_JSON_PATH, "w", encoding="utf-8") as f:
                        json.dump(empty_data, f, indent=2)
                logger.info(f"📄 빈 전략 파일 생성: {STRATEGY_JSON_PATH}")
                return empty_data
        try:
                with open(STRATEGY_JSON_PATH, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        data.setdefault("params", {})
                        data.setdefault("best", {})
                        return data
        except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"⚠️ JSON 로드 실패: {e}. 빈 데이터 사용.")
                return {"params": {}, "best": {}}

# 전략 비중 계산
def calculate_target_weights(prices: pd.DataFrame) -> Dict[str, float]:
        if prices.empty:
                print("⚠️ 가격 데이터가 비어 있습니다. 균등 비중으로 설정합니다.")
                return {ticker: round(1.0 / len(CONFIG["strategy_tickers"]), 2) for ticker in CONFIG["strategy_tickers"]}

        returns = prices.pct_change().dropna()
        if returns.empty:
                print("⚠️ 수익률 데이터가 비어 있습니다. 균등 비중으로 설정합니다.")
                return {ticker: round(1.0 / len(CONFIG["strategy_tickers"]), 2) for ticker in CONFIG["strategy_tickers"]}

        try:
                vol = returns.std()
                inv_vol = 1 / vol
                weights = inv_vol / inv_vol.sum()
                target = {ticker: round(float(w), 2) for ticker, w in zip(prices.columns, weights)}
                print(f"🎯 자동 계산 target_weights (risk parity): {target}")
                return target
        except Exception as e:
                print(f"❌ 비중 계산 중 오류 발생: {e}")
                return {ticker: round(1.0 / len(CONFIG["strategy_tickers"]), 2) for ticker in CONFIG["strategy_tickers"]}

# 전략 결과 저장
def save_strategy_json(data):
        try:
                with open(STRATEGY_JSON_PATH, "w", encoding="utf-8") as f:
                        json.dump(data, f, indent=2, default=str)
                logger.info("💾 전략 데이터 저장 완료")
        except Exception as e:
                logger.error(f"❌ JSON 저장 실패: {e}")

# 전략 성과 출력
def format_pct(value, default="N/A"):
        if isinstance(value, (int, float, np.float64)):
                return f"{float(value)*100:.2f}%"
        return default

def print_report(ticker: str, result: Dict, strategy_name: str = ""):
        pf = result.get("portfolio")
        params = result.get("params", {})
        if pf and not pf.value().empty:
                stats = pf.stats()
                print(f"--- 📈 {ticker} ({strategy_name}) Report ---")
                print(f"   [Parameters] SL: {format_pct(params.get('sl'))}, TP: {format_pct(params.get('tp'))}")
                total_return_val = pf.total_return()
                if isinstance(total_return_val, pd.Series):
                        total_return_val = total_return_val.iloc[0]
                print(f"   Total Return: {format_pct(total_return_val)}")
                sharpe = stats.get('Sharpe Ratio', 0.0)
                print(f"   Sharpe Ratio: {'N/A' if np.isnan(sharpe) or np.isinf(sharpe) else f'{sharpe:.2f}'}")
                mdd = stats.get('Max Drawdown [%]', None)
                print(f"   Max Drawdown: {'N/A' if mdd is None or np.isnan(mdd) else f'{mdd:.2f}%'}")
                print("-" * 35)

# 전략 실행
def run_strategy_for_ticker(prices_df: pd.DataFrame, ticker: str, strategy_data: Dict[str, Any]) -> Dict[str, Any] | None:
        price_series = prices_df[ticker]
        # Pass n_iter as an integer, not price_series
        n_iter = 100  # Set to desired number of iterations
        result = optimize_strategy_vectorbt(ticker, prices_df, n_iter)
        if result is None:
                return None

        pf = result["portfolio"]
        params = result["params"]

        if pf is None or pf.value().empty:
                print(f"⚠️ {ticker} 포트폴리오 없음 또는 값 비어 있음")
                return None

        total_return = pf.total_return()
        if isinstance(total_return, pd.Series):
                total_return = total_return.iloc[0]

        stats = pf.stats()
        sharpe = stats.get('Sharpe Ratio', 0.0)
        mdd_pct = stats.get('Max Drawdown [%]', 0.0)
        mdd = abs(mdd_pct) / 100.0 if mdd_pct is not None else 0.0

        value_series = pf.value()
        start_value = value_series.iloc[0]
        end_value = value_series.iloc[-1]
        delta_days = (value_series.index[-1] - value_series.index[0]).days
        years = delta_days / 365.25
        cagr = (end_value / start_value) ** (1 / years) - 1 if years > 0 else 0.0

        strategy_data["params"][ticker] = {
                "SL": params["sl"],
                "TP": params["tp"],
                "target_weight": CONFIG["target_weights"].get(ticker),
                "total_return": total_return  # ✅ 추가 반영
        }

        return {
                "ticker": ticker,
                "total_return": total_return,
                "cagr": cagr,
                "sharpe": sharpe,
                "mdd": mdd,
                "params": params,
                "portfolio": pf
        }

# 전략 비중 적용
def apply_target_weights(strategy_data: Dict[str, Any], prices: pd.DataFrame) -> Dict[str, Any]:
        target_weights = calculate_target_weights(prices)
        for ticker in strategy_data["params"]:
                strategy_data["params"][ticker]["target_weight"] = target_weights.get(ticker, 0.0)
        return strategy_data