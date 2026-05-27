import json
import numpy as np
import pandas as pd
import yfinance as yf
import vectorbt as vbt
import warnings
from datetime import datetime

warnings.filterwarnings('ignore', category=FutureWarning)


def run_backtest():
    print("======================================================================")
    print("📡 SOXL 2년 주기 + 30% 익절 백테스트")
    print("🏆 Final Tactical Backtester  (고정 세그먼트 방식)")
    print("======================================================================\n")

    # 데이터 다운로드
    print("📥 5년치 데이터 다운로드 중...")
    soxl = yf.download("SOXL", period="5y", interval="1d", progress=False, auto_adjust=True)
    vix = yf.download("^VIX", period="5y", interval="1d", progress=False, auto_adjust=True)

    if soxl.empty or vix.empty:
        print("❌ 데이터 다운로드 실패")
        return

    # 데이터 정리
    vix_close = vix['Close'].reindex(soxl.index).ffill()
    df = soxl[['Open', 'High', 'Low', 'Close']].copy()
    df['VIX'] = vix_close
    df['Prev_Close'] = df['Close'].shift(1)
    df['Gap_Ratio'] = (df['Open'] - df['Prev_Close']) / df['Prev_Close']
    df = df.dropna()

    print(f"✅ 총 {len(df)} 거래일 데이터 로드 완료\n")

    # ==================== 백테스트 설정 ====================
    SIGMA = 0.0460
    MULT_NORMAL = 1.40
    MULT_FEAR = 2.70
    MULT_EXTREME = 2.80
    TAKE_PROFIT = 0.30
    FEES = 0.00065
    DAYS_1Y = 252
    DAYS_2Y = DAYS_1Y * 2

    # 고정 세그먼트 방식 (롤링하지 않음)
    start_indices = [0, DAYS_1Y, DAYS_1Y * 2, DAYS_1Y * 3]
    segments = []

    print("📊 고정 2년 세그먼트 생성 중...")
    for start_idx in start_indices:
        end_idx = start_idx + DAYS_2Y
        if end_idx <= len(df):
            segment = df.iloc[start_idx:end_idx].copy()
            segments.append(segment)
            print(f"   • 세그먼트 {len(segments)}: {segment.index[0].date()} ~ {segment.index[-1].date()}")

    print(f"\n총 {len(segments)}개 세그먼트로 백테스트 진행\n")

    # ==================== 백테스트 실행 ====================
    results = []

    for i, seg in enumerate(segments):
        vix_val = seg['VIX'].values
        gap = seg['Gap_Ratio'].values
        price = seg['Close'].values
        open_price = seg['Open'].values

        # 멀티플라이어 계산
        base_mult = np.where(vix_val >= 30, MULT_EXTREME,
                    np.where(vix_val >= 20, MULT_FEAR, MULT_NORMAL))
        
        adj_mult = np.select(
            [gap >= -0.03, gap >= -0.05, gap >= -0.07, gap >= -0.10],
            [base_mult, 0.45, 0.25, 0.10],
            default=0.0
        )

        target = open_price * np.exp(-SIGMA * adj_mult)

        # 첫 1년 동안만 매수
        buy_window = np.arange(len(seg)) < DAYS_1Y
        entries = (price <= target) & buy_window

        # 마지막 날 강제 청산
        exits = pd.Series(False, index=seg.index)
        exits.iloc[-1] = True

        pf = vbt.Portfolio.from_signals(
            close=seg['Close'],
            entries=entries,
            exits=exits,
            init_cash=10000,
            fees=FEES,
            freq='1D',
            tp_stop=TAKE_PROFIT
        )

        results.append({
            'segment': i + 1,
            'period': f"{seg.index[0].date()} ~ {seg.index[-1].date()}",
            'return': pf.total_return() * 100,
            'mdd': pf.max_drawdown() * 100,
            'trades': int(entries.sum())
        })

    # ==================== 결과 출력 ====================
    returns = [r['return'] for r in results]
    mdds = [r['mdd'] for r in results]
    trades = [r['trades'] for r in results]

    print("======================================================================")
    print("🎯 백테스트 최종 결과 보고서")
    print("----------------------------------------------------------------------")
    for r in results:
        print(f"세그먼트 {r['segment']} ({r['period']})")
        print(f"   수익률 : {r['return']:+.2f}%")
        print(f"   MDD    : {r['mdd']:.2f}%")
        print(f"   매수횟수: {r['trades']}회\n")

    print("======================================================================")
    print(f"평균 수익률     : {np.mean(returns):+.2f}%")
    print(f"최악 MDD        : {np.min(mdds):.2f}%")
    print(f"평균 매수 횟수  : {np.mean(trades):.1f}회")
    print("======================================================================\n")

    # config.json 업데이트
    if input("\n💾 현재 설정을 config.json에 저장하시겠습니까? (y/n): ").strip().lower() == 'y':
        try:
            try:
                with open("config.json", "r", encoding="utf-8") as f:
                    cfg = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                cfg = {}

            cfg.setdefault("VIX_CONFIG", {}).setdefault("LONG", {})
            cfg["VIX_CONFIG"]["LONG"].update({
                "FIXED_SIGMA": round(float(SIGMA), 4),
                "MULT_NORMAL": round(float(MULT_NORMAL), 2),
                "MULT_FEAR": round(float(MULT_FEAR), 2),
                "MULT_EXTREME": round(float(MULT_EXTREME), 2),
                "TAKE_PROFIT_RATIO": round(float(TAKE_PROFIT), 2)
            })

            cfg.setdefault("STRATEGY", {})
            cfg["STRATEGY"].update({
                "CYCLE_YEARS": 2,
                "BUY_DURATION_DAYS": DAYS_1Y,
                "HOLD_DURATION_DAYS": DAYS_1Y
            })

            with open("config.json", "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=4, ensure_ascii=False)

            print("✅ config.json 업데이트 완료")
        except Exception as e:
            print(f"❌ config.json 저장 실패: {e}")
    else:
        print("⚠️ 저장이 취소되었습니다.")


if __name__ == "__main__":
    run_backtest()