import yfinance as yf
import pandas as pd
from datetime import datetime
from zoneinfo import ZoneInfo

# 설정
TICKERS = ['TSLA', 'IONQ']
today = datetime.now(ZoneInfo("America/New_York")).date()

print(f"\n{'='*50}")
print(f"   ♥ 우리 가족 투자 보고서 ♥")
print(f"   오늘 날짜: {today} (뉴욕 기준)")
print(f"{'='*50}")

# 데이터 불러오기 (progress=False 삭제!)
prices = pd.concat([
    yf.Ticker(t).history(period="2y")['Close'].rename(t)  # ← 여기만 수정!
    for t in TICKERS
], axis=1).dropna()

returns = prices.pct_change().dropna()

# 지표 계산
vol_6m = returns.tail(126).std() * (252 ** 0.5)
recent_ret_annual = returns.tail(63).mean() * 252

# 체제 판단
if recent_ret_annual['TSLA'] >= 0.15 and vol_6m['TSLA'] <= vol_6m['IONQ'] * 1.8:
    regime = "Bull"
    tsla_weight = 0.88
    ionq_weight = 0.12
else:
    regime = "Bear"
    tsla_weight = 0.65
    ionq_weight = 0.35

# 예쁘게 출력
tsla_pct = int(tsla_weight * 100)
ionq_pct = int(ionq_weight * 100)

print(f"\n오늘 우리 가족 체제: {regime}")
print(f"TSLA 최근 3개월 연수익률: {recent_ret_annual['TSLA']:.1%}")
print(f"TSLA vs IONQ 변동성 비교: {vol_6m['TSLA']:.1%} vs {vol_6m['IONQ']:.1%}\n")

print(f"♥♥♥ 추천 비중 ♥♥♥")
print(f"    TSLA: {tsla_pct}%")
print(f"    IONQ: {ionq_pct}%")
print(f"\n이대로 리밸런싱 해주면 됩니다 ♥")
print(f"아내 사랑해요~ ")

print(f"\n{'='*50}")