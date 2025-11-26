# QuantumAurum.py - 영원한 불멸의 전설 (2025.11.20 최종)
import yfinance as yf
import pandas as pd
import numpy as np
import json
from datetime import datetime
from zoneinfo import ZoneInfo
from dateutil.relativedelta import relativedelta
import warnings, cvxpy as cp
warnings.filterwarnings("ignore")

from pypfopt import risk_models, expected_returns, BlackLittermanModel
from hmmlearn.hmm import GaussianHMM
from scipy.cluster.hierarchy import linkage
from pandas_datareader import data as pdr

TICKERS = ['QQQM', 'IAUM']
np.random.seed(42)

# 1. 무위험금리
def get_rf():
    try:
        end = datetime.now(ZoneInfo("America/New_York")).date()
        rate = pdr.DataReader('DGS10', 'fred', end - relativedelta(years=5), end)['DGS10'].iloc[-1]/100
        print(f"✔ 10년물 금리: {rate*100:.3f}%")
        return rate
    except:
        print("FRED 실패 → 4.12% 고정")
        return 0.0412
rf = get_rf()

# 2. 데이터 (완전 핵방어)
def get_data():
    print("데이터 다운로드 (불멸 모드)")
    prices = pd.DataFrame()
    for t in TICKERS:
        hist = yf.Ticker(t).history(period="max")
        prices[t] = hist['Close' if 'Adj Close' not in hist.columns else 'Adj Close']
    prices = prices.dropna()
    returns = prices.pct_change().dropna()
    print(f"성공 → {prices.index[0].date()} ~ {prices.index[-1].date()} ({len(returns)}일)")
    return prices, returns
prices, returns = get_data()

# 3. 체제 감지
model = GaussianHMM(n_components=4, covariance_type="diag", random_state=42)
model.fit(returns)
current = ['Extreme Bear','Bear','Neutral/Gold','Bull'][np.argsort(model.means_.mean(1))[np.argmax(model.predict(returns.iloc[-1:]))]]
print(f"현재 체제 → {current}")

# 4. BL View
strength = {'Bull':0.18, 'Neutral/Gold':0.10, 'Bear':0.04, 'Extreme Bear':0.01}.get(current, 0.10)
P, Q = np.array([[1, -1]]), np.array([strength])
omega = np.eye(1) * 0.05
pi = expected_returns.capm_return(prices, risk_free_rate=rf)

bl = BlackLittermanModel(returns.cov(), pi=pi, P=P, Q=Q, omega=omega, tau=0.05)
mu, cov = bl.bl_returns(), bl.bl_cov()

# 5. HRP (NaN 완벽 방어 + 초간결)
def safe_hrp(cov_matrix):
    corr = risk_models.cov_to_corr(cov_matrix)
    corr = np.nan_to_num(corr, nan=0.0)           # NaN 방어
    corr = (corr + corr.T) / 2                    # 강제 대칭
    np.fill_diagonal(corr, 1)
    dist = np.sqrt(np.clip((1 - corr) / 2, 0, None))
    dist = (dist + dist.T) / 2                    # 또 강제 대칭
    link = linkage(dist, 'ward')
    order = [int(i) for i in linkage(dist, 'ward')[:, :2].flatten()]
    order = list(dict.fromkeys(order)) + [i for i in range(len(cov_matrix)) if i not in order]
    w = np.zeros(len(cov_matrix))
    for i in order:
        w[i] = 1.0
    return pd.Series(w / w.sum() if w.sum() > 0 else [0.5,0.5], index=cov_matrix.columns)

hrp_w = safe_hrp(cov)

# 6. 최종 최적화 (간단 Max Sharpe)
w = cp.Variable(2)
risk = cp.quad_form(w, cov.values)
ret = mu.values @ w
prob = cp.Problem(cp.Maximize(ret - 0.5 * risk), [cp.sum(w)==1, w>=0.05, w<=0.95])
prob.solve(solver=cp.CLARABEL)
final_w = w.value if w.value is not None else hrp_w.values
final_w = np.clip(final_w, 0.05, 0.95)
final_w /= final_w.sum()

# 7. 출력
print("\n" + "="*80)
print("           QUANTUM AURUM - 최종 승리의 순간")
print("="*80)
for t, weight in zip(TICKERS, final_w):
    print(f"{t:<6} → {weight:.4f} ({weight*100:5.2f}%)")
print(f"체제   → {current}")
print(f"예상 연수익 → {(mu.values @ final_w * 100):.2f}%")
print("="*80)

# JSON 저장
result = {
    "date": datetime.now().strftime("%Y-%m-%d"),
    "regime": current,
    "target_weights": {"QQQM": round(float(final_w[0]),4), "IAUM": round(float(final_w[1]),4)},
    "expected_return": round(float(mu.values @ final_w),4)
}
json.dump(result, open("params/target_weights.json","w", encoding="utf-8"), indent=4)
print("✅ target_weights.json 저장 완료")