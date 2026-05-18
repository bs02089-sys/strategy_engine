import yfinance as yf
import pandas as pd
import numpy as np

def calculate_annual_sigma(closes, window: int = 90) -> float:
    """최종판 라이브 엔진과 100% 일치하는 연환산 시그마 함수"""
    arr = np.array(closes).flatten().astype(float)
    arr = arr[~np.isnan(arr)]
    window = min(window, len(arr) - 1)
    if window < 5: return 0.70
    
    log_ret = np.diff(np.log(arr[-(window + 1):]))
    log_ret = log_ret[np.isfinite(log_ret)]
    if len(log_ret) < 5: return 0.70
    
    daily_sigma = float(np.std(log_ret, ddof=1))
    annual_sigma = daily_sigma * np.sqrt(252)
    return min(annual_sigma, 2.0)  # 변동성 한계치 200% 상향 동기화

def run_backtest():
    ticker = "SOXL"
    
    # 데이터 수집 및 정렬
    df = yf.download(ticker, period="2y", interval="1d", auto_adjust=True, progress=False)
    vix = yf.download("^VIX", period="2y", interval="1d", auto_adjust=True, progress=False)
    
    if df.empty or vix.empty: return
    df, vix = df.sort_index(), vix.sort_index()

    if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.droplevel(1)
    if isinstance(vix.columns, pd.MultiIndex): vix.columns = vix.columns.droplevel(1)

    # 🚀 선생님의 실제 투자 시작 시점 고정 (2025년 5월 1일 ~ 현재)
    start_date = "2025-05-01"
    test_df = df.loc[start_date:].copy()
    vix_df = vix.reindex(test_df.index).ffill()

    results = []
    total_hits = 0

    for i in range(len(test_df)):
        current_date = test_df.index[i]
        
        # [과거 데이터 철저 제한] 어제까지의 종가 데이터만 추출 (미래 유출 방지)
        history_closes = df['Close'].loc[:current_date].iloc[:-1].values
        if len(history_closes) < 2: continue
        
        prev_close = float(history_closes[-1])
        today_open = float(test_df['Open'].iloc[i])   # 🚀 당일 아침 확정된 시가 반영!
        today_low = float(test_df['Low'].iloc[i])     # 장중에 움직인 최저가
        vix_val = float(vix_df['Close'].iloc[i])
        
        # 90일 연간 변동성 및 주간 변동성 산출
        annual_sig = calculate_annual_sigma(history_closes)
        weekly_sig = annual_sig / np.sqrt(52)
        
        # 🚀 [핵심 이식] 라이브 엔진의 장중 시가(Open) 기준 갭하락 보정 알고리즘
        # 당일 시가가 전일 종가보다 낮게 시작(갭하락)하면 그 갭 비율을 계산합니다.
        gap_ratio = (today_open - prev_close) / prev_close
        
        if gap_ratio < 0:
            # 📉 시가가 갭하락 출발하면 기존 '장기 적립 방어선'에 갭하락 폭을 더 깊게 반영하여 
            # 타점을 아래로 유연하게 보정합니다. (실전 엔진의 핵심 로직 메커니즘 반영)
            target_price = today_open * (1 - weekly_sig * 1.5)
            sub_msg = "📉 장중 시가 갭하락 보정 작동"
        else:
            # 🟢 정상 출발 시 기존 전일 종가 기준 매수선 유지
            target_price = prev_close * (1 - weekly_sig * 1.5)
            sub_msg = "🟢 정상 시가 기준 방어선"
            
        # 10% 하한 안전 가드
        target_price = max(target_price, prev_close * 0.10)
        
        # 체결 조건 감시 (장중 저가가 보정된 타점에 닿았는가?)
        is_hit = today_low <= target_price
        
        if is_hit:
            total_hits += 1
            results.append({
                "Date": current_date.date(),
                "VIX": round(vix_val, 2),
                "Annual_Sigma(%)": round(annual_sig * 100, 2),
                "Daily_Sigma(%)": round((annual_sig / np.sqrt(252)) * 100, 2),
                "Prev_Close": round(prev_close, 2),
                "Today_Open": round(today_open, 2),
                "Target": round(target_price, 2),
                "Low": round(today_low, 2),
                "Type": sub_msg
            })

    # 결과 리포트 출력
    print(f"\n[ ⚙️ SOXL 시가 보정 로직 탑재 동적 백테스트 결과 ]")
    print(f"시뮬레이션 기간: {start_date} ~ 현재 ({len(test_df)} 거래일)")
    print(f"동적 로직 총 체결 횟수: {total_hits}회")
    print("-" * 85)
    
    report_df = pd.DataFrame(results)
    if not report_df.empty:
        columns_to_show = ["Date", "VIX", "Annual_Sigma(%)", "Daily_Sigma(%)", "Today_Open", "Target", "Low"]
        print(report_df[columns_to_show].to_string(index=False))
        print("-" * 85)
    else:
        print("💡 시가 보정을 반영했음에도 거대 그물망 타점에는 도달하지 못했습니다.")
    
    return total_hits

if __name__ == "__main__":
    run_backtest()