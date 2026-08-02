#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
═══════════════════════════════════════════════════════════════
  MA 레짐 전략 — 기존 시그마 DCA + MA 레짐 필터
═══════════════════════════════════════════════════════════════

개념:
  기존 전략(시그마 LOC 분할매수 + 전고점 50% 청산)에 MA(이동평균) 레짐
  필터를 얹어 하락장에서 포지션을 정리하고, 추세 회복 시 재진입합니다.

  - MA 하향 이탈(종가 < MA) → 전량 청산 후 현금 대기
  - MA 상향 재돌파(종가 > MA) → 재진입
      reentry="lump"      : 보유 현금의 reentry_pct만큼 즉시 올인 매수
      reentry="dca_reset" : 매수 카운터 리셋 후 기존 DCA 분할매수 재개
  - MA 위 레짐: 기존 로직(시그마 LOC 매수 + 전고점 50% 청산) 그대로

티커별 기본 설정 (10년 백테스트 검증 결과 기반):
  TQQQ : MA 20일 + lump 100%  → +2,138.5% / MDD -41.2% (기준 -49.1% 대비 개선)
  SOXL : MA 250일 + dca_reset → +265.2%  / MDD -34.8% (기준 +92.6% 대비 수익 3배)
         (MDD 절감 대안: --ma 30 --reentry dca_reset → +49.1% / MDD -16.2%)
  ※ SOXL에 TQQQ식 MA20 올인을 적용하면 MDD가 -84.7%로 폭증 — 금지

Usage:
  python3 DCA_MA_strategy.py                            # TQQQ 백테스트 (기본 설정)
  python3 DCA_MA_strategy.py --ticker SOXL              # SOXL 백테스트
  python3 DCA_MA_strategy.py --ticker TQQQ --ma 20 --reentry lump --reentry-pct 1.0
  python3 DCA_MA_strategy.py --signal                   # 오늘 신호 (TQQQ)
  python3 DCA_MA_strategy.py --signal --ticker SOXL     # 오늘 신호 (SOXL)
  python3 DCA_MA_strategy.py --fee 0.001                # 수수료 0.1% 반영
  python3 DCA_MA_strategy.py --signal --discord         # 신호를 Discord로 발송 (GitHub Actions 연동)
  python3 DCA_MA_strategy.py --signal --discord --all   # 전 종목(TQQQ+SOXL) 단일 메시지로 발송
  # 참고: --all과 함께 --ma/--reentry를 주면 모든 종목에 동일하게 적용됩니다.
"""

import sys
import os
import json
import numpy as np
import pandas as pd
import yfinance as yf
from datetime import date, timedelta, datetime
from zoneinfo import ZoneInfo

# ⚠️ 엔진 로직은 sigma_DCA_manager.py의 전략(시그마 LOC 매수 + 전고점 50% 청산)을 복제한 것입니다.
# 원본 엔진(매수/전고점 청산/쿨다운) 수정 시 이 파일도 함께 동기화하세요.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sigma_DCA_manager import (
    _calculate_volatility_from_closes,
    _calculate_loc_from_sigma,
    _is_stage5_trigger,
    _parse_ath_trigger,
    calculate_loc_price,
    check_peak_sell_signal_with_cooldown,
    resolve_discord_config,
    _send_discord,
)

# ══════════════════════════════════════════════
# Configuration
# ══════════════════════════════════════════════
INITIAL_CASH   = 50_000.0
BUY_AMOUNT     = 2_500.0
MAX_BUYS       = 20
LOOKBACK_DAYS  = 252
VOL_METHOD     = "EWMA"
EWMA_LAMBDA    = 0.94
SELL_PCT       = 0.50
DEFAULT_MULTIPLIER = 1.1
TEST_START  = date(2016, 8, 2)
TEST_END    = date(2026, 8, 2)
DATA_START  = "2013-12-01"

# 티커별 기본 설정 (백테스트 검증 기반, CLI로 재정의 가능)
TICKER_DEFAULTS = {
    "TQQQ": {"ma_days": 20, "reentry": "lump", "reentry_pct": 1.0},
    "SOXL": {"ma_days": 250, "reentry": "dca_reset", "reentry_pct": None},
}


def load_config(ticker: str) -> dict:
    """portfolio_config.json에서 티커 설정 읽기 (없으면 기본값)."""
    try:
        with open("portfolio_config.json", "r", encoding="utf-8") as f:
            cfg = json.load(f)
        pos = cfg.get("POSITIONS", {}).get(ticker, {})
        return {"entry_multiplier": float(pos.get("ENTRY_MULTIPLIER", DEFAULT_MULTIPLIER))}
    except Exception:
        return {"entry_multiplier": DEFAULT_MULTIPLIER}


# ══════════════════════════════════════════════
# Backtest Engine (시그마 DCA + MA 레짐 필터)
# ══════════════════════════════════════════════
def backtest(df: pd.DataFrame, ma_days: int | None = None,
             reentry: str = "lump", reentry_pct: float = 1.0,
             entry_multiplier: float = DEFAULT_MULTIPLIER,
             initial_cash: float = INITIAL_CASH,
             buy_amount: float = BUY_AMOUNT,
             max_buys: int = MAX_BUYS,
             fee_rate: float = 0.0) -> dict:
    """
    기존 시그마 DCA(전고점 50% 청산 포함) + MA 레짐 필터 백테스트.

    ma_days=None → MA 필터 없음 (기존 전략 그대로)
    reentry="lump"      → 재돌파 시 현금의 reentry_pct만큼 올인 매수
    reentry="dca_reset" → 재돌파 시 매수 카운터 리셋 후 DCA 재개
    fee_rate → 매매 체결금액 대비 수수료(0.001 = 0.1%)
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

        # MA 레짐 판단
        if ma_days is not None and ma_valid[i]:
            ma_ok = today_close > ma_arr[i]
            prev_ok = (closes[i - 1] > ma_arr[i - 1]) if ma_valid[i - 1] else True
            cross_down = prev_ok and not ma_ok
            cross_up = (not prev_ok) and ma_ok
        else:
            ma_ok, cross_down, cross_up = True, False, False

        # MA 하향 이탈 → 전량 청산
        if ma_days is not None and cross_down and shares > 0.01:
            notional = shares * today_close
            cash += notional * (1 - fee_rate)
            total_sold += notional
            sells += 1
            ma_exits += 1
            sell_log.append({
                "date": today_date, "price": round(today_close, 2),
                "shares": round(shares, 4), "amount": round(notional, 2),
                "cash_after": round(cash, 2), "type": "MA_EXIT",
                "reasons": f"종가 < MA{ma_days}",
            })
            shares = 0.0

        # MA 상향 재돌파 → 재진입
        if ma_days is not None and cross_up and cash > 1.0:
            if reentry == "lump":
                invest = cash * reentry_pct
                shares += invest * (1 - fee_rate) / today_close
                cash -= invest
                reentries += 1
            else:  # dca_reset
                buys = 0
                reentries += 1

        # DCA 매수 (MA 위 레짐에서만)
        if ma_ok and cash >= buy_amount and buys < max_buys:
            lookback_window = pd.Series(closes[i - LOOKBACK_DAYS: i])
            sigma, _ = _calculate_volatility_from_closes(
                lookback_window, LOOKBACK_DAYS, VOL_METHOD, EWMA_LAMBDA
            )
            loc_price = _calculate_loc_from_sigma(prev_close, sigma, entry_multiplier)
            triggered = today_low <= loc_price
            if triggered:
                buy_price = min(today_close, loc_price)
                amt = min(buy_amount, cash)
                shares += amt * (1 - fee_rate) / buy_price
                cash -= amt
                buys += 1
                total_buys += 1
                buy_log.append({
                    "date": today_date, "price": round(buy_price, 2),
                    "shares": round(amt / buy_price, 4), "amount": round(amt, 2),
                    "sigma": round(sigma, 4), "loc": round(loc_price, 2),
                    "cash_remaining": round(cash, 2), "type": "BUY",
                })

        # 전고점 근접 50% 청산 (기존 로직)
        if i >= start_idx + 21 and shares > 0.01:
            lookback_closes = pd.Series(closes[max(0, i - 252): i])
            if len(lookback_closes) >= 21:
                signal = check_peak_sell_signal_with_cooldown(
                    lookback_closes, lookback_closes,
                    last_sell_idx=last_sell_idx, current_idx=i
                )
                if signal["signal"]:
                    sold_shares = shares * SELL_PCT
                    sell_notional = sold_shares * today_close
                    cash += sell_notional * (1 - fee_rate)
                    total_sold += sell_notional
                    shares -= sold_shares
                    sells += 1
                    last_sell_idx = i
                    sell_log.append({
                        "date": today_date, "price": round(today_close, 2),
                        "shares": round(sold_shares, 4),
                        "amount": round(sell_notional, 2),
                        "cash_after": round(cash, 2), "type": "SELL",
                        "reasons": ", ".join(signal["reasons"]),
                    })

        portfolio_value = cash + shares * today_close
        daily_values.append({
            "date": today_date, "close": today_close,
            "value": round(portfolio_value, 2),
        })

    # Metrics
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
        "reentry_pct": reentry_pct, "fee_rate": fee_rate,
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
# Data & Signal
# ══════════════════════════════════════════════
def _resolve_discord() -> tuple[str, str]:
    """Discord 웹훅/유저 ID — env var(DISCORD_WEBHOOK/DISCORD_USER_ID) 우선,
    portfolio_config.json 값 폴백. 비어 있으면 _send_discord가 조용히 스킵."""
    try:
        with open("portfolio_config.json", "r", encoding="utf-8") as f:
            return resolve_discord_config(json.load(f))
    except Exception:
        return resolve_discord_config({})


def load_data(ticker: str, end: date | None = None) -> pd.DataFrame:
    """티커 종가/저가 조회. end 미지정 시 오늘까지(실시간 신호용);
    백테스트는 TEST_END(고정 검증 윈도우)를 명시적으로 전달해 재현성을 유지한다."""
    if end is None:
        # NY(거래일) 기준 날짜 — GHA 러너(UTC)와의 날짜 불일치 방지
        end = datetime.now(ZoneInfo("America/New_York")).date()
    print(f"📥 {ticker} 데이터 다운로드 ({DATA_START} → {end.isoformat()})...")
    raw = yf.download(ticker, start=DATA_START,
                      end=(end + timedelta(days=1)).isoformat(),
                      auto_adjust=True, progress=False)
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    df = raw[["Close", "Low"]].dropna().copy()
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df[(df.index >= pd.Timestamp(TEST_START)) & (df.index <= pd.Timestamp(end))]
    return df


def current_signal(ticker: str, ma_days: int, reentry: str, reentry_pct: float | None,
                   entry_multiplier: float) -> dict:
    """최신 데이터 기준 현재 레짐/신호 산출."""
    df = load_data(ticker)
    closes = df["Close"]
    ma = closes.rolling(ma_days).mean()

    last_close = float(closes.iloc[-1])
    last_ma = float(ma.iloc[-1])
    prev_close = float(closes.iloc[-2])
    prev_ma = float(ma.iloc[-2])
    above_now = last_close > last_ma
    above_prev = prev_close > prev_ma
    crossed_down = above_prev and not above_now
    crossed_up = (not above_prev) and above_now

    # 현재 레짐 지속일수 (오늘부터 역방향으로 레짐이 바뀌기 전까지 세기)
    days_in_regime = 0
    for i in range(len(closes) - 1, -1, -1):
        if np.isnan(ma.iloc[i]):
            break
        if (closes.iloc[i] > ma.iloc[i]) != above_now:
            break
        days_in_regime += 1

    if crossed_down:
        action = "🔴 전량 매도 (MA 하향 이탈 → 현금 전환)"
        state = "CASH (방금 이탈)"
    elif not above_now:
        action = "🟡 현금 유지 (MA 아래 — 매수 금지, 재돌파 대기)"
        state = "CASH"
    elif crossed_up:
        if reentry == "lump":
            pct = f"{reentry_pct*100:.0f}%" if reentry_pct else "100%"
            action = f"🟢 전액 매수 (MA 상향 재돌파 → 현금의 {pct} 올인 재진입)"
        else:
            action = "🟢 분할매수 재개 (MA 상향 재돌파 → DCA 카운터 리셋)"
        state = "IN_MARKET (방금 재돌파)"
    else:
        action = "🟢 보유 유지 (MA 위 — LOC 분할매수 조건 확인)"
        state = "IN_MARKET"

    return {
        "ticker": ticker, "as_of": closes.index[-1].date(),
        "close": last_close, "ma_days": ma_days, "ma": last_ma,
        "distance_pct": (last_close / last_ma - 1) * 100 if last_ma > 0 else 0.0,
        "state": state, "action": action,
        "days_in_regime": days_in_regime,
    }


# ══════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════
def parse_args(argv: list[str]) -> dict:
    opts = {"ticker": "TQQQ", "signal": False, "discord": False, "all": False,
            "fee": 0.0, "ma": None, "reentry": None, "reentry_pct": None}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--ticker" and i + 1 < len(argv):
            opts["ticker"] = argv[i + 1].upper(); i += 2; continue
        if a == "--signal":
            opts["signal"] = True; i += 1; continue
        if a == "--discord":
            opts["discord"] = True; i += 1; continue
        if a == "--all":
            opts["all"] = True; i += 1; continue
        if a == "--fee" and i + 1 < len(argv):
            opts["fee"] = float(argv[i + 1]); i += 2; continue
        if a == "--ma" and i + 1 < len(argv):
            opts["ma"] = int(argv[i + 1]); i += 2; continue
        if a == "--reentry" and i + 1 < len(argv):
            opts["reentry"] = argv[i + 1].lower()
            if opts["reentry"] not in ("lump", "dca_reset"):
                print(f"⚠️ 잘못된 --reentry 값: {opts['reentry']} (lump 또는 dca_reset만 가능)")
                sys.exit(1)
            i += 2
            continue
        if a == "--reentry-pct" and i + 1 < len(argv):
            opts["reentry_pct"] = float(argv[i + 1]); i += 2; continue
        i += 1
    return opts


def _resolve_signal(ticker: str, opts: dict) -> tuple[dict, float | None, int, dict]:
    """티커별 신호 dict + LOC 매수가 + ATH 정보 계산. (sig, loc, ma_days, ath) 반환."""
    cfg = load_config(ticker)
    dflt = TICKER_DEFAULTS.get(ticker, {"ma_days": 20, "reentry": "lump", "reentry_pct": 1.0})
    ma_days = opts["ma"] if opts["ma"] is not None else dflt["ma_days"]
    reentry = opts["reentry"] if opts["reentry"] is not None else dflt["reentry"]
    reentry_pct = opts["reentry_pct"] if opts["reentry_pct"] is not None else dflt.get("reentry_pct", 1.0)

    sig = current_signal(ticker, ma_days, reentry, reentry_pct, cfg["entry_multiplier"])

    # LOC 매수가 — 메인 브리핑과 동일: 전일종가 × (1 - sigma × ENTRY_MULTIPLIER)
    loc: float | None = None
    try:
        with open("portfolio_config.json", "r", encoding="utf-8") as f:
            loc = calculate_loc_price(ticker, sig["close"], json.load(f))
    except Exception as e:
        print(f"⚠️ {ticker} LOC 계산 실패: {e}")

    ath = _ath_info(ticker)
    return sig, loc, ma_days, ath


def _ath_info(ticker: str) -> dict:
    """ATH 대비 MDD + 다음 비상 트리거 정보 — 비상 모드 판단용.

    ATH/MDD 계산은 sigma_DCA_manager._compute_ath_drawdown과 동일 방법론
    (1y auto_adjust=True, expanding max)을 단일 조회로 수행한다.
    다음 트리거 갭은 check_ath_dca_signals와 같은 로직(미사용 분할 중
    첫 번째의 PCT 임계값 vs 현재 DD)이다.
    """
    info: dict = {"ath": None, "ath_date": None, "dd_pct": None,
                  "next_trigger": None, "next_gap_pct": None, "all_done": False,
                  "mode": "LOC"}
    try:
        with open("portfolio_config.json", "r", encoding="utf-8") as f:
            cfg = json.load(f)
        pos = cfg.get("POSITIONS", {}).get(ticker, {})
        info["mode"] = str(pos.get("STRATEGY_MODE", "LOC")).upper()
        ath_dca = pos.get("ATH_DCA", {})
        enabled = bool(ath_dca.get("ENABLED", False))

        hist = yf.Ticker(ticker).history(period="1y", interval="1d", auto_adjust=True)
        if hist.empty:
            return info
        closes = hist["Close"].dropna()
        if len(closes) < 20:
            return info
        current_price = float(closes.iloc[-1])
        rolling_ath = float(closes.expanding().max().iloc[-1])
        if rolling_ath <= 0:
            return info
        ath_idx = closes.idxmax()
        info["ath"] = round(rolling_ath, 2)
        info["ath_date"] = ath_idx.date().strftime("%m-%d")
        # 하락률(음수) — 메인 브리핑의 "하락률 -25.74%"와 동일 부호
        info["dd_pct"] = round((current_price - rolling_ath) / rolling_ath * 100, 1)

        if not enabled:
            return info
        total_splits = int(ath_dca.get("SPLITS", 3))
        used = pos.get("ATH_DCA_USED_SPLITS", []) or []
        if not isinstance(used, list):
            used = []
        used = [int(s) for s in used]
        current_dd = abs(info["dd_pct"]) / 100.0
        next_found = False
        for i in range(1, total_splits + 1):
            if i in used:
                continue
            raw = ath_dca.get(f"TRIGGER_{i}")
            if _is_stage5_trigger(raw):
                info["next_trigger"] = f"{i}차(Stage 5 바닥)"
                next_found = True
                break
            val = _parse_ath_trigger(raw)
            if val is not None and 0 < val < 1:
                info["next_trigger"] = f"{i}차(-{val*100:.0f}%)"
                info["next_gap_pct"] = round((val - current_dd) * 100, 1)
                next_found = True
                break
            # 파싱 실패/범위 밖이면 다음 분할로 (check_ath_dca_signals와 동일)
        if not next_found:
            info["all_done"] = True
    except Exception as exc:
        print(f"  ⚠️ {ticker} ATH info failed: {exc}")
    return info


def _ath_line(ath: dict) -> str:
    """ATH 대비 낙폭 + 다음 비상 트리거 요약 (prefix 없이). 없으면 빈 문자열."""
    if not ath.get("dd_pct"):
        return ""
    line = f"${ath['ath']:.2f} ({ath['ath_date']}) 대비 {ath['dd_pct']:+.1f}%"
    if ath.get("mode") == "ATH_DCA":
        line += " | 🚨 비상 모드"
    if ath.get("next_trigger"):
        if ath.get("next_gap_pct") is not None:
            # 갭 = 추가 하락 필요 낙폭(음수) — 비상 트리거까지 "-X.X%p"로 표시
            line += f" | 비상 {ath['next_trigger']}까지 {-ath['next_gap_pct']:+.1f}%p"
        else:
            line += f" | 다음 비상 {ath['next_trigger']}"
    elif ath.get("all_done"):
        line += " | 비상 분할 완료"
    return line


def _signal_discord_block(sig: dict, loc: float | None, ma_days: int, ath: dict) -> str:
    """티커별 Discord 블록 — 종가(날짜), MA, ATH 대비 MDD, LOC 매수가, 상태, 액션."""
    loc_part = f"LOC 매수: ${loc:.2f} | " if loc else "LOC 매수: — | "
    lines = [
        f"**{sig['ticker']} MA{ma_days} 레짐 전략 신호**",
        f"종가 ${sig['close']:.2f} ({sig['as_of']}) | "
        f"MA{ma_days} ${sig['ma']:.2f} ({sig['distance_pct']:+.1f}%)",
    ]
    ath_line = _ath_line(ath)
    if ath_line:
        lines.append(f"ATH {ath_line}")
    lines.append(f"{loc_part}상태: {sig['state']} (레짐 {sig['days_in_regime']}일)")
    lines.append(f"▶ {sig['action']}")
    return "\n".join(lines)


def main():
    opts = parse_args(sys.argv[1:])
    ticker = opts["ticker"]
    cfg = load_config(ticker)
    dflt = TICKER_DEFAULTS.get(ticker, {"ma_days": 20, "reentry": "lump", "reentry_pct": 1.0})
    ma_days = opts["ma"] if opts["ma"] is not None else dflt["ma_days"]
    reentry = opts["reentry"] if opts["reentry"] is not None else dflt["reentry"]
    reentry_pct = opts["reentry_pct"] if opts["reentry_pct"] is not None else dflt.get("reentry_pct", 1.0)
    fee = opts["fee"]

    # --discord는 --signal을 암시 (단독 사용 시 자동 적용)
    if opts["discord"] and not opts["signal"]:
        print("⚠️ --discord는 --signal과 함께 사용됩니다. --signal을 자동 적용합니다.")
        opts["signal"] = True

    # ── 실시간 신호 모드 ─────────────────────────────────────
    if opts["signal"]:
        tickers = list(TICKER_DEFAULTS.keys()) if opts["all"] else [ticker]
        discord_blocks = []
        for t in tickers:
            sig, loc, md, ath = _resolve_signal(t, opts)
            print("\n" + "═" * 72)
            print(f"  📡 {sig['ticker']} MA{md} 레짐 전략 — 현재 신호")
            print("═" * 72)
            print(f"  기준일        : {sig['as_of']}")
            print(f"  종가          : ${sig['close']:.2f} ({sig['as_of']})")
            print(f"  MA{md}        : ${sig['ma']:.2f}  (종가 대비 {sig['distance_pct']:+.1f}%)")
            if loc:
                print(f"  LOC 매수      : ${loc:.2f}")
            ath_line = _ath_line(ath)
            if ath_line:
                print(f"  ATH          : {ath_line}")
            print(f"  현재 상태     : {sig['state']}")
            print(f"  레짐 지속     : {sig['days_in_regime']}일")
            print(f"\n  ▶ {sig['action']}")
            print("\n" + "═" * 72)
            discord_blocks.append(_signal_discord_block(sig, loc, md, ath))

        # ── Discord 발송 (--discord) — 전 종목 단일 메시지 ────────
        if opts["discord"] and discord_blocks:
            webhook, user_id = _resolve_discord()
            content = "\n\n".join(discord_blocks)
            print(content)  # Actions 로그 기록용 — 발송 실패/이미지 전달 시에도 확인 가능
            title = "📡 DCA MA 레짐 전략 신호 (전 종목)" if opts["all"] else f"📡 {tickers[0]} 신호"
            _send_discord(webhook, user_id, title, content)
        return

    # ── 백테스트 모드 (고정 검증 윈도우 사용) ──────────────────
    df = load_data(ticker, end=TEST_END)
    years = (df.index[-1] - df.index[0]).days / 365.25
    base = backtest(df, ma_days=None, entry_multiplier=cfg["entry_multiplier"])
    hyb = backtest(df, ma_days=ma_days, reentry=reentry, reentry_pct=reentry_pct,
                   entry_multiplier=cfg["entry_multiplier"], fee_rate=fee)

    print("\n" + "═" * 84)
    print(f"  📊 {ticker} — MA{ma_days} {reentry} 레짐 전략 백테스트  |  $50,000 / {years:.1f}년")
    print("═" * 84)
    print(f"  설정: MA {ma_days}일 | 재진입 {reentry} | "
          + (f"재진입비율 {reentry_pct*100:.0f}%" if reentry == "lump" else "DCA 재개")
          + f" | 수수료 {fee*100:.2f}%")
    print(f"  승수 {cfg['entry_multiplier']} | 매수 ${BUY_AMOUNT:,.0f}×{MAX_BUYS} | 전고점 {SELL_PCT*100:.0f}% 청산")
    print("─" * 84)
    for label, r in (("기존 전략 (MA 필터 없음)", base), ("레짐 필터 (MA 적용)", hyb)):
        print(f"\n  [{label}]")
        print(f"     총수익률 {r['total_return']:+.1f}% | MDD {r['mdd']:.1f}% | Sharpe {r['sharpe']:.2f} "
              f"| Calmar {r['calmar']:.1f}")
        print(f"     최종 ${r['final_value']:,.0f} | 매수 {r['buys']} / 매도 {r['sells']} "
              f"| MA청산 {r['ma_exits']} / 재진입 {r['reentries']}")
    print("─" * 84)
    print(f"\n  📌 요약: MDD {base['mdd']:.1f}% → {hyb['mdd']:.1f}% "
          f"({hyb['mdd'] - base['mdd']:+.1f}p) | "
          f"수익률 {base['total_return']:+.1f}% → {hyb['total_return']:+.1f}%")
    print("\n" + "═" * 84)


if __name__ == "__main__":
    main()
