# =======================================================================
# SOXL_SIGMA_BACKTEST.py
# SOXL 시그마 전략 백테스트
# - σ     : SOXL 250일 기준 일간 수익률 표준편차
# - 타점  : 전일 종가 × exp(-FIXED_SIGMA × σ)
# - 연초 실행 → DAILY_SIGMA, FIXED_SIGMA, ANNUAL_QUOTA → config.json 저장
# =======================================================================

import json
import numpy as np
import pandas as pd
import yfinance as yf
import warnings
from datetime import datetime

warnings.filterwarnings('ignore')

CONFIG_PATH    = "config.json"
BACKTEST_YEARS = 5
SIGMA_WINDOW   = 252  # σ 계산 기준 거래일


# ───────────────────────────────────────────────
# 설정 로드 / 저장
# ───────────────────────────────────────────────

def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"❌ config.json 로드 실패: {e}")
        return {}


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4, ensure_ascii=False)
        print("✅ config.json 업데이트 완료")
    except Exception as e:
        print(f"❌ config.json 저장 실패: {e}")


# ───────────────────────────────────────────────
# 데이터 다운로드
# ───────────────────────────────────────────────

def download_data():
    print(f"📥 SOXL 데이터 다운로드 중 ({BACKTEST_YEARS}년)...")
    df = yf.download("SOXL", period=f"{BACKTEST_YEARS}y", interval="1d",
                     progress=False, auto_adjust=True)
    if df.empty:
        print("❌ 데이터 다운로드 실패")
        return None, None

    df = df[['Open', 'High', 'Low', 'Close']].copy()
    df['Prev_Close'] = df['Close'].shift(1)
    df = df.dropna()

    # σ: 최근 252 거래일 기준 일간 수익률 표준편차 (고정값)
    log_returns = np.log(df['Close'] / df['Close'].shift(1)).dropna()
    daily_sigma = round(float(log_returns.iloc[-SIGMA_WINDOW:].std()), 6)

    print(f"✅ {len(df)} 거래일 데이터 로드 완료")
    print(f"   최근 {SIGMA_WINDOW}일 기준 일간 σ : {daily_sigma:.6f}\n")
    return df, daily_sigma


# ───────────────────────────────────────────────
# 백테스트 실행
# ───────────────────────────────────────────────

def run_backtest(df, fixed_sigma, daily_sigma):
    """
    타점 = 전일 종가 × exp(-fixed_sigma × daily_sigma)
    당일 저가 ≤ 타점이면 매수 체결로 간주
    """
    position  = None
    equity    = [1.0]
    peak      = 1.0
    max_dd    = 0.0
    buy_count = 0

    for _, row in df.iterrows():
        target = float(row['Prev_Close']) * np.exp(-fixed_sigma * daily_sigma)

        # 매수 체결: 보유 없음 + 당일 저가 ≤ 타점
        if position is None and float(row['Low']) <= target:
            position  = target
            buy_count += 1

        # 보유 중 손익 평가
        if position is not None:
            curr_val = float(row['Close']) / position
            equity.append(equity[-1] * curr_val)
            peak   = max(peak, equity[-1])
            dd     = (peak - equity[-1]) / peak
            max_dd = max(max_dd, dd)

    years         = len(df) / 252
    total_return  = (equity[-1] - 1.0) * 100
    annual_return = ((equity[-1]) ** (1 / years) - 1) * 100
    annual_trades = round(buy_count / years)

    return {
        "total_return":  round(total_return,  2),
        "annual_return": round(annual_return, 2),
        "max_drawdown":  round(max_dd * 100,  2),
        "buy_count":     buy_count,
        "annual_trades": annual_trades,
        "years":         round(years, 1),
    }


# ───────────────────────────────────────────────
# FIXED_SIGMA 배수 최적화
# ───────────────────────────────────────────────

def optimize_fixed_sigma(df, daily_sigma, target_trades_per_year):
    print(f"🔍 최적 FIXED_SIGMA 배수 탐색 중 (목표 연간 매수: {target_trades_per_year}회)...\n")

    best_sigma  = None
    best_diff   = float("inf")
    best_result = None

    for sigma in np.arange(0.1, 5.0, 0.1):
        sigma  = round(sigma, 1)
        result = run_backtest(df, sigma, daily_sigma)
        diff   = abs(result["annual_trades"] - target_trades_per_year)
        if diff < best_diff:
            best_diff   = diff
            best_sigma  = sigma
            best_result = result

    return best_sigma, best_result


# ───────────────────────────────────────────────
# 결과 출력
# ───────────────────────────────────────────────

def print_result(label, sigma, result):
    print(f"   FIXED_SIGMA 배수  : {sigma}")
    print(f"   기간              : {result['years']}년")
    print(f"   총 수익률         : {result['total_return']:+.2f}%")
    print(f"   연평균 수익률     : {result['annual_return']:+.2f}%")
    print(f"   최대 낙폭 (MDD)   : -{result['max_drawdown']:.2f}%")
    print(f"   총 매수 횟수      : {result['buy_count']}회")
    print(f"   연간 매수 횟수    : {result['annual_trades']}회\n")


# ───────────────────────────────────────────────
# 메인 실행
# ───────────────────────────────────────────────

def run():
    print("======================================================================")
    print("📊 SOXL_SIGMA_BACKTEST.py")
    print(f"🗓️  실행일: {datetime.now().strftime('%Y-%m-%d')}")
    print("======================================================================\n")

    df, daily_sigma = download_data()
    if df is None:
        return

    cfg     = load_config()
    pos_cfg = cfg.get("POSITIONS", {}).get("SOXL", {})

    current_fixed_sigma = pos_cfg.get("FIXED_SIGMA",        1.5)
    current_daily_sigma = pos_cfg.get("DAILY_SIGMA",  daily_sigma)
    quota_long          = pos_cfg.get("ANNUAL_QUOTA_LONG",   21)
    quota_short         = pos_cfg.get("ANNUAL_QUOTA_SHORT",  14)

    print(f"📋 현재 config 값")
    print(f"   FIXED_SIGMA        : {current_fixed_sigma}  (σ 배수)")
    print(f"   DAILY_SIGMA        : {current_daily_sigma}  (일간 σ)")
    print(f"   ANNUAL_QUOTA_LONG  : {quota_long}")
    print(f"   ANNUAL_QUOTA_SHORT : {quota_short}\n")

    # ── 현재 파라미터로 백테스트
    print("─" * 60)
    print(f"📈 현재 파라미터 백테스트 결과")
    print("─" * 60)
    result_current = run_backtest(df, current_fixed_sigma, daily_sigma)
    print_result("현재", current_fixed_sigma, result_current)

    # ── 최적 FIXED_SIGMA 탐색
    print("─" * 60)
    print(f"🔧 최적 FIXED_SIGMA 탐색 (목표: {quota_long}회/년)")
    print("─" * 60)
    best_sigma, best_result = optimize_fixed_sigma(df, daily_sigma, quota_long)
    print_result("최적", best_sigma, best_result)

    # ── 업데이트 여부 확인
    print("=" * 60)
    print(f"📌 새로운 DAILY_SIGMA : {daily_sigma}")
    print(f"📌 최적 FIXED_SIGMA   : {best_sigma}")
    answer = input("\nconfig.json에 위 파라미터를 업데이트하시겠습니까? (y/n): ").strip().lower()

    if answer == 'y':
        pos_cfg["DAILY_SIGMA"]       = daily_sigma
        pos_cfg["FIXED_SIGMA"]       = best_sigma
        pos_cfg["LAST_SIGMA_UPDATE"] = datetime.now().strftime("%Y-%m-%d")
        cfg["POSITIONS"]["SOXL"]     = pos_cfg
        save_config(cfg)
        print(f"\n   DAILY_SIGMA   → {daily_sigma}")
        print(f"   FIXED_SIGMA   → {best_sigma}")
        print(f"   업데이트 일자 → {pos_cfg['LAST_SIGMA_UPDATE']}")
    else:
        print("⚠️ 업데이트 취소됨. config.json 변경 없음.")

    print("\n======================================================================")
    print("✅ 백테스트 완료")
    print("======================================================================")


if __name__ == "__main__":
    run()