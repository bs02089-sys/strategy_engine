"""
loc_dca_comparison.py — LOC 분할매수 비중 비교 (No Rebalancing)
SOXL 1년 계획 검증용
"""

import json
import numpy as np
import pandas as pd
import yfinance as yf
import matplotlib.pyplot as plt

plt.rcParams['font.family'] = 'Malgun Gothic'
plt.rcParams['axes.unicode_minus'] = False

# ====================== 설정 ======================
TARGET_TICKERS = ["SOXL"]
START = "2022-01-01"
END = "2026-07-22"

# 당신 상황
ALREADY_SOXL_BUYS = 0          # 이미 SOXL 1회 진입
TOTAL_BUYS_PLANNED = 20        # 앞으로 1년 동안 총 매수 회차 계획 

print("🔍 LOC 분할매수 비중 백테스트\n")

# ====================== 데이터 로드 ======================
data = yf.download(TARGET_TICKERS, start=START, end=END, auto_adjust=True, group_by='ticker')

# ====================== LOC 신호 함수 ======================
def add_loc_signal(df, multiplier=1.41, lookback=365):
    df = df.copy()
    df['Return'] = df['Close'].pct_change()
    df['Sigma'] = df['Return'].rolling(lookback).std()
    df['LOC'] = df['Close'].shift(1) * np.exp(-multiplier * df['Sigma'].shift(1))
    df['Signal'] = (df['Close'] <= df['LOC']) & df['LOC'].notna()
    return df

# ====================== LOC DCA 시뮬레이션 ======================
def simulate_loc_dca(soxl_weight=0.3, plot=False):
    weights = {"SOXL": soxl_weight}
    
    equity_curves = {}
    results = {}
    
    total_buys = 0
    max_buys = TOTAL_BUYS_PLANNED
    
    for ticker in TARGET_TICKERS:
        df = add_loc_signal(data[ticker])
        equity = 1.0
        position = 0.0
        buys_in_period = 0
        already = ALREADY_SOXL_BUYS if ticker == "SOXL" else 0
        
        df['Equity'] = 1.0
        df['Position'] = 0.0
        
        for i in range(1, len(df)):
            price = df['Close'].iloc[i]
            signal = df['Signal'].iloc[i]
            
            # 매수 로직 (이미 진입분 + 앞으로 분할)
            if buys_in_period + already < max_buys and signal and position < 1.0:
                add_size = 1.0 / (max_buys - already)
                position += add_size
                buys_in_period += 1
                total_buys += 1
            
            # 일일 수익률 (No Rebalancing)
            if position > 0:
                daily_ret = (price / df['Close'].iloc[i-1] - 1) * position
            else:
                daily_ret = 0
            
            equity *= (1 + daily_ret)
            df.loc[df.index[i], 'Equity'] = equity
            df.loc[df.index[i], 'Position'] = position
        
        total_ret = equity - 1
        years = (df.index[-1] - df.index[0]).days / 365.25
        cagr = (equity ** (1 / years) - 1) if years > 0 else 0
        mdd = ((df['Equity'] / df['Equity'].cummax()) - 1).min()
        calmar = cagr / abs(mdd) if mdd != 0 else 0
        
        results[ticker] = {'CAGR': cagr, 'MDD': mdd, 'Calmar': calmar, 'Final_Position': position}
        equity_curves[ticker] = df['Equity']
    
    # 포트폴리오 (No Rebalancing)
    portfolio = pd.Series(0.0, index=equity_curves[TARGET_TICKERS[0]].index)
    for t in TARGET_TICKERS:
        portfolio = portfolio.add(equity_curves[t] * weights[t], fill_value=0)
    port_final = portfolio.iloc[-1]
    port_cagr = (port_final ** (1 / years) - 1)
    port_mdd = ((portfolio / portfolio.cummax()) - 1).min()
    port_calmar = port_cagr / abs(port_mdd)
    
    if plot:
        plt.figure(figsize=(14, 8))
        for t in TARGET_TICKERS:
            plt.plot(equity_curves[t], label=f"{t} ({weights[t]*100:.0f}%)")
        plt.plot(portfolio, label="Portfolio", linewidth=3, color='red')
        plt.title(f'LOC DCA 전략 - SOXL {weights["SOXL"]*100:.0f}%')
        plt.legend()
        plt.grid(True)
        plt.show()
    
    return {
        'SOXL_%': int(weights["SOXL"]*100),
        'CAGR': port_cagr,
        'MDD': port_mdd,
        'Calmar': port_calmar,
        'Total_Return': port_final - 1
    }

# ====================== 여러 비중 테스트 ======================
test_weights = [0.2, 0.3, 0.4, 0.5, 0.6]
comparison = []

print("비중별 LOC 분할매수 백테스트 시작...\n")
for w in test_weights:
    res = simulate_loc_dca(soxl_weight=w)
    comparison.append(res)
    print(f"SOXL {res['SOXL_%']:2}% | CAGR {res['CAGR']:6.2%} | MDD {res['MDD']:6.1%} | Calmar {res['Calmar']:.2f}")

# 결과 정리
comp_df = pd.DataFrame(comparison)
print("\n" + "="*80)
print("📊 LOC 분할매수 비중 비교 결과")
print("="*80)
print(comp_df.sort_values('Calmar', ascending=False).round(4))

# 최고 성과 비중
best = comp_df.loc[comp_df['Calmar'].idxmax()]
print(f"\n🏆 Calmar 기준 최적 비중: SOXL {best['SOXL_%']}%")