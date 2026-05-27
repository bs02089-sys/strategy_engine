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
    print("📡 [SOXL_VIX_FIXED_SIGMA_LOC.py] 고정 변동성 & 만기 일괄 청산 백테스트")
    print("🎯 베프님 맞춤형 연 20~28회 LOC 묵직한 저격망 관제탑\n")

    # ==================== 1. 데이터 로드 및 정제 ====================
    soxl = yf.download("SOXL", period="4y", interval="1d", progress=False, auto_adjust=True)
    vix = yf.download("^VIX", period="4y", interval="1d", progress=False, auto_adjust=True)

    if soxl.empty or vix.empty:
        print("❌ 데이터 다운로드 실패")
        return

    if isinstance(soxl.columns, pd.MultiIndex): soxl = soxl.droplevel(1, axis=1)
    if isinstance(vix.columns, pd.MultiIndex): vix = vix.droplevel(1, axis=1)

    vix_close = vix['Close'].reindex(soxl.index).ffill()

    df = pd.DataFrame({
        'Open': soxl['Open'].values,
        'High': soxl['High'].values,
        'Low': soxl['Low'].values,
        'Close': soxl['Close'].values,
        'VIX': vix_close.values
    }, index=soxl.index).dropna()

    df = df.asfreq('B').dropna()

    # 🎯 베프님의 앵커 기준점: 고정 일간 변동성 4.5% 주입
    FIXED_SIGMA = 0.045  
    df['Daily_Sigma'] = FIXED_SIGMA
    
    df['Prev_Close'] = df['Close'].shift(1)
    df['Gap_Ratio'] = np.where(df['Prev_Close'] != 0, (df['Open'] - df['Prev_Close']) / df['Prev_Close'], 0.0)
    df = df.dropna().copy()

    # ==================== 2. 만기 일괄 청산 시그널 ====================
    n_rows = len(df)
    exits_arr = np.zeros(n_rows, dtype=bool)
    exits_arr[-1] = True # 중간에 절대 팔지 않고 마지막 날 전량 청산
    exits_series = pd.Series(exits_arr, index=df.index)

    total_years = len(df) / 252
    print(f"📊 분석 기간 : {df.index[0].date()} ~ {df.index[-1].date()} ({len(df)} 거래일)")
    print(f"📌 고정 일간변동성(Sigma) : {FIXED_SIGMA * 100:.2f}%")
    print(f"⏱️  청산 조건 : 중간 청산 없음 ➔ 최종 만기일 일괄 전량 청산\n")

    SAFETY_BOUNDS = {
        "MULT_NORMAL":  (0.55, 0.95), 
        "MULT_FEAR":    (1.95, 2.50),
        "MULT_EXTREME": (2.50, 2.95)
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
        triggered = df['Close'].values <= target_prices
        return triggered, df['Close'].values

    FEES = 0.00065  

    # ==================== 4. 누적 매수 최적화 루프 ====================
    print("🔍 고정 변동성 + 일괄 청산 기준 최적 배수 탐색 중...")

    def _steps(lo, hi, step=0.05):
        return np.linspace(lo, hi, round((hi - lo) / step) + 1)

    best_score = -np.inf
    best_params = None

    for normal, fear, extreme in product(_steps(*SAFETY_BOUNDS["MULT_NORMAL"]), 
                                         _steps(*SAFETY_BOUNDS["MULT_FEAR"]), 
                                         _steps(*SAFETY_BOUNDS["MULT_EXTREME"])):
        if extreme <= fear: continue

        triggered, exec_prices = evaluate_parameters(normal, fear, extreme)
        trade_count = int(triggered.sum())
        
        # 🎯 베프님이 정조준한 연간 20~28회 범위 필터
        annual_freq = trade_count / total_years
        if annual_freq < 20 or annual_freq > 28: continue

        pf = vbt.Portfolio.from_signals(
            close=df['Close'], entries=pd.Series(triggered, index=df.index), exits=exits_series,
            price=pd.Series(exec_prices, index=df.index), init_cash=10000, fees=FEES, freq='1D',
            accumulate=True, allow_partial=False
        )

        total_return = pf.total_return()
        mdd = pf.max_drawdown()
        score = total_return * 100 - (mdd * 100 * 0.4)

        if score > best_score:
            best_score = score
            best_params = (normal, fear, extreme, trade_count, total_return*100, mdd*100)

    # ==================== 5. 결과 출력 및 자동 저장 ====================
    if best_params:
        normal, fear, extreme, trades, ret, mdd_opt = best_params
        opt_annual_quota = int(trades / total_years)
        print("\n🏆 [조건 부합 최적 배수 조합 발견!]")
        print(f"   평시 : {normal:.2f}x | 공포 : {fear:.2f}x | 극단 : {extreme:.2f}x")
        print(f"   총 매수 횟수 : {trades}회 (4년)")
        print(f"   🎯 확정 연간 쿼터 (ANNUAL_QUOTA) : {opt_annual_quota}회")
        print(f"   총 수익률 : {ret:+.2f}% | MDD : {mdd_opt:.2f}%\n")

        answer = input("💾 위 최적 설정값을 config.json에 저장하시겠습니까? (y/n): ").strip().lower()
        if answer == "y":
            try:
                with open("config.json", "r", encoding="utf-8") as f: cfg = json.load(f)
                if "VIX_CONFIG" in cfg:
                    for mode in ["LONG", "SHORT"]:
                        if mode in cfg["VIX_CONFIG"]:
                            cfg["VIX_CONFIG"][mode]["MULT_NORMAL"]  = round(float(normal),  2)
                            cfg["VIX_CONFIG"][mode]["MULT_FEAR"]    = round(float(fear),    2)
                            cfg["VIX_CONFIG"][mode]["MULT_EXTREME"] = round(float(extreme), 2)
                if "POSITIONS" in cfg and "SOXL" in cfg["POSITIONS"]:
                    cfg["POSITIONS"]["SOXL"]["ANNUAL_QUOTA_LONG"] = opt_annual_quota
                with open("config.json", "w", encoding="utf-8") as f: json.dump(cfg, f, indent=4, ensure_ascii=False)
                print("✅ config.json에 자동 저장 및 동기화 완료!")
            except Exception as e: print(f"❌ 저장 실패: {e}")
    else:
        print("❌ 설정하신 연 20~28회 범위 내에서 조합을 찾지 못했습니다. SAFETY_BOUNDS 범위를 넓히거나 FIXED_SIGMA를 미세조정해 보세요.")

if __name__ == "__main__":
    verify_fixed_sigma_loc_logic()