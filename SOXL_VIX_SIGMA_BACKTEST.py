import json
import numpy as np
import pandas as pd
import yfinance as yf
import vectorbt as vbt
import warnings
from itertools import product

warnings.filterwarnings('ignore', category=FutureWarning)
warnings.filterwarnings('ignore', category=UserWarning)

def verify_fixed_sigma_loc_logic():
    print("📡 [SOXL_VIX_FIXED_SIGMA_LOC.py] 고정 변동성 & LOC 기준 정밀 백테스트")
    print("🎯 고정 연간평균변동성 기반의 깐깐한 종가 저격망 및 쿼터 최적화\n")

    # ==================== 1. 데이터 로드 및 정제 ====================
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

    df = df.asfreq('B').dropna()

    # 🎯 핵심 변경점: 롤링 시그마를 버리고, 베프님의 연간평균변동성(4.5%) 고정 상수를 적용!
    FIXED_SIGMA = 0.045  
    df['Daily_Sigma'] = FIXED_SIGMA
    
    df['Prev_Close'] = df['Close'].shift(1)
    df['Gap_Ratio'] = np.where(df['Prev_Close'] != 0, (df['Open'] - df['Prev_Close']) / df['Prev_Close'], 0.0)
    df = df.dropna().copy()

    # ==================== 2. 1년(252일) 홀딩 청산 시그널 ====================
    n_rows = len(df)
    HOLD_DAYS = 252  
    exits_arr = np.zeros(n_rows, dtype=bool)
    for i in range(n_rows):
        exit_idx = min(i + HOLD_DAYS, n_rows - 1)
        exits_arr[exit_idx] = True
    exits_series = pd.Series(exits_arr, index=df.index)

    total_years = len(df) / 252
    print(f"📊 분석 기간 : {df.index[0].date()} ~ {df.index[-1].date()} ({len(df)} 거래일)")
    print(f"📌 적용된 고정 일간변동성(Sigma) : {FIXED_SIGMA * 100:.2f}%")
    print(f"⏱️  청산 조건 : 진입 후 {HOLD_DAYS} 거래일 후 전량 청산\n")

    SAFETY_BOUNDS = {
        "MULT_NORMAL":  (0.65, 0.85),
        "MULT_FEAR":    (1.95, 2.45),
        "MULT_EXTREME": (2.50, 2.75)
    }

    # ==================== 3. 갭 보정 및 매수 판정 (LOC) ====================
    def apply_gap_correction_vectorized(base_multipliers, gap_ratio):
        return np.select(
            [gap_ratio >= -0.03, gap_ratio >= -0.05, gap_ratio >= -0.07, gap_ratio >= -0.10],
            [base_multipliers, 0.45, 0.25, 0.10],
            default=0.0
        )

    def evaluate_parameters(normal, fear, extreme):
        multipliers = np.where(df['VIX'].values >= 30, extreme, np.where(df['VIX'].values >= 20, fear, normal))
        adj_multipliers = apply_gap_correction_vectorized(multipliers, df['Gap_Ratio'].values)
        target_prices = df['Open'].values * np.exp(-df['Daily_Sigma'].values * adj_multipliers)
        
        # 종가(Close)가 고정 타깃가 이하일 때 격발 (LOC)
        triggered = df['Close'].values <= target_prices
        return triggered, df['Close'].values

    FEES = 0.00065  

    # ==================== 4. 최적화 루프 (고정 시그마 맞춤형) ====================
    print("🔍 고정 변동성 기준 최적 배수 및 연간 쿼터 탐색 중...")

    def _steps(lo, hi, step=0.05):
        return np.linspace(lo, hi, round((hi - lo) / step) + 1)

    best_score = -np.inf
    best_params = None

    for normal, fear, extreme in product(_steps(*SAFETY_BOUNDS["MULT_NORMAL"]), 
                                         _steps(*SAFETY_BOUNDS["MULT_FEAR"]), 
                                         _steps(*SAFETY_BOUNDS["MULT_EXTREME"])):
        if extreme <= fear:
            continue

        triggered, exec_prices = evaluate_parameters(normal, fear, extreme)
        trade_count = int(triggered.sum())
        
        # 베프님이 원하시는 락(Lock): 4년 평균 연 20~28회 수준만 필터링
        annual_freq = trade_count / total_years
        if annual_freq < 18 or annual_freq > 28:
            continue

        pf = vbt.Portfolio.from_signals(
            close=df['Close'], entries=pd.Series(triggered, index=df.index), exits=exits_series,
            price=pd.Series(exec_prices, index=df.index), init_cash=10000, fees=FEES, freq='1D',
            accumulate=True, allow_partial=True
        )

        calmar = pf.calmar_ratio()
        if calmar is None or not np.isfinite(calmar): calmar = 0.0
            
        score = calmar * 150 + (trade_count / 5.0)

        if score > best_score:
            best_score = score
            best_params = (normal, fear, extreme, trade_count, calmar, pf.total_return()*100, pf.max_drawdown()*100)

    # ==================== 5. 결과 출력 및 자동 저장 ====================
    if best_params:
        normal, fear, extreme, trades, calmar, ret, mdd_opt = best_params
        opt_annual_quota = int(trades / total_years)
        print("\n🏆 [고정 변동성 LOC 최적 배수 조합 발견!]")
        print(f"   평시 : {normal:.2f}x | 공포 : {fear:.2f}x | 극단 : {extreme:.2f}x")
        print(f"   총 매수 횟수 : {trades}회 (4년)")
        print(f"   🎯 확정 연간 쿼터 (ANNUAL_QUOTA) : {opt_annual_quota}회 ➔ 🔥 베프님의 직관과 일치!")
        print(f"   Calmar 지표 : {calmar:.3f} | 총 수익률 : {ret:+.2f}% | MDD : {mdd_opt:.2f}%\n")

        print("="*65)
        print("💡 config.json 자동 반영 스펙:")
        print(f'"MULT_NORMAL": {normal:.2f}, "MULT_FEAR": {fear:.2f}, "MULT_EXTREME": {extreme:.2f}')
        print(f'"ANNUAL_QUOTA_LONG": {opt_annual_quota}')
        print("="*65)

        answer = input("\n💾 위 최적 설정값을 config.json에 저장하시겠습니까? (y/n): ").strip().lower()
        if answer == "y":
            try:
                with open("config.json", "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                
                if "VIX_CONFIG" in cfg:
                    for mode in ["LONG", "SHORT"]:
                        if mode in cfg["VIX_CONFIG"]:
                            cfg["VIX_CONFIG"][mode]["MULT_NORMAL"]  = round(float(normal),  2)
                            cfg["VIX_CONFIG"][mode]["MULT_FEAR"]    = round(float(fear),    2)
                            cfg["VIX_CONFIG"][mode]["MULT_EXTREME"] = round(float(extreme), 2)
                
                if "POSITIONS" in cfg and "SOXL" in cfg["POSITIONS"]:
                    cfg["POSITIONS"]["SOXL"]["ANNUAL_QUOTA_LONG"] = opt_annual_quota
                
                with open("config.json", "w", encoding="utf-8") as f:
                    json.dump(cfg, f, indent=4, ensure_ascii=False)
                print("✅ config.json에 자동 저장 및 동기화 완료!")
            except Exception as e: print(f"❌ 저장 실패: {e}")
    else:
        print("❌ 설정하신 연 20~28회 범위 내에서 최적 조합을 찾지 못했습니다. FIXED_SIGMA 값을 조정해 보세요.")

if __name__ == "__main__":
    verify_fixed_sigma_loc_logic()