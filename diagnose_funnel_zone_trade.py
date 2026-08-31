"""
신호 필터링 단계별 진단 스크립트

param_sensitivity.py 결과가 0건에 가깝게 나올 때, 어느 단계(존 존재 여부 / 터치 여부 /
되돌림 여부 / 존 신선도 / 반전 확인 캔들)에서 대부분 걸러지는지 찾기 위한 도구.

사용법:
    python diagnose_funnel.py --ticker PLTR --period 2y --lookback-days 250
"""
import argparse

import zone_trade as zt


def diagnose(ticker, df, lookback_days, cfg):
    num_bins = int(cfg.get("num_bins", 24))
    volume_percentile = float(cfg.get("volume_percentile", 80))
    max_touches = int(cfg.get("max_touches", 2))
    wick_ratio = float(cfg.get("wick_ratio", 1.5))
    volume_multiplier = float(cfg.get("volume_multiplier", 1.2))
    vol_avg_window = int(cfg.get("vol_avg_window", 20))
    profile_lookback = int(cfg.get("profile_lookback", 120))
    close_position_threshold = float(cfg.get("close_position_threshold", 0.66))

    min_start = max(num_bins, vol_avg_window) + 5
    start_i = max(min_start, len(df) - lookback_days)
    end_i = len(df)

    counters = {
        "총_검사일": 0,
        "저항_또는_지지_존_존재": 0,
        "존에_닿음(touched)": 0,
        "닿았고_되돌림도_있음(reverted)": 0,
        "그중_존_신선도_통과(touch<max)": 0,
        "그중_반전확인캔들까지_통과(최종신호)": 0,
    }
    example_near_misses = []  # 마지막 단계 직전까지 갔다가 탈락한 사례 몇 개 기록

    for i in range(start_i, end_i):
        counters["총_검사일"] += 1

        df_hist_full = df.iloc[:i]
        df_hist = df_hist_full.tail(profile_lookback)
        today_row = df.iloc[i]
        ref_price = df["Close"].iloc[i - 1]

        edges, bin_volume = zt.build_volume_profile(df_hist, num_bins)
        zones = zt.extract_zones(edges, bin_volume, volume_percentile)
        resistance_candidates, support_candidates = zt.sorted_zone_candidates(zones, ref_price)

        if not resistance_candidates and not support_candidates:
            continue
        counters["저항_또는_지지_존_존재"] += 1

        avg_volume = df_hist["Volume"].tail(vol_avg_window).mean()
        entry_price = today_row["Close"]

        day_touched = False
        day_reverted = False
        day_fresh = False
        day_confirmed = False

        for zone in resistance_candidates:
            touched = today_row["High"] >= zone["low"]
            if not touched:
                continue
            day_touched = True
            reverted = entry_price <= zone["high"]
            if not reverted:
                continue
            day_reverted = True
            touches_before = zt.count_touches(df_hist, zone)
            if touches_before >= max_touches:
                continue
            day_fresh = True
            if zt.check_confirmation(today_row, avg_volume, "short", wick_ratio, volume_multiplier,
                                      close_position_threshold):
                day_confirmed = True
            elif len(example_near_misses) < 5:
                example_near_misses.append(
                    (str(df.index[i].date()), "resistance", zone, dict(today_row[["Open", "High", "Low", "Close", "Volume"]]), avg_volume)
                )

        for zone in support_candidates:
            touched = today_row["Low"] <= zone["high"]
            if not touched:
                continue
            day_touched = True
            reverted = entry_price >= zone["low"]
            if not reverted:
                continue
            day_reverted = True
            touches_before = zt.count_touches(df_hist, zone)
            if touches_before >= max_touches:
                continue
            day_fresh = True
            if zt.check_confirmation(today_row, avg_volume, "long", wick_ratio, volume_multiplier,
                                      close_position_threshold):
                day_confirmed = True
            elif len(example_near_misses) < 5:
                example_near_misses.append(
                    (str(df.index[i].date()), "support", zone, dict(today_row[["Open", "High", "Low", "Close", "Volume"]]), avg_volume)
                )

        if day_touched:
            counters["존에_닿음(touched)"] += 1
        if day_reverted:
            counters["닿았고_되돌림도_있음(reverted)"] += 1
        if day_fresh:
            counters["그중_존_신선도_통과(touch<max)"] += 1
        if day_confirmed:
            counters["그중_반전확인캔들까지_통과(최종신호)"] += 1

    return counters, example_near_misses


def main():
    parser = argparse.ArgumentParser(description="존 트레이딩 신호 필터 단계별 진단")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--period", default="2y")
    parser.add_argument("--lookback-days", type=int, default=250)
    parser.add_argument("--volume-percentile", type=float, default=70)
    parser.add_argument("--max-touches", type=int, default=3)
    parser.add_argument("--wick-ratio", type=float, default=1.2)
    parser.add_argument("--volume-multiplier", type=float, default=1.1)
    parser.add_argument("--close-position-threshold", type=float, default=0.66,
                         help="반전 확인 시 종가가 당일 range의 몇 %% 지점 이상이어야 하는지 (0~1)")
    args = parser.parse_args()

    df, err = zt.fetch_clean_data(args.ticker, args.period)
    if err:
        print(err)
        return

    cfg = {
        "num_bins": 24,
        "profile_lookback": 120,
        "vol_avg_window": 20,
        "volume_percentile": args.volume_percentile,
        "max_touches": args.max_touches,
        "wick_ratio": args.wick_ratio,
        "volume_multiplier": args.volume_multiplier,
        "close_position_threshold": args.close_position_threshold,
    }

    counters, near_misses = diagnose(args.ticker, df, args.lookback_days, cfg)

    print("=" * 60)
    print(f"[{args.ticker}] 신호 필터 단계별 통과 일수 (총 {counters['총_검사일']}일 중)")
    print("=" * 60)
    for label, count in counters.items():
        if label == "총_검사일":
            continue
        print(f"  {label}: {count}일")

    if near_misses:
        print("\n마지막 단계(반전 확인 캔들)에서 탈락한 사례 (최대 5개):")
        for date, side, zone, candle, avg_vol in near_misses:
            print(f"  {date} [{side}] 존={zone['low']:.2f}~{zone['high']:.2f} "
                  f"캔들={candle} 평균거래량={avg_vol:,.0f}")
    else:
        print("\n반전 확인 캔들 단계까지 도달한 사례 자체가 없습니다 "
              "(그 이전 단계에서 이미 대부분 걸러짐).")


if __name__ == "__main__":
    main()