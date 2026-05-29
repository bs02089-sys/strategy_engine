# =======================================================================
# SOXL_SIGMA_BACKTEST.py
# SOXL 시그마 전략 백테스트 (2026년 버전)
# - DAILY_SIGMA : 최근 252일 일간 수익률 표준편차
# - FIXED_SIGMA : 최적 배수 탐색
# - ledger.json 구조와 완전 호환
# =======================================================================

import json
import numpy as np
import yfinance as yf
import warnings
from datetime import datetime

warnings.filterwarnings('ignore')

CONFIG_PATH = "config.json"


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


# ===================================================================
# 데이터 다운로드
# ===================================================================
def download_data():
    print(f"📥 SOXL 데이터 다운로드 중...")
    df = yf.download("SOXL", period="5y", interval="1d", progress=False, auto_adjust=True)
    
    if df.empty:
        print("❌ 데이터 다운로드 실패")
        return None, None

    df = df[['Close']].copy()
    df['Prev_Close'] = df['Close'].shift(1)
    df = df.dropna()

    # DAILY_SIGMA 계산 (최근 252일)
    log_returns = np.log(df['Close'] / df['Close'].shift(1)).dropna()
    daily_sigma = round(float(log_returns.iloc[-252:].std()), 6)

    print(f"✅ {len(df)} 거래일 데이터 로드 완료")
    print(f"   최근 252일 기준 DAILY_SIGMA : {daily_sigma:.6f}\n")
    return df, daily_sigma


# ===================================================================
# 백테스트 실행
# ===================================================================
def run_backtest(df, fixed_sigma, daily_sigma):
    equity = [1.0]
    peak = 1.0
    max_dd = 0.0
    buy_count = 0
    position = None

    for _, row in df.iterrows():
        target = float(row['Prev_Close']) * np.exp(-fixed_sigma * daily_sigma)

        # 매수 신호
        if position is None and float(row['Close']) <= target * 1.005:   # 약간의 슬리피지 허용
            position = float(row['Close'])
            buy_count += 1

        # 손익 계산
        if position is not None:
            curr_val = float(row['Close']) / position
            equity.append(equity[-1] * curr_val)
            peak = max(peak, equity[-1])
            dd = (peak - equity[-1]) / peak
            max_dd = max(max_dd, dd)

    years = len(df) / 252
    total_return = (equity[-1] - 1.0) * 100
    annual_return = ((equity[-1]) ** (1 / years) - 1) * 100 if years > 0 else 0

    return {
        "total_return": round(total_return, 2),
        "annual_return": round(annual_return, 2),
        "max_drawdown": round(max_dd * 100, 2),
        "buy_count": buy_count,
        "annual_trades": round(buy_count / years),
        "years": round(years, 1),
    }


# ===================================================================
# FIXED_SIGMA 최적화
# ===================================================================
def optimize_fixed_sigma(df, daily_sigma, target_trades):
    print(f"🔍 FIXED_SIGMA 최적화 중 (목표 연간 매수: {target_trades}회)...\n")
    
    best_sigma = None
    best_diff = float("inf")
    best_result = None

    for sigma in np.arange(0.8, 3.0, 0.05):
        sigma = round(sigma, 2)
        result = run_backtest(df, sigma, daily_sigma)
        diff = abs(result["annual_trades"] - target_trades)

        if diff < best_diff:
            best_diff = diff
            best_sigma = sigma
            best_result = result

    return best_sigma, best_result


# ===================================================================
# 메인 실행
# ===================================================================
def run():
    print("======================================================================")
    print("📊 SOXL SIGMA 백테스트")
    print(f"🗓️  실행일: {datetime.now().strftime('%Y-%m-%d')}")
    print("======================================================================\n")

    df, daily_sigma = download_data()
    if df is None:
        return

    cfg = load_config()
    pos_cfg = cfg.setdefault("POSITIONS", {}).setdefault("SOXL", {})

    current_fixed_sigma = pos_cfg.get("FIXED_SIGMA", 1.5)
    quota_long = pos_cfg.get("ANNUAL_QUOTA_LONG", 21)

    print(f"📋 현재 설정")
    print(f"   FIXED_SIGMA       : {current_fixed_sigma}")
    print(f"   ANNUAL_QUOTA_LONG : {quota_long}")
    print(f"   DAILY_SIGMA       : {daily_sigma:.6f}\n")

    # 현재 파라미터 백테스트
    print("─" * 70)
    print("📈 현재 FIXED_SIGMA 백테스트 결과")
    print("─" * 70)
    result_current = run_backtest(df, current_fixed_sigma, daily_sigma)
    print(f"   총 수익률     : {result_current['total_return']:+.2f}%")
    print(f"   연평균 수익률 : {result_current['annual_return']:+.2f}%")
    print(f"   최대 낙폭     : -{result_current['max_drawdown']:.2f}%")
    print(f"   연간 매수 횟수: {result_current['annual_trades']}회\n")

    # 최적 FIXED_SIGMA 탐색
    print("─" * 70)
    print(f"🔧 최적 FIXED_SIGMA 탐색 (목표: {quota_long}회/년)")
    print("─" * 70)
    best_sigma, best_result = optimize_fixed_sigma(df, daily_sigma, quota_long)
    print(f"   최적 FIXED_SIGMA : {best_sigma}")
    print(f"   연간 매수 횟수   : {best_result['annual_trades']}회")
    print(f"   총 수익률        : {best_result['total_return']:+.2f}%")
    print(f"   최대 낙폭        : -{best_result['max_drawdown']:.2f}%\n")

    # 업데이트 여부
    answer = input("\nconfig.json에 최적 파라미터를 업데이트하시겠습니까? (y/n): ").strip().lower()

    if answer == 'y':
        pos_cfg["DAILY_SIGMA"] = round(daily_sigma, 6)
        pos_cfg["FIXED_SIGMA"] = best_sigma
        pos_cfg["LAST_BACKTEST"] = datetime.now().strftime("%Y-%m-%d")
        
        cfg["POSITIONS"]["SOXL"] = pos_cfg
        save_config(cfg)
        
        print(f"\n✅ 업데이트 완료!")
        print(f"   DAILY_SIGMA → {daily_sigma:.6f}")
        print(f"   FIXED_SIGMA → {best_sigma}")
    else:
        print("⚠️ 업데이트 취소")

    print("\n======================================================================")
    print("✅ 백테스트 완료")
    print("======================================================================")


if __name__ == "__main__":
    run()