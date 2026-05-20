import numpy as np
import pandas as pd
import yfinance as yf
import vectorbt as vbt
import warnings

# 🛡️ 판다스 및 시스템 경고 메시지 거울처럼 깨끗하게 제어
warnings.filterwarnings('ignore', category=FutureWarning)
warnings.filterwarnings('ignore', category=UserWarning)
pd.set_option('future.no_silent_downcasting', True)

def verify_live_open_base_logic():
    print("📡 [vix_sigma_open_guard.py] 엔진 기동 - SOXL 및 VIX 데이터 수집 중...")
    
    # 1. 최근 3개년 시장 사이클 데이터 확보
    soxl = yf.download("SOXL", period="3y", interval="1d", progress=False)
    vix = yf.download("^VIX", period="3y", interval="1d", progress=False)
    
    if soxl.empty or vix.empty:
        print("❌ 야후 파이낸스 데이터 수집에 실패했습니다.")
        return

    # yfinance MultiIndex 컬럼 방어 구조 정렬
    if isinstance(soxl.columns, pd.MultiIndex): 
        soxl.columns = soxl.columns.droplevel(1)
    if isinstance(vix.columns, pd.MultiIndex): 
        vix.columns = vix.columns.droplevel(1)

    # 일체형 타점 연산을 위한 데이터프레임 빌드 (강제 float 타입 일치)
    df = pd.DataFrame({
        'Open': soxl['Open'].astype(float),
        'High': soxl['High'].astype(float),
        'Low': soxl['Low'].astype(float),
        'Close': soxl['Close'].astype(float),
        'VIX': vix['Close'].astype(float)
    }).dropna()

    # 📐 [선장님 오리지널 엔진 축 복원] 90일 로그수익률 기반 일간 시그마 추출
    df['Prev_Close'] = df['Close'].shift(1)
    df['Log_Ret'] = np.log(df['Close'] / df['Prev_Close'])
    
    # 순수 일간 평균 변동성(Daily_Sigma)을 메인 엔진의 유일한 핵심 축으로 선택
    df['Daily_Sigma'] = df['Log_Ret'].rolling(window=90).std()
    df = df.dropna().copy()

    # 🎯 [1순위 특명] VIX 레이어 분기 멀티플라이어 눈금 정밀 교정
    conditions = [
        (df['VIX'] >= 35.0),
        (df['VIX'] >= 25.0) & (df['VIX'] < 35.0),
        (df['VIX'] < 25.0)
    ]
    # 실증 데이터 기반 배수 세팅
    multipliers = [1.47, 0.74, 0.60]
    labels = ["🔴 극단적 공포 (VIX 35+ / -1.47σ)", "⚠️ 공포 장세 (VIX 25-35 / -0.74σ)", "✨ 평시 장세 (VIX 25미만 / -0.60σ)"]
    
    # 구간별 멀티플라이어 시리즈 생성
    df['Target_Multiplier'] = np.select(conditions, multipliers, default=0.60)
    
    # 📐 [주간 변동성 완전 삭제 ➔ 일간 평균 변동성 축 np.exp 공식 동기화]
    # 선장님의 의도대로 주간 변동성을 제거하고, 오직 일간 평균 변동성(Daily_Sigma)으로 영점 일치
    df['Target_Price'] = df['Open'] * np.exp(-df['Daily_Sigma'] * df['Target_Multiplier'])
    
    # 당일 실시간 최저가(Low)가 계산된 기하학적 그물망 타점 이하로 내려갔는가 판별
    df['Is_Triggered'] = df['Low'] <= df['Target_Price']
    
    print(f"📊 총 분석 거래일수 : {len(df)}일")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    
    # 구간별 개별 확률 분포도 출력 및 5일 보유 후 청산 수익률 검증
    for cond, multiplier, label in zip(conditions, multipliers, labels):
        sub_df = df[cond]
        sub_total = len(sub_df)
        if sub_total == 0:
            continue
            
        hit_count = sub_df['Is_Triggered'].sum()
        hit_ratio = (hit_count / sub_total) * 100
        
        # 기습 포격 진입 시점 인덱스 추출
        triggered_indices = np.where(df['Is_Triggered'] & cond)[0]
        returns_5d = []
        
        for idx in triggered_indices:
            if idx + 5 < len(df):
                buy_p = df['Target_Price'].iloc[idx]
                sell_p = df['Close'].iloc[idx + 5]
                returns_5d.append((sell_p - buy_p) / buy_p)
                
        avg_ret_5d = np.mean(returns_5d) * 100 if returns_5d else 0.0
        
        print(f"[{label}]")
        print(f"  └─ 장세 발생 : {sub_total}일 중 {hit_count}회 체결 성공")
        print(f"  └─ 체결 확률(분포도) : {hit_ratio:.1f}%")
        print(f"  └─ 진입 후 5일 청산 평균 수익률 : {avg_ret_5d:+.2f}%\n")
        
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    
    # 3. VectorBT 포트폴리오 성과 분석
    entries_series = df['Is_Triggered'].astype(bool)
    exits_series = entries_series.shift(5).fillna(False).astype(bool)
    
    close_prices = df['Close'].astype(float)
    entry_prices = df['Target_Price'].astype(float)
    
    pf = vbt.Portfolio.from_signals(
        close=close_prices,
        entries=entries_series,
        exits=exits_series,
        price=entry_prices,
        init_cash=10000,
        fees=0.001,
        freq='d'
    )
    
    print("📦 [VectorBT 엔진 종합 성과지표 (일간 변동성 통일 축)]")
    
    try:
        total_trades_count = pf.trades.count()
        win_rate_pct = pf.trades.win_rate * 100
        profit_factor_val = pf.trades.profit_factor
        
        print(f" • 총 집행 거래 횟수 : {total_trades_count}회")
        print(f" • 승률 (Win Rate)  : {win_rate_pct:.2f}%")
        print(f" • 프로핏 팩터 (PF) : {profit_factor_val:.2f}")
        print(f" • 최종 포트폴리오 누적 수익률 : {pf.total_return() * 100:+.2f}%")
    except Exception as vbt_err:
        print(f" ⚠️ VectorBT 세부 지표 연산 중 예외 발생 : {vbt_err}")
        print(f" • 단순 누적 수익률로 대체 출력 : {pf.total_return() * 100:+.2f}%")

if __name__ == "__main__":
    verify_live_open_base_logic()