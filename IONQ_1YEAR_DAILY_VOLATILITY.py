import yfinance as yf
import numpy as np
from datetime import datetime, timedelta

# IONQ 티커 설정
symbol = 'IONQ'

# 최근 12개월 데이터 가져오기
end_date = datetime.now()
start_date = end_date - timedelta(days=252)  # 12개월 = 252일
df = yf.download(symbol, start=start_date, end=end_date, interval='1d', auto_adjust=False)

# 데이터 다운로드 확인
if df is None or df.empty:
	print(f"Error: Could not download data for {symbol}")
	exit()

# 일일 수익률 계산 (Close 사용)
df['daily_return'] = df['Close'].pct_change()

# 평균 일간 변동성 계산 (표준편차)
daily_volatility = df['daily_return'].std()
# 연환산 변동성 계산 (ETF 거래일 기준 약 252일)
annualized_volatility = daily_volatility * np.sqrt(252)

# 결과 출력
print(f"IONQ 최근 12개월 평균 일간 변동성: {daily_volatility:.4f}")
print(f"IONQ 최근 12개월 연환산 변동성: {annualized_volatility:.4f}")