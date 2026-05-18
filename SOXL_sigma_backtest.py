import yfinance as yf
import pandas as pd
import numpy as np

def calculate_annual_sigma(closes, window: int = 90) -> float:
    """오전 최종 확정판 엔진과 100% 일치하는 90일 연환산 시그마 함수"""
    arr = np.array(closes).flatten().astype(float)
    arr = arr[~np.isnan(arr)]
    window = min(window, len(arr) - 1)
    if window < 5:
        return 0.70
    
    # 로그 수익률 변환 후 표준편차 계산
    log_ret = np.diff(np.log(arr[-(window + 1):]))
    log_ret = log_ret[np.isfinite(log_ret)]
    if len(log_ret) < 5:
        return 0.70
    
    daily_sigma = float(np.std(log_ret, ddof=1))
    annual_sigma = daily_sigma * np.sqrt(252)
    
    # 🚀 핵심 동기화: 레버리지 변동성 캡을 라이브 엔진과 동일하게 2.0(200%)으로 상향
    return min(annual_sigma, 2.0)

def run_backtest():
    ticker = "SOXL"
    # 90일 시그마의 안정적인 계산 및 최근 1년 테스트를 위해 2년치 데이터 수집
    df = yf.download(ticker, period="2y", auto_adjust=True, progress=False)
    vix = yf.download("^VIX", period="2y", auto_adjust=True, progress=False)
    
    if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.droplevel(1)
    if isinstance(vix.columns, pd.MultiIndex): vix.columns = vix.columns.droplevel(1)

    # 분석 대상 기간: 최근 1년 (약 252 영업일)
    test_df = df.iloc[-252:].copy()
    vix_df = vix.reindex(test_df.index)

    results = []
    total_hits = 0

    for i in range(len(test_df)):
        current_date = test_df.index[i]
        
        # 현재 날짜 기준 과거 데이터 전체 슬라이싱 (당일 데이터 유출 방지 및 누적 데이터 전달)
        history_closes = df['Close'].loc[:current_date].values
        # 시그마 계산 시점은 '당일 시작 전'이므로 오늘 종가를 제외한 전일까지의 데이터로 계산
        if len(history_closes) > 1:
            history_closes = history_closes[:-1]
            
        prev_close = history_closes[-1]
        today_open = test_df['Open'].iloc[i]
        today_low = test_df['Low'].iloc[i]
        vix_val = vix_df['Close'].iloc[i]
        
        # 라이브 엔진 규칙에 따른 연간 변동성(σ) 계산
        annual_sig = calculate_annual_sigma(history_closes)
        
        # 🚀 라이브 엔진의 LONG 모드 '장기 적립 방어선' 공식 완벽 이식
        # 주간(Weekly) 변동성 환산 (루트 52)
        weekly_sig = annual_sig / np.sqrt(52)
        
        # 주간 변동성의 1.5배 하락을 타점으로 설정
        target_price = prev_close * (1 - weekly_sig * 1.5)
        # 하한 가드 장치 (최소 10%선 유지)
        target_price = max(target_price, prev_close * 0.10)
        
        reason = "📈 장기 적립 방어선"

        # 체결 여부 확인 (당일 주가의 최저가가 매수 예정가 이하로 내려갔는가?)
        is_hit = today_low <= target_price
        
        if is_hit:
            total_hits += 1
            results.append({
                "Date": current_date.date(),
                "Reason": reason,
                "VIX": round(vix_val, 2),
                "Annual_Sigma(%)": round(annual_sig * 100, 2),
                "Daily_Sigma(%)": round((annual_sig / np.sqrt(252)) * 100, 2), # 일간 변동성 역산 기록
                "Target": round(target_price, 2),
                "Low": round(today_low, 2)
            })

    # 결과 리포트 출력
    print(f"\n[ 최근 1년 SOXL 매매엔진 동기화 백테스트 결과 ]")
    print(f"총 거래일: {len(test_df)}일")
    print(f"전체 체결 횟수: {total_hits}회 (연간 자금 소진율 쿼터와 비교 분석용)")
    print("-" * 65)
    
    report_df = pd.DataFrame(results)
    if not report_df.empty:
        print("■ 최근 5건 체결 상세 (최신판 로직 기준):")
        columns_to_show = ["Date", "VIX", "Annual_Sigma(%)", "Daily_Sigma(%)", "Target", "Low"]
        print(report_df[columns_to_show].tail(5).to_string(index=False))
        print("-" * 65)
    else:
        print("💡 해당 기간 동안 장기 적립 방어선 타점에 도달한 내역이 없습니다.")
    
    return total_hits

if __name__ == "__main__":
    run_backtest()