#!/usr/bin/env python3
"""
═══════════════════════════════════════════════════════════════
  Sigma DCA Backtest — 1-Year Walk-Forward Simulation
═══════════════════════════════════════════════════════════════

Reuses sigma_DCA_manager.py's sigma-calculation & LOC logic.

Supports two modes:
  1. Normal    : single backtest with ENTRY_MULTIPLIER from config
  2. Sweep     : run over a range of multipliers and report optimal values

Usage:
  python3 sigma_backtest.py                          # normal single run
  python3 sigma_backtest.py --sweep                   # single-period multiplier sweep
  python3 sigma_backtest.py --multi-sweep              # multi-period cross-validation
  python3 sigma_backtest.py --portfolio-sweep           # TQQQ/SOXL weight optimization
  python3 sigma_backtest.py --multi-portfolio-sweep     # multi-period weight validation
"""

import sys
import os
import numpy as np
import pandas as pd
import yfinance as yf
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sigma_DCA_manager import (
    _calculate_volatility_from_closes,
    _calculate_loc_from_sigma,
    _calculate_rsi,
    _parse_ath_trigger,
    _is_stage5_trigger,
    check_peak_sell_signal_with_cooldown,
    _SELL_ATH_RATIO as MGR_ATH_RATIO,
    _SELL_RALLY_THRESHOLD as MGR_RALLY_THRESHOLD,
    _SELL_SIGMA_RATIO as MGR_SIGMA_RATIO,
)

# ══════════════════════════════════════════════
# Configuration
# ══════════════════════════════════════════════

import json

CONFIG_PATH       = "portfolio_config.json"
INITIAL_CASH      = 50_000
TICKER            = "SOXL"
LOOKBACK_DAYS     = 252
VOL_METHOD        = "EWMA"
EWMA_LAMBDA       = 0.94
BUY_AMOUNT        = 2_500
MAX_BUYS          = 20
BACKTEST_DAYS     = 252
FETCH_BUFFER_DAYS = 60

# Peak sell signal parameters (imported from sigma_DCA_manager — single source of truth)
SELL_ATH_RATIO       = MGR_ATH_RATIO       # 전고점 threshold
SELL_RALLY_THRESHOLD = MGR_RALLY_THRESHOLD # 20일 상승률 threshold
SELL_SIGMA_RATIO     = MGR_SIGMA_RATIO     # 시그마 비율 threshold

# Sell execution
SELL_PCT             = 0.50    # 청산 비율 50%

# Stage 5 bottom-confirmation delay: the 60-day low must be at least this
# many trading days old before the Stage-5 proxy can fire.  Prevents the
# proxy from triggering on the FIRST day of a crash (when the current close
# IS the fresh 60-day low and RSI has just collapsed) and instead fires only
# after the market has stabilized near the bottom.
STAGE5_CONFIRM_DAYS = 5

# Sweep range
SWEEP_START = 0.6
SWEEP_STOP  = 3.0
SWEEP_STEP  = 0.1


def load_entry_multiplier(ticker: str | None = None) -> float:
    """Read ENTRY_MULTIPLIER from portfolio_config.json for a given ticker.
    If ticker is None, uses the global TICKER, then falls back to first position."""
    target = ticker if ticker else TICKER
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    positions = cfg.get("POSITIONS", {})
    if target in positions and "ENTRY_MULTIPLIER" in positions[target]:
        return float(positions[target]["ENTRY_MULTIPLIER"])
    # Try first available position
    for pos in positions.values():
        if "ENTRY_MULTIPLIER" in pos:
            return float(pos["ENTRY_MULTIPLIER"])
    raise KeyError(f"ENTRY_MULTIPLIER not found in {CONFIG_PATH} for {target}")


ENTRY_MULTIPLIER = load_entry_multiplier()


def load_ath_dca_config(ticker: str) -> dict:
    """Read ATH_DCA config (splits, triggers, strategy) from portfolio_config.json."""
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    positions = cfg.get("POSITIONS", {})
    if ticker in positions and "ATH_DCA" in positions[ticker]:
        ath = positions[ticker]["ATH_DCA"]
        if not isinstance(ath, dict) or not ath.get("ENABLED", False):
            raise KeyError(f"ATH_DCA not enabled for {ticker}")
        return ath
    raise KeyError(f"ATH_DCA config not found for {ticker}")


# ══════════════════════════════════════════════
# Data Fetching
# ══════════════════════════════════════════════

def fetch_data(ticker: str, end_date: date | None = None,
               include_volume: bool = False) -> pd.DataFrame:
    """Download OHLCV data for backtest ending on end_date (default: today).
    Uses auto_adjust=True (분할/배당 조정).

    By default returns only Close/Low for consistency with the standard
    ATH methodology.  When ``include_volume=True`` (needed for ATH DCA
    Stage 5 proxy), Volume is also returned.
    """
    if end_date is None:
        end_date = date.today()
    total_calendar = BACKTEST_DAYS + LOOKBACK_DAYS + FETCH_BUFFER_DAYS
    start_date = end_date - timedelta(days=total_calendar)

    print(f"📥 Downloading {ticker} ({start_date} → {end_date})...")
    stock = yf.Ticker(ticker)
    hist = stock.history(start=start_date, end=end_date, interval="1d",
                         auto_adjust=True)
    if hist.empty:
        raise RuntimeError(f"{ticker} returned no data.")

    if include_volume:
        df: pd.DataFrame = hist[['Close', 'Low', 'Volume']].copy()
        df.columns = ['Close', 'Low', 'Volume']
        df = df.dropna(subset=['Close', 'Low', 'Volume'])
    else:
        df = hist[['Close', 'Low']].copy()
        df.columns = ['Close', 'Low']
        df = df.dropna(subset=['Close', 'Low'])
    print(f"   → {len(df)} trading days loaded.")
    return df


# ══════════════════════════════════════════════
# Backtest Engine — DCA Only (original, uses Close & Low)
# ══════════════════════════════════════════════

def run_backtest(df: pd.DataFrame, entry_multiplier: float = ENTRY_MULTIPLIER,
                 verbose: bool = False,
                 initial_cash: float | None = None, buy_amount: float | None = None) -> dict:
    """
    Walk forward through df.  Returns a flat result dict with all metrics.
    If verbose=True, prints buy events to stdout.
    """
    if initial_cash is None:
        initial_cash = float(INITIAL_CASH)
    if buy_amount is None:
        buy_amount = float(BUY_AMOUNT)

    closes    = df['Close'].to_numpy(dtype=float)
    lows      = df['Low'].to_numpy(dtype=float)
    dates_idx = df.index

    cash         = float(initial_cash)
    shares       = 0.0
    buys         = 0
    trade_log    = []
    daily_values = []
    start_idx    = LOOKBACK_DAYS

    for i in range(start_idx, len(df)):
        prev_close  = float(closes[i - 1])
        today_low   = float(lows[i])
        today_close = float(closes[i])
        today_date: pd.Timestamp = dates_idx[i]  # type: ignore[assignment]

        lookback_window = pd.Series(closes[i - LOOKBACK_DAYS : i])
        sigma, _ = _calculate_volatility_from_closes(
            lookback_window, LOOKBACK_DAYS, VOL_METHOD, EWMA_LAMBDA
        )
        loc_price = _calculate_loc_from_sigma(prev_close, sigma, entry_multiplier)
        triggered = today_low <= loc_price

        buy_price: float | None = min(today_close, loc_price) if triggered else None
        buy_amt = 0.0
        buy_shares = 0.0

        if triggered and cash >= buy_amount and buys < MAX_BUYS and buy_price is not None:
            buy_shares = buy_amount / buy_price
            buy_amt = buy_amount
            cash  -= buy_amt
            shares += buy_shares
            buys += 1

            trade_log.append({
                'date': today_date, 'price': round(buy_price, 2),
                'shares': round(buy_shares, 4), 'amount': round(buy_amt, 2),
                'sigma': round(sigma, 4), 'loc': round(loc_price, 2),
                'cash_remaining': round(cash, 2),
            })
            if verbose:
                print(f"  📌 {today_date.date()} | LOC ${loc_price:.2f} hit"
                      f" | Bought {buy_shares:.2f} sh @ ${buy_price:.2f}"
                      f" | Sigma {sigma:.4f}")

        portfolio_value = cash + shares * today_close
        daily_values.append({
            'date': today_date, 'close': today_close,
            'value': round(portfolio_value, 2),
        })

    # ── Compute metrics ──────────────────────────────────────────
    dv_array   = np.array([d['value'] for d in daily_values])
    daily_ret  = dv_array[1:] / dv_array[:-1] - 1
    ret_mean = float(daily_ret.mean())
    ret_std = float(daily_ret.std())
    sharpe     = float(np.sqrt(252) * ret_mean / ret_std) if ret_std > 0 else 0.0

    peak   = np.maximum.accumulate(dv_array)
    dd     = (dv_array - peak) / peak
    mdd    = float(dd.min() * 100)

    asset_start = float(daily_values[0]['close'])
    asset_end   = float(daily_values[-1]['close'])
    buy_hold_ret = (asset_end - asset_start) / asset_start * 100

    total_invested = sum(t['amount'] for t in trade_log)
    avg_buy_price  = np.mean([t['price'] for t in trade_log]) if trade_log else 0
    final_price    = float(daily_values[-1]['close'])

    wins = sum(1 for t in trade_log if final_price > t['price']) if trade_log else 0
    win_rate = wins / len(trade_log) * 100 if trade_log else 0.0

    final_val = float(daily_values[-1]['value'])
    total_ret = (final_val - initial_cash) / initial_cash * 100

    return {
        'multiplier':      entry_multiplier,
        'total_return':    round(total_ret, 2),
        'final_value':     round(final_val, 2),
        'sharpe':          round(sharpe, 2),
        'mdd':             round(mdd, 2),
        'buy_hold_ret':    round(buy_hold_ret, 2),
        'total_buys':      buys,
        'total_invested':  round(total_invested, 2),
        'avg_buy_price':   round(avg_buy_price, 2),
        'win_rate':        round(win_rate, 1),
        'final_price':     round(final_price, 2),
        'remaining_cash':  round(cash, 2),
        'final_shares':    round(shares, 4),
        'period_start':    daily_values[0]['date'],
        'period_end':      daily_values[-1]['date'],
        'trade_log':       trade_log,
        'daily_values':    daily_values,
    }


# ══════════════════════════════════════════════
# Backtest Engine — DCA + 전고점 50% 청산
# ══════════════════════════════════════════════

def run_backtest_with_sell(df: pd.DataFrame, entry_multiplier: float = ENTRY_MULTIPLIER,
                           sell_ath_ratio: float = SELL_ATH_RATIO,
                           sell_rally_threshold: float = SELL_RALLY_THRESHOLD,
                           sell_sigma_ratio: float = SELL_SIGMA_RATIO,
                           sell_pct: float = SELL_PCT,
                           verbose: bool = False,
                           initial_cash: float | None = None,
                           buy_amount: float | None = None) -> dict:
    """
    Walk forward through df, performing both DCA buys (Sigma LOC) AND
    peak sell signals (전고점 근접 50% 청산).

    Returns a result dict with extra sell-related metrics and a combined
    trade_log including sells.
    """
    if initial_cash is None:
        initial_cash = float(INITIAL_CASH)
    if buy_amount is None:
        buy_amount = float(BUY_AMOUNT)

    closes    = df['Close'].to_numpy(dtype=float)
    lows      = df['Low'].to_numpy(dtype=float)
    dates_idx = df.index

    # ── State ──────────────────────────────────────────────────────
    cash           = float(initial_cash)
    shares         = 0.0
    buys           = 0
    sells          = 0
    total_sold     = 0.0  # total USD sold
    buy_log        = []
    sell_log       = []
    daily_values   = []
    start_idx      = LOOKBACK_DAYS
    last_sell_idx  = None  # cooldown tracker
    rolling_ath_val = 0.0  # rolling ATH (Close 기준, auto_adjust=True)

    for i in range(start_idx, len(df)):
        prev_close  = float(closes[i - 1])
        today_low   = float(lows[i])
        today_close = float(closes[i])
        today_date: pd.Timestamp = dates_idx[i]  # type: ignore[assignment]

        # ── Update rolling ATH (Close 기준, auto_adjust=True) ─────────
        if today_close > rolling_ath_val:
            rolling_ath_val = today_close

        # ── DCA Buy Logic (unchanged from original) ──────────────────
        lookback_window = pd.Series(closes[i - LOOKBACK_DAYS : i])
        sigma, _ = _calculate_volatility_from_closes(
            lookback_window, LOOKBACK_DAYS, VOL_METHOD, EWMA_LAMBDA
        )
        loc_price = _calculate_loc_from_sigma(prev_close, sigma, entry_multiplier)
        triggered = today_low <= loc_price

        buy_price: float | None = min(today_close, loc_price) if triggered else None
        buy_amt = 0.0
        buy_shares = 0.0

        if triggered and cash >= buy_amount and buys < MAX_BUYS and buy_price is not None:
            buy_shares = buy_amount / buy_price
            buy_amt = buy_amount
            cash  -= buy_amt
            shares += buy_shares
            buys += 1

            buy_log.append({
                'date': today_date, 'price': round(buy_price, 2),
                'shares': round(buy_shares, 4), 'amount': round(buy_amt, 2),
                'sigma': round(sigma, 4), 'loc': round(loc_price, 2),
                'cash_remaining': round(cash, 2),
                'type': 'BUY',
            })

        # ── Peak Sell Signal Logic (with cooldown) ──────────────────
        if i >= start_idx + 21:
            lookback_closes = pd.Series(closes[i - 252 : i]) if i >= 252 else pd.Series(closes[:i])

            if len(lookback_closes) >= 21:
                if shares > 0.01:
                    signal = check_peak_sell_signal_with_cooldown(
                        lookback_closes, lookback_closes,  # closes만 사용 (ATH 통일)
                        last_sell_idx=last_sell_idx,
                        current_idx=i
                    )

                    if signal['signal']:
                        sell_shares = shares * sell_pct
                        sell_amt = sell_shares * today_close
                        shares -= sell_shares
                        cash += sell_amt
                        total_sold += sell_amt
                        sells += 1
                        last_sell_idx = i

                        sell_log.append({
                            'date': today_date,
                            'price': round(today_close, 2),
                            'shares': round(sell_shares, 4),
                            'amount': round(sell_amt, 2),
                            'cash_after': round(cash, 2),
                            'ath_pct': signal['ath_pct'],
                            'rally_20d': signal['rally_20d'],
                            'sigma_ratio': signal['sigma_ratio'],
                            'reasons': ', '.join(signal['reasons']),
                            'type': 'SELL',
                            'cooldown': signal.get('cooldown', False),
                            'cooldown_remaining': signal.get('cooldown_remaining', 0),
                        })

        # ── Portfolio valuation ──────────────────────────────────────
        portfolio_value = cash + shares * today_close
        daily_values.append({
            'date': today_date, 'close': today_close,
            'value': round(portfolio_value, 2),
        })

    # ── Combine trade log (chronological) ───────────────────────────
    # Simply concatenate and sort by date (avoids tz-aware/naive comparison issues)
    all_events = list(buy_log) + list(sell_log)
    trade_log = sorted(all_events, key=lambda e: e['date'])

    # ── Compute metrics ──────────────────────────────────────────
    dv_array   = np.array([d['value'] for d in daily_values])
    daily_ret  = dv_array[1:] / dv_array[:-1] - 1
    ret_mean = float(daily_ret.mean())
    ret_std = float(daily_ret.std())
    sharpe     = float(np.sqrt(252) * ret_mean / ret_std) if ret_std > 0 else 0.0

    peak   = np.maximum.accumulate(dv_array)
    dd     = (dv_array - peak) / peak
    mdd    = float(dd.min() * 100)

    asset_start = float(daily_values[0]['close'])
    asset_end   = float(daily_values[-1]['close'])
    buy_hold_ret = (asset_end - asset_start) / asset_start * 100

    total_invested = sum(t['amount'] for t in buy_log)
    avg_buy_price  = np.mean([t['price'] for t in buy_log]) if buy_log else 0
    final_price    = float(daily_values[-1]['close'])

    # Win rate: compare final_price to avg_buy_price for remaining shares
    wins = sum(1 for t in buy_log if final_price > t['price']) if buy_log else 0
    win_rate = wins / len(buy_log) * 100 if buy_log else 0.0

    final_val = float(daily_values[-1]['value'])
    total_ret = (final_val - initial_cash) / initial_cash * 100

    return {
        'multiplier':        entry_multiplier,
        'total_return':      round(total_ret, 2),
        'final_value':       round(final_val, 2),
        'sharpe':            round(sharpe, 2),
        'mdd':               round(mdd, 2),
        'buy_hold_ret':      round(buy_hold_ret, 2),
        'total_buys':        buys,
        'total_sells':       sells,
        'total_sold_amount': round(total_sold, 2),
        'total_invested':    round(total_invested, 2),
        'avg_buy_price':     round(avg_buy_price, 2),
        'win_rate':          round(win_rate, 1),
        'final_price':       round(final_price, 2),
        'remaining_cash':    round(cash, 2),
        'final_shares':      round(shares, 4),
        'period_start':      daily_values[0]['date'],
        'period_end':        daily_values[-1]['date'],
        'buy_log':           buy_log,
        'sell_log':          sell_log,
        'trade_log':         trade_log,
        'daily_values':      daily_values,
    }


# ══════════════════════════════════════════════
# ATH DCA Backtest — 듀얼 모드 (LOC + ATH_DCA + Stage 5)
# ══════════════════════════════════════════════

def _is_backtest_stage5(prices: pd.Series, volumes: pd.Series) -> bool:
    """
    Simplified Stage 5 (market bottom) proxy for backtesting.

    Approximates MarketStageSystem's bottom detection with:
      1. RSI(14) < 35 (oversold territory)
      2. Price within 5%% of 60-day low
      3. Volume contraction < 85%% of 20-day MA (optional — skipped
         when volume data is unavailable, e.g. fetch_data() returns
         only Close/Low)
      4. Bottom-confirmation delay: the 60-day low must be at least
         STAGE5_CONFIRM_DAYS trading days old.  On the first day of a
         sharp crash the current close IS the fresh 60-day low and RSI
         has just collapsed, so without this guard the "bottom" fires
         immediately at the start of a crash instead of at the real
         bottom.

    When conditions align, it suggests the market has found a bottom —
    triggers the 3rd ATH DCA split.
    """
    if len(prices) < 60:
        return False

    rsi = _calculate_rsi(prices, 14).dropna()
    if rsi.empty:
        return False
    latest_rsi = float(rsi.iloc[-1])

    tail60 = prices.tail(60)
    low_60 = float(tail60.min())
    current = float(prices.iloc[-1])
    near_low = current <= low_60 * 1.05

    # Bottom-confirmation guard (#4 above)
    # Compute the low's position in the full price series directly so we
    # avoid type issues when the index is non-unique or when get_loc() returns
    # a slice/boolean mask instead of a single integer.
    low_pos_in_series = len(prices) - len(tail60) + int(np.argmin(tail60.to_numpy()))
    days_since_low = len(prices) - 1 - low_pos_in_series
    if days_since_low < STAGE5_CONFIRM_DAYS:
        return False

    # Volume contraction — graceful fallback when volume unavailable
    has_volume = len(volumes) > 20 and float(volumes.tail(20).mean()) > 0
    if has_volume:
        vol_ma20 = float(volumes.tail(20).mean())
        current_vol = float(volumes.iloc[-1])
        low_vol = current_vol < vol_ma20 * 0.85
    else:
        low_vol = True  # skip volume check when data unavailable

    return near_low and latest_rsi < 35 and low_vol


def run_backtest_ath_dca(
    df: pd.DataFrame,
    entry_multiplier: float = ENTRY_MULTIPLIER,
    ticker: str = TICKER,
    ath_dca_config: dict | None = None,
    verbose: bool = False,
    initial_cash: float | None = None,
    loc_buy_amount: float | None = None,
) -> dict:
    """
    Walk-forward backtest of the dual-mode (LOC ↔ ATH_DCA) system.

    Simulates:
      - 📗 **LOC mode** (normal): Sigma-based LOC DCA buys (same as
        run_backtest()) while ATH drawdown is below TRIGGER_1.
      - 🚨 **ATH_DCA mode** (crash): Activated when ATH drawdown hits
        TRIGGER_1. LOC buys stop; 3 equal splits are deployed:
         * 1차: at TRIGGER_1 (mode switch point)
         * 2차: at TRIGGER_2 (deeper ATH drawdown)
         * 3차: at Stage 5 proxy (_is_backtest_stage5) or if TRIGGER_3
           is a PCT trigger, at that drawdown threshold.

    Returns a result dict with all metrics PLUS mode-transition logs.
    """
    if ath_dca_config is None:
        ath_dca_config = load_ath_dca_config(ticker)
    if initial_cash is None:
        initial_cash = float(INITIAL_CASH)
    if loc_buy_amount is None:
        loc_buy_amount = float(BUY_AMOUNT)

    # Parse ATH DCA triggers
    total_splits = int(ath_dca_config.get("SPLITS", 3))
    triggers: dict[int, tuple[str, float]] = {}
    for i in range(1, total_splits + 1):
        raw = ath_dca_config.get(f"TRIGGER_{i}")
        if _is_stage5_trigger(raw):
            triggers[i] = ("STAGE5", 0.0)
        else:
            val = _parse_ath_trigger(raw)
            if val is not None and 0 < val < 1:
                triggers[i] = ("PCT", val)

    # Equal split of total capital for ATH DCA
    ath_split_amount = initial_cash / total_splits

    closes = df['Close'].to_numpy(dtype=float)
    lows = df['Low'].to_numpy(dtype=float)
    volumes_arr = df.get('Volume')
    if volumes_arr is not None:
        volumes_arr = volumes_arr.to_numpy(dtype=float)
    dates_idx = df.index

    # ── State ──────────────────────────────────────────────────────
    cash = float(initial_cash)
    shares = 0.0
    dca_buys = 0
    ath_buys = 0
    strategy_mode = "LOC"  # starts in normal mode
    used_splits: list[int] = []
    rolling_ath = 0.0
    trade_log = []
    daily_values = []
    mode_log = []  # track mode transitions
    start_idx = LOOKBACK_DAYS
    stage5_triggered_at: str | None = None  # track when Stage 5 fired

    for i in range(start_idx, len(df)):
        prev_close = float(closes[i - 1])
        today_low = float(lows[i])
        today_close = float(closes[i])
        today_date: pd.Timestamp = dates_idx[i]

        # ── Update rolling ATH ─────────────────────────────────────
        if today_close > rolling_ath:
            rolling_ath = today_close

        current_dd = ((rolling_ath - today_close) / rolling_ath) if rolling_ath > 0 else 0.0

        # ── Mode switch check (LOC → ATH_DCA) ────────────────────────
        if strategy_mode == "LOC":
            # Check TRIGGER_1 for mode switch
            if 1 in triggers and triggers[1][0] == "PCT":
                t1_threshold = triggers[1][1]
                if current_dd >= t1_threshold:
                    strategy_mode = "ATH_DCA"
                    mode_log.append({
                        'date': today_date,
                        'mode': 'ATH_DCA',
                        'reason': f"ATH DD {current_dd*100:.1f}% >= T1 ({t1_threshold*100:.0f}%)",
                        'dd_pct': round(current_dd * 100, 1),
                    })
                    if verbose:
                        print(f"  🔄 {today_date.date()} | LOC → ATH_DCA (DD={current_dd*100:.1f}%)")

        # ── In ATH_DCA mode: evaluate split triggers ────────────────
        if strategy_mode == "ATH_DCA":
            for split_num in sorted(triggers):
                if split_num in used_splits:
                    continue

                trigger_type, threshold = triggers[split_num]
                triggered = False
                trigger_label = ""

                if trigger_type == "PCT":
                    if current_dd >= threshold:
                        triggered = True
                        trigger_label = f"ATH DD {current_dd*100:.1f}% >= -{threshold*100:.0f}%"
                else:  # STAGE5
                    # Build price series for Stage 5 proxy
                    price_slice = df['Close'].iloc[:i + 1]
                    vol_slice = (df['Volume'].iloc[:i + 1]
                                 if 'Volume' in df.columns and volumes_arr is not None
                                 else pd.Series(dtype=float))
                    if _is_backtest_stage5(price_slice, vol_slice):
                        triggered = True
                        trigger_label = "Stage 5 proxy (RSI<35 + price near 60d low + vol contraction)"

                if triggered:
                    # Deploy this split
                    split_amount = ath_split_amount
                    buy_price = today_close
                    buy_shares = min(split_amount / buy_price, cash / buy_price) if buy_price > 0 else 0
                    actual_amt = buy_shares * buy_price

                    cash -= actual_amt
                    shares += buy_shares
                    ath_buys += 1
                    used_splits.append(split_num)

                    trade_log.append({
                        'date': today_date,
                        'type': f'ATH_{split_num}차',
                        'price': round(buy_price, 2),
                        'shares': round(buy_shares, 4),
                        'amount': round(actual_amt, 2),
                        'dd_pct': round(current_dd * 100, 1),
                        'trigger': trigger_label,
                        'cash_after': round(cash, 2),
                        'mode': 'ATH_DCA',
                    })

                    if trigger_type == "STAGE5":
                        stage5_triggered_at = today_date.strftime("%Y-%m-%d")

                    if verbose:
                        print(f"  🚨 {today_date.date()} | ATH {split_num}차 filled @ ${buy_price:.2f}"
                              f" | {trigger_label}")

        # ── LOC mode: normal DCA buy (only when NOT in ATH_DCA mode) ─
        if strategy_mode == "LOC":
            lookback_window = pd.Series(closes[i - LOOKBACK_DAYS: i])
            sigma, _ = _calculate_volatility_from_closes(
                lookback_window, LOOKBACK_DAYS, VOL_METHOD, EWMA_LAMBDA
            )
            loc_price = _calculate_loc_from_sigma(prev_close, sigma, entry_multiplier)
            triggered = today_low <= loc_price

            buy_price = min(today_close, loc_price) if triggered else None

            if triggered and cash >= loc_buy_amount and dca_buys < MAX_BUYS and buy_price is not None:
                buy_shares = loc_buy_amount / buy_price
                cash -= loc_buy_amount
                shares += buy_shares
                dca_buys += 1

                trade_log.append({
                    'date': today_date,
                    'type': 'LOC',
                    'price': round(buy_price, 2),
                    'shares': round(buy_shares, 4),
                    'amount': round(loc_buy_amount, 2),
                    'sigma': round(sigma, 4),
                    'loc': round(loc_price, 2),
                    'cash_after': round(cash, 2),
                    'mode': 'LOC',
                })

                if verbose:
                    print(f"  📌 {today_date.date()} | LOC filled @ ${buy_price:.2f}"
                          f" (σ={sigma:.4f}, loc=${loc_price:.2f})")

        # ── Record portfolio value ─────────────────────────────────
        portfolio_value = cash + shares * today_close
        daily_values.append({
            'date': today_date,
            'close': today_close,
            'value': round(portfolio_value, 2),
            'mode': strategy_mode,
        })

    # ── Compute metrics ──────────────────────────────────────────
    dv_array = np.array([d['value'] for d in daily_values])
    if len(dv_array) > 1:
        daily_ret = dv_array[1:] / dv_array[:-1] - 1
    else:
        daily_ret = np.array([0.0])
    ret_mean = float(daily_ret.mean())
    ret_std = float(daily_ret.std())
    sharpe = float(np.sqrt(252) * ret_mean / ret_std) if ret_std > 0 else 0.0

    peak = np.maximum.accumulate(dv_array)
    dd = (dv_array - peak) / peak
    mdd = float(dd.min() * 100)

    asset_start = float(daily_values[0]['close'])
    asset_end = float(daily_values[-1]['close'])
    buy_hold_ret = (asset_end - asset_start) / asset_start * 100

    total_invested = sum(t['amount'] for t in trade_log)
    final_price = float(daily_values[-1]['close'])

    # Win rate: percentage of buys where final price > buy price
    wins = sum(1 for t in trade_log if final_price > t['price']) if trade_log else 0
    win_rate = wins / len(trade_log) * 100 if trade_log else 0.0

    final_val = float(daily_values[-1]['value'])
    total_ret = (final_val - initial_cash) / initial_cash * 100

    return {
        'multiplier': entry_multiplier,
        'ticker': ticker,
        'total_return': round(total_ret, 2),
        'final_value': round(final_val, 2),
        'sharpe': round(sharpe, 2),
        'mdd': round(mdd, 2),
        'buy_hold_ret': round(buy_hold_ret, 2),
        'total_buys': dca_buys + ath_buys,
        'dca_buys': dca_buys,
        'ath_buys': ath_buys,
        'total_invested': round(total_invested, 2),
        'win_rate': round(win_rate, 1),
        'final_price': round(final_price, 2),
        'remaining_cash': round(cash, 2),
        'final_shares': round(shares, 4),
        'period_start': daily_values[0]['date'],
        'period_end': daily_values[-1]['date'],
        'strategy_mode': strategy_mode,
        'used_splits': used_splits,
        'stage5_triggered_at': stage5_triggered_at,
        'mode_log': mode_log,
        'trade_log': trade_log,
        'daily_values': daily_values,
    }


# ══════════════════════════════════════════════
# ATH DCA Backtest — 비상 모드 종료 (Emergency Mode Exit) 변형
# ══════════════════════════════════════════════
# 문제: 현행 듀얼 모드는 ATH_DCA(크래시)로 들어가면 수동 전환 전까지
# 그 모드에 갇혀, 1차 이후 반등하는 장에서 2차/3차 대기 중 기회비용 발생.
#
# 비상 모드 종료 규칙: 크래시 모드에 있는 동안 아래 신호가 모두 충족되면
# 자동으로 LOC(정상) 모드로 복귀해 남은 예비금의 일부를 20분할 LOC로
# 전환한다.
#   1) DD가 TRIGGER_1 × recovery_dd_ratio 이하로 좁혀짐 (회복 확인)
#   2) [선택] MA20 > MA60 (불리시 정렬) — recovery_ma_confirm
#   3) 크래시 진입 후 최소 recovery_min_days 거래일 경과 (베어트랩 필터)
# 이후 시장이 다시 무너지면 (DD >= TRIGGER_1) 자동으로 ATH_DCA 복귀,
# 사용 안 된 분할(2차/3차)은 남은 현금에서 이어서 발동한다.


def run_backtest_ath_dca_recovery(
    df: pd.DataFrame,
    entry_multiplier: float = ENTRY_MULTIPLIER,
    ticker: str = TICKER,
    ath_dca_config: dict | None = None,
    verbose: bool = False,
    initial_cash: float | None = None,
    loc_buy_amount: float | None = None,
    recovery_dd_ratio: float = 0.5,
    recovery_ma_confirm: bool = True,
    recovery_min_days: int = 30,
    recovery_loc_budget_pct: float = 0.5,
) -> dict:
    """
    Walk-forward backtest of the dual-mode system with RECOVERY RE-ENTRY.

    Same engine as run_backtest_ath_dca() plus an automatic ATH_DCA → LOC
    transition (recovery re-entry): while in crash mode, once the market
    recovers (DD narrowed to recovery_dd_ratio × TRIGGER_1, optionally MA
    bullish alignment, and at least recovery_min_days elapsed), the strategy
    switches back to LOC mode and resumes sigma-based LOC buying using a
    budget of `remaining_cash × recovery_loc_budget_pct` (the rest stays
    reserved for 2차/3차 crash splits).

    New parameters:
      - recovery_dd_ratio:      DD narrowing threshold relative to TRIGGER_1
                                (0.5 = drawdown cut in half from T1).
      - recovery_ma_confirm:    also require MA20 > MA60 (bullish alignment)
                                before re-entering LOC.
      - recovery_min_days:      minimum trading days in crash mode before a
                                recovery switch is allowed (bear-trap filter).
      - recovery_loc_budget_pct: fraction of remaining cash freed up for LOC
                                buying on re-entry (rest stays crash reserve).

    Returns the same result shape as run_backtest_ath_dca() plus
    recovery_transitions log and recovery loc buys flagged in trade_log.
    """
    if ath_dca_config is None:
        ath_dca_config = load_ath_dca_config(ticker)
    if initial_cash is None:
        initial_cash = float(INITIAL_CASH)
    if loc_buy_amount is None:
        loc_buy_amount = float(BUY_AMOUNT)

    # Parse ATH DCA triggers
    total_splits = int(ath_dca_config.get("SPLITS", 3))
    triggers: dict[int, tuple[str, float]] = {}
    for i in range(1, total_splits + 1):
        raw = ath_dca_config.get(f"TRIGGER_{i}")
        if _is_stage5_trigger(raw):
            triggers[i] = ("STAGE5", 0.0)
        else:
            val = _parse_ath_trigger(raw)
            if val is not None and 0 < val < 1:
                triggers[i] = ("PCT", val)

    t1_threshold = triggers[1][1] if (1 in triggers and triggers[1][0] == "PCT") else None
    if t1_threshold is None:
        raise ValueError("Recovery re-entry requires TRIGGER_1 as a PCT trigger.")

    ath_split_amount = initial_cash / total_splits

    closes = df['Close'].to_numpy(dtype=float)
    lows = df['Low'].to_numpy(dtype=float)
    volumes_arr = df.get('Volume')
    if volumes_arr is not None:
        volumes_arr = volumes_arr.to_numpy(dtype=float)
    dates_idx = df.index

    # ── State ──────────────────────────────────────────────────────
    cash = float(initial_cash)
    shares = 0.0
    dca_buys = 0
    ath_buys = 0
    strategy_mode = "LOC"  # starts in normal mode
    used_splits: list[int] = []
    rolling_ath = 0.0
    trade_log = []
    daily_values = []
    mode_log = []
    recovery_transitions = []
    crash_since_idx: int | None = None   # idx when ATH_DCA mode was entered
    loc_budget = 0.0                     # remaining LOC budget during recovery phase
    in_recovery_phase = False            # True = LOC buys come from recovery budget
    start_idx = LOOKBACK_DAYS
    stage5_triggered_at: str | None = None

    # Precompute MA20/MA60 for the whole series (avoid recomputing per day)
    closes_s = pd.Series(closes)
    ma20_arr = closes_s.rolling(20).mean().to_numpy(dtype=float)
    ma60_arr = closes_s.rolling(60).mean().to_numpy(dtype=float)

    for i in range(start_idx, len(df)):
        prev_close = float(closes[i - 1])
        today_low = float(lows[i])
        today_close = float(closes[i])
        today_date: pd.Timestamp = dates_idx[i]

        # ── Update rolling ATH ─────────────────────────────────────
        if today_close > rolling_ath:
            rolling_ath = today_close

        current_dd = ((rolling_ath - today_close) / rolling_ath) if rolling_ath > 0 else 0.0

        # ── Mode switch check (LOC → ATH_DCA) ──────────────────────
        if strategy_mode == "LOC":
            if current_dd >= t1_threshold:
                strategy_mode = "ATH_DCA"
                crash_since_idx = i
                loc_budget = 0.0
                in_recovery_phase = False
                mode_log.append({
                    'date': today_date, 'mode': 'ATH_DCA',
                    'reason': f"ATH DD {current_dd*100:.1f}% >= T1 ({t1_threshold*100:.0f}%)",
                    'dd_pct': round(current_dd * 100, 1),
                })
                if verbose:
                    print(f"  🔄 {today_date.date()} | LOC → ATH_DCA (DD={current_dd*100:.1f}%)")

        # ── In ATH_DCA mode: evaluate split triggers ────────────────
        if strategy_mode == "ATH_DCA":
            for split_num in sorted(triggers):
                if split_num in used_splits:
                    continue

                trigger_type, threshold = triggers[split_num]
                triggered = False
                trigger_label = ""

                if trigger_type == "PCT":
                    if current_dd >= threshold:
                        triggered = True
                        trigger_label = f"ATH DD {current_dd*100:.1f}% >= -{threshold*100:.0f}%"
                else:  # STAGE5
                    price_slice = df['Close'].iloc[:i + 1]
                    vol_slice = (df['Volume'].iloc[:i + 1]
                                 if 'Volume' in df.columns and volumes_arr is not None
                                 else pd.Series(dtype=float))
                    if _is_backtest_stage5(price_slice, vol_slice):
                        triggered = True
                        trigger_label = "Stage 5 proxy (RSI<35 + price near 60d low + vol contraction)"

                if triggered:
                    split_amount = ath_split_amount
                    buy_price = today_close
                    buy_shares = min(split_amount / buy_price, cash / buy_price) if buy_price > 0 else 0
                    actual_amt = buy_shares * buy_price

                    cash -= actual_amt
                    shares += buy_shares
                    ath_buys += 1
                    used_splits.append(split_num)

                    trade_log.append({
                        'date': today_date,
                        'type': f'ATH_{split_num}차',
                        'price': round(buy_price, 2),
                        'shares': round(buy_shares, 4),
                        'amount': round(actual_amt, 2),
                        'dd_pct': round(current_dd * 100, 1),
                        'trigger': trigger_label,
                        'cash_after': round(cash, 2),
                        'mode': 'ATH_DCA',
                        'recovery': False,
                    })

                    if trigger_type == "STAGE5":
                        stage5_triggered_at = today_date.strftime("%Y-%m-%d")

                    if verbose:
                        print(f"  🚨 {today_date.date()} | ATH {split_num}차 filled @ ${buy_price:.2f}"
                              f" | {trigger_label}")

            # ── RECOVERY RE-ENTRY: ATH_DCA → LOC ────────────────────
            # Only when not all splits used yet, enough time elapsed, and
            # the market has recovered from the crash drawdown.
            remaining_splits = [s for s in triggers if s not in used_splits]
            if (remaining_splits and crash_since_idx is not None
                    and (i - crash_since_idx) >= recovery_min_days):
                dd_recovered = current_dd <= t1_threshold * recovery_dd_ratio

                ma_ok = True
                if recovery_ma_confirm:
                    ma20 = ma20_arr[i]
                    ma60 = ma60_arr[i]
                    ma_ok = not (np.isnan(ma20) or np.isnan(ma60)) and today_close > ma20 > ma60

                if dd_recovered and ma_ok:
                    strategy_mode = "LOC"
                    loc_budget = cash * recovery_loc_budget_pct
                    in_recovery_phase = True
                    recovery_transitions.append({
                        'date': today_date,
                        'dd_pct': round(current_dd * 100, 1),
                        'reason': (f"DD {current_dd*100:.1f}% <= {recovery_dd_ratio}×T1"
                                   + (" + MA20>MA60" if recovery_ma_confirm else "")
                                   + f" (D+{i - crash_since_idx})"),
                        'loc_budget': round(loc_budget, 2),
                        'remaining_splits': list(remaining_splits),
                    })
                    mode_log.append({
                        'date': today_date, 'mode': 'LOC',
                        'reason': f"Emergency mode exit (DD {current_dd*100:.1f}%)",
                        'dd_pct': round(current_dd * 100, 1),
                    })
                    crash_since_idx = None
                    if verbose:
                        print(f"  🔄 {today_date.date()} | ATH_DCA → LOC (emergency mode exit, DD={current_dd*100:.1f}%)")

        # ── LOC mode: normal DCA buy (only when NOT in ATH_DCA mode) ─
        if strategy_mode == "LOC":
            lookback_window = pd.Series(closes[i - LOOKBACK_DAYS: i])
            sigma, _ = _calculate_volatility_from_closes(
                lookback_window, LOOKBACK_DAYS, VOL_METHOD, EWMA_LAMBDA
            )
            loc_price = _calculate_loc_from_sigma(prev_close, sigma, entry_multiplier)
            triggered = today_low <= loc_price

            buy_price = min(today_close, loc_price) if triggered else None

            if triggered and buy_price is not None and dca_buys < MAX_BUYS:
                # Recovery phase: ONLY spend the remaining loc_budget — once
                # it is exhausted, LOC buying STOPS so the crash reserve for
                # 2차/3차 splits is preserved. Normal phase: buy freely.
                if in_recovery_phase:
                    amt = min(loc_buy_amount, loc_budget, cash)
                else:
                    amt = min(loc_buy_amount, cash)

                if amt >= 1.0:
                    buy_shares = amt / buy_price
                    cash -= amt
                    loc_budget = max(0.0, loc_budget - amt)
                    shares += buy_shares
                    dca_buys += 1

                    trade_log.append({
                        'date': today_date,
                        'type': 'LOC',
                        'price': round(buy_price, 2),
                        'shares': round(buy_shares, 4),
                        'amount': round(amt, 2),
                        'sigma': round(sigma, 4),
                        'loc': round(loc_price, 2),
                        'cash_after': round(cash, 2),
                        'mode': 'LOC',
                        'recovery': in_recovery_phase,
                    })

                    if verbose:
                        print(f"  📌 {today_date.date()} | LOC filled @ ${buy_price:.2f}"
                              f" (σ={sigma:.4f}, loc=${loc_price:.2f})")

        # ── Record portfolio value ─────────────────────────────────
        portfolio_value = cash + shares * today_close
        daily_values.append({
            'date': today_date,
            'close': today_close,
            'value': round(portfolio_value, 2),
            'mode': strategy_mode,
        })

    # ── Compute metrics (identical to base engine) ─────────────────
    dv_array = np.array([d['value'] for d in daily_values])
    if len(dv_array) > 1:
        daily_ret = dv_array[1:] / dv_array[:-1] - 1
    else:
        daily_ret = np.array([0.0])
    ret_mean = float(daily_ret.mean())
    ret_std = float(daily_ret.std())
    sharpe = float(np.sqrt(252) * ret_mean / ret_std) if ret_std > 0 else 0.0

    peak = np.maximum.accumulate(dv_array)
    dd = (dv_array - peak) / peak
    mdd = float(dd.min() * 100)

    asset_start = float(daily_values[0]['close'])
    asset_end = float(daily_values[-1]['close'])
    buy_hold_ret = (asset_end - asset_start) / asset_start * 100

    total_invested = sum(t['amount'] for t in trade_log)
    final_price = float(daily_values[-1]['close'])
    wins = sum(1 for t in trade_log if final_price > t['price']) if trade_log else 0
    win_rate = wins / len(trade_log) * 100 if trade_log else 0.0

    final_val = float(daily_values[-1]['value'])
    total_ret = (final_val - initial_cash) / initial_cash * 100

    return {
        'multiplier': entry_multiplier,
        'ticker': ticker,
        'total_return': round(total_ret, 2),
        'final_value': round(final_val, 2),
        'sharpe': round(sharpe, 2),
        'mdd': round(mdd, 2),
        'buy_hold_ret': round(buy_hold_ret, 2),
        'total_buys': dca_buys + ath_buys,
        'dca_buys': dca_buys,
        'ath_buys': ath_buys,
        'total_invested': round(total_invested, 2),
        'win_rate': round(win_rate, 1),
        'final_price': round(final_price, 2),
        'remaining_cash': round(cash, 2),
        'final_shares': round(shares, 4),
        'period_start': daily_values[0]['date'],
        'period_end': daily_values[-1]['date'],
        'strategy_mode': strategy_mode,
        'used_splits': used_splits,
        'stage5_triggered_at': stage5_triggered_at,
        'mode_log': mode_log,
        'trade_log': trade_log,
        'daily_values': daily_values,
        # Recovery-specific additions
        'recovery_transitions': recovery_transitions,
        'recovery_loc_buys': sum(1 for t in trade_log if t.get('recovery') and t['type'] == 'LOC'),
        'loc_budget_pct': recovery_loc_budget_pct,
        'recovery_dd_ratio': recovery_dd_ratio,
        'recovery_ma_confirm': recovery_ma_confirm,
        'recovery_min_days': recovery_min_days,
    }


def print_ath_dca_report(r: dict):
    """Print a detailed report for the ATH DCA dual-mode backtest."""
    p_start: pd.Timestamp = r['period_start']
    p_end: pd.Timestamp = r['period_end']

    print("\n")
    print("═" * 72)
    print("  🚀 ATH DCA 듀얼 모드 백테스트 리포트")
    print("═" * 72)
    print(f"  Ticker    : {r['ticker']}")
    print(f"  Capital   : ${INITIAL_CASH:,}")
    print(f"  Period    : {p_start.date()}  →  {p_end.date()}")
    print(f"  Buy size  : ${BUY_AMOUNT:,} (LOC) / ${INITIAL_CASH/3:,.0f} (ATH split)")
    print("─" * 72)

    print(f"\n  📈 Performance")
    print(f"     Final Portfolio     : ${r['final_value']:,.2f}")
    print(f"     Total Return        : {r['total_return']:+.2f}%")
    print(f"     Buy & Hold          : {r['buy_hold_ret']:+.2f}%")
    print(f"     Alpha vs B&H        : {r['total_return'] - r['buy_hold_ret']:+.2f}%")
    print(f"     Sharpe Ratio        : {r['sharpe']}")
    print(f"     Max Drawdown        : {r['mdd']:.2f}%")

    print(f"\n  📋 Activity")
    print(f"     LOC fills           : {r['dca_buys']}")
    print(f"     ATH DCA fills       : {r['ath_buys']}")
    print(f"     Total invested      : ${r['total_invested']:,.2f}")
    print(f"     Remaining cash      : ${r['remaining_cash']:,.2f}")
    print(f"     Win rate            : {r['win_rate']:.1f}%")

    print(f"\n  📋 ATH DCA Status")
    print(f"     Final mode          : {r['strategy_mode']}")
    print(f"     Used splits         : {r['used_splits']}")
    print(f"     Stage 5 triggered   : {r['stage5_triggered_at'] or 'No'}")

    if r['mode_log']:
        print(f"\n  🔄 Mode Transitions")
        for m in r['mode_log']:
            print(f"     {m['date'].strftime('%Y-%m-%d')}"
                  f" → {m['mode']} ({m['reason']})")

    if r['trade_log']:
        print(f"\n  📝 Trade Log")
        print(f"  {'Date':<14} {'Type':<10} {'Price':>8} {'Shares':>10} {'Amount':>9} {'Detail':<30}")
        print(f"  {'─'*14} {'─'*10} {'─'*8} {'─'*10} {'─'*9} {'─'*30}")
        for t in r['trade_log']:
            detail = t.get('trigger', '') or f"σ={t.get('sigma', 0):.4f}"
            print(f"  {t['date'].strftime('%Y-%m-%d'):<14}"
                  f" {t['type']:<10}"
                  f" ${t['price']:>6.2f}"
                  f" {t['shares']:>10.2f}"
                  f" ${t['amount']:>7,.0f}"
                  f" {detail:<30}")

    # Monthly breakdown
    monthly = {}
    for d in r['daily_values']:
        mk = d['date'].strftime('%Y-%m')
        monthly.setdefault(mk, []).append(d['value'])
    if len(monthly) > 1:
        print(f"\n  📅 Monthly Portfolio Value")
        print(f"  {'Month':<8} {'Start':>10} {'End':>10} {'Return':>8}")
        print(f"  {'─'*8} {'─'*10} {'─'*10} {'─'*8}")
        for m in sorted(monthly):
            vals = monthly[m]
            s, e = vals[0], vals[-1]
            print(f"  {m:<8} ${s:>7,.0f} ${e:>7,.0f} {(e-s)/s*100:>+7.2f}%")

    print("\n" + "═" * 72)
    print("  ✅ ATH DCA Backtest Complete")
    print("═" * 72)


def print_recovery_comparison_report(r_base: dict, r_rec: dict):
    """Side-by-side comparison: 현행 (3차 대기) vs 비상 모드 종료 (Emergency Mode Exit)."""
    p_start: pd.Timestamp = r_base['period_start']
    p_end: pd.Timestamp = r_base['period_end']

    print("\n")
    print("═" * 88)
    print(f"  📊 {r_base['ticker']} — 현행 vs 비상 모드 종료 성능 비교")
    print("═" * 88)
    print(f"  Capital : ${INITIAL_CASH:,}")
    print(f"  Period  : {p_start.date()}  →  {p_end.date()}")
    print("─" * 88)

    # ── Key Metrics Table ───────────────────────────────────────────
    def _diff(a: float, b: float) -> float:
        return b - a

    metrics = [
        ("Total Return", f"{r_base['total_return']:+.2f}%", f"{r_rec['total_return']:+.2f}%",
         _diff(r_base['total_return'], r_rec['total_return'])),
        ("Final Value", f"${r_base['final_value']:,.2f}", f"${r_rec['final_value']:,.2f}",
         _diff(r_base['final_value'], r_rec['final_value'])),
        ("Sharpe Ratio", f"{r_base['sharpe']}", f"{r_rec['sharpe']}",
         _diff(r_base['sharpe'], r_rec['sharpe'])),
        ("Max Drawdown", f"{r_base['mdd']:.2f}%", f"{r_rec['mdd']:.2f}%",
         -_diff(r_base['mdd'], r_rec['mdd'])),  # positive = improvement
        ("Buy & Hold", f"{r_base['buy_hold_ret']:+.2f}%", f"{r_rec['buy_hold_ret']:+.2f}%", 0),
        ("Alpha vs B&H", f"{r_base['total_return'] - r_base['buy_hold_ret']:+.2f}%",
         f"{r_rec['total_return'] - r_rec['buy_hold_ret']:+.2f}%",
         _diff(r_base['total_return'] - r_base['buy_hold_ret'],
               r_rec['total_return'] - r_rec['buy_hold_ret'])),
    ]

    print(f"  {'Metric':<16} {'현행 (3차 대기)':>16} {'비상 모드 종료':>16} {'Diff':>9}")
    print(f"  {'─'*16} {'─'*16} {'─'*16} {'─'*9}")
    for name, base_val, rec_val, diff in metrics:
        diff_str = f"{diff:+.2f}" if isinstance(diff, (int, float)) and abs(diff) > 0.005 else ""
        arrow = ""
        if isinstance(diff, (int, float)) and abs(diff) > 0.005 and name not in ("Buy & Hold",):
            arrow = " 🟢" if diff > 0 else " 🔴"
        print(f"  {name:<16} {base_val:>16} {rec_val:>16} {diff_str:>7}{arrow}")

    # ── Activity ────────────────────────────────────────────────────
    print(f"\n  {'Activity':<18} {'현행':>12} {'비상 모드 종료':>12}")
    print(f"  {'─'*18} {'─'*12} {'─'*12}")
    print(f"  {'LOC fills':<18} {r_base['dca_buys']:>12} {r_rec['dca_buys']:>12}")
    print(f"  {'ATH DCA fills':<18} {r_base['ath_buys']:>12} {r_rec['ath_buys']:>12}")
    print(f"  {'Used splits':<18} {str(r_base['used_splits']):>12} {str(r_rec['used_splits']):>12}")
    print(f"  {'Total invested':<18} ${r_base['total_invested']:>9,.0f} ${r_rec['total_invested']:>9,.0f}")
    print(f"  {'Remaining cash':<18} ${r_base['remaining_cash']:>9,.0f} ${r_rec['remaining_cash']:>9,.0f}")
    print(f"  {'Final shares':<18} {r_base['final_shares']:>12.1f} {r_rec['final_shares']:>12.1f}")

    if r_rec.get('recovery_transitions'):
        print(f"\n  🔄 비상 모드 종료 전환 ({len(r_rec['recovery_transitions'])}회)")
        for t in r_rec['recovery_transitions']:
            print(f"     {t['date'].strftime('%Y-%m-%d')} | DD {t['dd_pct']:+.1f}% | {t['reason']}"
                  f" | LOC budget ${t['loc_budget']:,.0f}")
        budget = r_rec.get('recovery_loc_budget_pct', 0.0)
        print(f"  📋 Emergency mode exit budget : {budget*100:.0f}% of cash on exit"
              f" | LOC fills from budget: {r_rec.get('recovery_loc_buys', 0)}"
              f" | ⚠️ 예산 소진 후엔 크래시 예비금 보존을 위해 LOC 매수 중단")
    print(f"\n  🔎 해석:")
    ret_diff = r_rec['total_return'] - r_base['total_return']
    if abs(ret_diff) < 0.01:
        print("     두 전략의 수익이 동일 (회복 전환이 발생하지 않았거나 무영향)")
    elif ret_diff > 0:
        print(f"     비상 모드 종료가 현행 대비 수익 {ret_diff:+.2f}%p 우위")
    else:
        print(f"     비상 모드 종료가 현행 대비 수익 {ret_diff:+.2f}%p 열위 (드라이 파우더 소진 효과)")
    print("\n" + "═" * 88)
    print("  ✅ Recovery Comparison Complete")
    print("═" * 88)


# ══════════════════════════════════════════════
# Single-run Reporting
# ══════════════════════════════════════════════

def print_report(r: dict):
    print("\n")
    print("═" * 62)
    print("  📊 Sigma DCA Backtest Report")
    print("═" * 62)
    print(f"  Ticker    : {TICKER}")
    print(f"  Strategy  : EWMA (λ={EWMA_LAMBDA}) × {r['multiplier']} LOC")
    print(f"  Capital   : ${INITIAL_CASH:,}")
    p_start: pd.Timestamp = r['period_start']
    p_end: pd.Timestamp = r['period_end']
    print(f"  Period    : {p_start.date()}  →  {p_end.date()}")
    print(f"  Buy size  : ${BUY_AMOUNT:,} / trigger (max {MAX_BUYS}×)")
    print("─" * 62)

    print(f"\n  📈 Performance")
    print(f"     Final Portfolio  : ${r['final_value']:,.2f}")
    print(f"     Total Return     : {r['total_return']:+.2f}%")
    print(f"     Buy & Hold (SOXL): {r['buy_hold_ret']:+.2f}%")
    print(f"     Alpha vs B&H     : {r['total_return'] - r['buy_hold_ret']:+.2f}%")
    print(f"     Sharpe Ratio     : {r['sharpe']}")
    print(f"     Max Drawdown     : {r['mdd']:.2f}%")

    print(f"\n  📋 DCA Activity")
    print(f"     Total LOC Fills  : {r['total_buys']}")
    print(f"     Total Invested   : ${r['total_invested']:,.2f}")
    print(f"     Avg Buy Price    : ${r['avg_buy_price']:.2f}")
    print(f"     Current Price    : ${r['final_price']:.2f}")
    print(f"     Win Rate         : {r['win_rate']:.1f}%")
    print(f"     Remaining Cash   : ${r['remaining_cash']:,.2f}")
    if r['final_shares'] > 0:
        unrealized = (r['final_price'] - r['avg_buy_price']) * r['final_shares']
        print(f"     Shares Held      : {r['final_shares']:.4f}")
        print(f"     Unrealized P&L   : ${unrealized:+,.2f}")

    if 'total_sells' in r:
        print(f"\n  📋 Peak Sell Activity")
        print(f"     Total Sells      : {r['total_sells']}")
        print(f"     Total Sold ($)   : ${r.get('total_sold_amount', 0):,.2f}")

    if r['trade_log']:
        print(f"\n  📝 Combined Trade Log")
        print(f"  {'Date':<14} {'Type':<6} {'Price':>8} {'Shares':>10} {'Amount':>9}")
        print(f"  {'─'*14} {'─'*6} {'─'*8} {'─'*10} {'─'*9}")
        for t in r['trade_log']:
            ttype = t.get('type', 'BUY')
            print(f"  {t['date'].strftime('%Y-%m-%d'):<14}"
                  f" {ttype:<6}"
                  f" ${t['price']:>6.2f} {abs(t['shares']):>10.2f}"
                  f" ${t['amount']:>7,.0f}")
            if ttype == 'SELL':
                reasons = t.get('reasons', '')
                if reasons:
                    print(f"  {'':14} {'↳':>6} {reasons}")

    # Monthly breakdown
    monthly = {}
    for d in r['daily_values']:
        mk = d['date'].strftime('%Y-%m')
        monthly.setdefault(mk, []).append(d['value'])
    if len(monthly) > 1:
        print(f"\n  📅 Monthly Portfolio Value")
        print(f"  {'Month':<8} {'Start':>10} {'End':>10} {'Return':>8}")
        print(f"  {'─'*8} {'─'*10} {'─'*10} {'─'*8}")
        for m in sorted(monthly):
            vals = monthly[m]
            s, e = vals[0], vals[-1]
            print(f"  {m:<8} ${s:>7,.0f} ${e:>7,.0f} {(e-s)/s*100:>+7.2f}%")

    print("\n" + "═" * 62)
    print("  ✅ Backtest Complete")
    print("═" * 62)


# ══════════════════════════════════════════════
# Comparison Report — DCA vs DCA+PeakSell
# ══════════════════════════════════════════════

def print_comparison_report(r_dca: dict, r_sell: dict):
    """Print side-by-side comparison of DCA-only vs DCA+PeakSell."""
    print("\n")
    print("═" * 80)
    print("  📊 DCA vs DCA+전고점50%청산 — 성능 비교")
    print("═" * 80)
    print(f"  Ticker    : {TICKER}")
    print(f"  Capital   : ${INITIAL_CASH:,}")
    print(f"  Multiplier: ×{r_dca['multiplier']} LOC")
    print(f"  Sell Trig : ATH≥{SELL_ATH_RATIO*100:.0f}% + 20일≥{SELL_RALLY_THRESHOLD*100:.0f}% + Sigma≥{SELL_SIGMA_RATIO:.1f}x | 청산 {SELL_PCT*100:.0f}%")
    p_start: pd.Timestamp = r_dca['period_start']
    p_end: pd.Timestamp = r_dca['period_end']
    print(f"  Period    : {p_start.date()}  →  {p_end.date()}")
    print("─" * 80)

    # ── Key Metrics Table ───────────────────────────────────────────
    metrics = [
        ("Total Return",      f"{r_dca['total_return']:+.2f}%", f"{r_sell['total_return']:+.2f}%",
         r_sell['total_return'] - r_dca['total_return']),
        ("Final Value",       f"${r_dca['final_value']:,.2f}", f"${r_sell['final_value']:,.2f}",
         r_sell['final_value'] - r_dca['final_value']),
        ("Sharpe Ratio",      f"{r_dca['sharpe']}", f"{r_sell['sharpe']}",
         r_sell['sharpe'] - r_dca['sharpe']),
        ("Max Drawdown",      f"{r_dca['mdd']:.2f}%", f"{r_sell['mdd']:.2f}%",
         -(r_sell['mdd'] - r_dca['mdd'])),  # positive = improvement
        ("Buy & Hold",        f"{r_dca['buy_hold_ret']:+.2f}%", f"{r_sell['buy_hold_ret']:+.2f}%",
         0),
        ("Alpha vs B&H",      f"{r_dca['total_return'] - r_dca['buy_hold_ret']:+.2f}%",
         f"{r_sell['total_return'] - r_sell['buy_hold_ret']:+.2f}%",
         (r_sell['total_return'] - r_sell['buy_hold_ret']) - (r_dca['total_return'] - r_dca['buy_hold_ret'])),
    ]

    print(f"  {'Metric':<20} {'DCA Only':>14} {'DCA+Sell':>14} {'Diff':>10}")
    print(f"  {'─'*20} {'─'*14} {'─'*14} {'─'*10}")
    for name, dca_val, sell_val, diff in metrics:
        diff_str = f"{diff:+.2f}" if isinstance(diff, (int, float)) else ""
        # Color indicator
        arrow = ""
        if isinstance(diff, (int, float)) and abs(diff) > 0.01:
            if diff > 0 and name not in ("Buy & Hold",):
                arrow = " 🟢"
            elif diff < 0 and name not in ("Buy & Hold",):
                arrow = " 🔴"
        print(f"  {name:<20} {dca_val:>14} {sell_val:>14} {diff_str:>8}{arrow}")

    # ── Activity ────────────────────────────────────────────────────
    print(f"\n  {'Activity':<20} {'DCA Only':>14} {'DCA+Sell':>14}")
    print(f"  {'─'*20} {'─'*14} {'─'*14}")
    print(f"  {'Total Buys':<20} {r_dca['total_buys']:>14} {r_sell['total_buys']:>14}")
    print(f"  {'Total Sells (50%)':<20} {'0':>14} {r_sell.get('total_sells', 0):>14}")
    print(f"  {'Total Invested':<20} ${r_dca['total_invested']:>11,.2f} ${r_sell['total_invested']:>11,.2f}")

    # ── Sell Events Details ─────────────────────────────────────────
    if r_sell.get('sell_log'):
        print(f"\n  📝 Sell Events Details")
        print(f"  {'Date':<14} {'Price':>8} {'Sold $':>10} {'ATH%':>7} {'20dR%':>7} {'SigRx':>7} {'Reasons'}")
        print(f"  {'─'*14} {'─'*8} {'─'*10} {'─'*7} {'─'*7} {'─'*7} {'─'*20}")
        for s in r_sell['sell_log']:
            print(f"  {s['date'].strftime('%Y-%m-%d'):<14}"
                  f" ${s['price']:>6.2f}"
                  f" ${s['amount']:>8,.0f}"
                  f" {s['ath_pct']:>5.0f}%"
                  f" {s['rally_20d']:>5.0f}%"
                  f" {s['sigma_ratio']:>5.1f}x"
                  f" {s['reasons']}")

    print("\n" + "═" * 80)
    print("  ✅ Comparison Complete")
    print("═" * 80)


# ══════════════════════════════════════════════
# Multiplier Sweep
# ══════════════════════════════════════════════

def run_multiplier_sweep(df: pd.DataFrame):
    """Run backtest for each multiplier in [SWEEP_START .. SWEEP_STOP]."""
    multipliers = np.arange(SWEEP_START, SWEEP_STOP + 1e-9, SWEEP_STEP)
    results = []

    print(f"\n🧪 Multiplier Sensitivity Sweep")
    print(f"   Range : {SWEEP_START} → {SWEEP_STOP} (step {SWEEP_STEP})")
    print(f"   Tests : {len(multipliers)}")
    print("─" * 106)
    header = (f"  {'Mult':>5} | {'Return':>8} | {'Final $':>10} | {'Sharpe':>7}"
              f" | {'MDD':>7} | {'Buys':>5} | {'Avg$':>7} | {'WinRate':>7} | {'Cash':>8}")
    print(header)
    print("  " + "─" * 100)

    for mult in multipliers:
        r = run_backtest(df, entry_multiplier=round(mult, 1), verbose=False)
        results.append(r)
        print(f"  {mult:>5.1f} | {r['total_return']:>+7.2f}%"
              f" | ${r['final_value']:>8,.0f} | {r['sharpe']:>7.2f}"
              f" | {r['mdd']:>6.2f}% | {r['total_buys']:>3d}"
              f" | ${r['avg_buy_price']:>6.2f} | {r['win_rate']:>5.1f}%"
              f" | ${r['remaining_cash']:>7,.0f}")

    # ── Find optimum by several metrics ────────────────────────────
    print("─" * 106)

    best_return   = max(results, key=lambda r: r['total_return'])
    best_sharpe   = max(results, key=lambda r: r['sharpe'])
    best_mdd      = min(results, key=lambda r: r['mdd'])   # least negative
    best_winrate  = max(results, key=lambda r: r['win_rate'])
    best_alpha    = max(results, key=lambda r: r['total_return'] - r['buy_hold_ret'])

    print(f"\n  🏆 Optimum by metric:")
    print(f"     Max Return     : multiplier={best_return['multiplier']:.1f}"
          f"  → {best_return['total_return']:+.2f}%  (Sharpe {best_return['sharpe']})")
    print(f"     Max Sharpe     : multiplier={best_sharpe['multiplier']:.1f}"
          f"  → {best_sharpe['sharpe']:.2f}  (Return {best_sharpe['total_return']:+.2f}%)")
    print(f"     Min Drawdown   : multiplier={best_mdd['multiplier']:.1f}"
          f"  → {best_mdd['mdd']:.2f}%")
    print(f"     Max Win Rate   : multiplier={best_winrate['multiplier']:.1f}"
          f"  → {best_winrate['win_rate']:.1f}%")
    print(f"     Max Alpha      : multiplier={best_alpha['multiplier']:.1f}"
          f"  → {best_alpha['total_return'] - best_alpha['buy_hold_ret']:+.2f}%")

    # ── Show old 1.41 baseline for historical reference ───────────
    baseline = next((r for r in results if abs(r['multiplier'] - 1.41) < 0.05), None)
    if baseline:
        print(f"\n  📌 Previous baseline (mult=1.41, for reference):")
        print(f"     Return {baseline['total_return']:+.2f}%"
              f" | Sharpe {baseline['sharpe']:.2f}"
              f" | MDD {baseline['mdd']:.2f}%"
              f" | WinRate {baseline['win_rate']:.1f}%")
    print("\n" + "═" * 62)
    print("  ✅ Sweep Complete")
    print("═" * 62)

    return results


# ══════════════════════════════════════════════
# Multi-Period Sweep (Generalization Test)
# ══════════════════════════════════════════════

def run_multi_period_sweep():
    """
    Run multiplier sweep across multiple historical 1-year periods to test
    whether a single multiplier generalizes across market regimes.
    """
    today = date.today()
    periods = [
        (today - timedelta(days=365), today, "🔥 Current (Strong Bull)"),
        (today - timedelta(days=730), today - timedelta(days=365), "📈 Bull"),
        (today - timedelta(days=1095), today - timedelta(days=730), "📊 Recovery"),
        (today - timedelta(days=1460), today - timedelta(days=1095), "📉 Bear Bottom"),
        (today - timedelta(days=1825), today - timedelta(days=1460), "💥 Bear Crash"),
    ]

    multipliers = np.arange(SWEEP_START, SWEEP_STOP + 1e-9, SWEEP_STEP)

    print(f"\n🌍 Multi-Period Multiplier Generalization Test")
    print(f"   Testing {len(periods)} market regimes × {len(multipliers)} multipliers")
    print("=" * 120)

    all_results = {}  # period_label -> {multiplier -> result}

    for start_dt, end_dt, label in periods:
        print(f"\n📅 Period: {label}  ({start_dt} → {end_dt})")
        print("─" * 120)

        df = fetch_data(TICKER, end_date=end_dt)
        # Trim to only cover the intended backtest window (compare by date
        # to avoid timezone-aware vs timezone-naive dtype mismatch)
        df = df[df.index.date <= end_dt]  # type: ignore[operator]

        period_results = []
        for mult in multipliers:
            mult_rounded = round(mult, 1)
            r = run_backtest(df, entry_multiplier=mult_rounded, verbose=False)  # type: ignore[arg-type]
            period_results.append(r)

        all_results[label] = {r['multiplier']: r for r in period_results}

        # Print summary for this period
        best_by_return = max(period_results, key=lambda r: r['total_return'])
        best_by_sharpe = max(period_results, key=lambda r: r['sharpe'])

        # Also show baseline 0.9, 1.41, 1.618
        for ref_mult in [0.9, 1.41, 1.618]:
            ref = next((r for r in period_results if abs(r['multiplier'] - ref_mult) < 0.05), None)
            if ref:
                print(f"   mult={ref_mult:5.2f} → Return {ref['total_return']:>+7.2f}%"
                      f" | Sharpe {ref['sharpe']:>5.2f} | MDD {ref['mdd']:>6.2f}%"
                      f" | Buys {ref['total_buys']:>2d} | Win {ref['win_rate']:>5.1f}%")

        print(f"   🏆 Best Return   : mult={best_by_return['multiplier']:.1f}"
              f" → {best_by_return['total_return']:+.2f}% (Sharpe {best_by_return['sharpe']})")
        print(f"   🏆 Best Sharpe   : mult={best_by_sharpe['multiplier']:.1f}"
              f" → {best_by_sharpe['sharpe']:.2f} (Return {best_by_sharpe['total_return']:+.2f}%)")

    # ── Cross-period ranking ───────────────────────────────────────
    print("\n" + "═" * 120)
    print("  🏆 Cross-Period Consistency Ranking")
    print("  (lower rank = more consistent across all periods)")
    print("─" * 120)

    # For each multiplier, calculate its average rank across all periods
    mult_rankings = {}
    for mult_raw in multipliers:
        mult = round(float(mult_raw), 1)
        ranks = []
        for label in all_results:
            period_results = list(all_results[label].values())
            # Rank by return (1 = best)
            sorted_by_return = sorted(period_results, key=lambda r: -r['total_return'])
            rank = next(i + 1 for i, r in enumerate(sorted_by_return)
                        if abs(r['multiplier'] - mult) < 0.05)
            ranks.append(rank)

        avg_rank = np.mean(ranks)
        std_rank = np.std(ranks)
        mult_rankings[mult] = (avg_rank, std_rank)

    # Show top 10 most consistent multipliers
    print(f"  {'Rank':<6} {'Mult':>6} {'AvgRank':>8} {'StdRank':>8} {'Period1':>8} {'Period2':>8} {'Period3':>8} {'Period4':>8} {'Period5':>8}")
    print(f"  {'─'*6} {'─'*6} {'─'*8} {'─'*8} {'─'*8} {'─'*8} {'─'*8} {'─'*8} {'─'*8}")

    sorted_mults = sorted(mult_rankings.items(), key=lambda x: x[1][0])
    for rank_idx, (mult, (avg_rank, std_rank)) in enumerate(sorted_mults[:12], 1):
        # Get return for each period
        returns = []
        for label in all_results:
            r = all_results[label][mult]
            returns.append(f"{r['total_return']:>+7.2f}%")
        print(f"  {rank_idx:<6} {mult:>5.1f}  {avg_rank:>7.2f}  {std_rank:>7.2f}  "
              + "  ".join(returns))

    # ── Find the overall best ──────────────────────────────────────
    # Best by average rank
    best_consistent = sorted_mults[0]
    print(f"\n  🥇 Most Consistent Multiplier: {best_consistent[0]:.1f}"
          f" (avg rank {best_consistent[1][0]:.2f}, std {best_consistent[1][1]:.2f})")

    # Best average return across periods
    mult_avg_returns = {}
    for mult_raw in multipliers:
        mult = round(float(mult_raw), 1)
        rets = [all_results[label][mult]['total_return'] for label in all_results]
        if rets:
            mult_avg_returns[mult] = np.mean(rets)
    best_avg_return: tuple[float, float] = max(mult_avg_returns.items(), key=lambda x: x[1])  # type: ignore[arg-type]
    print(f"  🥇 Best Avg Return  : multiplier={best_avg_return[0]:.1f}"
          f" → avg {best_avg_return[1]:+.2f}% across all periods")

    # Best by Sharpe consistency
    mult_avg_sharpe = {}
    for mult_raw in multipliers:
        mult = round(float(mult_raw), 1)
        shs = [all_results[label][mult]['sharpe'] for label in all_results]
        if shs:
            mult_avg_sharpe[mult] = np.mean(shs)
    best_avg_sharpe: tuple[float, float] = max(mult_avg_sharpe.items(), key=lambda x: x[1])  # type: ignore[arg-type]
    print(f"  🥇 Best Avg Sharpe   : multiplier={best_avg_sharpe[0]:.1f}"
          f" → avg Sharpe {best_avg_sharpe[1]:.2f}")

    print("\n" + "═" * 120)
    print("  ✅ Multi-Period Sweep Complete")
    print("═" * 120)


# ══════════════════════════════════════════════
# Portfolio Weight Sweep (TQQQ + SOXL allocation)
# ══════════════════════════════════════════════

def _run_portfolio_sweep_core(dfs: dict, mults: dict, total_capital: float,
                               label: str = "", verbose: bool = True) -> list:
    """Core portfolio sweep logic. Returns list of result dicts for each weight."""
    weights = np.arange(0.1, 1.0, 0.1)  # 10% → 90%
    results = []

    for tqqq_w in weights:
        soxl_w = round(1.0 - tqqq_w, 1)
        tqqq_w = round(tqqq_w, 1)

        cap_tqqq = total_capital * tqqq_w
        cap_soxl = total_capital * soxl_w
        buy_tqqq = cap_tqqq / MAX_BUYS
        buy_soxl = cap_soxl / MAX_BUYS

        r_tqqq = run_backtest(dfs['TQQQ'], entry_multiplier=mults['TQQQ'],
                              initial_cash=cap_tqqq, buy_amount=buy_tqqq)
        r_soxl = run_backtest(dfs['SOXL'], entry_multiplier=mults['SOXL'],
                              initial_cash=cap_soxl, buy_amount=buy_soxl)

        dv_tqqq = {d['date'].date(): d['value'] for d in r_tqqq['daily_values']}
        dv_soxl = {d['date'].date(): d['value'] for d in r_soxl['daily_values']}
        all_dates = sorted(set(dv_tqqq.keys()) & set(dv_soxl.keys()))
        if not all_dates:
            continue

        combined_values = [dv_tqqq[d] + dv_soxl[d] for d in all_dates]
        portfolio_return = (combined_values[-1] - total_capital) / total_capital * 100

        cv_arr = np.array(combined_values)
        daily_ret = cv_arr[1:] / cv_arr[:-1] - 1
        sharpe = float(np.sqrt(252) * daily_ret.mean() / daily_ret.std()) if daily_ret.std() > 0 else 0.0

        peak = np.maximum.accumulate(cv_arr)
        dd = (cv_arr - peak) / peak
        mdd = float(dd.min() * 100)

        results.append({
            'tqqq_pct': tqqq_w * 100,
            'soxl_pct': soxl_w * 100,
            'total_return': round(portfolio_return, 2),
            'sharpe': round(sharpe, 2),
            'mdd': round(mdd, 2),
            'final_value': round(combined_values[-1], 2),
            'r_tqqq': r_tqqq,
            'r_soxl': r_soxl,
        })

        if verbose:
            print(f"  TQQQ {tqqq_w*100:>3.0f}% / SOXL {soxl_w*100:>3.0f}%"
                  f" | Return {portfolio_return:>+7.2f}% | Sharpe {sharpe:>5.2f}"
                  f" | MDD {mdd:>6.2f}% | Fills {r_tqqq['total_buys']}/{r_soxl['total_buys']}")

    return results


def run_portfolio_sweep():
    """Sweep over TQQQ/SOXL weights for the current 1-year period."""
    tickers = ["TQQQ", "SOXL"]
    mults = {t: load_entry_multiplier(t) for t in tickers}

    print(f"\n📊 Portfolio Weight Optimization — TQQQ + SOXL")
    print(f"   Total capital: ${INITIAL_CASH:,}")
    print(f"   Multipliers : TQQQ × {mults['TQQQ']}  |  SOXL × {mults['SOXL']}")
    print("=" * 130)

    dfs = {t: fetch_data(t) for t in tickers}
    results = _run_portfolio_sweep_core(dfs, mults, float(INITIAL_CASH), label="Current")

    if not results:
        return

    print("═" * 130)
    best_return = max(results, key=lambda r: r['total_return'])
    best_sharpe = max(results, key=lambda r: r['sharpe'])
    best_mdd    = min(results, key=lambda r: r['mdd'])

    print(f"\n  🏆 Optimal Allocations:")
    print(f"     Max Return : TQQQ {best_return['tqqq_pct']:.0f}% / SOXL {best_return['soxl_pct']:.0f}%"
          f"  → {best_return['total_return']:+.2f}% (Sharpe {best_return['sharpe']})")
    print(f"     Max Sharpe : TQQQ {best_sharpe['tqqq_pct']:.0f}% / SOXL {best_sharpe['soxl_pct']:.0f}%"
          f"  → Sharpe {best_sharpe['sharpe']:.2f} (Return {best_sharpe['total_return']:+.2f}%)")
    print(f"     Min MDD    : TQQQ {best_mdd['tqqq_pct']:.0f}% / SOXL {best_mdd['soxl_pct']:.0f}%"
          f"  → MDD {best_mdd['mdd']:.2f}%")

    best = best_sharpe
    b_tqqq = best['r_tqqq']
    b_soxl = best['r_soxl']
    print(f"\n  📋 Best Sharpe Details: TQQQ ${INITIAL_CASH*best['tqqq_pct']/100:,.0f}"
          f" / SOXL ${INITIAL_CASH*best['soxl_pct']/100:,.0f}"
          f" → {best['total_return']:+.2f}% | Sharpe {best['sharpe']}"
          f" | TQQQ {b_tqqq['total_buys']}buys {b_tqqq['win_rate']:.0f}%win"
          f" | SOXL {b_soxl['total_buys']}buys {b_soxl['win_rate']:.0f}%win")

    print("\n" + "═" * 130)
    print("  ✅ Portfolio Sweep Complete")
    print("═" * 130)


# ══════════════════════════════════════════════
# Multi-Period Portfolio Sweep (Generalization Test)
# ══════════════════════════════════════════════

def run_multi_period_portfolio_sweep():
    """
    Run portfolio weight sweep across multiple historical 1-year periods
    to test whether the optimal TQQQ/SOXL allocation generalizes.
    """
    today = date.today()
    periods = [
        (today - timedelta(days=365), today, "🔥 Strong Bull"),
        (today - timedelta(days=730), today - timedelta(days=365), "📈 Bull"),
        (today - timedelta(days=1095), today - timedelta(days=730), "📊 Recovery"),
        (today - timedelta(days=1460), today - timedelta(days=1095), "📉 Bear Bottom"),
        (today - timedelta(days=1825), today - timedelta(days=1460), "💥 Bear Crash"),
    ]

    tickers = ["TQQQ", "SOXL"]
    mults = {t: load_entry_multiplier(t) for t in tickers}

    print(f"\n🌍 Multi-Period Portfolio Weight Generalization Test")
    print(f"   Multipliers : TQQQ × {mults['TQQQ']}  |  SOXL × {mults['SOXL']}")
    print(f"   Testing {len(periods)} market regimes × 9 allocation ratios")
    print("=" * 185)

    all_optimals = {}   # period_label -> {'by_return': ..., 'by_sharpe': ..., 'all_results': [...]}

    for start_dt, end_dt, label in periods:
        print(f"\n📅 {label}  ({start_dt} → {end_dt})")
        print("─" * 130)

        dfs = {t: fetch_data(t, end_date=end_dt) for t in tickers}
        # Trim to backtest window
        for t in tickers:
            dfs[t] = dfs[t][dfs[t].index.date <= end_dt]  # type: ignore[operator]

        results = _run_portfolio_sweep_core(dfs, mults, float(INITIAL_CASH),
                                            label=label, verbose=True)

        if not results:
            continue

        best_return = max(results, key=lambda r: r['total_return'])
        best_sharpe = max(results, key=lambda r: r['sharpe'])
        print(f"   🏆 Best Return : TQQQ {best_return['tqqq_pct']:.0f}%"
              f" → {best_return['total_return']:+.2f}%")
        print(f"   🏆 Best Sharpe : TQQQ {best_sharpe['tqqq_pct']:.0f}%"
              f" → Sharpe {best_sharpe['sharpe']:.2f}")

        all_optimals[label] = {
            'by_return': best_return,
            'by_sharpe': best_sharpe,
            'all_results': results,
        }

    # ── Cross-period analysis ──────────────────────────────────────
    if not all_optimals:
        print("\n⚠️  No valid periods to analyze.")
        return

    print("\n" + "═" * 185)
    print("  🏆 Cross-Period Optimal Allocation Comparison")
    print("─" * 185)
    print(f"  {'Period':<20} {'Best Ret TQQQ':>14} {'Best Ret %':>12}"
          f" {'Best Sh TQQQ':>14} {'Best Sh Sharpe':>15}")
    print(f"  {'─'*20} {'─'*14} {'─'*12} {'─'*14} {'─'*15}")

    allocation_votes_return = {}
    allocation_votes_sharpe = {}

    for label in all_optimals:
        br = all_optimals[label]['by_return']
        bs = all_optimals[label]['by_sharpe']
        print(f"  {label:<20} {br['tqqq_pct']:>13.0f}% {br['total_return']:>+11.2f}%"
              f" {bs['tqqq_pct']:>13.0f}% {bs['sharpe']:>14.2f}")

        key_r = f"TQQQ {br['tqqq_pct']:.0f}%"
        key_s = f"TQQQ {bs['tqqq_pct']:.0f}%"
        allocation_votes_return[key_r] = allocation_votes_return.get(key_r, 0) + 1
        allocation_votes_sharpe[key_s] = allocation_votes_sharpe.get(key_s, 0) + 1

    # ── Find consensus ────────────────────────────────────────────
    print("─" * 185)
    consensus_ret = max(allocation_votes_return, key=allocation_votes_return.get)  # type: ignore[arg-type]
    consensus_sh = max(allocation_votes_sharpe, key=allocation_votes_sharpe.get)  # type: ignore[arg-type]

    print(f"\n  🗳️  Consensus by Max Return  : {consensus_ret}"
          f" ({allocation_votes_return[consensus_ret]}/{len(all_optimals)} periods)")
    print(f"  🗳️  Consensus by Max Sharpe  : {consensus_sh}"
          f" ({allocation_votes_sharpe[consensus_sh]}/{len(all_optimals)} periods)")

    # Average return of current best (10/90) across all periods
    print(f"\n  📌 TQQQ 10% / SOXL 90% performance per period:")
    ref_returns = []
    ref_sharpes = []
    for label, opt in all_optimals.items():
        # Find the 10/90 result from all_results
        ref = next((r for r in opt['all_results'] if abs(r['tqqq_pct'] - 10) < 0.5), None)
        if ref:
            ref_returns.append(ref['total_return'])
            ref_sharpes.append(ref['sharpe'])
            print(f"     {label:<20} → {ref['total_return']:+.2f}%  (Sharpe {ref['sharpe']})")
        else:
            print(f"     {label:<20} → 10/90 not tested")

    if ref_returns:
        avg_ret = np.mean(ref_returns)
        avg_sh = np.mean(ref_sharpes)
        print(f"     {'─'*20} ────────────")
        print(f"     {'Average':<20} → {avg_ret:+.2f}%  (Sharpe {avg_sh:.2f})")

    print("\n" + "═" * 185)
    print("  ✅ Multi-Period Portfolio Sweep Complete")
    print("═" * 185)


# ══════════════════════════════════════════════
# Portfolio Detail Run (from config allocation)
# ══════════════════════════════════════════════

def load_allocation_pct(ticker: str) -> float:
    """Read ALLOCATION_PCT from portfolio_config.json for a given ticker."""
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    positions = cfg.get("POSITIONS", {})
    if ticker in positions and "ALLOCATION_PCT" in positions[ticker]:
        return float(positions[ticker]["ALLOCATION_PCT"])
    raise KeyError(f"ALLOCATION_PCT not found in {CONFIG_PATH} for {ticker}")


def print_portfolio_report(r_portfolio: dict):
    """Print a detailed portfolio report with combined and per-ticker results."""
    R = r_portfolio
    tqqq = R['r_tqqq']
    soxl = R['r_soxl']
    total = float(INITIAL_CASH)

    print("\n")
    print("═" * 72)
    print("  📊 Portfolio DCA Backtest Report — TQQQ + SOXL")
    print("═" * 72)
    print(f"  Allocation : TQQQ {R['tqqq_pct']:.0f}%  /  SOXL {R['soxl_pct']:.0f}%")
    print(f"  Capital    : ${total:,.0f}  (TQQQ ${total*R['tqqq_pct']/100:,.0f} / SOXL ${total*R['soxl_pct']/100:,.0f})")
    print(f"  Multiplier : TQQQ × {tqqq['multiplier']}  |  SOXL × {soxl['multiplier']}")
    print(f"  Period     : {R['period_start']}  →  {R['period_end']}")
    print("─" * 72)

    # Combined performance
    print(f"\n  📈 Portfolio Performance")
    print(f"     Total Return      : {R['total_return']:+.2f}%")
    print(f"     Final Value       : ${R['final_value']:,.2f}")
    print(f"     Sharpe Ratio      : {R['sharpe']}")
    print(f"     Max Drawdown      : {R['mdd']:.2f}%")

    # Per-ticker comparison
    print(f"\n  📋 Per-Ticker Comparison")
    print(f"  {'Ticker':<8} {'Capital':>10} {'Return':>10} {'Fill':>5} {'AvgBuy':>8}"
          f" {'WinRate':>8} {'CashRem':>10}")
    print(f"  {'─'*8} {'─'*10} {'─'*10} {'─'*5} {'─'*8} {'─'*8} {'─'*10}")
    for label, r in [('TQQQ', tqqq), ('SOXL', soxl)]:
        print(f"  {label:<8} ${total*R[f'{label.lower()}_pct']/100:>7,.0f}"
              f" {r['total_return']:>+9.2f}% {r['total_buys']:>3d}"
              f" ${r['avg_buy_price']:>6.2f} {r['win_rate']:>6.1f}%"
              f" ${r['remaining_cash']:>7,.0f}")

    # Monthly combined breakdown
    combined_dv = R['combined_daily_values']
    if combined_dv:
        monthly = {}
        for d in combined_dv:
            mk = d['date'].strftime('%Y-%m')
            monthly.setdefault(mk, []).append(d['value'])
        print(f"\n  📅 Monthly Portfolio Value")
        print(f"  {'Month':<8} {'Start':>10} {'End':>10} {'Return':>8}")
        print(f"  {'─'*8} {'─'*10} {'─'*10} {'─'*8}")
        for m in sorted(monthly):
            vals = monthly[m]
            s, e = vals[0], vals[-1]
            print(f"  {m:<8} ${s:>7,.0f} ${e:>7,.0f} {(e-s)/s*100:>+7.2f}%")

    # Buy logs
    for label, r in [('TQQQ', tqqq), ('SOXL', soxl)]:
        if r['trade_log']:
            print(f"\n  📝 {label} Buy Log")
            print(f"  {'Date':<14} {'Price':>8} {'Shares':>10} {'Amount':>9} {'Sigma':>8}")
            print(f"  {'─'*14} {'─'*8} {'─'*10} {'─'*9} {'─'*8}")
            for t in r['trade_log']:
                print(f"  {t['date'].strftime('%Y-%m-%d'):<14}"
                      f" ${t['price']:>6.2f} {t['shares']:>10.2f}"
                      f" ${t['amount']:>7,.0f} {t['sigma']:>8.4f}")

    print("\n" + "═" * 72)
    print("  ✅ Portfolio Run Complete")
    print("═" * 72)


def run_portfolio_detail():
    """Run a single portfolio backtest with the allocation from portfolio_config.json."""
    tickers = ["TQQQ", "SOXL"]
    mults = {t: load_entry_multiplier(t) for t in tickers}
    alloc = {t: load_allocation_pct(t) for t in tickers}

    total_alloc = sum(alloc.values())
    tqqq_pct = alloc['TQQQ'] / total_alloc * 100
    soxl_pct = alloc['SOXL'] / total_alloc * 100

    total_capital = float(INITIAL_CASH)
    cap_tqqq = total_capital * tqqq_pct / 100
    cap_soxl = total_capital * soxl_pct / 100

    dfs = {t: fetch_data(t) for t in tickers}

    r_tqqq = run_backtest(dfs['TQQQ'], entry_multiplier=mults['TQQQ'],
                          initial_cash=cap_tqqq, buy_amount=cap_tqqq / MAX_BUYS,
                          verbose=True)
    r_soxl = run_backtest(dfs['SOXL'], entry_multiplier=mults['SOXL'],
                          initial_cash=cap_soxl, buy_amount=cap_soxl / MAX_BUYS,
                          verbose=True)

    # Combine daily values
    dv_tqqq = {d['date'].date(): d['value'] for d in r_tqqq['daily_values']}
    dv_soxl = {d['date'].date(): d['value'] for d in r_soxl['daily_values']}
    all_dates = sorted(set(dv_tqqq.keys()) & set(dv_soxl.keys()))

    if not all_dates:
        print("⚠️  No overlapping trading days between tickers.")
        return

    combined_values = [dv_tqqq[d] + dv_soxl[d] for d in all_dates]
    combined_dv = [{'date': pd.Timestamp(d), 'value': round(cv, 2)}
                   for d, cv in zip(all_dates, combined_values)]

    portfolio_return = (combined_values[-1] - total_capital) / total_capital * 100
    cv_arr = np.array(combined_values)
    daily_ret = cv_arr[1:] / cv_arr[:-1] - 1
    sharpe = float(np.sqrt(252) * daily_ret.mean() / daily_ret.std()) if daily_ret.std() > 0 else 0.0
    peak = np.maximum.accumulate(cv_arr)
    dd = (cv_arr - peak) / peak
    mdd = float(dd.min() * 100)

    portfolio_result = {
        'tqqq_pct': round(tqqq_pct, 1),
        'soxl_pct': round(soxl_pct, 1),
        'total_return': round(portfolio_return, 2),
        'sharpe': round(sharpe, 2),
        'mdd': round(mdd, 2),
        'final_value': round(combined_values[-1], 2),
        'period_start': all_dates[0],
        'period_end': all_dates[-1],
        'combined_daily_values': combined_dv,
        'r_tqqq': r_tqqq,
        'r_soxl': r_soxl,
    }

    print_portfolio_report(portfolio_result)


# ══════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════

if __name__ == "__main__":
    sweep_mode      = "--sweep" in sys.argv
    multi_sweep     = "--multi-sweep" in sys.argv
    portfolio_sweep = "--portfolio-sweep" in sys.argv
    multi_portfolio = "--multi-portfolio-sweep" in sys.argv
    portfolio_run   = "--portfolio-run" in sys.argv
    sell_mode       = "--sell" in sys.argv
    ath_dca_mode    = "--ath-dca" in sys.argv
    recovery_mode   = "--ath-dca-recovery" in sys.argv

    # Optional --end-date YYYY-MM-DD (default: today). Useful to backtest
    # historical crash→recovery cycles (e.g. --end-date 2023-06-30 for the
    # 2022 bear market bottom, or 2025-03-31 for the Aug-2024 V-recovery).
    end_date: date | None = None
    if "--end-date" in sys.argv:
        idx = sys.argv.index("--end-date")
        if idx + 1 < len(sys.argv):
            try:
                end_date = date.fromisoformat(sys.argv[idx + 1])
            except ValueError:
                print(f"⚠️ Invalid --end-date value: {sys.argv[idx + 1]} (use YYYY-MM-DD)")
                sys.exit(1)

    if recovery_mode:
        tickers = ["TQQQ", "SOXL"]
        print(f"\n🚀 ATH DCA + 비상 모드 종료(Emergency Mode Exit) 백테스트 비교")
        print(f"   Capital: ${INITIAL_CASH:,}")
        print(f"   Period : {BACKTEST_DAYS} trading days ending {end_date or 'today'}")
        print("─" * 88)
        for tkr in tickers:
            mult = load_entry_multiplier(tkr)
            ath_cfg = load_ath_dca_config(tkr)
            print(f"\n📊 {tkr} — entry_mult={mult}, ATH_DCA: {ath_cfg.get('SPLITS', 3)}splits")
            # include_volume=True: Stage-5 proxy needs volume-contraction data
            df = fetch_data(tkr, end_date=end_date, include_volume=True)
            r_base = run_backtest_ath_dca(
                df, entry_multiplier=mult, ticker=tkr,
                ath_dca_config=ath_cfg, verbose=False
            )
            r_rec = run_backtest_ath_dca_recovery(
                df, entry_multiplier=mult, ticker=tkr,
                ath_dca_config=ath_cfg, verbose=False
            )
            print_recovery_comparison_report(r_base, r_rec)
    elif ath_dca_mode:
        tickers = ["TQQQ", "SOXL"]
        print(f"\n🚀 ATH DCA 듀얼 모드 백테스트")
        print(f"   Capital: ${INITIAL_CASH:,}")
        print(f"   Period : Recent {BACKTEST_DAYS} trading days (~1 year)")
        print("─" * 72)
        for tkr in tickers:
            mult = load_entry_multiplier(tkr)
            ath_cfg = load_ath_dca_config(tkr)
            print(f"\n📊 {tkr} — entry_mult={mult}, ATH_DCA: {ath_cfg.get('SPLITS', 3)}splits")
            # include_volume=True: Stage-5 proxy needs volume-contraction data
            df = fetch_data(tkr, include_volume=True)
            r = run_backtest_ath_dca(
                df, entry_multiplier=mult, ticker=tkr,
                ath_dca_config=ath_cfg, verbose=True
            )
            print_ath_dca_report(r)
    elif portfolio_run:
        run_portfolio_detail()
    elif multi_portfolio:
        run_multi_period_portfolio_sweep()
    elif portfolio_sweep:
        run_portfolio_sweep()
    elif multi_sweep:
        run_multi_period_sweep()
    elif sweep_mode:
        df = fetch_data(TICKER)
        run_multiplier_sweep(df)
    elif sell_mode:
        df = fetch_data(TICKER)
        print(f"\n🚀 Sigma DCA + 전고점50%청산 Backtest")
        print(f"   Asset    : {TICKER}")
        print(f"   Capital  : ${INITIAL_CASH:,}")
        print(f"   Period   : Recent {BACKTEST_DAYS} trading days (~1 year)")
        print(f"   Method   : {VOL_METHOD} λ={EWMA_LAMBDA} × {ENTRY_MULTIPLIER} LOC")
        print(f"   Sell Trig: ATH≥{SELL_ATH_RATIO*100:.0f}% + 20일≥{SELL_RALLY_THRESHOLD*100:.0f}% + Sigma≥{SELL_SIGMA_RATIO:.1f}x")
        print(f"   Sell Amt : {SELL_PCT*100:.0f}% 청산")
        print("─" * 80)

        # Run both backtests
        print("\n📋 [1/2] DCA Only 백테스트 실행중...")
        r_dca = run_backtest(df, entry_multiplier=ENTRY_MULTIPLIER, verbose=True)

        print("\n📋 [2/2] DCA+전고점50%청산 백테스트 실행중...")
        r_sell = run_backtest_with_sell(df, entry_multiplier=ENTRY_MULTIPLIER, verbose=True)

        # Comparison report
        print_comparison_report(r_dca, r_sell)
    else:
        df = fetch_data(TICKER)
        print(f"\n🚀 Sigma DCA Backtest")
        print(f"   Asset  : {TICKER} (100%)")
        print(f"   Capital: ${INITIAL_CASH:,}")
        print(f"   Period : Recent {BACKTEST_DAYS} trading days (~1 year)")
        print(f"   Method : {VOL_METHOD} λ={EWMA_LAMBDA} × {ENTRY_MULTIPLIER}")
        print("─" * 62)
        r = run_backtest(df, entry_multiplier=ENTRY_MULTIPLIER, verbose=True)
        print_report(r)
