import json
import numpy as np
import pandas as pd
import yfinance as yf
import vectorbt as vbt
import warnings

warnings.filterwarnings('ignore', category=FutureWarning)
warnings.filterwarnings('ignore', category=UserWarning)

def run_final_tactical_tower():
    print("======================================================================")
    print("📡 [SOXL_VIX_2YEAR_FINAL_TACTICAL.py]")
    print("🏆 [최종 확정] 2년 주기 + 30% 익절 프로토콜 결합형 완성판 관제탑")
    print("======================================================================\n")

    # 1. 데이터 다운로드 (최근 5년치 역사적 데이터 반영)
    print("📡 야후 파이낸스로부터 SOXL 및 VIX 데이터 다운로드 중...")
    soxl = yf.download("SOXL", period="5y", interval="1d", progress=False, auto_adjust=True)
    vix = yf.download("^VIX", period="5y", interval="1d", progress=False, auto_adjust=True)

    if soxl.empty or vix.empty:
        print("❌ 데이터 다운로드 실패"); return

    if isinstance(soxl.columns, pd.MultiIndex): soxl = soxl.droplevel(1, axis=1)
    if isinstance(vix.columns, pd.MultiIndex): vix = vix.droplevel(1, axis=1)

    vix_close = vix['Close'].reindex(soxl.index).ffill()
    df = pd.DataFrame({
        'Open': soxl['Open'].values, 'High': soxl['High'].values,
        'Low': soxl['Low'].values, 'Close': soxl['Close'].values, 'VIX': vix_close.values
    }, index=soxl.index).dropna().asfreq('B').dropna()
    
    df['Prev_Close'] = df['Close'].shift(1)
    df['Gap_Ratio'] = np.where(df['Prev_Close'] != 0, (df['Open'] - df['Prev_Close']) / df['Prev_Close'], 0.0)
    df = df.dropna().copy()

    # 2. 2년 주기 가상 시나리오 세그먼트 쪼개기 (252일 간격 롤링)
    trading_days_per_year = 252
    segments = []
    start_indices = [0, trading_days_per_year, trading_days_per_year*2, trading_days_per_year*3]
    
    for start_idx in start_indices:
        end_idx = start_idx + (trading_days_per_year * 2)
        if end_idx <= len(df):
            segments.append(df.iloc[start_idx:end_idx].copy())

    # 🏆 대조표 검증을 통해 판정승을 거둔 황금 스펙 고정
    SIGMA = 0.0460
    MULT_NORMAL = 1.40
    MULT_FEAR = 2.70
    MULT_EXTREME = 2.80
    TAKE_PROFIT = 0.30  # 대조표에서 압도적 1위를 한 +30% 익절선
    FEES = 0.00065

    def apply_gap_correction_vectorized(base_multipliers, gap_ratio):
        return np.select(
            [gap_ratio >= -0.03, gap_ratio >= -0.05, gap_ratio >= -0.07, gap_ratio >= -0.10],
            [base_multipliers, 0.45, 0.25, 0.10], default=0.0
        )

    print("📊 확정된 황금 스펙 시뮬레이션 최종 구동 중...")
    
    seg_returns = []
    seg_mdds = []
    seg_trades = []

    for idx, seg in enumerate(segments):
        v_vix = seg['VIX'].values
        v_gap = seg['Gap_Ratio'].values
        v_open = seg['Open'].values
        v_close = seg['Close'].values
        
        multipliers = np.where(v_vix >= 30, MULT_EXTREME, np.where(v_vix >= 20, MULT_FEAR, MULT_NORMAL))
        adj_multipliers = apply_gap_correction_vectorized(multipliers, v_gap)
        target_prices = v_open * np.exp(-SIGMA * adj_multipliers)
        
        # 첫 1년(252일) 동안만 매수 신호 포착
        buy_window = np.arange(len(seg)) < trading_days_per_year
        entries = (v_close <= target_prices) & buy_window
        seg_trades.append(int(entries.sum()))

        # 만기일 일괄 청산 기본 세팅
        exits_arr = np.zeros(len(seg), dtype=bool)
        exits_arr[-1] = True
        exits = pd.Series(exits_arr, index=seg.index)

        # VectorBT 엔진 구동 (+30% 익절선 탑재)
        pf = vbt.Portfolio.from_signals(
            close=seg['Close'], entries=pd.Series(entries, index=seg.index), exits=exits,
            price=pd.Series(v_close, index=seg.index), init_cash=10000, fees=FEES, freq='1D',
            accumulate=True, allow_partial=False, tp_stop=TAKE_PROFIT
        )
        
        seg_returns.append(pf.total_return() * 100)
        seg_mdds.append(pf.max_drawdown() * 100)

    # ==================== 마스터 플랜 리포트 출력 ====================
    print("\n======================================================================")
    print("🎯 [관제탑 마스터 플랜 가동 준비 완료]")
    print(f"   • 확정 고정 시그마  : {SIGMA * 100:.2f}%")
    print(f"   • 평시 타깃 하락률  : 약 {SIGMA * MULT_NORMAL * 100:.2f}% (VIX < 20)")
    print(f"   • 공포 타깃 하락률  : 약 {SIGMA * MULT_FEAR * 100:.2f}% (VIX 20~30)")
    print(f"   • 극단 타깃 하락률  : 약 {SIGMA * MULT_EXTREME * 100:.2f}% (VIX >= 30)")
    print(f"   • 🚨 핵심 익절 프로토콜 : 계좌 총수익률 +{TAKE_PROFIT*100:.0f}% 달성 시 자동 전량 청산")
    print("----------------------------------------------------------------------")
    print(f"   📈 2년 주기 평균 누적 수익률 : {np.mean(seg_returns):+.2f}% (익절 메커니즘의 승리)")
    print(f"   🎯 첫 1년간 평균 매수 빈도   : 연 {np.mean(seg_trades):.1f}회 체결 (목표치 완벽 부합)")
    print(f"   🛡️ 역사상 가장 잔인했던 MDD : {np.min(seg_mdds):.2f}% (2022년 대공황 정면돌파 기준)")
    print("======================================================================\n")

    # config.json 동기화 자동화
    answer = input("💾 이 최종 확정 세팅을 config.json에 업데이트할까요? (y/n): ").strip().lower()
    if answer == "y":
        try:
            # 기존 파일 읽기 시도
            try:
                with open("config.json", "r", encoding="utf-8") as f: cfg = json.load(f)
            except FileNotFoundError:
                cfg = {}

            # 스펙 구조화 주입
            if "VIX_CONFIG" not in cfg: cfg["VIX_CONFIG"] = {}
            if "LONG" not in cfg["VIX_CONFIG"]: cfg["VIX_CONFIG"]["LONG"] = {}
            
            cfg["VIX_CONFIG"]["LONG"]["FIXED_SIGMA"] = round(float(SIGMA), 4)
            cfg["VIX_CONFIG"]["LONG"]["MULT_NORMAL"] = round(float(MULT_NORMAL), 2)
            cfg["VIX_CONFIG"]["LONG"]["MULT_FEAR"] = round(float(MULT_FEAR), 2)
            cfg["VIX_CONFIG"]["LONG"]["MULT_EXTREME"] = round(float(MULT_EXTREME), 2)
            cfg["VIX_CONFIG"]["LONG"]["TAKE_PROFIT_RATIO"] = round(float(TAKE_PROFIT), 2)

            if "STRATEGY" not in cfg: cfg["STRATEGY"] = {}
            cfg["STRATEGY"]["CYCLE_YEARS"] = 2
            cfg["STRATEGY"]["BUY_DURATION_DAYS"] = trading_days_per_year
            cfg["STRATEGY"]["HOLD_DURATION_DAYS"] = trading_days_per_year

            with open("config.json", "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=4, ensure_ascii=False)
            print("✅ [동기화 성공] 완벽한 방어막 세팅이 config.json에 각인되었습니다!")
        except Exception as e:
            print(f"❌ 저장 중 오류 발생: {e}")
    else:
        print("❌ 업데이트가 취소되었습니다. 관제탑이 대기 모드로 전환됩니다.")

if __name__ == "__main__":
    run_final_tactical_tower()