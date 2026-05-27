import json
import numpy as np
import pandas as pd
import yfinance as yf
import vectorbt as vbt
import warnings
from itertools import product

warnings.filterwarnings('ignore', category=FutureWarning)
warnings.filterwarnings('ignore', category=UserWarning)

def verify_long_term_hold_logic():
    print("📡 [SOXL_VIX_SIGMA_BACKTEST_V2.py] 장기 보유 전략 정밀 백테스트")
    print("🎯 장중 저점(Low) 저격망 및 연간 쿼터 최적화 관제탑\n")

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

    # 인덱스 빈도(Frequency) 명시적 지정 (vectorbt ValueError 에러 원천 차단)
    df = df.asfreq('B').dropna()

    df['Log_Ret'] = np.log(df['Close'] / df['Close'].shift(1))
    df['Daily_Sigma'] = df['Log_Ret'].rolling(window=90, min_periods=60).std(ddof=1)
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

    final_price = df['Close'].iloc[-1]
    print(f"📊 분석 기간 : {df.index[0].date()} ~ {df.index[-1].date()} ({len(df)} 거래일)")
    print(f"📈 SOXL 최종 종가 : ${final_price:.2f}")
    print(f"⏱️  청산 조건 : 진입 후 {HOLD_DAYS} 거래일 후 전량 청산\n")

    SAFETY_BOUNDS = {
        "MULT_NORMAL":  (0.65, 0.85),
        "MULT_FEAR":    (1.95, 2.45),
        "MULT_EXTREME": (2.50, 2.75)
    }

    # ==================== 3. 갭 보정 연산 (벡터화) ====================
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
        
        # 🎯 핵심: 장중 저점(Low)이 타깃가 이하로 내려갔을 때 매수 격발!
        triggered = df['Low'].values <= target_prices
        exec_prices = np.minimum(df['Open'].values, target_prices)
        return triggered, exec_prices

    FEES = 0.00065  

    # ==================== 4. 현재 설정값 성과 출력 ====================
    try:
        with open("config.json", "r", encoding="utf-8") as _f:
            _cfg = json.load(_f)
        _vix_long = _cfg.get("VIX_CONFIG", {}).get("LONG", {})
        CURRENT_NORMAL  = _vix_long.get("MULT_NORMAL",  0.85)
        CURRENT_FEAR    = _vix_long.get("MULT_FEAR",    1.95)
        CURRENT_EXTREME = _vix_long.get("MULT_EXTREME", 2.40)
    except Exception:
        CURRENT_NORMAL, CURRENT_FEAR, CURRENT_EXTREME = 0.85, 1.95, 2.40

    cur_triggered, cur_exec_prices = evaluate_parameters(CURRENT_NORMAL, CURRENT_FEAR, CURRENT_EXTREME)
    
    # freq='1D' 추가로 인덱스 에러 방지
    cur_pf = vbt.Portfolio.from_signals(
        close=df['Close'], entries=pd.Series(cur_triggered, index=df.index), exits=exits_series,
        price=pd.Series(cur_exec_prices, index=df.index), init_cash=10000, fees=FEES, freq='1D',
        accumulate=True, allow_partial=True
    )
    
    total_years = len(df) / 252
    print("[현재 config.json 설정값 성과]")
    print(f"   평시: {CURRENT_NORMAL:.2f} | 공포: {CURRENT_FEAR:.2f} | 극단: {CURRENT_EXTREME:.2f}")
    print(f"   💥 총 매수 횟수 : {int(cur_triggered.sum())}회 (4년 총합)")
    print(f"   📊 연평균 매수 빈도 : {int(cur_triggered.sum() / total_years)}회 ➔ 🔥 이게 진짜 연간 쿼터 후보!")
    print(f"   최종 수익률  : {cur_pf.total_return()*100:+.2f}%")
    print(f"   최대 드로다운 : {cur_pf.max_drawdown()*100:.2f}%")
    print(f"   Calmar Ratio : {cur_pf.calmar_ratio():.3f}\n")

    # ==================== 5. 초고속 최적화 루프 = "고속 필터링 연산" ====================
    print("🔍 안전 범위 내에서 최적 배수 탐색 중 (연산 다이어트 완료)...")

    def _steps(lo, hi, step=0.05):
        return np.linspace(lo, hi, round((hi - lo) / step) + 1)

    normal_range  = _steps(SAFETY_BOUNDS["MULT_NORMAL"][0],  SAFETY_BOUNDS["MULT_NORMAL"][1])
    fear_range    = _steps(SAFETY_BOUNDS["MULT_FEAR"][0],    SAFETY_BOUNDS["MULT_FEAR"][1])
    extreme_range = _steps(SAFETY_BOUNDS["MULT_EXTREME"][0], SAFETY_BOUNDS["MULT_EXTREME"][1])

    best_score = -np.inf
    best_params = None

    for normal, fear, extreme in product(normal_range, fear_range, extreme_range):
        if extreme <= fear:
            continue

        triggered, exec_prices = evaluate_parameters(normal, fear, extreme)
        trade_count = int(triggered.sum())
        
        # 📌 현실적인 제약조건으로 변경: 4년 동안 최소 50번 이상은 사지는 조합만 탐색
        if trade_count < 50:
            continue

        # 가벼운 지표 계산을 선행하여 불필요한 포트폴리오 객체 생성 최소화
        pf = vbt.Portfolio.from_signals(
            close=df['Close'], entries=pd.Series(triggered, index=df.index), exits=exits_series,
            price=pd.Series(exec_prices, index=df.index), init_cash=10000, fees=FEES, freq='1D',
            accumulate=True, allow_partial=True
        )

        calmar = pf.calmar_ratio()
        if calmar is None or not np.isfinite(calmar): 
            calmar = 0.0
            
        # 스코어링 전략: Calmar(안정성 대비 수익) 최적화 + 적절한 매수 빈도 가산점
        score = calmar * 150 + (trade_count / 10.0)

        if score > best_score:
            best_score = score
            best_params = (normal, fear, extreme, trade_count, calmar, pf.total_return()*100, pf.max_drawdown()*100)

    # ==================== 6. 최적화 결과 및 자동 저장 ====================
    if best_params:
        normal, fear, extreme, trades, calmar, ret, mdd_opt = best_params
        opt_annual_quota = int(trades / total_years)
        print("\n🏆 [안전 범위 내 최적 배수 조합 발견!]")
        print(f"   평시 : {normal:.2f}x | 공포 : {fear:.2f}x | 극단 : {extreme:.2f}x")
        print(f"   총 매수 횟수 : {trades}회 (4년)")
        print(f"   🎯 추천 연간 쿼터 (ANNUAL_QUOTA) : {opt_annual_quota}회")
        print(f"   Calmar 지표 : {calmar:.3f} | 총 수익률 : {ret:+.2f}% | MDD : {mdd_opt:.2f}%\n")

        print("="*65)
        print("💡 config.json에 반영할 스펙:")
        print(f'"MULT_NORMAL": {normal:.2f}, "MULT_FEAR": {fear:.2f}, "MULT_EXTREME": {extreme:.2f}')
        print(f'"ANNUAL_QUOTA_LONG": {opt_annual_quota}')
        print("="*65)

        answer = input("\n💾 위 최적 설정값을 config.json에 저장하시겠습니까? (y/n): ").strip().lower()
        if answer == "y":
            try:
                with open("config.json", "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                
                # 배수 업데이트
                for mode in ("LONG", "SHORT"):
                    cfg["VIX_CONFIG"][mode]["MULT_NORMAL"]  = round(float(normal),  2)
                    cfg["VIX_CONFIG"][mode]["MULT_FEAR"]    = round(float(fear),    2)
                    cfg["VIX_CONFIG"][mode]["MULT_EXTREME"] = round(float(extreme), 2)
                
                # 🎯 직접 검증한 '장중 저점 기준 연간 쿼터' 자동 주입!
                cfg["POSITIONS"]["SOXL"]["ANNUAL_QUOTA_LONG"] = opt_annual_quota
                
                with open("config.json", "w", encoding="utf-8") as f:
                    json.dump(cfg, f, indent=4, ensure_ascii=False)
                print("✅ config.json에 최적 배수 및 연간 쿼터가 연동 저장되었습니다.")
            except Exception as e:
                print(f"❌ config.json 저장 실패: {e}")
    else:
        print("❌ 최적 배수 조합을 찾지 못했습니다.")

if __name__ == "__main__":
    verify_long_term_hold_logic()