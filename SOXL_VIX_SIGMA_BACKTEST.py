import numpy as np
import pandas as pd
import yfinance as yf
import vectorbt as vbt
import warnings
from itertools import product

warnings.filterwarnings('ignore', category=FutureWarning)
warnings.filterwarnings('ignore', category=UserWarning)

def verify_long_term_hold_logic():
    print("📡 [SOXL_VIX_SIGMA_BACKTEST.py] 장기 보유 전략 백테스트")
    print("🎯 1년 분할매수(24회 목표) + 1년 홀딩 전략 최적화\n")

    # ==================== 데이터 로드 ====================
    soxl = yf.download("SOXL", period="4y", interval="1d", progress=False, auto_adjust=True)
    vix = yf.download("^VIX", period="4y", interval="1d", progress=False, auto_adjust=True)

    if soxl.empty or vix.empty:
        print("❌ 데이터 다운로드 실패")
        return

    if isinstance(soxl.columns, pd.MultiIndex):
        soxl = soxl.droplevel(1, axis=1)
    if isinstance(vix.columns, pd.MultiIndex):
        vix = vix.droplevel(1, axis=1)

    vix_close = vix['Close'].reindex(soxl.index).ffill()

    df = pd.DataFrame({
        'Open': soxl['Open'].values,
        'High': soxl['High'].values,
        'Low': soxl['Low'].values,
        'Close': soxl['Close'].values,
        'VIX': vix_close.values
    }, index=soxl.index).dropna()

    df['Log_Ret'] = np.log(df['Close'] / df['Close'].shift(1))
    df['Daily_Sigma'] = df['Log_Ret'].rolling(window=90, min_periods=60).std(ddof=1)
    df['Prev_Close'] = df['Close'].shift(1)
    # 갭 비율: 전일 종가 대비 당일 시가 (음수 = 갭 하락)
    df['Gap_Ratio'] = np.where(
        df['Prev_Close'] != 0,
        (df['Open'] - df['Prev_Close']) / df['Prev_Close'],
        0.0
    )
    df = df.dropna().copy()  # DatetimeIndex 유지 (reset_index 사용 안 함)

    final_price = df['Close'].iloc[-1]

    print(f"📊 분석 기간 : {df.index[0].date()} ~ {df.index[-1].date()} ({len(df)} 거래일)")
    print(f"📈 SOXL 최종 종가 : ${final_price:.2f}\n")

    # ====================== 갭 하락 구간별 계단식 배수 보정 ======================
    # sigma_position_manager.py 실전 로직과 완전 동기화
    #   0%  ~ -3%  : 배수 유지
    #  -3%  ~ -5%  : 0.45
    #  -5%  ~ -7%  : 0.25
    #  -7%  ~ -10% : 0.10
    #  -10% 초과   : 0.0
    def apply_gap_correction(base_mult: float, gap_ratio: float) -> float:
        if gap_ratio >= -0.03: return base_mult
        elif gap_ratio >= -0.05: return 0.45
        elif gap_ratio >= -0.07: return 0.25
        elif gap_ratio >= -0.10: return 0.10
        else: return 0.0

    # ====================== 안전 범위 정의 (매년 업데이트해도 과도하게 변하지 않도록) ======================
    SAFETY_BOUNDS = {
        "MULT_NORMAL":  (0.65, 0.85),
        "MULT_FEAR":    (1.95, 2.45),
        "MULT_EXTREME": (2.40, 2.75)
    }

    # ====================== 백테스트 함수 ======================
    def run_backtest(normal, fear, extreme, name):
        def get_mult(v):
            if v >= 30: return extreme
            elif v >= 20: return fear
            else: return normal
        
        df_temp = df.copy()
        df_temp['Multiplier'] = df_temp['VIX'].apply(get_mult)
        # 갭 하락 보정 배수 적용 (실전 로직 동기화)
        df_temp['Adj_Multiplier'] = df_temp.apply(
            lambda r: apply_gap_correction(r['Multiplier'], r['Gap_Ratio']), axis=1
        )
        df_temp['Target_Price'] = df_temp['Open'] * np.exp(-df_temp['Daily_Sigma'] * df_temp['Adj_Multiplier'])
        df_temp['Triggered'] = df_temp['Low'] <= df_temp['Target_Price']

        entries = df_temp['Triggered']
        pf = vbt.Portfolio.from_signals(
            close=df_temp['Close'],
            entries=entries,
            exits=pd.Series(False, index=df_temp.index),
            price=df_temp['Target_Price'],
            init_cash=10000,
            fees=0.0015,
            freq='1D',
            accumulate=True,
            allow_partial=True
        )

        print(f"[{name}]")
        print(f"   평시: {normal:.2f} | 공포: {fear:.2f} | 극단: {extreme:.2f}")
        print(f"   총 매수 횟수 : {int(entries.sum())}회")
        print(f"   최종 수익률  : {pf.total_return()*100:+.2f}%")
        print(f"   최대 드로다운 : {pf.max_drawdown()*100:.2f}%")
        print(f"   Calmar Ratio : {pf.calmar_ratio():.3f}\n")

    run_backtest(0.75, 2.20, 2.60, "현재 추천 설정")

    # ====================== 안전 범위 내 최적화 ======================
    print("🔍 안전 범위 내에서 최적 배수 탐색 중...\n")

    normal_range = np.arange(SAFETY_BOUNDS["MULT_NORMAL"][0], SAFETY_BOUNDS["MULT_NORMAL"][1] + 0.01, 0.05)
    fear_range   = np.arange(SAFETY_BOUNDS["MULT_FEAR"][0], SAFETY_BOUNDS["MULT_FEAR"][1] + 0.01, 0.05)
    extreme_range = np.arange(SAFETY_BOUNDS["MULT_EXTREME"][0], SAFETY_BOUNDS["MULT_EXTREME"][1] + 0.01, 0.05)

    best_score = -np.inf
    best_params = None

    for normal, fear, extreme in product(normal_range, fear_range, extreme_range):
        def get_mult(v):
            if v >= 30: return extreme
            elif v >= 20: return fear
            else: return normal

        df_temp = df.copy()
        df_temp['Multiplier'] = df_temp['VIX'].apply(get_mult)
        # 갭 하락 보정 배수 적용 (실전 로직 동기화)
        df_temp['Adj_Multiplier'] = df_temp.apply(
            lambda r: apply_gap_correction(r['Multiplier'], r['Gap_Ratio']), axis=1
        )
        df_temp['Target_Price'] = df_temp['Open'] * np.exp(-df_temp['Daily_Sigma'] * df_temp['Adj_Multiplier'])
        df_temp['Triggered'] = df_temp['Low'] <= df_temp['Target_Price']

        trade_count = int(df_temp['Triggered'].sum())
        if trade_count < 170 or trade_count > 240:   # 조금 더 여유롭게 설정
            continue

        pf = vbt.Portfolio.from_signals(
            close=df_temp['Close'],
            entries=df_temp['Triggered'],
            exits=pd.Series(False, index=df_temp.index),
            price=df_temp['Target_Price'],
            init_cash=10000,
            fees=0.0015,
            freq='1D',
            accumulate=True,
            allow_partial=True
        )

        calmar = pf.calmar_ratio() or 0
        score = calmar * 150 + (trade_count / 2.8)

        if score > best_score:
            best_score = score
            best_params = (normal, fear, extreme, trade_count, calmar, pf.total_return()*100, pf)

    if best_params:
        normal, fear, extreme, trades, calmar, ret, pf = best_params
        print("🏆 안전 범위 내 최적 배수 조합")
        print(f"   평시 : {normal:.2f}x | 공포 : {fear:.2f}x | 극단 : {extreme:.2f}x")
        print(f"   총 매수 횟수 : {trades}회 | Calmar : {calmar:.3f} | 수익률 : {ret:+.2f}%\n")

        print("="*65)
        print("💡 config.json에 바로 사용하세요:")
        print(f'"MULT_NORMAL": {normal:.2f},')
        print(f'"MULT_FEAR": {fear:.2f},')
        print(f'"MULT_EXTREME": {extreme:.2f}')
        print("="*65)

    print("\n✅ 백테스트 완료 - 매년 실행해도 안전한 범위 내에서 추천됩니다.")


if __name__ == "__main__":
    verify_long_term_hold_logic()