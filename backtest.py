"""
backtest_soxl_tqqq_vectorbt.py — vectorbt를 활용한 비중 최적화 버전
"""

import numpy as np
import pandas as pd
import vectorbt as vbt
import matplotlib.pyplot as plt
from itertools import product

plt.rcParams['font.family'] = 'Malgun Gothic'
plt.rcParams['axes.unicode_minus'] = False

# ====================== 설정 ======================
TICKERS = ["SOXL", "TQQQ"]
START = "2022-01-01"
END = "2026-06-05"

# vectorbt로 데이터 다운로드
print("📥 데이터 다운로드 중...")
data = vbt.YFData.download(TICKERS, start=START, end=END, auto_adjust=True)
price = data.get("Close")

print(f"📊 데이터 기간: {price.index[0].date()} ~ {price.index[-1].date()}")
print(f"   종목: {TICKERS}\n")

# ====================== 그리드 서치 최적화 ======================
weight_steps = np.arange(0.30, 0.81, 0.05)  # SOXL 비중 30% ~ 80%, 5% 단위

results = []
best_cagr = -np.inf
best_weights = None
best_pf = None

print("🚀 SOXL/TQQQ 비중 그리드 서치 시작...\n")

for w_soxl in weight_steps:
    weights = np.array([w_soxl, 1 - w_soxl])
    weight_dict = dict(zip(TICKERS, weights))
    
    # 매일 리밸런싱 가정 (원하는 주기: 'W', 'M' 등으로 변경 가능)
    pf = vbt.Portfolio.from_weights(
        price,
        weights=weights,           # array 또는 Series
        freq='D',
        init_cash=100_000,
        fees=0.0005,               # 0.05% 거래수수료 (필요시 조정)
        slippage=0.0005,
    )
    
    tr = pf.total_return()
    cagr = pf.annualized_return()
    mdd = pf.max_drawdown()
    sharpe = pf.sharpe_ratio(risk_free=0.0)
    
    results.append({
        'SOXL_%': round(w_soxl * 100, 1),
        'TQQQ_%': round((1 - w_soxl) * 100, 1),
        'Total_Return': tr,
        'CAGR': cagr,
        'MDD': mdd,
        'Sharpe': sharpe,
        'Final_Value': pf.value().iloc[-1]
    })
    
    print(f"SOXL {w_soxl*100:5.1f}% | TQQQ {(1-w_soxl)*100:5.1f}% → "
          f"CAGR: {cagr:6.2%} | MDD: {mdd:6.1%} | Sharpe: {sharpe:.2f}")
    
    if cagr > best_cagr:
        best_cagr = cagr
        best_weights = weight_dict
        best_pf = pf

# ====================== 결과 정리 ======================
results_df = pd.DataFrame(results)
print("\n" + "="*100)
print("🎯 최적화 결과 Top 5 (CAGR 기준)")
print("="*100)
print(results_df.sort_values('CAGR', ascending=False).head(5).round(4))

print("\n" + "="*80)
print(f"🏆 BEST 조합: SOXL {best_weights['SOXL']*100:.1f}% | "
      f"TQQQ {best_weights['TQQQ']*100:.1f}%")
print(f"CAGR     : {best_pf.annualized_return():.2%}")
print(f"총수익률 : {best_pf.total_return():.1%}")
print(f"Max DD   : {best_pf.max_drawdown():.1%}")
print(f"Sharpe   : {best_pf.sharpe_ratio():.2f}")
print("="*80)

# ====================== 차트 ======================
fig = plt.figure(figsize=(15, 10))

# Equity Curve
ax1 = plt.subplot(2, 1, 1)
best_pf.plot_value(ax=ax1)
ax1.set_title(f'SOXL + TQQQ 최적 포트폴리오 Equity Curve (SOXL {best_weights["SOXL"]*100:.1f}%)')
ax1.grid(True)

# Drawdown
ax2 = plt.subplot(2, 1, 2)
best_pf.plot_drawdown(ax=ax2)
ax2.grid(True)

plt.tight_layout()
plt.savefig('soxl_tqqq_vectorbt_best.png', dpi=200, bbox_inches='tight')
plt.show()

# Heatmap (CAGR)
pivot = results_df.pivot(index='SOXL_%', columns='TQQQ_%', values='CAGR')
plt.figure(figsize=(10, 8))
plt.title('SOXL vs TQQQ 비중별 CAGR Heatmap')
import seaborn as sns
sns.heatmap(pivot * 100, annot=True, fmt=".1f", cmap="viridis")
plt.xlabel('TQQQ %')
plt.ylabel('SOXL %')
plt.savefig('soxl_tqqq_heatmap.png', dpi=200)
plt.show()