"""
backtest_soxl_tqqq_vectorbt_final.py — vectorbt 최적화 (에러 완전 해결)
"""

import numpy as np
import pandas as pd
import vectorbt as vbt
import matplotlib.pyplot as plt
import seaborn as sns

plt.rcParams['font.family'] = 'Malgun Gothic'
plt.rcParams['axes.unicode_minus'] = False

# ====================== 설정 ======================
TICKERS = ["SOXL", "TQQQ"]
START = "2022-01-01"
END = "2026-06-05"

print("📥 데이터 다운로드 중...")
data = vbt.YFData.download(TICKERS, start=START, end=END, auto_adjust=True)
price = data.get("Close")

print(f"📊 데이터 기간: {price.index[0].date()} ~ {price.index[-1].date()} | "
      f"행 개수: {len(price)}\n")

# ====================== 안전한 scalar 변환 함수 ======================
def to_scalar(x):
    if isinstance(x, pd.Series):
        return float(x.iloc[-1]) if not x.empty else 0.0
    elif isinstance(x, pd.DataFrame):
        return float(x.iloc[-1, -1]) if not x.empty else 0.0
    else:
        return float(x)

# ====================== 그리드 서치 ======================
weight_steps = np.arange(0.30, 0.81, 0.05)

results = []
best_cagr = -np.inf
best_weights = None
best_pf = None

print("🚀 SOXL/TQQQ 비중 그리드 서치 시작...\n")

for w_soxl in weight_steps:
    w_tqqq = 1 - w_soxl
    
    target_weights = pd.DataFrame(
        [[w_soxl, w_tqqq]] * len(price),
        index=price.index,
        columns=TICKERS
    )
    
    pf = vbt.Portfolio.from_orders(
        price,
        size=target_weights,
        size_type='targetpercent',
        init_cash=100_000,
        fees=0.0005,
        slippage=0.0005,
        freq='D',
        cash_sharing=True,
        group_by=False
    )
    
    # 안전 변환
    tr = to_scalar(pf.total_return())
    cagr = to_scalar(pf.annualized_return())
    mdd = to_scalar(pf.max_drawdown())
    sharpe = to_scalar(pf.sharpe_ratio(risk_free=0.0))
    final_value = to_scalar(pf.value())
    
    results.append({
        'SOXL_%': round(w_soxl * 100, 1),
        'TQQQ_%': round(w_tqqq * 100, 1),
        'Total_Return': tr,
        'CAGR_%': round(cagr * 100, 2),
        'MDD_%': round(mdd * 100, 2),
        'Sharpe': round(sharpe, 2),
        'Final_Value': final_value
    })
    
    print(f"SOXL {w_soxl*100:5.1f}% | TQQQ {w_tqqq*100:5.1f}% → "
          f"CAGR: {cagr:6.2%} | MDD: {mdd:6.1%} | Sharpe: {sharpe:.2f}")
    
    if cagr > best_cagr:
        best_cagr = cagr
        best_weights = (w_soxl, w_tqqq)
        best_pf = pf

# ====================== 결과 출력 ======================
results_df = pd.DataFrame(results)

print("\n" + "="*110)
print("🎯 최적화 결과 Top 5 (CAGR 기준)")
print("="*110)
print(results_df.sort_values('CAGR_%', ascending=False).head(7).round(2))

print("\n" + "="*90)
print(f"🏆 BEST 조합: SOXL {best_weights[0]*100:.1f}% | TQQQ {best_weights[1]*100:.1f}%")
print(f"CAGR        : {to_scalar(best_pf.annualized_return()):.2%}")
print(f"총수익률    : {to_scalar(best_pf.total_return()):.1%}")
print(f"Max Drawdown: {to_scalar(best_pf.max_drawdown()):.1%}")
print(f"Sharpe Ratio: {to_scalar(best_pf.sharpe_ratio(risk_free=0.0)):.2f}")
print(f"Final Value : {to_scalar(best_pf.value()):,.0f}")
print("="*90)

# ====================== 차트 ======================
fig = plt.figure(figsize=(15, 10))
ax1 = plt.subplot(2, 1, 1)
best_pf.plot_value(ax=ax1, title=f'Best Portfolio Equity Curve (SOXL {best_weights[0]*100:.1f}%)')
ax1.grid(True)

ax2 = plt.subplot(2, 1, 2)
best_pf.plot_drawdown(ax=ax2)
ax2.grid(True)

plt.tight_layout()
plt.savefig('soxl_tqqq_vectorbt_best.png', dpi=200, bbox_inches='tight')
plt.show()

# ====================== Heatmap ======================
pivot = results_df.pivot(index='SOXL_%', columns='TQQQ_%', values='CAGR_%')

plt.figure(figsize=(10, 8))
sns.heatmap(pivot, annot=True, fmt=".1f", cmap="viridis", linewidths=0.5)
plt.title('SOXL vs TQQQ 비중별 CAGR Heatmap (%)')
plt.xlabel('TQQQ %')
plt.ylabel('SOXL %')
plt.savefig('soxl_tqqq_heatmap.png', dpi=200)
plt.show()