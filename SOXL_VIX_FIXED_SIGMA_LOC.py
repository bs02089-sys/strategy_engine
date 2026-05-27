import json
import numpy as np
import pandas as pd
import yfinance as yf
import vectorbt as vbt
import warnings
from itertools import product

# 불필요한 경고창 제거
warnings.filterwarnings('ignore', category=FutureWarning)
warnings.filterwarnings('ignore', category=UserWarning)

def verify_fixed_sigma_5year_final():
    print("======================================================================")
    print("📡 [SOXL_VIX_FIXED_SIGMA_LOC_FINAL.py] 고정 변동성 & 5년 마스터 플랜 백테스트")
    print("🎯 대상승장/대하락장 완전 통합형 적립망 관제탑 (연 15~30회 타깃)")
    print("======================================================================\n")

    # ==================== 1. 5년 데이터 로드 및 정제 ====================
    print("📡 야후 파이낸스로부터 최근 5년치 SOXL 및 VIX 데이터를 다운로드 중...")
    soxl = yf.download("SOXL", period="5y", interval="1d", progress=False, auto_adjust=True)
    vix = yf.download("^VIX", period="5y", interval="1d", progress=False, auto_adjust=True)

    if soxl.empty or vix.empty:
        print("❌ 데이터 다운로드 실패: 네트워크 연결이나 티커명을 확인하세요.")
        return

    # 멀티인덱스 컬럼일 경우 단일 인덱스로 평탄화
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

    # 평일(Business Day) 기준으로 정렬 및 결측치 제거
    df = df.asfreq('B').dropna()

    # 📌 베프님의 단단한 기준 앵커: 고정 일간 변동성 상수를 4.5%로 주입
    FIXED_SIGMA = 0.045  
    df['Daily_Sigma'] = FIXED_SIGMA
    
    df['Prev_Close'] = df['Close'].shift(1)
    df['Gap_Ratio'] = np.where(df['Prev_Close'] != 0, (df['Open'] - df['Prev_Close']) / df['Prev_Close'], 0.0)
    df = df.dropna().copy()

    # ==================== 2. 만기 일괄 청산 세팅 (2027년 스케줄) ====================
    n_rows = len(df)
    exits_arr = np.zeros(n_rows, dtype=bool)
    exits_arr[-1] = True # 5년 내내 중간 청산 없이 오직 마지막 날에만 전량 매도
    exits_series = pd.Series(exits_arr, index=df.index)

    total_years = len(df) / 252
    print(f"📊 시뮬레이션 기간 : {df.index[0].date()} ~ {df.index[-1].date()} ({len(df)} 거래일, 약 {total_years:.1f}년)")
    print(f"📌 고정 일간변동성(Sigma) : {FIXED_SIGMA * 100:.2f}%")
    print(f"⏱️  청산 조건 : 5년간 묻지마 적립 ➔ 최종 만기일 일괄 전량 청산\n")

    # VIX 국면별 배수 탐색 범위 (상승장 얕은 조정 소외를 막기 위해 평시 하한을 0.30으로 배치)
    SAFETY_BOUNDS = {
        "MULT_NORMAL":  (0.30, 0.90), 
        "MULT_FEAR":    (1.80, 2.50),
        "MULT_EXTREME": (2.40, 3.10)
    }

    # ==================== 3. 갭 보정 및 매수 판정 (LOC 시스템) ====================
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
        
        # LOC 조건: 당일 종가(Close)가 계산된 타깃가 이하일 때 체결
        triggered = df['Close'].values <= target_prices
        return triggered, df['Close'].values

    FEES = 0.00065  # 왕복 수수료 감안 보수적 세팅

    # ==================== 4. 최적화 루프 (연평균 15~30회 확장형) ====================
    print("🔍 탐색 범위를 넓혀 황금 조합 분석 중 (약 5초 소요)...")

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
        
        # 🎯 5년 장기 레이스를 감안하여 연평균 매수 빈도 필터를 15~30회로 버퍼 확장!
        annual_freq = trade_count / total_years
        if annual_freq < 15 or annual_freq > 30: continue

        # 포트폴리오 백테스트 가동 (분할 매집용 accumulate=True)
        pf = vbt.Portfolio.from_signals(
            close=df['Close'], entries=pd.Series(triggered, index=df.index), exits=exits_series,
            price=pd.Series(exec_prices, index=df.index), init_cash=10000, fees=FEES, freq='1D',
            accumulate=True, allow_partial=False
        )

        total_return = pf.total_return()
        mdd = pf.max_drawdown()
        
        # 스코어링 수식 (장기 우상향 자산 증식 극대화형)
        score = total_return * 100 - (mdd * 100 * 0.2)

        if score > best_score:
            best_score = score
            best_params = (normal, fear, extreme, trade_count, total_return*100, mdd*100)

    # ==================== 5. 결과 출력 및 config.json 정밀 자동 저장 ====================
    if best_params:
        normal, fear, extreme, trades, ret, mdd_opt = best_params
        opt_annual_quota = int(trades / total_years)
        
        print("\n🏆 [5년 마스터 플랜 최적 조합 발견!]")
        print(f"   평시(VIX <20)  : {normal:.2f}x (타깃 하락률: 약 {FIXED_SIGMA*normal*100:.2f}%)")
        print(f"   공포(VIX 20~30): {fear:.2f}x (타깃 하락률: 약 {FIXED_SIGMA*fear*100:.2f}%)")
        print(f"   극단(VIX >=30) : {extreme:.2f}x (타깃 하락률: 약 {FIXED_SIGMA*extreme*100:.2f}%)")
        print(f"   5년간 총 매수 횟수 : {trades}회 (5년 총합)")
        print(f"   🎯 확정 연간 쿼터  : {opt_annual_quota}회")
        print(f"   🚀 5년 최종 누적 수익률 : {ret:+.2f}% | 최고 MDD : {mdd_opt:.2f}%\n")

        answer = input("💾 이 최적 스펙을 config.json에 동기화할까요? (y/n): ").strip().lower()
        if answer == "y":
            try:
                with open("config.json", "r", encoding="utf-8") as f: 
                    cfg = json.load(f)
                
                # 1. VIX_CONFIG 하위 딕셔너리 안전 업데이트
                if "VIX_CONFIG" in cfg:
                    for mode in ["LONG", "SHORT"]:
                        if mode in cfg["VIX_CONFIG"]:
                            cfg["VIX_CONFIG"][mode]["MULT_NORMAL"]  = round(float(normal),  2)
                            cfg["VIX_CONFIG"][mode]["MULT_FEAR"]    = round(float(fear),    2)
                            cfg["VIX_CONFIG"][mode]["MULT_EXTREME"] = round(float(extreme), 2)
                
                # 2. SOXL 포지션 내부 연간 쿼터 업데이트
                if "POSITIONS" in cfg and "SOXL" in cfg["POSITIONS"]:
                    cfg["POSITIONS"]["SOXL"]["ANNUAL_QUOTA_LONG"] = opt_annual_quota
                
                with open("config.json", "w", encoding="utf-8") as f: 
                    json.dump(cfg, f, indent=4, ensure_ascii=False)
                
                print("======================================================================")
                print("✅ [동기화 성공] 5년 검증 기반 배수와 연간 쿼터가 장부에 자동 기록되었습니다!")
                print("======================================================================")
            except Exception as e: 
                print(f"❌ config.json 저장 실패: {e}")
        else:
            print("⏭️ 저장을 건너뜁니다. 기존 설정이 유지됩니다.")
    else:
        print("❌ 조합 탐색 실패. 조건 범위 내에 부합하는 배수가 없습니다.")
        print("💡 팁: FIXED_SIGMA를 0.042 또는 0.040으로 낮춰 그물망을 약간 위로 조율해 보세요.")

if __name__ == "__main__":
    verify_fixed_sigma_5year_final()