import numpy as np
import pandas as pd
import yfinance as yf

def run_sigma_frequency_pure_pandas(ticker="SOXL", period="2y"):
    print(f"📡 {ticker} 최근 {period} 데이터 수집 중 (무결점 Pandas 엔진)...")
    df = yf.download(ticker, period=period, interval="1d", progress=False)
    if df.empty:
        print("❌ 데이터를 가져오지 못했습니다.")
        return

    # 야후 파이낸스 MultiIndex 컬럼 구조 강제 단일화 방어
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.droplevel(1)

    # 1. 90일 로그수익률 기반 일간 시그마 계산 (메인 엔진과 완벽 동기화)
    log_returns = np.log(df['Close'] / df['Close'].shift(1))
    rolling_std = log_returns.rolling(window=90).std()
    
    df['Daily_Sigma'] = rolling_std
    df = df.dropna().copy()

    # 테스트할 하락 시그마 배수 레이어
    sigma_multipliers = [-1.0, -0.9, -0.8, -0.7, -0.6, -0.5]
    report_data = []
    
    print("\n📊 각 시그마 비율별 빈도수 및 실전 시뮬레이션 결과")
    print("=" * 75)
    print(f"{'시그마 배수':^12} | {'총 거래일':^10} | {'체결 횟수':^10} | {'체결 빈도율':^12} | {'5일 보유 후 평군수익률'}")
    print("=" * 75)

    for m in sigma_multipliers:
        # 당일 시가 기준 매수 목표가 역산
        target_prices = df['Open'] * (1 + m * df['Daily_Sigma'])
        
        # 당일 저가가 목표가 이하로 내려갔으면 체결(Hit)로 판정
        is_triggered = df['Low'] <= target_prices
        hit_count = is_triggered.sum()
        total_days = len(df)
        hit_ratio = (hit_count / total_days) * 100
        
        # 🎯 [순수 판다스 연산] 체결된 날 당일 타점가 매수 -> 5거래일 후 종가 청산 시 수익률 전수조사
        returns = []
        triggered_indices = np.where(is_triggered)[0]
        
        for idx in triggered_indices:
            # 5거래일 뒤의 데이터가 존재하는 경우에만 계산 (오버플로우 방지)
            if idx + 5 < len(df):
                buy_price = target_prices.iloc[idx]
                sell_price = df['Close'].iloc[idx + 5]
                trade_return = (sell_price - buy_price) / buy_price
                returns.append(trade_return)
        
        # 평균 수익률 산출
        if returns:
            avg_return = np.mean(returns) * 100
            return_str = f"{avg_return:+.2f}%"
        else:
            avg_return = 0.0
            return_str = "0.00%"

        print(f"{m:^14.1f} | {total_days:^12} | {hit_count:^12} | {hit_ratio:^12.1f}% | {return_str:^18}")
        
        report_data.append({
            "Sigma": m,
            "Hit_Count": hit_count,
            "Hit_Ratio": hit_ratio,
            "Avg_Return": avg_return
        })

    print("=" * 75)
    
    report_df = pd.DataFrame(report_data)
    best_hit = report_df.loc[report_df['Hit_Count'].idxmax()]
    best_ret = report_df.loc[report_df['Avg_Return'].idxmax()]
    
    print(f"\n💡 [관제탑 분석 결과]")
    print(f" 🎯 가장 촘촘하게 자주 체결된 그물망: {best_hit['Sigma']:.1f}σ (빈도율 {best_hit['Hit_Ratio']:.1f}%)")
    print(f" 💰 타점 대비 탈출 효율이 가장 좋은 그물망: {best_ret['Sigma']:.1f}σ (평균 수익률 {best_ret['Avg_Return']:+.2f}%)")

if __name__ == "__main__":
    run_sigma_frequency_pure_pandas("SOXL", "2y")