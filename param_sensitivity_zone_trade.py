"""
파라미터 민감도 확인 스크립트

zone_trade.py의 evaluate_at_index()를 과거 여러 날짜에 반복 적용해서,
파라미터 조합별로 최근 N일 동안 LONG/SHORT 신호가 몇 번 떴는지를 세어본다.

주의: 이건 "신호가 얼마나 자주 뜨는지"만 보는 것이지, 그 신호를 따라 매매했을 때
실제로 수익이 났는지(진짜 백테스트, SL/TP 도달 여부 추적)는 확인하지 않는다.
신호 빈도만으로 좋은 파라미터인지 판단하지 말고, 참고 자료로만 활용할 것.

사용법:
    python param_sensitivity.py                     # 기본 종목/기간으로 실행
    python param_sensitivity.py --tickers PLTR TQQQ
    python param_sensitivity.py --period 2y --lookback-days 250
"""
import argparse
import itertools

import pandas as pd

import zone_trade as zt

# 스윕할 파라미터 그리드 (필요에 따라 값 추가/삭제해서 조정)
PARAM_GRID = {
    "volume_percentile": [70, 80, 90],
    "max_touches": [1, 2, 3],
    "wick_ratio": [1.2, 1.5, 2.0],
    "volume_multiplier": [1.1, 1.2, 1.5],
}

FIXED_DEFAULTS = {
    "num_bins": 24,
    "profile_lookback": 120,
    "rr_ratio": 1.5,
    "vol_avg_window": 20,
    "close_position_threshold": 0.66,
}


def run_sweep_for_ticker(ticker, df, lookback_days, grid):
    """
    df: fetch_clean_data()로 정리된 전체 OHLCV
    lookback_days: 최근 며칠에 대해 신호를 세어볼지 (분석 기간)
    """
    min_start = max(FIXED_DEFAULTS["num_bins"], FIXED_DEFAULTS["vol_avg_window"]) + 5
    start_i = max(min_start, len(df) - lookback_days)
    end_i = len(df)  # 마지막 날(오늘)까지 포함

    if start_i >= end_i:
        print(f"[{ticker}] 스윕 가능한 구간이 없습니다 (데이터 {len(df)}일, 최소 {min_start}일 필요).")
        return []

    keys = list(grid.keys())
    combos = list(itertools.product(*grid.values()))
    print(f"[{ticker}] 파라미터 조합 {len(combos)}개 x 검사일 {end_i - start_i}일 스윕 중...")

    rows = []
    for combo in combos:
        cfg = {**FIXED_DEFAULTS, **dict(zip(keys, combo))}
        long_count = 0
        short_count = 0
        for i in range(start_i, end_i):
            result = zt.evaluate_at_index(ticker, df, i, cfg)
            if result["signal"] == "LONG":
                long_count += 1
            elif result["signal"] == "SHORT":
                short_count += 1

        row = dict(zip(keys, combo))
        row["ticker"] = ticker
        row["days_checked"] = end_i - start_i
        row["long_signals"] = long_count
        row["short_signals"] = short_count
        row["total_signals"] = long_count + short_count
        row["signals_per_100_days"] = round((long_count + short_count) / (end_i - start_i) * 100, 1)
        rows.append(row)

    return rows


def main():
    parser = argparse.ArgumentParser(description="존 트레이딩 전략 파라미터 민감도 확인")
    parser.add_argument("--tickers", nargs="+", default=["PLTR", "TQQQ"])
    parser.add_argument("--period", default="2y", help="yfinance 다운로드 기간 (예: 1y, 2y, 5y)")
    parser.add_argument("--lookback-days", type=int, default=250,
                         help="최근 며칠에 대해 신호 빈도를 셀지")
    parser.add_argument("--output", default="param_sensitivity_result.csv")
    args = parser.parse_args()

    all_rows = []
    for ticker in args.tickers:
        df, err = zt.fetch_clean_data(ticker, args.period)
        if err:
            print(err)
            continue
        rows = run_sweep_for_ticker(ticker, df, args.lookback_days, PARAM_GRID)
        all_rows.extend(rows)

    if not all_rows:
        print("결과가 없습니다. 종목/기간 설정을 확인해주세요.")
        return

    result_df = pd.DataFrame(all_rows)
    result_df = result_df.sort_values(["ticker", "total_signals"], ascending=[True, False])
    result_df.to_csv(args.output, index=False, encoding="utf-8-sig")

    print("\n" + "=" * 70)
    print(f"결과 저장: {args.output}")
    print("=" * 70)

    for ticker in args.tickers:
        sub = result_df[result_df["ticker"] == ticker]
        if sub.empty:
            continue
        print(f"\n[{ticker}] 신호 많은 순 상위 5개 파라미터 조합:")
        cols = ["volume_percentile", "max_touches", "wick_ratio", "volume_multiplier",
                "long_signals", "short_signals", "signals_per_100_days"]
        print(sub[cols].head(5).to_string(index=False))

        print(f"\n[{ticker}] 신호 적은 순 하위 5개 파라미터 조합:")
        print(sub[cols].tail(5).to_string(index=False))


if __name__ == "__main__":
    main()