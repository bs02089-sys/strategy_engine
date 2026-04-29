import yfinance as yf
import numpy as np
from datetime import datetime, timedelta

# 최근 1년 데이터
end_date = datetime.today()
start_date = end_date - timedelta(days=365)
ticker = "SOXL"
df = yf.download(ticker, start=start_date.strftime("%Y-%m-%d"),
                 end=end_date.strftime("%Y-%m-%d"), progress=False)

df = df[['Close','Low']].dropna().reset_index(drop=True)

# σ 계산
df['Return'] = df['Close'].pct_change()
sigma = df['Return'].std()

# 초기 자본
initial_capital = 10_000.0
invest_amount = initial_capital / 20
occurrence_count = 0
max_occurrence = 20
shares_p1 = 0.0
shares_p2 = 0.0

for i in range(1, len(df)):
    prev_close = df['Close'].iloc[i-1].item()   # 스칼라 변환
    low = df['Low'].iloc[i].item()
    today_close = df['Close'].iloc[i].item()
    
    p1 = prev_close * (1 - sigma)   # -1σ 가격
    p2 = prev_close * (1 - 2*sigma) # -2σ 가격
    
    # LOC 체결 여부 확인 (체결가는 당일 종가)
    if low <= p1 and occurrence_count < max_occurrence:
        occurrence_count += 1
        shares_p1 += invest_amount / today_close
    if low <= p2 and occurrence_count < max_occurrence:
        occurrence_count += 1
        shares_p2 += invest_amount / today_close

# 최종 평가
final_price = df['Close'].iloc[-1].item()
final_capital_p1 = shares_p1 * final_price
final_capital_p2 = shares_p2 * final_price

years = len(df) / 252.0
cagr_p1 = ((final_capital_p1 / initial_capital) ** (1 / years) - 1) * 100 if final_capital_p1 > 0 else 0
cagr_p2 = ((final_capital_p2 / initial_capital) ** (1 / years) - 1) * 100 if final_capital_p2 > 0 else 0

print("최근 1년간 일간 σ:", round(sigma*100,2), "%")
print("연간 투자 횟수 (최대 20회 제한):", occurrence_count)
print("최종 자본 (-1σ LOC):", round(final_capital_p1,2))
print("최근 1년간 CAGR (-1σ LOC):", round(cagr_p1,2), "%")
print("최종 자본 (-2σ LOC):", round(final_capital_p2,2))
print("최근 1년간 CAGR (-2σ LOC):", round(cagr_p2,2), "%")