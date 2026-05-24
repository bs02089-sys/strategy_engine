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
    df = df.dropna().copy()

    final_price = df['Close'].iloc[-1]
    print(f"📊 분석 기간 : {df.index[0].date()} ~ {df.index[-1].date()} ({len(df)} 거래일)")
    print(f"📈 SOXL 최종 종가 : ${final_price:.2f}\n")

    # ====================== 안전 범위 정의 및 조건 설정 ======================
    # config.json 기준값(MULT_EXTREME: 2.40)을 포함하도록 탐색 범위 설정
    SAFETY_BOUNDS = {
        "MULT_NORMAL":  (0.65, 0.85),
        "MULT_FEAR":    (1.95, 2.45),
        "MULT_EXTREME": (2.40, 2.75)
    }

    # 갭 하락 구간별 계단식 배수 보정 매핑 (Vectorized 연산을 위해 딕셔너리 대신 조건 연산 유지)
    def apply_gap_correction(base_mult: float, gap_ratio: float) -> float:
        if gap_ratio >= -0.03: return base_mult
        elif gap_ratio >= -0.05: return 0.45
        elif gap_ratio >= -0.07: return 0.25
        elif gap_ratio >= -0.10: return 0.10
        else: return 0.0

    # ====================== 최적화 핵심 연산 함수 ======================
    def evaluate_parameters(normal, fear, extreme):
        """특정 파라미터 조합의 매수 횟수와 시그널 배열을 반환합니다."""
        # VIX 조건 설정 최적화 (apply 대신 np.select로 속도 향상 가능하나 안전성 위해 로직 유지)
        multipliers = np.where(df['VIX'] >= 30, extreme, np.where(df['VIX'] >= 20, fear, normal))
        
        # 갭 보정 적용
        # [BUG #1 FIX] np.array 변환으로 numpy 연산 속도 최적화
        adj_multipliers = np.array([apply_gap_correction(m, g) for m, g in zip(multipliers, df['Gap_Ratio'])])
        
        target_prices = df['Open'].values * np.exp(-df['Daily_Sigma'].values * adj_multipliers)
        triggered = df['Low'].values <= target_prices
        
        return triggered, target_prices

    # ====================== 현재 설정값 기준 성과 먼저 출력 ======================
    # [BUG #3 FIX] 최적화 결과와 비교할 현재 config.json 설정 기준 성과 출력
    CURRENT_NORMAL, CURRENT_FEAR, CURRENT_EXTREME = 0.85, 1.95, 2.40
    cur_triggered, cur_target_prices = evaluate_parameters(CURRENT_NORMAL, CURRENT_FEAR, CURRENT_EXTREME)
    cur_pf = vbt.Portfolio.from_signals(
        close=df['Close'],
        entries=pd.Series(cur_triggered, index=df.index),
        exits=pd.Series(False, index=df.index),
        price=pd.Series(cur_target_prices, index=df.index),
        init_cash=10000,
        fees=0.0015,
        freq='1D',
        accumulate=True,
        allow_partial=True
    )
    cur_calmar_raw = cur_pf.calmar_ratio()
    cur_calmar = 0.0 if (cur_calmar_raw is None or not np.isfinite(cur_calmar_raw)) else float(cur_calmar_raw)
    cur_ret_raw = cur_pf.total_return()
    cur_ret = float(cur_ret_raw * 100) if np.isfinite(cur_ret_raw) else 0.0
    print("[현재 config.json 설정값]")
    print(f"   평시: {CURRENT_NORMAL:.2f} | 공포: {CURRENT_FEAR:.2f} | 극단: {CURRENT_EXTREME:.2f}")
    print(f"   총 매수 횟수 : {int(cur_triggered.sum())}회")
    print(f"   최종 수익률  : {cur_ret:+.2f}%")
    print(f"   최대 드로다운 : {cur_pf.max_drawdown()*100:.2f}%")
    print(f"   Calmar Ratio : {cur_calmar:.3f}\n")

    # ====================== 안전 범위 내 최적화 탐색 ======================
    print("🔍 안전 범위 내에서 최적 배수 탐색 중...\n")

    normal_range = np.arange(SAFETY_BOUNDS["MULT_NORMAL"][0], SAFETY_BOUNDS["MULT_NORMAL"][1] + 0.01, 0.05)
    fear_range   = np.arange(SAFETY_BOUNDS["MULT_FEAR"][0], SAFETY_BOUNDS["MULT_FEAR"][1] + 0.01, 0.05)
    extreme_range = np.arange(SAFETY_BOUNDS["MULT_EXTREME"][0], SAFETY_BOUNDS["MULT_EXTREME"][1] + 0.01, 0.05)

    best_score = -np.inf
    best_params = None

    for normal, fear, extreme in product(normal_range, fear_range, extreme_range):
        triggered, target_prices = evaluate_parameters(normal, fear, extreme)
        trade_count = int(triggered.sum())
        
        # 매수 횟수 필터링 (조건 만족 안 하면 포트폴리오 연산 생략하여 속도 업)
        if trade_count < 170 or trade_count > 240:
            continue

        pf = vbt.Portfolio.from_signals(
            close=df['Close'],
            entries=pd.Series(triggered, index=df.index),
            exits=pd.Series(False, index=df.index),
            price=pd.Series(target_prices, index=df.index),
            init_cash=10000,
            fees=0.0015,
            freq='1D',
            accumulate=True,
            allow_partial=True
        )

        # [BUG #2 FIX] nan or 0 → nan 그대로 반환되는 문제 수정
        calmar_raw = pf.calmar_ratio()
        calmar = 0.0 if (calmar_raw is None or not np.isfinite(calmar_raw)) else float(calmar_raw)
        score = calmar * 150 + (trade_count / 2.8)

        if score > best_score:
            best_score = score
            # [BUG #5 FIX] total_return() nan 방어
            total_ret_raw = pf.total_return()
            ret = float(total_ret_raw * 100) if np.isfinite(total_ret_raw) else 0.0
            best_params = (normal, fear, extreme, trade_count, calmar, ret)

    # ====================== 결과 출력 ======================
    if best_params:
        normal, fear, extreme, trades, calmar, ret = best_params
        print("🏆 안전 범위 내 최적 배수 조합")
        print(f"   평시 : {normal:.2f}x | 공포 : {fear:.2f}x | 극단 : {extreme:.2f}x")
        print(f"   총 매수 횟수 : {trades}회 | Calmar : {calmar:.3f} | 수익률 : {ret:+.2f}%\n")

        print("="*65)
        print("💡 config.json에 바로 사용하세요:")
        print(f'"MULT_NORMAL": {normal:.2f},')
        print(f'"MULT_FEAR": {fear:.2f},')
        print(f'"MULT_EXTREME": {extreme:.2f}')
        print("="*65)
    else:
        print("❌ 조건(매수 횟수 170~240회)을 만족하는 최적 배수 조합을 찾지 못했습니다.")

    print("\n✅ 백테스트 완료 - 매년 실행해도 안전한 범위 내에서 추천됩니다.")

if __name__ == "__main__":
    verify_long_term_hold_logic()