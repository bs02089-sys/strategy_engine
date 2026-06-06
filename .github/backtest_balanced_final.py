"""
backtest_balanced_final.py — 균형 추천 최종 버전
AIQ 35% | SOXX 35% | SOXL 30%
"""

import json
import numpy as np
import pandas as pd
import yfinance as yf
import matplotlib.pyplot as plt

# ====================== config 로드 ======================
with open("config.json", "r", encoding="utf-8") as f:
    cfg = json.load(f)

POSITIONS = cfg["POSITIONS"]
STRATEGY = cfg.get("STRATEGY", {})

TARGET_TICKERS = ["AIQ", "SOXX", "SOXL"]
BUY_DAYS = STRATEGY.get("BUY_DURATION_DAYS", 252)
HOLD_DAYS = STRATEGY.get("HOLD_DURATION_DAYS", 252)
CYCLE_DAYS = BUY_DAYS + HOLD_DAYS

# ==================== 균형 추천 비중 ====================
WEIGHTS = {
    "AIQ": 0.35,
    "SOXX": 0.35,
    "SOXL": 0.30
}

QUARTERLY_COUNT = {
    "AIQ": 28,
    "SOXX": 20,
    "SOXL": 25
}

print(f"🚀 균형 추천 비중 백테스트 (AIQ 35% | SOXX 35% | SOXL 30%)")
print(f"   Buy Phase: {BUY_DAYS}일 분할매수 | Hold Phase: {HOLD_DAYS}일\n")

data = yf.download(TARGET_TICKERS, start="2022-01-01", end="2026-06-06", 
                   auto_adjust=True, group_by='ticker')

results = {}
equity_curves = {}

for ticker in TARGET_TICKERS:
    print(f"\n🔹 {ticker} ({WEIGHTS[ticker]*100:.0f}%, {QUARTERLY_COUNT[ticker]}회 분할)")
    df = data[ticker].copy().dropna()
    
    pos = POSITIONS[ticker]
    multiplier = pos["ENTRY_MULTIPLIER"]
    lookback = pos["LOOKBACK_DAYS"]
    
    df['Return'] = df['Close'].pct_change()
    df['Sigma'] = df['Close'].pct_change().rolling(lookback).std()
    df['LOC'] = df['Close'].shift(1) * np.exp(-multiplier * df['Sigma'].shift(1))
    df['Signal'] = (df['Close'] <= df['LOC']) & df['LOC'].notna()
    
    equity = 1.0
    position = 0.0
    avg_entry = 0.0
    cycle_start = 0
    buys_in_cycle = 0
    max_buys = QUARTERLY_COUNT[ticker]
    
    df['Equity'] = 1.0
    df['Daily_Return'] = 0.0
    
    for i in range(1, len(df)):
        price = df['Close'].iloc[i]
        signal = df['Signal'].iloc[i]
        days_in_cycle = i - cycle_start
        
        # 새 사이클 시작
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
portfolio = sum(equity_curves[t] * WEIGHTS[t] for t in TARGET_TICKERS)

print("\n" + "="*85)
print("🏆 균형 추천 비중 (35 / 35 / 30) 최종 결과")
print("="*85)
for t, r in results.items():
    print(f"{t:6} | {WEIGHTS[t]*100:2.0f}% | TR: {r['TR']:8.1%} | CAGR: {r['CAGR']:6.2%} | MDD: {r['MDD']:7.1%}")

print(f"\n💼 Total Portfolio → {portfolio.iloc[-1]-1:8.1%} "
      f"(CAGR {(portfolio.iloc[-1] ** (1/years) -1):6.2%} | "
      f"MDD {((portfolio/portfolio.cummax())-1).min():6.1%})")

# 차트
plt.figure(figsize=(15, 9))
for t in TARGET_TICKERS:
    plt.plot(equity_curves[t], label=f"{t} ({WEIGHTS[t]*100:.0f}%)")
plt.plot(portfolio, label="Balanced Portfolio (35/35/30)", linewidth=3, color='red', linestyle='--')
plt.title('반도체 슈퍼사이클 전략 - 균형 추천 비중 (AIQ 35% | SOXX 35% | SOXL 30%)')
plt.legend()
plt.grid(True)
plt.savefig('backtest_balanced_35_35_30.png', dpi=200, bbox_inches='tight')
plt.show()