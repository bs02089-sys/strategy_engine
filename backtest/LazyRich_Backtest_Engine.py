import yfinance as yf
import numpy as np
import pandas as pd

# 데이터 다운로드
tickers = ["QQQM", "IAUM"]
data = yf.download(tickers, period="5y", auto_adjust=True)["Close"]
returns = data.pct_change().dropna()

# 포트폴리오 수익률 기록
weights = []
portfolio_returns = []

for i in range(126, len(returns)):  # 6개월 이후부터 시작
    window = returns.iloc[i-126:i]
    vol_6m = window.std() * np.sqrt(252)
    mom_3m = (data.iloc[i] / data.iloc[i-63] - 1) * (252/63)

    if mom_3m["QQQM"] >= 0.12 and vol_6m["QQQM"] <= 0.28:
        wq, wi = 0.9, 0.1
    elif mom_3m["QQQM"] >= 0.05:
        wq, wi = 0.8, 0.2
    elif mom_3m["QQQM"] >= -0.08:
        wq, wi = 0.7, 0.3
    else:
        wq, wi = 0.55, 0.45

    weights.append((wq, wi))
    pr = wq * returns["QQQM"].iloc[i] + wi * returns["IAUM"].iloc[i]
    portfolio_returns.append(pr)

# 누적 성과
cum = (1 + pd.Series(portfolio_returns)).cumprod()
years = len(portfolio_returns)/252
CAGR = cum.iloc[-1]**(1/years) - 1
MDD = ((cum / cum.cummax()) - 1).min()
Sharpe = np.mean(portfolio_returns)/np.std(portfolio_returns)*np.sqrt(252)

# 소숫점 둘째 자리까지 반올림하여 출력
print("CAGR:", round(CAGR, 2))
print("MDD:", round(MDD, 2))
print("Sharpe:", round(Sharpe, 2))