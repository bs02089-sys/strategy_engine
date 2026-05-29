# =======================================================================
# SOXL_VIX_SIGMA_BACKTEST.py
# SOXL 시그마 전략 백테스트
# - 타점 = 전일 종가 × exp(-FIXED_SIGMA × σ)
#   σ     : SOXL 일간 수익률 표준편차 (과거 252일 롤링)
#   FIXED_SIGMA : 시그마 배수 (config.json, 기본 1.5)
# - 연초 실행 → ANNUAL_QUOTA, FIXED_SIGMA config.json 업데이트
# =======================================================================

import json
import numpy as np
import pandas as pd
import yfinance as yf
import warnings
from datetime import datetime

warnings.filterwarnings('ignore')

CONFIG_PATH   = "config.json"
BACKTEST_YEARS = 5


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
        return None

    df = df[['Open', 'High', 'Low', 'Close']].copy()

    # 일간 수익률 표준편차 (롤링 252일)
    df['Prev_Close'] = df['Close'].shift(1)
    df = df.dropna()

    print(f"✅ {len(df)} 거래일 데이터 로드 완료\n")
    return df


# ───────────────────────────────────────────────
# 백테스트 실행
# ───────────────────────────────────────────────

def run_backtest(df, fixed_sigma):
    """
    타점 = 전일 종가 × exp(-fixed_sigma × σ)
    당일 저가가 타점 이하로 내려오면 매수 체결로 간주
    """
    trades    = []
    position  = None
    equity    = [1.0]
    peak      = 1.0
    max_dd    = 0.0
    buy_count = 0

    for i, row in df.iterrows():
        target = float(row['Prev_Close']) * np.exp(-fixed_sigma)

        # 매수 체결: 보유 없음 + 당일 저가 ≤ 타점
        if position is None and float(row['Low']) <= target:
            position  = target
            buy_count += 1
            trades.append({"date": str(i.date()), "buy": round(target, 4)})

        # 보유 중 평가
        if position is not None:
            curr_val = float(row['Close']) / position
            equity.append(equity[-1] * curr_val)
            peak   = max(peak, equity[-1])
            dd     = (peak - equity[-1]) / peak
            max_dd = max(max_dd, dd)

    trading_days  = len(df)
    years         = trading_days / 252
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
# 시그마 배수 최적화 (연간 매수 횟수 기준)
# ───────────────────────────────────────────────

def optimize_sigma(df, target_trades_per_year):
    print(f"🔍 최적 SIGMA 배수 탐색 중 (목표 연간 매수: {target_trades_per_year}회)...\n")

    best_sigma  = None
    best_diff   = float("inf")
    best_result = None

    for sigma in np.arange(0.1, 5.0, 0.1):
        result = run_backtest(df, round(sigma, 1))
        diff   = abs(result["annual_trades"] - target_trades_per_year)
        if diff < best_diff:
            best_diff   = diff
            best_sigma  = round(sigma, 1)
            best_result = result

    return best_sigma, best_result


# ───────────────────────────────────────────────
# 메인 실행
# ───────────────────────────────────────────────

def run():
    print("======================================================================")
    print("📊 SOXL_VIX_SIGMA_BACKTEST.py")
    print(f"🗓️  실행일: {datetime.now().strftime('%Y-%m-%d')}")
    print("======================================================================\n")

    df = download_data()
    if df is None:
        return

    cfg     = load_config()
    pos_cfg = cfg.get("POSITIONS", {}).get("SOXL", {})

    current_sigma = pos_cfg.get("FIXED_SIGMA",        1.5)
    quota_long    = pos_cfg.get("ANNUAL_QUOTA_LONG",   21)
    quota_short   = pos_cfg.get("ANNUAL_QUOTA_SHORT",  14)

    print(f"📋 현재 config 값")
    print(f"   FIXED_SIGMA        : {current_sigma}  (σ 배수)")
    print(f"   ANNUAL_QUOTA_LONG  : {quota_long}")
    print(f"   ANNUAL_QUOTA_SHORT : {quota_short}\n")

    # ── 현재 시그마 배수로 백테스트
    print("─" * 60)
    print(f"📈 현재 SIGMA 배수 ({current_sigma}) 백테스트 결과")
    print("─" * 60)
    result = run_backtest(df, current_sigma)
    print(f"   기간              : {result['years']}년")
    print(f"   총 수익률         : {result['total_return']:+.2f}%")
    print(f"   연평균 수익률     : {result['annual_return']:+.2f}%")
    print(f"   최대 낙폭 (MDD)   : -{result['max_drawdown']:.2f}%")
    print(f"   총 매수 횟수      : {result['buy_count']}회")
    print(f"   연간 매수 횟수    : {result['annual_trades']}회\n")

    # ── LONG 기준 최적 시그마 배수 탐색
    print("─" * 60)
    print(f"🔧 LONG 기준 최적 SIGMA 배수 탐색 (목표: {quota_long}회/년)")
    print("─" * 60)
    best_sigma, best_result = optimize_sigma(df, target_trades_per_year=quota_long)
    print(f"   최적 SIGMA 배수   : {best_sigma}")
    print(f"   총 수익률         : {best_result['total_return']:+.2f}%")
    print(f"   연평균 수익률     : {best_result['annual_return']:+.2f}%")
    print(f"   최대 낙폭 (MDD)   : -{best_result['max_drawdown']:.2f}%")
    print(f"   총 매수 횟수      : {best_result['buy_count']}회")
    print(f"   연간 매수 횟수    : {best_result['annual_trades']}회\n")

    # ── 업데이트 여부 확인
    print("=" * 60)
    answer = input("config.json에 위 파라미터를 업데이트하시겠습니까? (y/n): ").strip().lower()

    if answer == 'y':
        pos_cfg["FIXED_SIGMA"]       = best_sigma
        pos_cfg["LAST_SIGMA_UPDATE"] = datetime.now().strftime("%Y-%m-%d")
        cfg["POSITIONS"]["SOXL"]     = pos_cfg
        save_config(cfg)
        print(f"\n   FIXED_SIGMA   → {best_sigma}")
        print(f"   업데이트 일자 → {pos_cfg['LAST_SIGMA_UPDATE']}")
    else:
        print("⚠️ 업데이트 취소됨. config.json 변경 없음.")

    print("\n======================================================================")
    print("✅ 백테스트 완료")
    print("======================================================================")


if __name__ == "__main__":
    run()
