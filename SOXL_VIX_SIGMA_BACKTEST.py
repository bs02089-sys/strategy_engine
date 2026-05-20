import numpy as np
import pandas as pd
import yfinance as yf
import vectorbt as vbt
import warnings

# 🛡️ 판다스 및 시스템 경고 메시지 방어
warnings.filterwarnings('ignore', category=FutureWarning)
warnings.filterwarnings('ignore', category=UserWarning)
pd.set_option('future.no_silent_downcasting', True)

def verify_long_term_hold_logic():
    # [BUG #6 FIX] 출력 메시지 파일명을 실제 파일명으로 교정
    print("📡 [SOXL_VIX_SIGMA_BACKTEST.py] 장기 보유(2028년 관점) 엔진 기동...")
    print("🎬 매수 후 매도 없이 최종 시점까지 홀딩했을 때의 진짜 성과를 측정합니다.\n")
    
    # 1. 최근 3개년 데이터 확보
    soxl = yf.download("SOXL", period="3y", interval="1d", progress=False)
    vix = yf.download("^VIX", period="3y", interval="1d", progress=False)
    
    if soxl.empty or vix.empty:
        print("❌ 야후 파이낸스 데이터 수집에 실패했습니다.")
        return

    if isinstance(soxl.columns, pd.MultiIndex): 
        soxl.columns = soxl.columns.droplevel(1)
    if isinstance(vix.columns, pd.MultiIndex): 
        vix.columns = vix.columns.droplevel(1)

    # [BUG #1 FIX] VIX 인덱스를 SOXL 기준으로 정렬 후 ffill → 날짜 불일치로 인한 샘플 누락 방지
    vix_close = vix['Close'].reindex(soxl.index).ffill()

    df = pd.DataFrame({
        'Open':  soxl['Open'].astype(float),
        'High':  soxl['High'].astype(float),
        'Low':   soxl['Low'].astype(float),
        'Close': soxl['Close'].astype(float),
        'VIX':   vix_close.astype(float),
    }).dropna()

    # 📐 90일 로그수익률 기반 일간 표준편차(Daily Sigma)
    df['Prev_Close'] = df['Close'].shift(1)
    df['Log_Ret'] = np.log(df['Close'] / df['Prev_Close'])
    df['Daily_Sigma'] = df['Log_Ret'].rolling(window=90).std(ddof=1)
    df = df.dropna().copy()

    # [BUG #2 FIX] reset_index로 정수 위치 기반 iloc 접근을 안전하게 통일
    df = df.reset_index(drop=True)

    # 🎯 VIX 눈금 및 황금 배수
    conditions = [
        (df['VIX'] >= 30.0),
        (df['VIX'] >= 20.0) & (df['VIX'] < 30.0),
        (df['VIX'] < 20.0)
    ]
    
    multipliers = [2.45, 1.95, 0.60]
    labels = ["🔴 극단적 공포 (VIX 30+ / -2.45σ)", "⚠️ 공포 장세 (VIX 20-30 / -1.95σ)", "✨ 평시 장세 (VIX 20미만 / -0.60σ)"]
    
    df['Target_Multiplier'] = np.select(conditions, multipliers, default=0.60)
    
    # 📐 로그 복리 공식 기반 정밀 타점 산출
    df['Target_Price'] = df['Open'] * np.exp(-df['Daily_Sigma'] * df['Target_Multiplier'])
    df['Is_Triggered'] = df['Low'] <= df['Target_Price']
    
    final_close_price = df['Close'].iloc[-1]
    
    print(f"📊 총 분석 거래일수 : {len(df)}일")
    print(f"📈 SOXL 최종 현재가 : ${final_close_price:.2f}")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    
    # 구간별 매수 후 '만기 무조건 보유' 수익률 정산
    for cond, multiplier, label in zip(conditions, multipliers, labels):
        sub_df = df[cond]
        sub_total = len(sub_df)
        if sub_total == 0:
            continue
            
        hit_count  = int(sub_df['Is_Triggered'].sum())  # [BUG #4 FIX] int 명시적 변환
        hit_ratio  = (hit_count / sub_total) * 100

        # [BUG #2 FIX] DatetimeIndex 혼용 방지 — df 전체 기준 정수 위치 인덱스로 통일
        triggered_indices = np.where((df['Is_Triggered'] & cond).values)[0]
        returns_to_present = []
        
        for idx in triggered_indices:
            # 진입 가격 계산
            buy_p = df['Open'].iloc[idx] * np.exp(-df['Daily_Sigma'].iloc[idx] * multiplier)
            # [BUG #3 FIX] buy_p == 0 시 ZeroDivisionError 방어
            if buy_p <= 0:
                continue
            # 매도 없이 '최종일 종가'로 수익률 계산
            ret = (final_close_price - buy_p) / buy_p
            returns_to_present.append(ret * 100)
                
        avg_ret_present = np.mean(returns_to_present) if returns_to_present else 0.0
        
        print(f"[{label}]")
        print(f"  └─ 장세 발생 : {sub_total}일 중 {hit_count}회 체결 성공")
        print(f"  └─ 타점 체결 확률 : {hit_ratio:.1f}%")
        print(f"  └─ 🛒 매수 후 '현재까지 보유 시' 평균 수익률 : {avg_ret_present:+.2f}%\n")
        
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    
    # 3. VectorBT 장기 누적 포트폴리오 성과 분석 (매도 시그널 차단)
    entries_series = df['Is_Triggered'].astype(bool)
    
    # 장기 투자를 위해 exits_series를 모두 False로 두어 중간 매도를 원천 차단합니다.
    exits_series = pd.Series(False, index=df.index).astype(bool)
    
    close_prices = df['Close'].astype(float)
    entry_prices = df['Target_Price'].astype(float)
    
    # accumulate=True 세팅으로 시그널이 올 때마다 추매(분할 매수) 형태로 누적합니다.
    pf = vbt.Portfolio.from_signals(
        close=close_prices,
        entries=entries_series,
        exits=exits_series,
        price=entry_prices,
        init_cash=10000,
        fees=0.001,
        freq='d',
        accumulate=True 
    )
    
    print("📦 [VectorBT 엔진 종합 성과지표 (2년 장기 투자 Hold 축)]")
    try:
        # [BUG #5 FIX] entries_series.sum() = Is_Triggered True 날 수 = 추매 집행 횟수 (동일 날 중복 없음)
        total_trades_count = int(entries_series.sum())
        print(f" • 총 기습 포격 집행(추매) 횟수 : {total_trades_count}회")
        print(f" • 포트폴리오 최종 누적 수익률 : {pf.total_return() * 100:+.2f}%")
        print(f" • 포트폴리오 최종 자산 가치 : ${pf.value().iloc[-1]:.2f}")
    except Exception as vbt_err:
        print(f" ⚠️ 지표 연산 중 예외 발생 : {vbt_err}")

if __name__ == "__main__":
    # Ensure the correct entrypoint is called. The previous conditional referenced
    # verify_live_open_base_logic which is not defined in this module and caused
    # a NameError in some execution contexts. Call the defined function directly.
    verify_long_term_hold_logic()