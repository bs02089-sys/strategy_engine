from datetime import datetime, timedelta
import pandas as pd
from massive import RESTClient

# ==================== Massive API 키 ====================
MASSIVE_API_KEY = 'pa7gh7QKIngG4hJoIQUzVNudPkankyAD'

client = RESTClient(api_key=MASSIVE_API_KEY)

# ==================== 설정 ====================
tickers = ['BITU', 'IONQ']
holdings = {'BITU': 192, 'IONQ': 470}

# ★★★ 여기만 바꾸면 됩니다 ★★★
target_weights = [0.40, 0.60]   # BITU 40%, IONQ 60% (원하는 비중으로 자유롭게 변경)

# ==================== 현재 가격 가져오기 ====================
print("📡 현재 가격 가져오는 중...")
end_date = datetime.now().date()
start_date = end_date - timedelta(days=5)   # 최근 가격만 필요

prices = {}
for ticker in tickers:
    aggs = list(client.list_aggs(ticker=ticker, multiplier=1, timespan='day',
                                 from_=start_date.strftime('%Y-%m-%d'),
                                 to=end_date.strftime('%Y-%m-%d'), limit=10))
    df = pd.DataFrame([{'close': a.close} for a in aggs])
    prices[ticker] = df['close'].iloc[-1]   # 가장 최근 종가
    print(f"✅ {ticker}: ${prices[ticker]:.2f}")

# ==================== 리밸런싱 계산 ====================
total_value = sum(holdings[t] * prices[t] for t in tickers)

print(f"\n💰 총 포트폴리오 가치: ${total_value:,.2f}")

print("\n📌 리밸런싱 목표 주수")
for i, t in enumerate(tickers):
    target_value = target_weights[i] * total_value
    target_shares = round(target_value / prices[t])
    diff = target_shares - holdings[t]
    
    action = f"매수 +{diff}주" if diff > 0 else f"매도 {abs(diff)}주" if diff < 0 else "유지"
    print(f"   {t}: 목표 {target_shares}주 ({target_weights[i]:.0%}) → (현재 {holdings[t]}주 → {action})")

print("\n✅ 수동 목표 비중 적용 완료")