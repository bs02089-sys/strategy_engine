"""
backtest_soxl_tqqq.py — SOXL + TQQQ 최적화 버전 (2026-06 기준)
"""

import json
import numpy as np
import yfinance as yf
import matplotlib.pyplot as plt
from itertools import product

# ====================== 한글 폰트 설정 ======================
plt.rcParams['font.family'] = 'Malgun Gothic'
plt.rcParams['axes.unicode_minus'] = False

# ====================== config 로드 ======================
with open("config.json", "r", encoding="utf-8") as f:
    cfg = json.load(f)

POSITIONS = cfg["POSITIONS"]
STRATEGY = cfg.get("STRATEGY", {})

TARGET_TICKERS = ["SOXL", "TQQQ"]

BUY_DAYS = STRATEGY.get("BUY_DURATION_DAYS", 252)
HOLD_DAYS = STRATEGY.get("HOLD_DURATION_DAYS", 252)
CYCLE_DAYS = BUY_DAYS + HOLD_DAYS

# ==================== 기본 추천 비중 ====================
DEFAULT_WEIGHTS = {
    "SOXL": 0.60,
    "TQQQ": 0.40
}

# 종목별 분할 횟수 (조정 가능)
DEFAULT_YEARLY_COUNT = {
    "SOXL": 20,
    "TQQQ": 20
}

print(f"🚀 SOXL + TQQQ 백테스트 (2종목 전략)")
print(f"   Buy Phase: {BUY_DAYS}일 | Hold Phase: {HOLD_DAYS}일\n")


def run_backtest(weights: dict = None, yearly_count: dict = None):
    """단일 비중으로 백테스트 실행"""
    if weights is None:
        weights = DEFAULT_WEIGHTS
    if yearly_count is None:
        yearly_count = DEFAULT_YEARLY_COUNT

    data = yf.download(TARGET_TICKERS, start="2022-01-01", end="2026-06-05",
                       auto_adjust=True, group_by='ticker')

    results = {}
    equity_curves = {}

    for ticker in TARGET_TICKERS:
        print(f"🔹 {ticker} ({weights[ticker]*100:.0f}%, {yearly_count[ticker]}회 분할)")
        df = data[ticker].copy().dropna()
        
        pos = POSITIONS.get(ticker, {})
        multiplier = pos.get("ENTRY_MULTIPLIER", 1.48)
        lookback = pos.get("LOOKBACK_DAYS", 365)
        
        df['Return'] = df['Close'].pct_change()
        df['Sigma'] = df['Close'].pct_change().rolling(lookback).std()
        df['LOC'] = df['Close'].shift(1) * np.exp(-multiplier * df['Sigma'].shift(1))
        df['Signal'] = (df['Close'] <= df['LOC']) & df['LOC'].notna()
        
        equity = 1.0
        position = 0.0
        avg_entry = 0.0
        cycle_start = 0
        buys_in_cycle = 0
        max_buys = yearly_count[ticker]
        
        df['Equity'] = 1.0
        df['Daily_Return'] = 0.0
        
        for i in range(1, len(df)):
            price = df['Close'].iloc[i]
            signal = df['Signal'].iloc[i]
            days_in_cycle = i - cycle_start
            
            # 사이클 종료 & 재시작
            if days_in_cycle >= CYCLE_DAYS or cycle_start == 0:
                if position > 0:
                    final_ret = (price - avg_entry) / avg_entry if avg_entry != 0 else 0
                    df.loc[df.index[i], 'Daily_Return'] = final_ret * position
                cycle_start = i
                position = 0.0
                avg_entry = 0.0
                buys_in_cycle = 0
            
            # Buy Phase
            if days_in_cycle < BUY_DAYS and position < 1.0 and buys_in_cycle < max_buys:
                if signal:
                    add_size = 1.0 / max_buys
                    if position == 0.0:
                        avg_entry = price
                    else:
                        avg_entry = (avg_entry * position + price * add_size) / (position + add_size)
                    position += add_size
                    buys_in_cycle += 1
            
            # 일일 수익률
            if position > 0:
                daily_ret = (price / df['Close'].iloc[i-1] - 1) * position
                df.loc[df.index[i], 'Daily_Return'] = daily_ret
            
            equity *= (1 + df.loc[df.index[i], 'Daily_Return'])
            df.loc[df.index[i], 'Equity'] = equity
        
        total_ret = equity - 1
        years = (df.index[-1] - df.index[0]).days / 365.25
        cagr = (equity ** (1 / years) - 1) if years > 0 else 0
        mdd = ((df['Equity'] / df['Equity'].cummax()) - 1).min()
        
        results[ticker] = {'TR': total_ret, 'CAGR': cagr, 'MDD': mdd}
        equity_curves[ticker] = df['Equity']
        
        print(f"   TR: {total_ret:8.1%} | CAGR: {cagr:6.2%} | MDD: {mdd:7.1%}")

    # ====================== 포트폴리오 ======================
    portfolio = sum(equity_curves[t] * weights[t] for t in TARGET_TICKERS)
    port_final = portfolio.iloc[-1]
    port_cagr = (port_final ** (1 / years) - 1) if years > 0 else 0
    port_mdd = ((portfolio / portfolio.cummax()) - 1).min()

    print("\n" + "="*80)
    print(f"🏆 SOXL {weights['SOXL']*100:.0f}% | TQQQ {weights['TQQQ']*100:.0f}% 결과")
    print("="*80)
    for t, r in results.items():
        print(f"{t:6} | {weights[t]*100:2.0f}% | TR: {r['TR']:8.1%} | CAGR: {r['CAGR']:6.2%} | MDD: {r['MDD']:7.1%}")
    
    print(f"\n💼 Portfolio → TR: {port_final-1:7.1%} | "
          f"CAGR: {port_cagr:6.2%} | MDD: {port_mdd:6.1%}")

    return {
        'weights': weights,
        'portfolio_equity': portfolio,
        'TR': port_final - 1,
        'CAGR': port_cagr,
        'MDD': port_mdd,
        'years': years
    }


def optimize_weights(step=0.05):
    """SOXL 비중을 변화시키며 최적화 (그리드 서치)"""
    print("\n🔍 SOXL/TQQQ 비중 최적화 시작...")
    best_cagr = -np.inf
    best_result = None
    results_list = []

    soxl_weights = np.arange(0.3, 0.81, step)
    
    for w_soxl in soxl_weights:
        weights = {"SOXL": round(w_soxl, 3), "TQQQ": round(1 - w_soxl, 3)}
        result = run_backtest(weights)
        
        results_list.append({
            'SOXL%': weights['SOXL']*100,
            'TQQQ%': weights['TQQQ']*100,
            'CAGR': result['CAGR'],
            'MDD': result['MDD'],
            'TR': result['TR']
        })
        
        if result['CAGR'] > best_cagr:
            best_cagr = result['CAGR']
            best_result = result

    print("\n" + "="*60)
    print("🎯 최적 비중 결과")
    print("="*60)
    print(f"Best → SOXL {best_result['weights']['SOXL']*100:.1f}% | "
          f"TQQQ {best_result['weights']['TQQQ']*100:.1f}%")
    print(f"CAGR: {best_result['CAGR']:.2%} | MDD: {best_result['MDD']:.1%} | "
          f"TR: {best_result['TR']:.1%}")

    return best_result, results_list


# ====================== 실행 ======================
if __name__ == "__main__":
    # 1. 기본 추천 비중으로 실행
    run_backtest(DEFAULT_WEIGHTS)
    
    # 2. 최적화 실행 (원하면 주석 해제)
    # best, _ = optimize_weights(step=0.05)
    
    # 3. 차트 (최종 실행한 비중으로)
    # plt.figure(figsize=(15, 9))
    # ... (필요시 추가)