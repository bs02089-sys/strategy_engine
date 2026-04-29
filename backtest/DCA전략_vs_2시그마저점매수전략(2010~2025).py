import yfinance as yf
import numpy as np

# 티커 설정: SPYM은 과거 데이터 없으므로 SPY (S&P 500 ETF)로 대체
ticker = 'SPY'

# 데이터 다운로드 (배당 조정된 Close 사용, 2010~2025 기간)
data = yf.download(ticker, start='2010-01-01', end='2026-01-12',
                   progress=False, auto_adjust=True)

if data.empty or 'Close' not in data.columns:
    print("데이터를 불러오지 못했습니다. 티커나 인터넷 연결 확인하세요.")
else:
    prices = data['Close'].dropna()

    if len(prices) == 0:
        print("가격 데이터가 없습니다.")
    else:
        # Warning 없이 스칼라 추출
        current_price = prices.iloc[-1].item()  # 또는 prices.iat[-1]

        print(f"불러온 데이터 기간: {prices.index[0].date()} ~ {prices.index[-1].date()}")
        print(f"최종 가격 (배당 조정 Close): ${current_price:.2f}\n")

        # 2시그마 계산: 20일 Bollinger Band lower
        window = 20
        rolling_mean = prices.rolling(window=window).mean()
        rolling_std = prices.rolling(window=window).std()
        lower_band = rolling_mean - 2 * rolling_std
        lower_band_shifted = lower_band.shift(1)

        # 매월 말일 (2010~2020년만)
        monthly_end = prices.resample('ME').last()
        monthly_end_2010_2020 = monthly_end[(monthly_end.index.year >= 2010) & (monthly_end.index.year <= 2020)]

        # 투자 금액
        investment = 1000

        # 1. DCA 전략
        dca_shares = 0.0
        dca_spent = 0
        for price_np in monthly_end_2010_2020.values:  # numpy array
            price = price_np.item()  # 스칼라 변환 (Warning 없음)
            if price > 0:
                shares = investment / price
                dca_shares += shares
                dca_spent += investment

        dca_value = dca_shares * current_price
        dca_return = (dca_value - dca_spent) / dca_spent * 100 if dca_spent > 0 else 0

        # 2. 2시그마 저점 매수 전략
        sigma_shares = 0.0
        sigma_spent = 0
        buy_count = 0
        price_values = prices.values
        lb_values = lower_band_shifted.values
        for i in range(len(prices)):
            date = prices.index[i]
            if date.year < 2010 or date.year > 2020:
                continue
            price = price_values[i].item()  # 스칼라 변환 (Warning 없음)
            lb = lb_values[i]
            if not np.isnan(lb) and price < lb and price > 0:
                shares = investment / price
                sigma_shares += shares
                sigma_spent += investment
                buy_count += 1

        sigma_value = sigma_shares * current_price
        sigma_return = (sigma_value - sigma_spent) / sigma_spent * 100 if sigma_spent > 0 else 0

        # 결과 출력
        print("=== SPY 2010~2020 전략 비교 ===")
        print("\n1. 매월 말일 DCA 전략")
        print(f"투자 횟수: {len(monthly_end_2010_2020)}")
        print(f"총 투자 금액: ${dca_spent:,.0f}")
        print(f"보유 주식 수: {dca_shares:.4f}")
        print(f"최종 가치: ${dca_value:,.2f}")
        print(f"수익률: {dca_return:.2f}%\n")

        print("2. 2시그마 저점 매수 전략")
        print(f"매수 기회 횟수: {buy_count}")
        print(f"총 투자 금액: ${sigma_spent:,.0f}")
        print(f"보유 주식 수: {sigma_shares:.4f}")
        print(f"최종 가치: ${sigma_value:,.2f}")
        print(f"수익률: {sigma_return:.2f}%")

        if abs(dca_return - sigma_return) < 0.1:
            print("\n결론: 두 전략 수익률이 거의 동일합니다.")
        elif dca_return > sigma_return:
            print("\n결론: 매월 말일 DCA 방식이 수익률이 더 높았습니다.")
        else:
            print("\n결론: 2시그마 저점 매수 방식이 수익률이 더 높았습니다.")

        print("\n참고: 총 투자 금액이 다를 수 있습니다 (2시그마는 매수 신호 없을 때 현금 보유).")
        print("거래 수수료, 세금, 슬리피지 미반영. 과거 백테스트 결과로 미래 성과 보장되지 않습니다.")