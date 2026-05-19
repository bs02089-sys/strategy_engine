import numpy as np
import pandas as pd
import yfinance as yf
import vectorbt as vbt
import warnings

# 🛡️ 시스템 및 판다스 경고 노이즈 완벽 차단
warnings.filterwarnings('ignore', category=FutureWarning)
warnings.filterwarnings('ignore', category=UserWarning)
pd.set_option('future.no_silent_downcasting', True)

def verify_live_open_base_logic():
    print("📡 [vix_sigma_open_guard.py] 통합 엔진 기동 - 데이터 수집 및 하락 갭 통계 연산 중...")
    
    # 1. 최근 3개년 시장 사이클 데이터 확보
    soxl = yf.download("SOXL", period="3y", interval="1d", progress=False)
    vix = yf.download("^VIX", period="3y", interval="1d", progress=False)
    
    if soxl.empty or vix.empty:
        print("❌ 야후 파이낸스 데이터 수집에 실패했습니다.")
        return

    # yfinance MultiIndex 컬럼 방어
    if isinstance(soxl.columns, pd.MultiIndex): 
        soxl.columns = soxl.columns.droplevel(1)
    if isinstance(vix.columns, pd.MultiIndex): 
        vix.columns = vix.columns.droplevel(1)

    # 일체형 데이터프레임 빌드 (강제 float 타입 일치)
    df = pd.DataFrame({
        'Open': soxl['Open'].astype(float),
        'High': soxl['High'].astype(float),
        'Low': soxl['Low'].astype(float),
        'Close': soxl['Close'].astype(float),
        'VIX': vix['Close'].astype(float)
    }).dropna()

    # 90일 로그수익률 기반 일간 시그마(Daily σ) 역산
    df['Prev_Close'] = df['Close'].shift(1).astype(float)
    df['Log_Ret'] = np.log(df['Close'] / df['Prev_Close']).astype(float)
    df['Daily_Sigma'] = df['Log_Ret'].rolling(window=90).std().astype(float)
    
    # 결측치 제거
    df = df.replace([np.inf, -np.inf], np.nan).dropna().copy()
    
    # 🔥 [하락 갭 통계 핵심 수식] 아침 시가 갭이 "몇 시그마(σ)" 크기인지 역산
    # 공식: (오늘 시가 - 전일 종가) / (전일 종가 * 일간 시그마)
    df['Gap_Ratio'] = (df['Open'] - df['Prev_Close']) / df['Prev_Close']
    df['Gap_In_Sigma'] = df['Gap_Ratio'] / df['Daily_Sigma']
    
    # 2. 실전 엔진과 100% 일치하는 VIX 레이어별 조건 분기
    conditions = [
        (df['VIX'] >= 35.0),
        (df['VIX'] >= 25.0) & (df['VIX'] < 35.0),
        (df['VIX'] < 25.0)
    ]
    multipliers = [-1.5, -1.0, -0.6]
    labels = [
        "극단적 공포 (VIX 35+ / -1.5σ)", 
        "공포 장세 (VIX 25-35 / -1.0σ)", 
        "평시 장세 (VIX 25미만 / -0.6σ)"
    ]
    
    # 단 하나의 기준인 '오늘 시가(Open)' 영점 단일화 통일
    df['Target_Multiplier'] = np.select(conditions, multipliers, default=-0.6)
    df['Target_Price'] = (df['Open'] * (1 + df['Target_Multiplier'] * df['Daily_Sigma'])).astype(float)
    
    # 당일 저가가 시가 기준 변동성 그물망을 터치(체결)했는지 판별
    df['Is_Triggered'] = df['Low'] <= df['Target_Price']
    
    print("\n📊 [오늘 시가 기준 통일 - 타점 체결률 및 반등 효율 실증 리포트]")
    print("=" * 85)
    print(f"{'장세 분류 (VIX 레이어)':^28} | {'발생 일수':^6} | {'체결 횟수':^6} | {'체결 확률':^8} | {'후속 5일 평균 수익률':^12}")
    print("=" * 85)

    for cond, multiplier, label in zip(conditions, multipliers, labels):
        sub_df = df[cond]
        sub_total = len(sub_df)
        if sub_total == 0:
            print(f"{label:<25} | {sub_total:^8} | {'-':^8} | {'-':^8} | {'-':^14}")
            continue
            
        hit_count = sub_df['Is_Triggered'].sum()
        hit_ratio = (hit_count / sub_total) * 100
        
        triggered_indices = np.where(df['Is_Triggered'] & cond)[0]
        returns_5d = []
        
        for idx in triggered_indices:
            if idx + 5 < len(df):
                buy_p = df['Target_Price'].iloc[idx]
                sell_p = df['Close'].iloc[idx + 5]
                returns_5d.append((sell_p - buy_p) / buy_p)
                
        avg_ret_5d = np.mean(returns_5d) * 100 if returns_5d else 0.0
        ret_str = f"{avg_ret_5d:+.2f}%" if returns_5d else "0.00%"
        
        print(f"{label:<25} | {sub_total:^8} | {hit_count:^8} | {hit_ratio:^7.1f}% | {ret_str:^16}")
    print("=" * 85)


    # 📈 [선장님 추가 특명: 아침 하락 갭(시가 갭) 시그마 분포 리포트]
    # 아침에 '하락 갭(전일 종가 대비 시가 하락)'이 발생한 날만 추려 장세별 깊이 측정
    df_gap_down = df[df['Gap_Ratio'] < 0].copy()
    gap_conditions = [
        (df_gap_down['VIX'] >= 35.0),
        (df_gap_down['VIX'] >= 25.0) & (df_gap_down['VIX'] < 35.0),
        (df_gap_down['VIX'] < 25.0)
    ]
    
    print("\n🔍 [VIX 레이어별 실제 아침 하락 갭 시그마(σ) 분포 리포트]")
    print("=" * 90)
    print(f"{'장세 분류 (VIX 레벨)':<22} | {'하락갭 일수':^10} | {'평균 갭 비율':^12} | {'평균 갭 크기(시그마)':^16} | {'최대 폭락 갭(시그마)':^16}")
    print("=" * 90)

    for cond, label in zip(gap_conditions, labels):
        sub_gap = df_gap_down[cond]
        gap_days = len(sub_gap)
        
        if gap_days == 0:
            print(f"{label:<25} | {'0일':^12} | {'-':^14} | {'-':^18} | {'-':^18}")
            continue
            
        avg_gap_pct = sub_gap['Gap_Ratio'].mean() * 100
        avg_gap_sigma = sub_gap['Gap_In_Sigma'].mean()
        max_panic_sigma = sub_gap['Gap_In_Sigma'].min() # 하락 갭이므로 최솟값이 가장 깊은 폭락 갭
        
        # 가독성을 위해 레이어 명칭 요약
        short_label = label.split(" (")[0]
        print(f" {short_label:<21} | {gap_days:^11} | {avg_gap_pct:>10.2f}% | {avg_gap_sigma:>13.2f}σ | {max_panic_sigma:>14.2f}σ")
    print("=" * 90)


    # 3. VectorBT 포트폴리오 성과 분석 (Numba 가드를 위해 타입 완전 강제 정의)
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
    
    print("\n📦 [VectorBT 엔진 종합 성과지표 (시가 기준 통일 일체형)]")
    try:
        total_trades_count = pf.trades.count()
        win_rate_pct = pf.trades.win_rate * 100
        profit_factor_val = pf.trades.profit_factor
        
        print(f" • 총 집행 거래 횟수 : {total_trades_count}회")
        print(f" • 승률 (Win Rate)    : {win_rate_pct:.1f}%")
        print(f" • 승패 비율 (Profit Factor) : {profit_factor_val:.2f}")
    except Exception:
        stats = pf.stats()
        print(f" • 총 집행 거래 횟수 : {int(stats.get('Total Trades', 0))}회")
        print(f" • 승률 (Win Rate)    : {float(stats.get('Win Rate [%]', 0.0)):.1f}%")
        print(f" • 승패 비율 (Profit Factor) : {float(stats.get('Profit Factor', 0.0)):.2f}")

if __name__ == "__main__":
    verify_live_open_base_logic()