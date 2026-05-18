import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime

def calculate_annual_sigma(closes, window: int = 90) -> float:
    """오전 최종 확정판 라이브 엔진과 100% 일치하는 연환산 시그마 함수"""
    arr = np.array(closes).flatten().astype(float)
    arr = arr[~np.isnan(arr)]
    window = min(window, len(arr) - 1)
    if window < 5:
        return 0.70
    
    log_ret = np.diff(np.log(arr[-(window + 1):]))
    log_ret = log_ret[np.isfinite(log_ret)]
    if len(log_ret) < 5:
        return 0.70
    
    daily_sigma = float(np.std(log_ret, ddof=1))
    annual_sigma = daily_sigma * np.sqrt(252)
    return min(annual_sigma, 2.0)  # 3배 레버리지 한계치 200% 상향 반영

def run_backtest():
    ticker = "SOXL"
    
    # 🚀 수정 1: 데이터 왜곡을 막기 위해 개별 다운로드 및 명확한 날짜 정렬(Sort) 적용
    df = yf.download(ticker, period="2y", interval="1d", auto_adjust=True, progress=False)
    vix = yf.download("^VIX", period="2y", interval="1d", auto_adjust=True, progress=False)
    
    if df.empty or vix.empty:
        print("❌ 데이터를 가져오지 못했습니다.")
        return
        
    df = df.sort_index()
    vix = vix.sort_index()

    # 단일 종목 다운로드 시 레벨 컬럼 깨기 방어
    if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.droplevel(1)
    if isinstance(vix.columns, pd.MultiIndex): vix.columns = vix.columns.droplevel(1)

    # 🚀 수정 2: 분석 대상 기간을 '최근 1년'이 아닌, 선생님의 실제 투자 시작일인 
    # '2025년 5월 1일'부터 '현재(2026년 5월)'까지로 고정하여 현실성을 확보합니다.
    start_date = "2025-05-01"
    test_df = df.loc[start_date:].copy()
    vix_df = vix.reindex(test_df.index).ffill()

    results = []
    total_hits = 0

    for i in range(len(test_df)):
        current_date = test_df.index[i]
        
        # 당일 데이터 유출 방지 (어제까지의 누적 데이터만 슬라이싱)
        history_df = df.loc[:current_date]
        if len(history_df) < 2:
            continue
        history_closes = history_df['Close'].iloc[:-1].values
        
        # 🚀 핵심 수정 3: 라이브 엔진의 장전/장중 연산 메커니즘 동기화
        # 라이브 엔진은 장중(is_open=True)일 때 당일 Open 가격을 base로 삼고, prev_close는 어제 종가를 씁니다.
        prev_close = float(history_closes[-1])
        today_open = float(test_df['Open'].iloc[i])
        today_low = float(test_df['Low'].iloc[i])
        vix_val = float(vix_df['Close'].iloc[i])
        
        # 90일 연간 변동성 및 일간 변동성 계산
        annual_sig = calculate_annual_sigma(history_closes)
        daily_sig_pct = (annual_sig / np.sqrt(252)) * 100
        
        # 주간 변동성 기반 타점 연산
        weekly_sig = annual_sig / np.sqrt(52)
        target_price = prev_close * (1 - weekly_sig * 1.5)
        target_price = max(target_price, prev_close * 0.10)  # 10% 하한 가드
        
        # 🚀 핵심 수정 4: 체결 조건 보정
        # 라이브 엔진 작동 시, 장중 실시간 주가가 매수 예정가 이하로 떨어지면 체결됩니다.
        # 즉, 당일의 최저가(Low)가 계산된 타점(target_price)보다 낮거나 같으면 무조건 체결 성공입니다.
        is_hit = today_low <= target_price
        
        if is_hit:
            total_hits += 1
            results.append({
                "Date": current_date.date(),
                "VIX": round(vix_val, 2),
                "Annual_Sigma(%)": round(annual_sig * 100, 2),
                "Daily_Sigma(%)": round(daily_sig_pct, 2),
                "Prev_Close": round(prev_close, 2),
                "Target": round(target_price, 2),
                "Low": round(today_low, 2)
            })

    # 결과 리포트 출력
    print(f"\n[ 🎯 SOXL 실전 매매엔진 완전 동기화 백테스트 결과 ]")
    print(f"시뮬레이션 기간: {start_date} ~ 현재 ({len(test_df)} 거래일)")
    print(f"총 체결 횟수: {total_hits}회 (선생님의 실제 집행 횟수 '4회'와 비교 검증용)")
    print("-" * 75)
    
    report_df = pd.DataFrame(results)
    if not report_df.empty:
        print("■ 전체 체결 및 타점 도달 상세 내역:")
        columns_to_show = ["Date", "VIX", "Annual_Sigma(%)", "Daily_Sigma(%)", "Prev_Close", "Target", "Low"]
        print(report_df[columns_to_show].to_string(index=False))
        print("-" * 75)
    else:
        print("💡 타점에 도달한 내역이 없습니다. 로직 조율이 필요합니다.")
    
    return total_hits

if __name__ == "__main__":
    run_backtest()