import yfinance as yf
import pandas as pd
import numpy as np

def run_backtest():
    ticker = "SOXL"
    # 90일 시그마 계산을 위해 약 1.5년치 데이터 수집
    df = yf.download(ticker, period="2y", auto_adjust=True, progress=False)
    vix = yf.download("^VIX", period="2y", auto_adjust=True, progress=False)
    
    if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.droplevel(1)
    if isinstance(vix.columns, pd.MultiIndex): vix.columns = vix.columns.droplevel(1)

    # 로그 수익률 및 90일 이동 표준편차(연율화)
    df['log_ret'] = np.log(df['Close'] / df['Close'].shift(1))
    df['sigma_90'] = df['log_ret'].rolling(window=90).std() * np.sqrt(252)
    
    # 분석 대상 기간: 최근 1년 (252일)
    test_df = df.iloc[-252:].copy()
    vix_df = vix.reindex(test_df.index)

    results = []
    total_hits = 0

    for i in range(len(test_df)):
        current_date = test_df.index[i]
        prev_close = df['Close'].iloc[df.index.get_loc(current_date)-1]
        today_open = test_df['Open'].iloc[i]
        today_low = test_df['Low'].iloc[i]
        daily_vol = test_df['sigma_90'].iloc[i] / np.sqrt(252)
        vix_val = vix_df['Close'].iloc[i]
        
        # 1% 이상 갭하락 여부
        gap_ratio = (today_open - prev_close) / prev_close
        
        target_price = 0
        reason = ""

        # [우선순위 1] VIX 비상 모드
        if vix_val >= 35.0:
            target_price = today_open * (1 - daily_vol * 2.5)
            reason = "🔴 VIX 비상 (-2.5σ)"
        # [우선순위 2] 갭하락 대응 모드
        elif gap_ratio <= -0.01:
            target_price = today_open * (1 - daily_vol * 0.5)
            reason = "📉 갭하락 대응 (-0.5σ)"
        # [우선순위 3] 정상 변동성 모드
        else:
            target_price = prev_close * (1 - daily_vol * 1.0)
            reason = "🟢 정상 매수 (-1.0σ)"

        # 체결 여부 확인 (당일 저가가 타점에 닿았는가?)
        is_hit = today_low <= target_price
        
        if is_hit:
            total_hits += 1
            results.append({
                "Date": current_date.date(),
                "Reason": reason,
                "VIX": round(vix_val, 2),
                "Target": round(target_price, 2),
                "Low": round(today_low, 2)
            })

    # 결과 출력
    print(f"\n[ 최근 1년 SOXL 백테스트 결과 ]")
    print(f"총 거래일: {len(test_df)}일")
    print(f"전체 체결 횟수: {total_hits}회 (연간 쿼터 20회 대비 분석 필요)")
    print("-" * 50)
    
    report_df = pd.DataFrame(results)
    if not report_df.empty:
        summary = report_df['Reason'].value_counts()
        print(f"■ 유형별 체결 현황:\n{summary}")
        print("-" * 50)
        print("■ 최근 5건 체결 상세:")
        print(report_df.tail(5))
    
    return total_hits

if __name__ == "__main__":
    run_backtest()