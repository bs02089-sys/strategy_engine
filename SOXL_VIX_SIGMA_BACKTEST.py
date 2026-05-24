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

    # [FIX #7] 통계 기준 일치: 백테스트 전 기간에 걸쳐 고정 window=90으로 rolling sigma 계산.
    # 단, rolling sigma는 과거 데이터에만 의존(미래 누수 없음)하므로 그대로 유지.
    # 분석 기간(4y) 전체를 동일한 90일 롤링 sigma 기준으로 통일 → 국면별 편차는 sigma 자체에 반영됨.
    df['Daily_Sigma'] = df['Log_Ret'].rolling(window=90, min_periods=60).std(ddof=1)

    df['Prev_Close'] = df['Close'].shift(1)

    # 갭 비율: 전일 종가 대비 당일 시가 (음수 = 갭 하락)
    df['Gap_Ratio'] = np.where(
        df['Prev_Close'] != 0,
        (df['Open'] - df['Prev_Close']) / df['Prev_Close'],
        0.0
    )
    df = df.dropna().copy()
    df.index = pd.to_datetime(df.index)

    # ==================== exits 시그널 생성 (FIX #3) ====================
    # 전략: 매수 기간 ~2027-05 / 청산 시점 ~2028-05 (각 매수일로부터 252거래일 후)
    # 구현: 각 진입일의 integer 위치 + 252 위치에 exit=True 설정
    # - 같은 날 복수 진입이 쌓여도 252거래일 후 동일하게 전량 청산
    # - 데이터 범위를 벗어나는 경우(마지막 252일) exit는 마지막 거래일에 설정
    n_rows = len(df)
    HOLD_DAYS = 252  # 1년 홀딩 (거래일 기준)
    exits_arr = np.zeros(n_rows, dtype=bool)

    # 매수 기간 제한: 데이터 기준 최근 2년 이내의 진입만 252일 후 청산
    # (백테스트 시뮬레이션에서는 전 기간 진입 허용, 진입 후 252일 후 청산)
    for i in range(n_rows):
        exit_idx = min(i + HOLD_DAYS, n_rows - 1)
        exits_arr[exit_idx] = True
    exits_series = pd.Series(exits_arr, index=df.index)

    final_price = df['Close'].iloc[-1]
    print(f"📊 분석 기간 : {df.index[0].date()} ~ {df.index[-1].date()} ({len(df)} 거래일)")
    print(f"📈 SOXL 최종 종가 : ${final_price:.2f}")
    print(f"⏱️  홀딩 기간 설정 : 진입 후 {HOLD_DAYS} 거래일(≈1년) 후 청산\n")

    # ====================== 안전 범위 정의 및 조건 설정 ======================
    # MULT_EXTREME 하한을 2.50으로 설정:
    #   - MULT_FEAR 최대값(2.45)보다 반드시 커야 한다는 설계 원칙을 코드에 명시적으로 표현
    #   - 2.40~2.45 구간은 어차피 `extreme <= fear` 필터에 걸려 탐색에서 제외되므로
    #     하한을 2.40으로 두나 2.50으로 두나 탐색 결과는 동일함
    #   - config.json의 현재 운용값(2.40)은 이 파일과 역할이 다름:
    #     백테스트 → 최적값 탐색 후 config.json에 저장 → optimize_strategy.py가 AI 미세조정
    #     즉 백테스트 실행 후 config.json은 2.50 이상으로 자동 갱신됨
    SAFETY_BOUNDS = {
        "MULT_NORMAL":  (0.65, 0.85),
        "MULT_FEAR":    (1.95, 2.45),
        "MULT_EXTREME": (2.50, 2.75)   # fear 최대(2.45) 초과 보장 — 탐색 의도를 코드에 명시
    }

    # ====================== [FIX #2] 갭 보정 완전 벡터화 ======================
    # apply_gap_correction Python 루프 제거 → np.select로 전면 교체
    # 조건은 gap_ratio 기준 계단식: -3% 이상=base, -5%=-0.45, -7%=0.25, -10%=0.10, 이하=0.0
    def apply_gap_correction_vectorized(base_multipliers: np.ndarray, gap_ratio: np.ndarray) -> np.ndarray:
        return np.select(
            [
                gap_ratio >= -0.03,
                gap_ratio >= -0.05,
                gap_ratio >= -0.07,
                gap_ratio >= -0.10,
            ],
            [
                base_multipliers,  # 갭 -3% 미만 아님 → 원래 배수 유지
                0.45,
                0.25,
                0.10,
            ],
            default=0.0            # 갭 -10% 초과 하락 → 매수 중단
        )

    # ====================== 최적화 핵심 연산 함수 ======================
    def evaluate_parameters(normal, fear, extreme):
        """특정 파라미터 조합의 매수 횟수와 시그널 배열을 반환합니다."""
        multipliers = np.where(
            df['VIX'].values >= 30, extreme,
            np.where(df['VIX'].values >= 20, fear, normal)
        )

        # [FIX #2] 벡터화된 갭 보정 적용
        adj_multipliers = apply_gap_correction_vectorized(multipliers, df['Gap_Ratio'].values)

        target_prices = df['Open'].values * np.exp(-df['Daily_Sigma'].values * adj_multipliers)

        # [FIX #8] 갭 하락 시 Open < target_price 케이스 처리:
        # 실제 체결가는 min(Open, target_price) → Open이 이미 target 아래면 Open으로 체결
        exec_prices = np.minimum(df['Open'].values, target_prices)

        triggered = df['Low'].values <= target_prices

        return triggered, exec_prices

    # ==================== 수수료 상수 (FIX #1) ====================
    FEES = 0.00065  # [FIX #1] 0.065%로 통일 (현재 설정 평가 / 최적화 탐색 동일 적용)

    # ====================== 현재 설정값 기준 성과 먼저 출력 ======================
    try:
        with open("config.json", "r", encoding="utf-8") as _f:
            _cfg = json.load(_f)
        _vix_long = _cfg.get("VIX_CONFIG", {}).get("LONG", {})
        CURRENT_NORMAL  = _vix_long.get("MULT_NORMAL",  0.85)
        CURRENT_FEAR    = _vix_long.get("MULT_FEAR",    1.95)
        CURRENT_EXTREME = _vix_long.get("MULT_EXTREME", 2.40)
    except Exception:
        CURRENT_NORMAL, CURRENT_FEAR, CURRENT_EXTREME = 0.85, 1.95, 2.40
        print("⚠️ config.json 로드 실패 — 기본값(0.85/1.95/2.40) 사용")

    cur_triggered, cur_exec_prices = evaluate_parameters(CURRENT_NORMAL, CURRENT_FEAR, CURRENT_EXTREME)
    cur_pf = vbt.Portfolio.from_signals(
        close=df['Close'],
        entries=pd.Series(cur_triggered, index=df.index),
        exits=exits_series,                                    # [FIX #3] 252거래일 후 청산
        price=pd.Series(cur_exec_prices, index=df.index),     # [FIX #8] min(Open, target) 체결가
        init_cash=10000,
        fees=FEES,                                             # [FIX #1] 통일된 수수료
        freq='1D',
        accumulate=True,
        allow_partial=True
    )
    cur_calmar_raw = cur_pf.calmar_ratio()
    cur_calmar = 0.0 if (cur_calmar_raw is None or not np.isfinite(cur_calmar_raw)) else float(cur_calmar_raw)
    cur_ret_raw = cur_pf.total_return()
    cur_ret = float(cur_ret_raw * 100) if np.isfinite(cur_ret_raw) else 0.0
    mdd_raw = cur_pf.max_drawdown()
    cur_mdd = float(mdd_raw * 100) if np.isfinite(mdd_raw) else 0.0
    print("[현재 config.json 설정값]")
    print(f"   평시: {CURRENT_NORMAL:.2f} | 공포: {CURRENT_FEAR:.2f} | 극단: {CURRENT_EXTREME:.2f}")
    print(f"   총 매수 횟수 : {int(cur_triggered.sum())}회")
    print(f"   최종 수익률  : {cur_ret:+.2f}%")
    print(f"   최대 드로다운 : {cur_mdd:.2f}%")
    print(f"   Calmar Ratio : {cur_calmar:.3f}\n")

    # ====================== 안전 범위 내 최적화 탐색 ======================
    print("🔍 안전 범위 내에서 최적 배수 탐색 중...\n")

    def _steps(lo, hi, step=0.05):
        n = round((hi - lo) / step) + 1
        return np.linspace(lo, hi, n)

    normal_range  = _steps(SAFETY_BOUNDS["MULT_NORMAL"][0],  SAFETY_BOUNDS["MULT_NORMAL"][1])
    fear_range    = _steps(SAFETY_BOUNDS["MULT_FEAR"][0],    SAFETY_BOUNDS["MULT_FEAR"][1])
    extreme_range = _steps(SAFETY_BOUNDS["MULT_EXTREME"][0], SAFETY_BOUNDS["MULT_EXTREME"][1])
    # [FIX #4] extreme 범위: 2.50~2.75 → fear 최대(2.45)보다 항상 크므로 extreme <= fear 필터에 안 걸림
    # 단, 안전망으로 필터는 유지
    total_combos = len(normal_range) * len(fear_range) * len(extreme_range)
    print(f"   탐색 조합 수: {total_combos:,}개 (normal×fear×extreme = "
          f"{len(normal_range)}×{len(fear_range)}×{len(extreme_range)})\n")

    best_score = -np.inf
    best_params = None

    for normal, fear, extreme in product(normal_range, fear_range, extreme_range):
        # 극단 배수는 반드시 공포 배수보다 커야 함
        if extreme <= fear:
            continue

        triggered, exec_prices = evaluate_parameters(normal, fear, extreme)
        trade_count = int(triggered.sum())

        # 매수 횟수 필터링
        if trade_count < 170 or trade_count > 240:
            continue

        pf = vbt.Portfolio.from_signals(
            close=df['Close'],
            entries=pd.Series(triggered, index=df.index),
            exits=exits_series,                               # [FIX #3] 252거래일 후 청산
            price=pd.Series(exec_prices, index=df.index),    # [FIX #8] min(Open, target) 체결가
            init_cash=10000,
            fees=FEES,                                        # [FIX #1] 통일된 수수료
            freq='1D',
            accumulate=True,
            allow_partial=True
        )

        calmar_raw = pf.calmar_ratio()
        calmar = 0.0 if (calmar_raw is None or not np.isfinite(calmar_raw)) else float(calmar_raw)
        score = calmar * 150 + (trade_count / 2.8)

        if score > best_score:
            best_score = score
            total_ret_raw = pf.total_return()
            ret = float(total_ret_raw * 100) if np.isfinite(total_ret_raw) else 0.0
            mdd_opt_raw = pf.max_drawdown()
            mdd_opt = float(mdd_opt_raw * 100) if np.isfinite(mdd_opt_raw) else 0.0
            best_params = (normal, fear, extreme, trade_count, calmar, ret, mdd_opt)

    # ====================== 결과 출력 ======================
    if best_params:
        normal, fear, extreme, trades, calmar, ret, mdd_opt = best_params
        print("🏆 안전 범위 내 최적 배수 조합")
        print(f"   평시 : {normal:.2f}x | 공포 : {fear:.2f}x | 극단 : {extreme:.2f}x")
        print(f"   총 매수 횟수 : {trades}회 | Calmar : {calmar:.3f} | 수익률 : {ret:+.2f}% | MDD : {mdd_opt:.2f}%\n")

        print("="*65)
        print("💡 config.json에 바로 사용하세요:")
        print(f'"MULT_NORMAL": {normal:.2f},')
        print(f'"MULT_FEAR": {fear:.2f},')
        print(f'"MULT_EXTREME": {extreme:.2f}')
        print("="*65)

        answer = input("\n💾 위 최적 배수를 config.json에 저장하시겠습니까? (y/n): ").strip().lower()
        if answer == "y":
            try:
                with open("config.json", "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                # [FIX #6] LONG / SHORT 동일한 최적 배수 적용 (SHORT도 동일 전략으로 운용)
                for mode in ("LONG", "SHORT"):
                    cfg["VIX_CONFIG"][mode]["MULT_NORMAL"]  = round(float(normal),  2)
                    cfg["VIX_CONFIG"][mode]["MULT_FEAR"]    = round(float(fear),    2)
                    cfg["VIX_CONFIG"][mode]["MULT_EXTREME"] = round(float(extreme), 2)
                with open("config.json", "w", encoding="utf-8") as f:
                    json.dump(cfg, f, indent=4, ensure_ascii=False)
                print("✅ config.json LONG/SHORT 양쪽에 최적 배수가 자동 저장되었습니다.")
            except Exception as e:
                print(f"❌ config.json 저장 실패: {e}")
        else:
            print("⏭️ 저장을 건너뜁니다. config.json은 변경되지 않았습니다.")
    else:
        print("❌ 조건(매수 횟수 170~240회)을 만족하는 최적 배수 조합을 찾지 못했습니다.")

    print("\n✅ 백테스트 완료 - 매년 실행해도 안전한 범위 내에서 추천됩니다.")

if __name__ == "__main__":
    verify_long_term_hold_logic()
