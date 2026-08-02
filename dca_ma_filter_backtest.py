#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
═══════════════════════════════════════════════════════════════
  기존 시그마 DCA 전략 + MA 필터 오버레이 — MDD 저감용 MA 일선 탐색
═══════════════════════════════════════════════════════════════

개념 재설계:
  기존 전략(시그마 LOC DCA + 전고점 50% 청산)은 하락장에서도 계속 분할
  매수를 진행해 MDD가 깊어집니다. 여기에 MA(이동평균) 레짐 필터를 얹어
  하락 국면에서 포지션을 정리하는 방식입니다.

  MA 필터 (청산형)         : 종가가 MA 아래로 하향 이탈하면 전량 청산(현금),
                             MA 위로 재돌파하면 재진입
  재진입 방식 2가지:
    dca_reset  — 사이클마다 매수 카운터 리셋 후 DCA 분할 매수 재개 (기존 DCA 성격 유지)
    lump       — 재돌파일에 보유 현금 전액 올인 재진입 (MA 크로스 성격)

  기준선(필터 없음) 대비 각 MA 일선(5~250일)의 MDD/수익률 변화를 보고
  기존 전략의 MDD를 가장 효과적으로 낮추는 MA 일선을 찾습니다.

Usage:
  python3 dca_ma_filter_backtest.py    # 전체 스윕 (dca_reset / lump × 5~250일)
"""

import sys
import os
import numpy as np
import pandas as pd
import yfinance as yf
from datetime import date, timedelta

# ⚠️ 엔진 로직은 sigma_backtest.run_backtest_with_sell의 복제본입니다.
# 원본 엔진(매수/전고점 청산/쿨다운)을 수정할 때 이 파일도 함께 동기화하세요.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sigma_DCA_manager import (
    _calculate_volatility_from_closes,
    _calculate_loc_from_sigma,
    check_peak_sell_signal_with_cooldown,
)

# ══════════════════════════════════════════════
# Configuration (기존 엔진과 동일한 파라미터)
# ══════════════════════════════════════════════
INITIAL_CASH   = 50_000.0
BUY_AMOUNT     = 2_500.0
MAX_BUYS       = 20
LOOKBACK_DAYS  = 252
VOL_METHOD     = "EWMA"
EWMA_LAMBDA    = 0.94
SELL_PCT       = 0.50
DEFAULT_MULTIPLIER = 1.1

TEST_START  = date(2016, 8, 2)   # 최근 10년 테스트 시작
TEST_END    = date(2026, 8, 2)
DATA_START  = "2013-12-01"

MA_DAYS_SWEEP = [5, 10, 15, 20, 25, 30, 40, 50, 60, 75, 90, 100, 120, 150, 200, 250]


# ══════════════════════════════════════════════
# 기존 엔진 + MA 필터
# ══════════════════════════════════════════════
def load_entry_multiplier(ticker: str) -> float:
    """portfolio_config.json에서 해당 티커의 ENTRY_MULTIPLIER를 읽음 (없으면 1.1)."""
    try:
        import json
        with open("portfolio_config.json", "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return float(cfg.get("POSITIONS", {}).get(ticker, {}).get("ENTRY_MULTIPLIER", DEFAULT_MULTIPLIER))
    except Exception:
        return DEFAULT_MULTIPLIER


def run_dca_ma_filter(df: pd.DataFrame, ma_days: int | None = None,
                      reentry: str = "dca_reset",
                      entry_multiplier: float = DEFAULT_MULTIPLIER,
                      initial_cash: float = INITIAL_CASH,
                      buy_amount: float = BUY_AMOUNT) -> dict:
    """
    기존 시그마 DCA(전고점 50% 청산 포함) 엔진을 그대로 재현하되,
    MA 레짐 필터(청산형)를 추가한 백테스트.

    - 종가가 MA 아래로 하향 이탈 → 전량 청산 (현금 전환)
    - 종가가 MA 위로 재돌파 → 재진입
        reentry="dca_reset": 매수 카운터 리셋 후 기존 DCA 분할 매수 재개
        reentry="lump"     : 보유 현금 전액 즉시 재투입
    - MA 위 레짐에서는 기존 로직(시그마 LOC 매수 + 전고점 50% 청산) 그대로
    ma_days=None → 필터 없음 (기존 엔진과 동일)
    """
    closes = df["Close"].to_numpy(dtype=float)
    lows = df["Low"].to_numpy(dtype=float)
    dates_idx = df.index
    n = len(df)

    if ma_days is not None:
        ma_arr = df["Close"].rolling(ma_days).mean().to_numpy(dtype=float)
        ma_valid = ~np.isnan(ma_arr)
    else:
        ma_arr = np.full(n, np.nan)
        ma_valid = np.zeros(n, dtype=bool)

    # ── State ──────────────────────────────────────────────
    cash = float(initial_cash)
    shares = 0.0
    buys = 0
    total_buys = 0
    sells = 0
    total_sold = 0.0
    ma_exits = 0
    reentries = 0
    buy_log = []
    sell_log = []
    daily_values = []
    start_idx = LOOKBACK_DAYS
    last_sell_idx = None
    rolling_ath_val = 0.0

    for i in range(start_idx, n):
        prev_close = float(closes[i - 1])
        today_low = float(lows[i])
        today_close = float(closes[i])
        today_date: pd.Timestamp = dates_idx[i]

        if today_close > rolling_ath_val:
            rolling_ath_val = today_close

        # ── MA 레짐 판단 ────────────────────────────────────
        if ma_days is not None and ma_valid[i]:
            ma_ok = today_close > ma_arr[i]
            prev_ok = (closes[i - 1] > ma_arr[i - 1]) if ma_valid[i - 1] else True
            cross_down = prev_ok and not ma_ok
            cross_up = (not prev_ok) and ma_ok
        else:
            ma_ok = True
            cross_down = False
            cross_up = False

        # ── MA 필터: 하향 이탈 시 전량 청산 ─────────────────
        if ma_days is not None and cross_down and shares > 0.01:
            sell_amt = shares * today_close
            cash += sell_amt
            total_sold += sell_amt
            sells += 1
            ma_exits += 1
            sell_log.append({
                "date": today_date, "price": round(today_close, 2),
                "shares": round(shares, 4), "amount": round(sell_amt, 2),
                "cash_after": round(cash, 2), "type": "MA_EXIT",
                "reasons": f"종가 < MA{ma_days}",
            })
            shares = 0.0

        # ── MA 필터: 재돌파 시 재진입 ───────────────────────
        if ma_days is not None and cross_up and cash > 1.0:
            if reentry == "lump":
                shares += cash / today_close
                cash = 0.0
                reentries += 1
            else:  # dca_reset
                buys = 0
                reentries += 1

        # ── DCA Buy Logic (기존 로직 — MA 위 레짐에서만) ────
        if ma_ok:
            lookback_window = pd.Series(closes[i - LOOKBACK_DAYS: i])
            sigma, _ = _calculate_volatility_from_closes(
                lookback_window, LOOKBACK_DAYS, VOL_METHOD, EWMA_LAMBDA
            )
            loc_price = _calculate_loc_from_sigma(prev_close, sigma, entry_multiplier)
            triggered = today_low <= loc_price

            buy_price: float | None = min(today_close, loc_price) if triggered else None
            if triggered and cash >= buy_amount and buys < MAX_BUYS and buy_price is not None:
                buy_shares = buy_amount / buy_price
                cash -= buy_amount
                shares += buy_shares
                buys += 1
                total_buys += 1
                buy_log.append({
                    "date": today_date, "price": round(buy_price, 2),
                    "shares": round(buy_shares, 4), "amount": round(buy_amount, 2),
                    "sigma": round(sigma, 4), "loc": round(loc_price, 2),
                    "cash_remaining": round(cash, 2), "type": "BUY",
                })

        # ── Peak Sell Signal Logic (기존 로직 그대로, 50% 청산) ──
        if i >= start_idx + 21:
            lookback_closes = pd.Series(closes[max(0, i - 252): i])
            if len(lookback_closes) >= 21 and shares > 0.01:
                signal = check_peak_sell_signal_with_cooldown(
                    lookback_closes, lookback_closes,
                    last_sell_idx=last_sell_idx, current_idx=i
                )
                if signal["signal"]:
                    sell_shares = shares * SELL_PCT
                    sell_amt = sell_shares * today_close
                    shares -= sell_shares
                    cash += sell_amt
                    total_sold += sell_amt
                    sells += 1
                    last_sell_idx = i
                    sell_log.append({
                        "date": today_date, "price": round(today_close, 2),
                        "shares": round(sell_shares, 4), "amount": round(sell_amt, 2),
                        "cash_after": round(cash, 2), "type": "SELL",
                        "reasons": ", ".join(signal["reasons"]),
                        "cooldown": signal.get("cooldown", False),
                        "cooldown_remaining": signal.get("cooldown_remaining", 0),
                    })

        portfolio_value = cash + shares * today_close
        daily_values.append({
            "date": today_date, "close": today_close,
            "value": round(portfolio_value, 2),
        })

    # ── Metrics ─────────────────────────────────────────────
    dv_array = np.array([d["value"] for d in daily_values], dtype=float)
    daily_ret = dv_array[1:] / dv_array[:-1] - 1
    ret_mean = float(daily_ret.mean())
    ret_std = float(daily_ret.std())
    sharpe = float(np.sqrt(252) * ret_mean / ret_std) if ret_std > 0 else 0.0

    peak = np.maximum.accumulate(dv_array)
    dd = (dv_array - peak) / peak
    mdd = float(dd.min() * 100)

    final_val = float(dv_array[-1])
    total_ret = (final_val - initial_cash) / initial_cash * 100

    return {
        "ma_days": ma_days, "reentry": reentry if ma_days is not None else "none",
        "total_return": round(total_ret, 2),
        "final_value": round(final_val, 2),
        "mdd": round(mdd, 2),
        "sharpe": round(sharpe, 2),
        "calmar": round(total_ret / abs(mdd), 2) if mdd != 0 else 0.0,
        "buys": total_buys,
        "sells": sells,
        "ma_exits": ma_exits,
        "reentries": reentries,
        "total_sold": round(total_sold, 2),
        "remaining_cash": round(cash, 2),
        "final_shares": round(shares, 4),
        "buy_log": buy_log,
        "sell_log": sell_log,
        "daily_values": daily_values,
    }


# ══════════════════════════════════════════════
# Data
# ══════════════════════════════════════════════
def load_data(ticker: str = "TQQQ") -> pd.DataFrame:
    print(f"📥 {ticker} 데이터 다운로드 ({DATA_START} → {TEST_END.isoformat()})...")
    raw = yf.download(ticker, start=DATA_START,
                      end=(TEST_END + timedelta(days=1)).isoformat(),
                      auto_adjust=True, progress=False)
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    df = raw[["Close", "Low"]].dropna().copy()
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df[(df.index >= pd.Timestamp(TEST_START)) & (df.index <= pd.Timestamp(TEST_END))]
    print(f"   → 테스트 구간 {df.index[0].date()} ~ {df.index[-1].date()} ({len(df)} 거래일)")
    return df


# ══════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════
def main():
    ticker = "TQQQ"
    if "--ticker" in sys.argv:
        i = sys.argv.index("--ticker")
        if i + 1 < len(sys.argv):
            ticker = sys.argv[i + 1].upper()
    multiplier = load_entry_multiplier(ticker)

    df = load_data(ticker)
    years = (df.index[-1] - df.index[0]).days / 365.25

    base = run_dca_ma_filter(df, ma_days=None, entry_multiplier=multiplier)
    print("\n" + "═" * 100)
    print(f"  📊 기존 시그마 DCA + MA 필터(청산형)  |  {ticker} 10년, $50,000")
    print("═" * 100)
    print(f"  기존 엔진 파라미터: 승수 {multiplier} | 매수 ${BUY_AMOUNT:,.0f}×{MAX_BUYS} | "
          f"전고점 {SELL_PCT*100:.0f}% 청산")
    print(f"  구간: {df.index[0].date()} ~ {df.index[-1].date()} ({years:.1f}년)")
    print("─" * 100)
    print(f"\n  📌 기준선 (MA 필터 없음 = 기존 전략)")
    print(f"     수익률 {base['total_return']:+.1f}% | MDD {base['mdd']:.1f}% | "
          f"매수 {base['buys']} / 매도 {base['sells']} | 최종 ${base['final_value']:,.0f}")

    all_rows = [base]
    for reentry in ("dca_reset", "lump"):
        label = ("🔄 사이클 DCA 재개 (재돌파 시 매수 카운터 리셋 → DCA 재개)"
                 if reentry == "dca_reset"
                 else "💨 올인 재진입 (재돌파 시 현금 전액 즉시 투입)")
        print(f"\n{'═' * 100}")
        print(f"  재진입 방식: {label}")
        print("═" * 100)
        print(f"  {'MA':>6} {'수익률':>9} {'MDD':>8} {'ΔMDD':>8} {'Sharpe':>6} {'Calmar':>6} "
              f"{'매수':>5} {'매도':>4} {'MA청산':>5} {'재진입':>5} {'최종가치':>11}")
        print("  " + "─" * 96)
        for md in MA_DAYS_SWEEP:
            r = run_dca_ma_filter(df, ma_days=md, reentry=reentry, entry_multiplier=multiplier)
            all_rows.append(r)
            print(f"  {'MA' + str(md):>6} {r['total_return']:>+8.1f}% {r['mdd']:>7.1f}% "
                  f"{r['mdd'] - base['mdd']:>+7.1f}p {r['sharpe']:>6.2f} {r['calmar']:>6.1f} "
                  f"{r['buys']:>5} {r['sells']:>4} {r['ma_exits']:>5} {r['reentries']:>5} "
                  f"${r['final_value']:>9,.0f}")

    df_res = pd.DataFrame(all_rows)
    df_res.to_csv("dca_ma_filter_results.csv", index=False)
    print(f"\n   → 전체 결과 저장: dca_ma_filter_results.csv")

    # ── 분석 요약 ────────────────────────────────────────────
    print("\n" + "═" * 100)
    print("  🔍 MDD 저감 최적 MA 일선 요약 (기준선 MDD {:.1f}% vs)".format(base["mdd"]))
    print("═" * 100)
    for reentry in ("dca_reset", "lump"):
        sub = df_res[(df_res["reentry"] == reentry)].copy()
        best_mdd = sub.loc[sub["mdd"].idxmax()]
        best_calmar = sub.loc[sub["calmar"].idxmax()]
        best_ret = sub.loc[sub["total_return"].idxmax()]
        name = "사이클 DCA 재개" if reentry == "dca_reset" else "올인 재진입"
        print(f"\n  [{name}]")
        print(f"     MDD 최소  : MA{int(best_mdd['ma_days'])}일 → MDD {best_mdd['mdd']:.1f}% "
              f"(Δ {best_mdd['mdd']-base['mdd']:+.1f}p) | 수익률 {best_mdd['total_return']:+.1f}%")
        print(f"     Calmar 최고: MA{int(best_calmar['ma_days'])}일 → MDD {best_calmar['mdd']:.1f}% | "
              f"수익률 {best_calmar['total_return']:+.1f}% | Calmar {best_calmar['calmar']:.2f}")
        print(f"     수익 최고 : MA{int(best_ret['ma_days'])}일 → 수익률 {best_ret['total_return']:+.1f}% | "
              f"MDD {best_ret['mdd']:.1f}%")
    print("\n" + "═" * 100)
    print("  ✅ MA Filter Backtest Complete")
    print("═" * 100)


if __name__ == "__main__":
    main()
