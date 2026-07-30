"""
─────────────────────────────────────────────────────────────
Sigma DCA Automation — LOC Target Price Discord Briefing
─────────────────────────────────────────────────────────────
Execution Flow:
  1. Load config.json
  2. Automatically update Sigma per ticker based on LOOKBACK_DAYS
  3. Send monthly operation ping on the 1st
  4. Calculate previous day's close and LOC target price per ticker
  5. Dispatch Discord briefing
"""
import csv
import os
import sys
import json
import shutil
import tempfile
import time
import numpy as np
import pandas as pd
import requests
import yfinance as yf
from datetime import datetime, timezone, date, time as dtime, timedelta
from zoneinfo import ZoneInfo


# ====================== Encoding Settings ======================
if hasattr(sys.stdout, "reconfigure"):
    getattr(sys.stdout, "reconfigure")(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    getattr(sys.stderr, "reconfigure")(encoding="utf-8")
    
CONFIG_PATH            = "portfolio_config.json"
_DISCORD_TITLE_LIMIT   = 256
_DISCORD_CONTENT_LIMIT = 4096

# Regular NYSE close is 16:00 America/New_York. A short settle buffer is
# added on top so the daily bar yfinance reports for "today" isn't treated
# as final while it's still catching up right at the closing bell — without
# this, a run at e.g. 16:02 could grab a not-yet-fully-settled print.
NY_MARKET_CLOSE_HOUR            = 16
NY_MARKET_CLOSE_MINUTE          = 0
NY_CLOSE_SETTLE_BUFFER_MINUTES  = 15


# ════════════════════════════════════════════
# I/O
# ════════════════════════════════════════════

def load_portfolio() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"POSITIONS": {}}

def save_portfolio(data: dict) -> None:
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json", encoding="utf-8") as tmp:
        json.dump(data, tmp, indent=4, ensure_ascii=False)
        temp_path = tmp.name
    shutil.move(temp_path, CONFIG_PATH)


# ═══════════════════════════════════════════════════════════
# Sigma Auto-Update — By LOOKBACK_DAYS
# ═══════════════════════════════════════════════════════════

# Function to log Sigma updates to CSV
def log_sigma_update(ticker: str, sigma: float, today: date) -> None:
    """
    NOTE: `today` must be passed in explicitly (NY-timezone date computed by
    the caller) instead of calling datetime.now() here — GitHub Actions runs
    in UTC, so a local datetime.now() could log a different calendar date
    than the one stored in LAST_SIGMA_UPDATE.
    """
    file_path = "sigma_history.csv"
    file_exists = os.path.isfile(file_path)
    
    with open(file_path, 'a', newline='') as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(['Date', 'Ticker', 'Sigma'])
        writer.writerow([today.strftime("%Y-%m-%d"), ticker, sigma])


def _fetch_closes_for_lookback(ticker: str, lookback_days: int, max_retries: int = 3):
    """
    Shared history fetcher used by both recompute_sigma_for_ticker() and
    get_realtime_sigma(). Requests enough CALENDAR-day buffer to guarantee
    at least `lookback_days` TRADING days come back (yfinance `period` is
    calendar days, not trading days — a flat +30d buffer is not enough once
    lookback_days gets large, e.g. 252), and retries on transient failures
    or insufficient data.
    """
    buffer_days = max(30, int(lookback_days * 0.6) + 30)
    period_days = lookback_days + buffer_days

    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            stock = yf.Ticker(ticker)
            hist = stock.history(period=f"{period_days}d", interval="1d", auto_adjust=False)
            if hist.empty:
                raise ValueError("Data empty.")
            closes = hist['Close'].dropna()
            if len(closes) < lookback_days:
                raise ValueError(f"Insufficient data points ({len(closes)}/{lookback_days}).")
            return closes
        except Exception as e:
            last_err = e
            if attempt < max_retries:
                time.sleep(2.0)
    raise RuntimeError(f"❌ {ticker} price history fetch failed after {max_retries} attempts") from last_err


def recompute_sigma_for_ticker(ticker: str, pos: dict, today: date) -> float:
    """
    Force recalculate DAILY_SIGMA for a ticker and update the config.
    (Used for forced recalculations like rotation expiration resets)
    """
    lookback_days = int(pos.get("LOOKBACK_DAYS", 252))
    vol_method = str(pos.get("VOL_METHOD", "EWMA")).upper()
    ewma_lambda = float(pos.get("EWMA_LAMBDA", 0.94))

    closes = _fetch_closes_for_lookback(ticker, lookback_days)
    sigma, actual_method = _calculate_volatility_from_closes(closes, lookback_days, vol_method, ewma_lambda)
    new_sigma = round(sigma, 4)

    pos["DAILY_SIGMA"] = new_sigma
    pos["LAST_SIGMA_UPDATE"] = today.strftime("%Y-%m-%d")
    pos["LAST_SIGMA_METHOD"] = actual_method
    pos["LAST_EWMA_LAMBDA"] = ewma_lambda if actual_method == "EWMA" else None
    log_sigma_update(ticker, new_sigma, today)
    return new_sigma


def refresh_sigma_if_stale(cfg: dict) -> list[str]:
    messages = []
    today = datetime.now(ZoneInfo("America/New_York")).date()
    positions_data = cfg.setdefault("POSITIONS", {})

    for ticker, pos in positions_data.items():
        lookback_days = int(pos.get("LOOKBACK_DAYS", 252))
        vol_method = str(pos.get("VOL_METHOD", "EWMA")).upper()
        ewma_lambda = float(pos.get("EWMA_LAMBDA", 0.94))
        pos["LOOKBACK_DAYS"] = lookback_days
        pos["VOL_METHOD"] = vol_method
        pos["EWMA_LAMBDA"] = ewma_lambda
        
        last_str = pos.get("LAST_SIGMA_UPDATE", "2000-01-01")
        last_dt = datetime.strptime(last_str, "%Y-%m-%d").date()
        
        # Check 63 trading days (approx. 90 calendar days)
        days_passed = (today - last_dt).days
        method_changed = pos.get("LAST_SIGMA_METHOD") != vol_method
        lambda_changed = vol_method == "EWMA" and pos.get("LAST_EWMA_LAMBDA") != ewma_lambda
        if "DAILY_SIGMA" in pos and days_passed < 90 and not method_changed and not lambda_changed:
            continue

        try:
            new_sigma = recompute_sigma_for_ticker(ticker, pos, today)
            messages.append(f"📊 {ticker} auto-updated [{lookback_days} days/{vol_method}]: {new_sigma:.4f}")
        except Exception as e:
            messages.append(f"⚠️ {ticker} update error: {e}")
    return messages


# ═══════════════════════════════════════════════════════════
# Price Lookup
# ═══════════════════════════════════════════════════════════

def _safe_float(val) -> float | None:
    try:
        if val is None:
            return None
        f = float(val)
        return None if (np.isnan(f) or np.isinf(f)) else f
    except (TypeError, ValueError):
        return None


def _get_recent_log_returns(closes, lookback_days: int):
    log_returns = np.log(closes / closes.shift(1))
    log_returns_clean = log_returns[~np.isnan(log_returns)]
    return log_returns_clean[-lookback_days:]


def _calculate_sigma_from_closes(closes, lookback_days: int) -> float:
    recent_returns = _get_recent_log_returns(closes, lookback_days)
    return float(recent_returns.std(ddof=1))


def _calculate_ewma_sigma_from_closes(closes, lookback_days: int, ewma_lambda: float) -> float:
    if not 0 < ewma_lambda < 1:
        raise ValueError(f"EWMA_LAMBDA must be between 0 and 1: {ewma_lambda}")

    recent_returns = _get_recent_log_returns(closes, lookback_days)
    if len(recent_returns) < 2:
        raise ValueError("Insufficient log return data for EWMA calculation.")

    # Seed with only the FIRST return's squared value, then recurse forward.
    # (Previously seeded with the full-window sample variance, which bakes
    # "future" data into the very first step of the recursion — a small
    # lookahead bias. Its effect decays as lambda^n, so it mattered more for
    # short lookbacks like the 63-day rotation tickers than the 252-day ones.)
    returns_arr = np.asarray(recent_returns, dtype=float)
    variance = float(returns_arr[0] ** 2)
    for r in returns_arr[1:]:
        variance = ewma_lambda * variance + (1 - ewma_lambda) * float(r) ** 2
    return float(np.sqrt(variance))


def _calculate_volatility_from_closes(closes, lookback_days: int, vol_method: str, ewma_lambda: float) -> tuple[float, str]:
    """
    Returns (sigma, method_actually_used) — same interface as the GARCH
    variant of this script, even though this file has no GARCH branch, so
    both scripts can share callers/patterns without special-casing.
    """
    method = vol_method.upper()
    if method == "EWMA":
        return _calculate_ewma_sigma_from_closes(closes, lookback_days, ewma_lambda), "EWMA"
    if method in {"STD", "HISTORICAL", "SIMPLE"}:
        return _calculate_sigma_from_closes(closes, lookback_days), method
    raise ValueError(f"Unsupported VOL_METHOD: {vol_method}")


def _calculate_loc_from_sigma(prev_close: float, sigma: float, multiplier: float) -> float:
    target_drop_rate = sigma * multiplier
    return round(prev_close * (1 - target_drop_rate), 2)
    
def _most_recent_trading_day(today: date) -> date:
    """Returns the most recent trading day before or on `today`, accounting
    for weekends only.  Does NOT account for NYSE holidays — that's fine for
    staleness detection because a single holiday would only make the expected
    date off by one day and the retry/fallback still converges on the right
    data; consecutive multi-day holiday closures (Christmas+New Year's)
    would trigger an extra retry but still fall through correctly."""
    candidate = today
    while candidate.weekday() >= 5:  # 5=Sat, 6=Sun
        candidate -= timedelta(days=1)
    return candidate


def get_prev_close(ticker: str) -> tuple[float | None, str]:
    """
    Reliable FINAL-close lookup using yfinance (with 3 retries).

    Always resolves to the official close of the most recently COMPLETED
    trading session — never a still-live/forming intraday print:
      - If today's session has already closed (past NY market close +
        settle buffer), use today's now-final close.
      - Otherwise (market still open, or hasn't opened yet today), use the
        last prior session's already-final close.
    This makes the result deterministic based on market state rather than
    on what wall-clock hour happens to be it's run at.
    """
    print(f"🔍 Starting price lookup for {ticker}...")
    now_ny = datetime.now(ZoneInfo("America/New_York"))
    today_ny = now_ny.date()

    market_close_settled_at = datetime.combine(
        today_ny,
        dtime(NY_MARKET_CLOSE_HOUR, NY_MARKET_CLOSE_MINUTE),
        tzinfo=ZoneInfo("America/New_York"),
    ) + timedelta(minutes=NY_CLOSE_SETTLE_BUFFER_MINUTES)
    today_session_settled = now_ny >= market_close_settled_at

    # Expected latest trading day the data should reach (at minimum).
    # If today's session is settled, the latest data should be today
    # (or the most recent trading day if today is a weekend/holiday);
    # otherwise it should be the most recent trading day that's already
    # in the books (yesterday or last Friday).
    #
    # NOTE: _most_recent_trading_day() is applied to BOTH branches so
    # that a weekend run (where today_session_settled may be True even
    # though no market session actually existed today) doesn't set
    # expected_latest_date to a non-trading-day date and trigger a
    # false-positive staleness retry.
    expected_latest_date = _most_recent_trading_day(
        today_ny if today_session_settled else today_ny - timedelta(days=1)
    )

    for attempt in range(1, 4):
        try:
            print(f"   → Trying yfinance history ({attempt}/3)...")
            stock = yf.Ticker(ticker)
            # NOTE: rounding=True is intentionally omitted.  yfinance's
            # internal cache uses URL+params as the key; the presence vs
            # absence of `rounding` creates a DIFFERENT cache entry from
            # the `auto_adjust=False` call made by
            # check_macro_and_technical_signals(), which avoids a scenario
            # where the preceding 120d fetch pollutes the cache for this
            # 1mo fetch and returns stale/lagged data.
            hist = stock.history(period="1mo", interval="1d", auto_adjust=False)

            if not hist.empty:
                close_series = hist['Close'].dropna()
                if not close_series.empty:
                    last_idx = close_series.index[-1]
                    if isinstance(last_idx, pd.Timestamp):
                        last_date: date = last_idx.date()
                    else:
                        last_date = pd.Timestamp(last_idx).date()  # type: ignore[arg-type]

                    # ── Staleness guard ──────────────────────────────────
                    # If the last row yfinance returned is OLDER than the
                    # expected latest trading day (e.g. returned 07-23 when
                    # 07-24 should be available), it's stale cached data.
                    # Retry with a longer backoff to let the cache expire.
                    if last_date < expected_latest_date:
                        print(f"   ⚠️ Stale data: last_date={last_date}, expected ≥{expected_latest_date}. Retrying...")
                        if attempt < 3:
                            time.sleep(5.0)
                        continue
                    # ─────────────────────────────────────────────────────

                    # DIAGNOSTIC: print what yfinance actually returned for the
                    # last few sessions, plus the settle-check inputs, so a
                    # yfinance data-lag issue (stale last row) can be told apart
                    # from a bug in the settle-buffer comparison itself.
                    tail = close_series.tail(5)
                    tail_str = ", ".join(
                        f"{idx.date() if isinstance(idx, pd.Timestamp) else idx}={val:.2f}"
                        for idx, val in tail.items()
                    )
                    print(f"   🩺 [debug] {ticker} raw last rows: {tail_str}")
                    print(
                        f"   🩺 [debug] now_ny={now_ny.isoformat()} today_ny={today_ny} "
                        f"last_date={last_date} settle_deadline={market_close_settled_at.isoformat()} "
                        f"today_session_settled={today_session_settled}"
                    )

                    # If the most recent bar is today's AND today's session
                    # hasn't finished settling yet, that bar isn't final —
                    # fall back to the prior (already-final) session's close.
                    if last_date == today_ny and not today_session_settled and len(close_series) >= 2:
                        prev_close = float(close_series.iloc[-2])
                        prev_idx = close_series.index[-2]
                        prev_date = prev_idx.date() if isinstance(prev_idx, pd.Timestamp) else pd.Timestamp(prev_idx).date()  # type: ignore[arg-type]
                        date_str = prev_date.strftime("%m-%d")
                    else:
                        prev_close = float(close_series.iloc[-1])
                        date_str = last_date.strftime("%m-%d")

                    print(f"✅ {ticker} yfinance success: ${prev_close:.2f} ({date_str})")
                    return prev_close, date_str

            print(f"   ⚠️ Attempt {attempt}: empty data returned.")

        except Exception as e:
            print(f"   ⚠️ Attempt {attempt} failed: {e}")

        # Standard backoff (used when the data IS returned but empty, or
        # an exception occurred — the staleness guard above used a longer
        # backoff and a ``continue``, so we only reach this point for
        # exceptions / truly empty responses).
        if attempt < 3:
            time.sleep(2.0)

    # Info fallback — "previousClose"/"regularMarketPreviousClose" are
    # official final closes, so they're tried first. "currentPrice" and
    # "regularMarketPrice" can be a still-live intraday quote, so they're
    # only used as a last resort when history and the close fields all
    # failed, and are flagged as such in the log.
    try:
        print(f"   → Trying yfinance info fallback...")
        info = yf.Ticker(ticker).info
        for key in ["previousClose", "regularMarketPreviousClose", "currentPrice", "regularMarketPrice"]:
            price = _safe_float(info.get(key))
            if price is not None and not np.isnan(price):
                if key in {"currentPrice", "regularMarketPrice"}:
                    print(f"⚠️ {ticker} info fallback used a possibly-live quote (key: {key}): ${price:.2f}")
                else:
                    print(f"✅ {ticker} info success: ${price:.2f} (key: {key})")
                return price, "N/A"
    except Exception as e:
        print(f"   ⚠️ Info fallback failed: {e}")

    print(f"❌ All price lookup methods failed for {ticker}")
    return None, "N/A"


# ═══════════════════════════════════════════════════════════
# Peak Sell Signal Engine — 전고점 근접 50% 청산 Signal
# ═══════════════════════════════════════════════════════════
# Triggers:
#   1) Current price > Rolling All-Time High × ATH_RATIO
#   2) 20-day return > RALLY_THRESHOLD (급등 확인)
#   3) Short-term sigma (20d) / Long-term sigma (252d) > SIGMA_RATIO
#   When ALL 3 conditions met → SELL SIGNAL (50% position trim recommended)

_SELL_ATH_RATIO       = 0.90   # 전고점 90% 이상 (원래 85%에서 상향 — 더 엄격하게)
_SELL_RALLY_THRESHOLD = 0.40   # 20일 상승률 40% 이상 (원래 30%에서 상향 — 큰 상승만 포착)
_SELL_SIGMA_RATIO     = 0.0    # 시그마 조건 비활성화 (SOXL 3x 레버리지 특성상
                                #    시그마 비율은 고점에서 0.7~1.0x, 폭락장에서 1.3~1.4x.
                                #    즉 시그마 급등 = 폭락 신호, 고점 신호가 아님.
                                #    0.0으로 설정해 조건을 항상 통과시킴)
_SELL_SHORT_LOOKBACK  = 20
_SELL_LONG_LOOKBACK   = 252


def get_rolling_ath(prices: pd.Series) -> pd.Series:
    """
    Returns expanding (rolling all-time) high series from the price series.
    First value = first price, monotonically non-decreasing.
    """
    return prices.expanding().max()


def get_20day_return(closes: pd.Series) -> float | None:
    """
    Returns the (close[-1] / close[-21] - 1) return over ~20 trading days.
    Uses shift(20) so the lookback is strictly prior to today; returns None
    if insufficient data.
    """
    if len(closes) < 21:
        return None
    prev = float(closes.iloc[-21])
    curr = float(closes.iloc[-1])
    if prev <= 0:
        return None
    return (curr - prev) / prev


def get_sigma_spike_ratio(closes: pd.Series,
                          short_lookback: int = 20,
                          long_lookback: int = 252,
                          vol_method: str = "EWMA",
                          ewma_lambda: float = 0.94) -> float | None:
    """
    Returns short_sigma / long_sigma.  A ratio > 1.0 means short-term
    volatility is elevated relative to the long-term baseline.  Returns None
    if either sigma can't be computed (not enough data).
    """
    if len(closes) < long_lookback:
        return None
    try:
        short_sigma, _ = _calculate_volatility_from_closes(
            closes, short_lookback, vol_method, ewma_lambda
        )
        long_sigma, _ = _calculate_volatility_from_closes(
            closes, long_lookback, vol_method, ewma_lambda
        )
    except Exception:
        return None
    if long_sigma <= 0:
        return None
    return float(short_sigma / long_sigma)


def check_peak_sell_signal(closes: pd.Series, ath_prices: pd.Series,
                           lookback_days: int = 252) -> dict:
    """
    Evaluates the 3-condition peak sell signal for an asset.

    ATH is computed from `ath_prices` (auto_adjust=True, Close-based per
    standard financial analyst methodology).

    Returns a dict:
      signal: bool       — True when ALL 3 conditions are met
      ath_price: float   — current rolling ATH
      ath_pct: float     — current price / ATH (as %)
      rally_20d: float   — 20-day return
      sigma_ratio: float — short/long sigma ratio
      reasons: list[str] — human-readable conditions met
    """
    result: dict = {
        'signal': False,
        'ath_price': 0.0,
        'ath_pct': 0.0,
        'rally_20d': 0.0,
        'sigma_ratio': 0.0,
        'reasons': [],
        'conditions': {'ath_ok': False, 'rally_ok': False, 'sigma_ok': False},
    }

    if len(closes) < max(lookback_days, 21) or len(ath_prices) < 1:
        return result

    current_price = float(closes.iloc[-1])

    # ── Condition 1: 전고점 85% 이상 ─────────────────────────────────
    # Rolling ATH from ath_prices (Close 기준, auto_adjust=True)
    rolling_ath = get_rolling_ath(ath_prices)
    ath_price = float(rolling_ath.iloc[-1])
    ath_pct = (current_price / ath_price * 100) if ath_price > 0 else 0.0
    condition_ath = bool(ath_pct >= _SELL_ATH_RATIO * 100)

    result['ath_price'] = round(ath_price, 2)
    result['ath_pct'] = round(ath_pct, 1)

    # ── Condition 2: 20일 상승률 30% 이상 ────────────────────────────
    rally_20d = get_20day_return(closes)
    condition_rally = bool(rally_20d is not None and rally_20d >= _SELL_RALLY_THRESHOLD)
    result['rally_20d'] = round(rally_20d * 100, 1) if rally_20d is not None else 0.0

    # ── Condition 3: 시그마 급등 (단기/장기 비율 1.5배) ────────────
    sigma_ratio = get_sigma_spike_ratio(
        closes, _SELL_SHORT_LOOKBACK, _SELL_LONG_LOOKBACK
    )
    condition_sigma = bool(sigma_ratio is not None and sigma_ratio >= _SELL_SIGMA_RATIO)
    result['sigma_ratio'] = round(sigma_ratio, 2) if sigma_ratio is not None else 0.0

    # ── Assemble result ─────────────────────────────────────────────
    result['conditions']['ath_ok'] = condition_ath
    result['conditions']['rally_ok'] = condition_rally
    result['conditions']['sigma_ok'] = condition_sigma

    if condition_ath:
        result['reasons'].append(f"전고점 {ath_pct:.0f}% 도달")
    if condition_rally:
        result['reasons'].append(f"20일 +{result['rally_20d']:.0f}% 급등")
    if condition_sigma:
        result['reasons'].append(f"변동성 {result['sigma_ratio']:.1f}배 급등")

    result['signal'] = condition_ath and condition_rally and condition_sigma

    return result


_COOLDOWN_DAYS = 60  # 매도 후 60거래일(약 3개월) 동안 재매도 금지


def check_peak_sell_signal_with_cooldown(closes: pd.Series, ath_prices: pd.Series,
                                          last_sell_idx: int | None = None,
                                          current_idx: int = 0) -> dict:
    """
    Same as check_peak_sell_signal() but with a cooldown guard:
    - If `last_sell_idx` is not None and the distance from `current_idx`
      to `last_sell_idx` is less than `_COOLDOWN_DAYS`, the signal is
      suppressed (returns signal=False + cooldown_active=True).
    """
    base = check_peak_sell_signal(closes, ath_prices)

    if last_sell_idx is not None:
        days_since_last_sell = current_idx - last_sell_idx
        if days_since_last_sell < _COOLDOWN_DAYS:
            base['signal'] = False
            base['cooldown'] = True
            base['cooldown_remaining'] = _COOLDOWN_DAYS - days_since_last_sell
    else:
        base['cooldown'] = False
        base['cooldown_remaining'] = 0

    return base


def get_period_ath(ticker: str, lookback_days: int = 252, max_retries: int = 3) -> tuple[float | None, str | None]:
    """
    Fetches the trading-period high (전고점) over the lookback window.

    Uses the same standard methodology financial analysts use:
      - Close prices (종가 기준)
      - auto_adjust=True (주식분할/배당 조정 → 과거 데이터와 연속성 유지)

    Same calendar-day buffering approach as _fetch_closes_for_lookback() so
    yfinance's calendar-day `period` still yields enough trading days.
    """
    buffer_days = max(30, int(lookback_days * 0.6) + 30)
    period_days = lookback_days + buffer_days

    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            stock = yf.Ticker(ticker)
            hist = stock.history(period=f"{period_days}d", interval="1d", auto_adjust=True)
            if hist.empty:
                raise ValueError("Data empty.")
            closes = hist['Close'].dropna()
            if closes.empty:
                raise ValueError("No close price data.")
            recent_closes: pd.Series = closes[-lookback_days:] if len(closes) >= lookback_days else closes  # type: ignore[no-redef]
            peak_idx = recent_closes.idxmax()
            peak_price = float(recent_closes.loc[peak_idx])
            peak_date_str = peak_idx.date().strftime("%Y-%m-%d") if isinstance(peak_idx, pd.Timestamp) else str(peak_idx)
            return peak_price, peak_date_str
        except Exception as e:
            last_err = e
            if attempt < max_retries:
                time.sleep(2.0)
    print(f"   ⚠️ {ticker} previous-high fetch failed after {max_retries} attempts: {last_err}")
    return None, None


def calculate_drawdown_and_recovery(prev_close: float, peak_price: float) -> tuple[float, float]:
    """
    Returns (drawdown_pct, recovery_needed_pct), both rounded to 2 decimals.
      - drawdown_pct: % decline of the current price from the previous high
        (전고점 대비 하락률). 0 or negative.
      - recovery_needed_pct: % gain required from the current price to get
        back up to the previous high (전고점 대비 상승 여력/필요 상승률).
        0 or positive.
    """
    drawdown_pct = round((prev_close - peak_price) / peak_price * 100, 2)
    recovery_needed_pct = round((peak_price - prev_close) / prev_close * 100, 2)
    return drawdown_pct, recovery_needed_pct


def format_drawdown_line(ticker: str, prev_close: float, lookback_days: int) -> str | None:
    """
    Builds the Discord line showing decline-from-high and required-rise-to-
    high, both to 2 decimal places. Returns None if the previous high could
    not be fetched (briefing continues without this line for that ticker).
    """
    peak_price, peak_date_str = get_period_ath(ticker, lookback_days)
    if peak_price is None:
        return None

    drawdown_pct, recovery_needed_pct = calculate_drawdown_and_recovery(prev_close, peak_price)
    return (
        f"• 📈 **전고점:** ${peak_price:.2f} ({peak_date_str}) 기준 "
        f"하락률 **{drawdown_pct:.2f}%** / 회복 필요 상승률 **{recovery_needed_pct:.2f}%**"
    )


def get_realtime_sigma(ticker: str, lookback_days: int, vol_method: str = "EWMA", ewma_lambda: float = 0.94) -> float:
    """
    Calculates Sigma using yfinance in real-time.
    (Fetch/retry logic now shared with recompute_sigma_for_ticker() via
    _fetch_closes_for_lookback(), instead of duplicating it here.)
    """
    vol_method = vol_method.upper()
    print(f"📊 Calculating real-time Sigma for {ticker} (Lookback: {lookback_days}/{vol_method})...")

    closes = _fetch_closes_for_lookback(ticker, lookback_days)
    new_sigma, actual_method = _calculate_volatility_from_closes(closes, lookback_days, vol_method, ewma_lambda)
    print(f"✅ {ticker} Sigma calculation success: {new_sigma:.4f} (method: {actual_method})")
    return new_sigma
            

def calculate_loc_price(ticker: str, prev_close: float, cfg: dict) -> float:
    """
    Calculate LOC target price.
    ENTRY_MULTIPLIER must be defined in portfolio_config.json — no hardcoded fallback.
    """
    pos_cfg = cfg.setdefault("POSITIONS", {}).setdefault(ticker, {})
    # ENTRY_MULTIPLIER must be defined in portfolio_config.json (single source of truth)
    multiplier = float(pos_cfg["ENTRY_MULTIPLIER"])
    sigma = pos_cfg.get("DAILY_SIGMA")
    lookback_days = int(pos_cfg.get("LOOKBACK_DAYS", 252))
    vol_method = str(pos_cfg.get("VOL_METHOD", "EWMA")).upper()
    ewma_lambda = float(pos_cfg.get("EWMA_LAMBDA", 0.94))

    if sigma is not None:
        return _calculate_loc_from_sigma(prev_close, sigma, multiplier)

    print(f"  ⚠️ No settings found for {ticker} → Calculating in real-time")
    sigma = get_realtime_sigma(ticker, lookback_days, vol_method, ewma_lambda)
    
    return _calculate_loc_from_sigma(prev_close, sigma, multiplier)


def get_market_score(filepath="signal_report.json"):
    """Returns the market score from the alert system."""
    if not os.path.exists(filepath):
        return 0 
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("total_score", 0)
    except:
        return 0


def calculate_final_loc(base_price: float) -> float:
    """Returns base LOC price without risk discount (disabled by user request)."""
    return base_price


# ═══════════════════════════════════════════════════════════
# ATH Drawdown DCA — Universal Buy-Split Monitor
# ═══════════════════════════════════════════════════════════
# Designed as a config-driven, lifecycle-aware strategy that
# deploys N equal splits as price drops from its All-Time High
# by configured percentages.  State is persisted in the config
# so that a used split is never re-triggered.
#
# Config schema (per-position):
#   "ATH_DCA": {
#       "ENABLED": true,
#       "SPLITS": 3,
#       "TRIGGER_1": "-30%",
#       "TRIGGER_2": "-40%",
#       "TRIGGER_3": "-55%",
#       "STRATEGY": "ATH drawdown DCA"
#   }
#   "ATH_DCA_USED_SPLITS": [1, 2]   ← auto-managed

def _parse_ath_trigger(raw) -> float | None:
    """Parse a trigger value that may be "-30%", -30, or "-30". Returns the
    positive fraction (e.g. 0.30 for -30%), or None on failure."""
    if raw is None:
        return None
    try:
        if isinstance(raw, str):
            s = raw.strip().replace("%", "")
            return abs(float(s)) / 100.0
        if isinstance(raw, (int, float)):
            return abs(float(raw)) / 100.0
    except (ValueError, TypeError):
        return None
    return None


def _compute_ath_dca_config_fingerprint(ath_dca: dict) -> str:
    """
    Create a deterministic fingerprint of ATH_DCA parameters to detect
    config changes. If SPLITS, any TRIGGER_N, or STRATEGY changes, the
    fingerprint changes and `check_ath_dca_signals()` auto-resets used
    splits so the new parameters take effect immediately.
    """
    if not ath_dca:
        return ""
    trigger_keys = sorted(k for k in ath_dca if k.startswith("TRIGGER_"))
    parts = [str(ath_dca.get("SPLITS", 3))]
    for k in trigger_keys:
        parts.append(str(ath_dca[k]))
    parts.append(str(ath_dca.get("STRATEGY", "")))
    return "|".join(parts)


def check_ath_dca_signals(cfg: dict) -> list[str]:
    """
    Evaluate ATH drawdown DCA triggers for every position that has
    ATH_DCA.ENABLED == true.

    For each ticker:
      1. Compute rolling All-Time High from 1 year of Close data.
      2. Calculate current drawdown % from that ATH.
      3. For every TRIGGER_N threshold, if the drawdown meets or
         exceeds it AND that split hasn't been marked as used yet,
         emit a BUY ALERT and persist the split number.
      4. Also emit "imminent" warnings when drawdown is within
         5 percentage points of a trigger.

    State is persisted via pos["ATH_DCA_USED_SPLITS"] (caller must
    save the config after this function returns).
    """
    messages = []
    positions = cfg.get("POSITIONS", {})

    for ticker, pos in positions.items():
        ath_dca = pos.get("ATH_DCA", {})
        if not ath_dca.get("ENABLED", False):
            continue

        total_splits = int(ath_dca.get("SPLITS", 3))
        used: list[int] = pos.get("ATH_DCA_USED_SPLITS", [])
        if not isinstance(used, list):
            used = []

        # ── Config change detection ──────────────────────────────────
        # If TRIGGER values, SPLITS count, or STRATEGY has changed,
        # auto-reset used splits so new parameters take effect.
        current_fp = _compute_ath_dca_config_fingerprint(ath_dca)
        stored_fp = pos.get("ATH_DCA_CONFIG_FINGERPRINT")
        if stored_fp is not None and current_fp != stored_fp:
            pos["ATH_DCA_USED_SPLITS"] = []
            pos.pop("ATH_DCA_CYCLE_ATH", None)
            pos["ATH_DCA_CONFIG_FINGERPRINT"] = current_fp
            used = []
            messages.append(
                f"🔄 **{ticker} ATH DCA 설정 변경 감지 → 분할 상태 초기화됨**\n"
                f"   • 기존: {stored_fp}\n"
                f"   • 변경: {current_fp}"
            )
        elif stored_fp is None:
            # First run with this config — record fingerprint
            pos["ATH_DCA_CONFIG_FINGERPRINT"] = current_fp

        # Parse triggers (handles "-30%", -30, "-30")
        triggers: dict[int, float] = {}
        for i in range(1, total_splits + 1):
            val = _parse_ath_trigger(ath_dca.get(f"TRIGGER_{i}"))
            if val is not None and 0 < val < 1:
                triggers[i] = val

        if not triggers:
            continue

        # Fetch price history & ATH
        try:
            stock = yf.Ticker(ticker)
            hist = stock.history(period="1y", interval="1d", auto_adjust=True)
            if hist.empty:
                continue
            closes = hist["Close"].dropna()
            if len(closes) < 20:
                continue
            current_price = float(closes.iloc[-1])
            rolling_ath_val = float(closes.expanding().max().iloc[-1])
        except Exception as exc:
            messages.append(f"  ⚠️ {ticker} ATH_DCA data fetch failed: {exc}")
            continue

        if rolling_ath_val <= 0:
            continue

        current_dd = (rolling_ath_val - current_price) / rolling_ath_val
        current_dd_pct = current_dd * 100
        changed = False
        triggered_this_run = False

        # Evaluate each trigger in order
        for split_num in sorted(triggers):
            threshold = triggers[split_num]
            if split_num in used:
                continue

            gap_pct = (threshold - current_dd) * 100

            if current_dd >= threshold:
                used.append(split_num)
                changed = True
                triggered_this_run = True
                target_price = round(rolling_ath_val * (1 - threshold), 2)

                messages.append(
                    f"🚨 **{ticker} ATH {split_num}차 DCA 매수 신호!** 🔥\n"
                    f"   • ATH: \\${rolling_ath_val:.2f}\n"
                    f"   • 현재 DD: {current_dd_pct:.1f}% (임계: -{threshold*100:.0f}%)\n"
                    f"   • 현재가: \\${current_price:.2f}\n"
                    f"   • 목표가: \\${target_price:.2f} (이하)\n"
                    f"   • **매수 실행 권장!** (잔여: {total_splits - len(used)}/{total_splits}차)"
                )

            elif gap_pct < 5.0:
                messages.append(
                    f"📡 **{ticker} ATH {split_num}차 DCA 임박!**\n"
                    f"   • ATH: \\${rolling_ath_val:.2f}\n"
                    f"   • 현재 DD: {current_dd_pct:.1f}% (목표: -{threshold*100:.0f}%)\n"
                    f"   • 추가 {gap_pct:.1f}%p 하락 시 트리거"
                )

        # Persist state if changed
        if changed:
            pos["ATH_DCA_USED_SPLITS"] = sorted(used)

        # ── ATH Reset / Re-entry detection ─────────────────────────
        # If all splits have been used, check whether a NEW all-time
        # high has been established since the cycle completed.  When
        # the current ATH exceeds the ATH recorded at cycle-end by
        # at least 1 %, reset ATH_DCA_USED_SPLITS so a fresh drawdown
        # cycle can begin.
        all_used = len(used) >= total_splits
        if all_used:
            cycle_ath = pos.get("ATH_DCA_CYCLE_ATH", None)
            if cycle_ath is not None:
                # ATH at cycle-end was recorded; check if price has
                # surpassed it by >= 1%
                try:
                    cycle_ath_f = float(cycle_ath)
                except (TypeError, ValueError):
                    cycle_ath_f = 0.0

                if cycle_ath_f > 0 and rolling_ath_val > cycle_ath_f * 1.01:
                    # New ATH confirmed — reset for next cycle
                    pos["ATH_DCA_USED_SPLITS"] = []
                    pos.pop("ATH_DCA_CYCLE_ATH", None)
                    changed = True
                    messages.append(
                        f"🔄 **{ticker} ATH DCA 재진입 준비 완료!**\n"
                        f"   • 신규 ATH: \\${rolling_ath_val:.2f} (이전: \\${cycle_ath_f:.2f})\n"
                        f"   • 새로운 하락 사이클 대기 중"
                    )
                    # After reset, the code below will show "대기중" status
                    # so we skip the else branch
                    all_used = False
                elif cycle_ath_f > 0:
                    # Still recovering toward new ATH
                    recovery_pct = (rolling_ath_val / cycle_ath_f - 1) * 100
                    messages.append(
                        f"✅ **{ticker} ATH {total_splits}차 DCA 전체 완료 (재진입 대기)**\n"
                        f"   • 현재 ATH: \\${rolling_ath_val:.2f}\n"
                        f"   • 재진입 조건: 신규 ATH > \\${cycle_ath_f:.2f}\n"
                        f"   • 회복 진행률: {recovery_pct:+.1f}%"
                    )
                    continue  # skip remaining status lines
            else:
                # First time reaching all-used — record the current ATH
                pos["ATH_DCA_CYCLE_ATH"] = round(rolling_ath_val, 2)
                changed = True
                messages.append(
                    f"✅ **{ticker} ATH {total_splits}차 DCA 모두 완료!**\n"
                    f"   • 사이클 ATH 기록: \\${rolling_ath_val:.2f}\n"
                    f"   • 신규 ATH 갱신 시 재진입 대기"
                )
                continue  # skip remaining status lines

        # Status line (skip if a trigger just fired this run to avoid
        # redundancy — the trigger alert already explains the state)
        remaining = [s for s in triggers if s not in used]
        if triggered_this_run:
            continue  # skip duplicate status line

        if not used:
            next_gap = (triggers[1] - current_dd) * 100
            messages.append(
                f"📡 **{ticker} ATH 1차 DCA 임박!**\n"
                f"   • ATH: \\${rolling_ath_val:.2f}\n"
                f"   • 현재 DD: {current_dd_pct:.1f}%\n"
                f"   • 1차(-{triggers[1]*100:.0f}%) 까지: {next_gap:+.1f}%p"
            )
        elif remaining:
            nxt = remaining[0]
            next_gap = (triggers[nxt] - current_dd) * 100
            messages.append(
                f"📊 **{ticker} ATH {nxt}차 DCA 완료**\n"
                f"   • ATH: \\${rolling_ath_val:.2f}\n"
                f"   • 실행: {len(used)}/{total_splits}차 ✅\n"
                f"   • 다음({nxt}차): 추가 {next_gap:+.1f}%p 하락 시"
            )

    return messages


# ═══════════════════════════════════════════════════════════
# MarketStageSystem Integration — All-In Trigger on Bottom Stage 5
# ═══════════════════════════════════════════════════════════
# Same loose, file-based coupling pattern as get_market_score() above:
# MarketStageSystem.py owns market_state.json and writes to it independently
# on its own schedule; this script only ever reads it. No import dependency
# between the two codebases, so either can be changed/redeployed without
# touching the other.

def get_bottom_stage(ticker: str, filepath: str = "market_state.json") -> int:
    """
    Reads the bottom-stage value (0-5) MarketStageSystem.py maintains for
    `ticker`. Returns 0 if the file, ticker entry, or field is missing —
    so the DCA briefing keeps running normally even if the stage tracker
    hasn't run yet, doesn't cover this ticker, or the file is stale/absent.
    """
    if not os.path.exists(filepath):
        return 0
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        return int(data.get(ticker, {}).get("bottom", 0))
    except Exception:
        return 0


def get_all_in_percent(ticker: str, filepath: str = "MarketStage_config.json") -> float | None:
    """
    Reads this ticker's ALL_IN_PERCENT from MarketStageSystem's own config
    file (MarketStage_config.json → TICKERS → <ticker> → ALL_IN_PERCENT).
    MarketStage_config.json is the single source of truth for "which tickers
    trigger an all-in and at what %" — portfolio_config.json is not touched
    for this setting. Returns None if the file, ticker, or key is missing.
    """
    if not os.path.exists(filepath):
        return None
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        tickers_cfg = data.get("TICKERS", {})
        if not isinstance(tickers_cfg, dict):
            return None
        return tickers_cfg.get(ticker, {}).get("ALL_IN_PERCENT")
    except Exception:
        return None


def _format_all_in_line(ticker: str) -> str | None:
    """
    Builds the all-in action line when MarketStageSystem.py reports bottom
    stage 5 for this ticker. Returns None (nothing appended to the briefing)
    when stage != 5.
    """
    if get_bottom_stage(ticker) != 5:
        return None
    pct = get_all_in_percent(ticker)
    if pct is None:
        return (f"• 🔥 **[Stage 5] {ticker} bottom signal confirmed — "
                f"no ALL_IN_PERCENT configured in MarketStage_config.json**")
    return (f"• 🔥 **[Stage 5 All-In] {ticker} bottom signal confirmed → "
            f"{pct}% lump-sum buy recommended (alongside ongoing LOC DCA)**")


# ==============================================================================
# Trend Signal Engine (VIX-free)
# ==============================================================================

def check_macro_and_technical_signals(ticker: str, pos_cfg: dict) -> tuple[bool, bool, str]:
    """
    Evaluates Buy/Sell signals based on investment type and price trends.

    Infrastructure/DCA assets (LONG_YEAR etc.): always return active buy
    signal — the mechanical LOC strategy runs unconditionally, with only
    the sigma-based LOC target determining entry.

    Rotation/End-of-Cycle assets (ROTATION_3M, END_DEC): fetch 120d price
    data for MA-based trend confirmation (MA20/MA60 crossover).
    """
    invest_type = str(pos_cfg.get("INVEST_TYPE", "")).upper()

    # ── Infrastructure / DCA assets (LONG_YEAR, etc.) ───────────────
    # Pure LOC mechanical strategy — no macro overlay needed.
    if invest_type not in {"ROTATION_3M", "END_DEC"}:
        return True, False, "LOC mechanical strategy active"

    # ── Rotation / End-of-Cycle assets ──────────────────────────────
    # Price trend (MA20, MA60) determines signal.
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period="120d", interval="1d", auto_adjust=False)

        if hist.empty:
            return False, False, "Price data delay (neutral)"

        closes = hist['Close'].dropna()
        if len(closes) < 60:
            return False, False, f"Insufficient 60-day data ({len(closes)})"

        current_price = float(closes.iloc[-1])
        close_ma20: pd.Series = closes.rolling(window=20).mean()  # type: ignore[assignment]
        close_ma60: pd.Series = closes.rolling(window=60).mean()  # type: ignore[assignment]
        ma20 = float(close_ma20.iloc[-1])
        ma60 = float(close_ma60.iloc[-1])
    except Exception as e:
        return False, False, f"API delay ({e})"

    buy_signal = bool(current_price > ma20 and current_price > ma60)
    sell_signal = bool(current_price < ma60)
    reason = "MA uptrend (price > MA20 > MA60)" if buy_signal else "MA downtrend or mixed"
    return buy_signal, sell_signal, reason


def _get_nyse_holidays(start_date: date, end_date: date) -> np.ndarray | None:
    try:
        import pandas_market_calendars as mcal
        nyse = mcal.get_calendar("NYSE")
        all_holidays = np.array(nyse.holidays(), dtype="datetime64[D]")
        start64 = np.datetime64(start_date)
        end64 = np.datetime64(end_date)
        return all_holidays[(all_holidays >= start64) & (all_holidays <= end64)]
    except (ImportError, Exception):
        return None


def business_days_elapsed(start_date: date, today: date) -> int:
    """Calculates elapsed business days."""
    if today <= start_date:
        return 0
    holidays = _get_nyse_holidays(start_date, today)
    if holidays is not None:
        return int(np.busday_count(start_date, today, holidays=holidays))
    return int(np.busday_count(start_date, today))


def check_rotation_exit_signal(pos_cfg: dict, today: date) -> tuple[bool, int, int]:
    """Checks if ROTATION_3M position has reached expiration."""
    invest_type = str(pos_cfg.get("INVEST_TYPE", "")).upper()
    if invest_type != "ROTATION_3M":
        return False, 0, 0

    start_str = pos_cfg.get("START_DATE")
    if not start_str:
        return False, 0, 0

    try:
        start_date = datetime.strptime(start_str, "%Y-%m-%d").date()
        exit_days = int(pos_cfg.get("ROTATION_EXIT_DAYS", pos_cfg.get("LOOKBACK_DAYS", 63)))
        elapsed_bd = business_days_elapsed(start_date, today)
        return elapsed_bd >= exit_days, elapsed_bd, exit_days
    except ValueError:
        return False, 0, 0


def reset_matured_rotation_positions(cfg: dict, today: date) -> list[str]:
    """Resets rotation positions upon reaching maturity."""
    messages = []
    positions = cfg.get("POSITIONS", {})

    for ticker, pos in positions.items():
        rotation_due, elapsed_bd, exit_days = check_rotation_exit_signal(pos, today)
        if not rotation_due:
            continue

        try:
            new_sigma = recompute_sigma_for_ticker(ticker, pos, today)
            pos["START_DATE"] = today.strftime("%Y-%m-%d")
            messages.append(
                f"🔄 {ticker} D+{exit_days} maturity reached → Position reset "
                f"(Business days elapsed: {elapsed_bd} / New cycle starting / Sigma recalculated: {new_sigma:.4f})"
            )
        except Exception as e:
            messages.append(f"⚠️ {ticker} reset failed — Manual check required: {e}")

    return messages


def format_position_meta(pos_cfg: dict, today: date) -> str:
    parts = []
    invest_type = pos_cfg.get("INVEST_TYPE")
    if invest_type:
        parts.append(str(invest_type))

    start_str = pos_cfg.get("START_DATE")
    if start_str:
        try:
            start_date = datetime.strptime(start_str, "%Y-%m-%d").date()
            # Business days now, to match the unit check_rotation_exit_signal()
            # actually uses for maturity — previously this showed calendar
            # days, so the D+N here didn't line up with the rotation D+N
            # printed elsewhere in the same briefing.
            parts.append(f"D+{business_days_elapsed(start_date, today)}")
        except ValueError:
            parts.append(f"START_DATE error: {start_str}")

    return f" | {' / '.join(parts)}" if parts else ""


def _format_loc_action_line(ticker: str, prev_close: float, cfg: dict) -> str:
    base_loc = calculate_loc_price(ticker, prev_close, cfg)
    final_loc = calculate_final_loc(base_loc)

    if base_loc != final_loc:
        return f"• 🎯 **[Action] LOC Buy:** ~~${base_loc:.2f}~~ ➡ **${final_loc:.2f}** (Risk Discount)"
    return f"• 🎯 **[Action] LOC Buy:** **${final_loc:.2f}**"


# ═══════════════════════════════════════════════════════════
# Discord Dispatcher
# ═══════════════════════════════════════════════════════════
def _send_discord(webhook_url: str, user_id: str, title: str, content: str) -> None:
    if not webhook_url:
        print("⚠️ DISCORD_WEBHOOK not set — Skipping send.")
        return
    if not webhook_url.startswith("https://discord.com/api/webhooks/"):
        print(f"⚠️ DISCORD_WEBHOOK invalid format: {webhook_url[:40]}...")
        return

    if len(title) > _DISCORD_TITLE_LIMIT:
        title = title[:_DISCORD_TITLE_LIMIT - 3] + "..."
    if len(content) > _DISCORD_CONTENT_LIMIT:
        content = content[:_DISCORD_CONTENT_LIMIT - 3] + "..."

    payload = {
        "content": f"<@{user_id}>" if user_id else "",
        "embeds": [{
            "title": title,
            "description": content,
            "color": 3447003,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }],
    }
    try:
        resp = requests.post(webhook_url, json=payload, timeout=15)
        if resp.ok:
            print(f"✅ Discord briefing sent successfully.")
        resp.raise_for_status()
    except Exception as e:
        print(f"❌ Discord transmission failed: {e}")


# ═══════════════════════════════════════════════════════════
# Monthly Ping
# ═══════════════════════════════════════════════════════════
def send_monthly_ping_if_due(cfg: dict, webhook: str, user_id: str, now_ny: datetime) -> None:
    # Takes now_ny explicitly instead of calling datetime.now() (which on
    # GitHub Actions returns UTC) — keeps the "is it the 1st?" check on the
    # same America/New_York clock the rest of the script uses.
    if now_ny.day != 1:
        return
    today_ym = now_ny.strftime("%Y-%m")
    if cfg.get("LAST_MONTHLY_PING") == today_ym:
        return
    
    msg = f"🔔 **Monthly Ping** | {now_ny.strftime('%Y-%m')}\nOperation system running normally."
    _send_discord(webhook, user_id, f"📅 Monthly Operation Ping", msg)
    
    cfg["LAST_MONTHLY_PING"] = today_ym
    save_portfolio(cfg)


# ═══════════════════════════════════════════════════════════
# RSI + Volume Composite Buy Signal (Verified Optimal Strategy)
# ═══════════════════════════════════════════════════════════
#
# SOXL (12yr backtest, RSI 14): Zone 1 RSI 25~34 Vol 0.3~0.7 | Zone 2 RSI 34~40 Vol 0.4~0.9
#   → Sharpe 2.62 | WR 71.4% | Avg +21.56%  (vs previous 25~32/32~40: Sharpe 2.46)
#
# TQQQ (12yr backtest, RSI 21): Zone 1 RSI 25~35 Vol 0.3~0.7 | Zone 2 RSI 35~50 Vol 0.4~1.0
#   → Sharpe 1.30 | WR 67.3% | Avg +7.48%   (vs RSI14 D-3:  Sharpe 1.48, RSI21 best: 3.57)


def _calculate_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI — identical to MarketStageSystem.py implementation."""
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss_clean = avg_loss.replace(0, float('nan'))
    rs = avg_gain / avg_loss_clean
    result: pd.Series = 100 - (100 / (1 + rs))
    return result


# ── Ticker-specific optimal zones (12-year backtest verified) ────
# Each ticker has its own RSI period and zone parameters:
#   - SOXL: RSI(14) — fast enough for 3x semi volatility
#   - TQQQ: RSI(21) — slower, better for Nasdaq trend filtering
_TICKER_ZONES: dict = {
    "SOXL": {
        "label": "SOXL",
        "yf_ticker": "SOXL",
        "rsi_period": 14,
        # zone1 RSI inclusive [25..34], zone2 RSI strict-lower (34..40]
        "zone1": {"name": "저RSI 저볼륨",  "rsi": (25, 34), "vol": (0.3, 0.7)},
        "zone2": {"name": "중간RSI 중볼륨", "rsi": (34, 40), "vol": (0.4, 0.9)},
        "stats": "Sharpe 2.62 | 승률 71% | 12yr 백테스트",
    },
    "TQQQ": {
        "label": "TQQQ",
        "yf_ticker": "TQQQ",
        "rsi_period": 21,
        # zone1 RSI inclusive [25..35], zone2 RSI strict-lower (35..50]
        "zone1": {"name": "저RSI 저볼륨",  "rsi": (25, 35), "vol": (0.3, 0.7)},
        "zone2": {"name": "중간RSI 중볼륨", "rsi": (35, 50), "vol": (0.4, 1.0)},
        "stats": "Sharpe 1.30 | 승률 67% | 12yr 백테스트",
    },
}


def _check_rsi_volume_signal(ticker: str) -> str | None:
    """
    Evaluate the composite RSI+Volume entry signal for SOXL or TQQQ.
    Returns a formatted Discord line, or None if ticker not supported or data unavailable.

    최적 조건이 충족되면 **🔥🔥 적극 매수 추천!** 을 강조 표시합니다.
    """
    ticker_upper = ticker.upper()
    if ticker_upper not in _TICKER_ZONES:
        return None

    zones = _TICKER_ZONES[ticker_upper]
    yf_symbol = zones["yf_ticker"]
    rsi_period = zones.get("rsi_period", 14)  # ticker-specific RSI period
    z1 = zones["zone1"]
    z2 = zones["zone2"]
    stats_line = zones["stats"]

    z1_rsi_min, z1_rsi_max = z1["rsi"]
    z1_vol_min, z1_vol_max = z1["vol"]
    z2_rsi_min, z2_rsi_max = z2["rsi"]
    z2_vol_min, z2_vol_max = z2["vol"]

    try:
        stock = yf.Ticker(yf_symbol)
        hist = stock.history(period="6mo", interval="1d", auto_adjust=False)

        if hist.empty or len(hist) < 40:
            return None

        # Flatten MultiIndex columns if present
        df = hist.copy()
        if isinstance(df.columns, pd.MultiIndex):
            df = df.droplevel(1, axis=1)

        if 'Close' not in df.columns or 'Volume' not in df.columns:
            return None

        prices = df['Close'].astype(float).dropna()
        volumes = df['Volume'].astype(float).dropna()

        if len(prices) < 35 or len(volumes) < 22:
            return None

        # RSI calculation (ticker-specific period: SOXL=14, TQQQ=21)
        rsi_series = _calculate_rsi(prices, rsi_period).dropna()
        if len(rsi_series) < 1:
            return None
        latest_rsi = float(rsi_series.iloc[-1])

        # 20-day Volume MA (shifted by 1 to avoid look-ahead)
        vol_ma20 = volumes.shift(1).rolling(20).mean().dropna()
        if len(vol_ma20) < 1 or pd.isna(vol_ma20.iloc[-1]) or vol_ma20.iloc[-1] == 0:
            return None

        latest_vol = float(volumes.iloc[-1])
        latest_vol_ma20 = float(vol_ma20.iloc[-1])
        vol_ratio = latest_vol / latest_vol_ma20

        # ── Zone Evaluation ──────────────────────────────────
        # zone1: inclusive on both ends [z1_rsi_min .. z1_rsi_max]
        # zone2: strict-lower on RSI  (z2_rsi_min .. z2_rsi_max]
        #   → RSI == z2_rsi_min belongs to zone1 only (no overlap, no gap)
        zone1_active = (z1_rsi_min <= latest_rsi <= z1_rsi_max) and \
                       (z1_vol_min <= vol_ratio <= z1_vol_max)
        zone2_active = (z2_rsi_min < latest_rsi <= z2_rsi_max) and \
                       (z2_vol_min <= vol_ratio <= z2_vol_max)

        # ── Signal Strength Classification ───────────────────
        buy_zone = None
        if zone1_active and zone2_active:
            buy_zone = "BOTH"
        elif zone1_active:
            buy_zone = "ZONE1"
        elif zone2_active:
            buy_zone = "ZONE2"

        # ── Build the Discord message ────────────────────────
        z1_label = f"RSI {z1_rsi_min}~{z1_rsi_max} Vol {z1_vol_min}~{z1_vol_max}×"
        z2_label = f"RSI {z2_rsi_min}~{z2_rsi_max} Vol {z2_vol_min}~{z2_vol_max}×"

        if buy_zone == "BOTH":
            return (
                f"\n🚨 **🔥🔥🔥 {ticker_upper} 적극 매수 추천! 🔥🔥🔥**\n"
                f"   📡 **RSI+Volume Signal:** 두 구역 동시 충족!\n"
                f"   └ RSI: **{latest_rsi:.1f}** | Vol: **{vol_ratio:.2f}×** 20일 평균\n"
                f"   └ ✅ [{z1_label}]  ✅ [{z2_label}]\n"
                f"   └ {stats_line}\n"
                f"   **▸ 즉시 LOC 매수 진입을 적극 검토하세요!**"
            )
        elif buy_zone == "ZONE1":
            return (
                f"\n🔥 **{ticker_upper} 매수 신호 발생!**\n"
                f"   📡 **RSI+Volume Signal:** {z1['name']} 구역 충족\n"
                f"   └ RSI: **{latest_rsi:.1f}** | Vol: **{vol_ratio:.2f}×** 20일 평균\n"
                f"   └ ✅ [{z1_label}] | [{z2_label}]\n"
                f"   └ {stats_line}\n"
                f"   **▸ LOC 매수 진입을 고려하세요** (12년 백테스트 검증)"
            )
        elif buy_zone == "ZONE2":
            return (
                f"\n🔥 **{ticker_upper} 매수 신호 발생!**\n"
                f"   📡 **RSI+Volume Signal:** {z2['name']} 구역 충족\n"
                f"   └ RSI: **{latest_rsi:.1f}** | Vol: **{vol_ratio:.2f}×** 20일 평균\n"
                f"   └ [{z1_label}] | ✅ [{z2_label}]\n"
                f"   └ {stats_line}\n"
                f"   **▸ LOC 매수 진입을 고려하세요** (12년 백테스트 검증)"
            )
        else:
            return (
                f"\n📡 **{ticker_upper} RSI+Volume:** ⏸️ 대기 (조건 미충족)\n"
                f"   └ RSI: {latest_rsi:.1f} | Vol: {vol_ratio:.2f}× 20일 평균\n"
                f"   └ [{z1_label}] | [{z2_label}]"
            )

    except Exception as exc:
        print(f"⚠️ {ticker_upper} RSI+Volume signal check failed: {exc}")
        return None


# ═══════════════════════════════════════════════════════════
# Briefing Builder
# ═══════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════
# Dual-Mode Strategy Orchestrator — LOC ↔ ATH_DCA
# ═══════════════════════════════════════════════════════════
# Mode definitions:
#   "LOC"     — Normal mode: 20-split Sigma-based LOC buying
#   "ATH_DCA" — Crash mode:  3-split ATH drawdown DCA buying
#
# Transition logic:
#   LOC → ATH_DCA: ATH drawdown >= TRIGGER_1 (crash detected)
#   ATH_DCA → LOC: Manual only (set STRATEGY_MODE="LOC" to resume)

def _evaluate_strategy_mode(ticker: str, pos: dict) -> str:
    """
    Evaluate whether a position's STRATEGY_MODE should switch.
    Returns the new mode ("LOC" or "ATH_DCA") without writing it.
    The caller is responsible for persisting the change.

    LOC → ATH_DCA transition:
      1. ATH_DCA.ENABLED must be true
      2. Current ATH drawdown >= TRIGGER_1 threshold

    ATH_DCA → LOC transition:
      Manual only — set STRATEGY_MODE back to "LOC" in portfolio_config.json
      to resume normal LOC buying after ATH DCA cycle is complete.
    """
    current_mode = str(pos.get("STRATEGY_MODE", "LOC")).upper()
    ath_dca = pos.get("ATH_DCA", {})

    # If ATH DCA is not enabled, always stay in LOC mode
    if not ath_dca.get("ENABLED", False):
        return "LOC"

    trigger_1_raw = _parse_ath_trigger(ath_dca.get("TRIGGER_1"))

    if current_mode == "LOC":
        # Check if we should switch to ATH_DCA mode
        if trigger_1_raw is None:
            return "LOC"
        try:
            stock = yf.Ticker(ticker)
            hist = stock.history(period="1y", interval="1d", auto_adjust=True)
            if hist.empty:
                return "LOC"
            closes = hist["Close"].dropna()
            if len(closes) < 20:
                return "LOC"
            current_price = float(closes.iloc[-1])
            rolling_ath = float(closes.expanding().max().iloc[-1])
            if rolling_ath <= 0:
                return "LOC"
            current_dd = (rolling_ath - current_price) / rolling_ath
        except Exception as exc:
            print(f"  ⚠️ {ticker} mode eval failed: {exc}")
            return "LOC"

        if current_dd >= trigger_1_raw:
            print(f"🔄 {ticker}: LOC → ATH_DCA mode switch (DD={current_dd*100:.1f}% >= T1={trigger_1_raw*100:.0f}%)")
            return "ATH_DCA"
        return "LOC"

    else:  # current_mode == "ATH_DCA"
        # Once in crash mode, stay until user manually reverts STRATEGY_MODE to LOC
        return "ATH_DCA"


def _evaluate_all_strategy_modes(cfg: dict) -> list[str]:
    """
    Evaluate strategy mode for every position and persist changes.
    Returns a list of mode-switch notification messages.
    """
    messages = []
    positions = cfg.get("POSITIONS", {})
    for ticker, pos in positions.items():
        new_mode = _evaluate_strategy_mode(ticker, pos)
        current_mode = str(pos.get("STRATEGY_MODE", "LOC")).upper()
        if new_mode != current_mode:
            pos["STRATEGY_MODE"] = new_mode
            messages.append(
                f"🔄 **{ticker}: {current_mode} → {new_mode} 모드 전환**"
            )
        # Ensure field exists even if no change
    return messages


def _build_briefing_lines(now_ny: datetime, cfg: dict) -> list[str]:
    lines = [f"🌙 **U.S. Market LOC Portfolio Briefing** ({now_ny.strftime('%Y-%m-%d %H:%M %Z')})"]
    today_ny = now_ny.date()
    
    market_score = get_market_score()
    lines.append(f"📊 **Market Risk Score:** {market_score} / 14")
    lines.append("─" * 40)

    positions = cfg.get("POSITIONS", {})

    for ticker, pos_cfg in positions.items():
        strategy_mode = str(pos_cfg.get("STRATEGY_MODE", "LOC")).upper()
        buy_sig, sell_sig, reason = check_macro_and_technical_signals(ticker, pos_cfg)
        
        prev_close, last_date_str = get_prev_close(ticker)
        if prev_close is None:
            lines.append(f"\n🔹 **{ticker}** — Price lookup failed ⚠️")
            continue

        position_meta = format_position_meta(pos_cfg, today_ny)
        lines.append(f"\n🔹 **{ticker}** (Close: ${prev_close:.2f} | {last_date_str}{position_meta})")

        lookback_days = int(pos_cfg.get("LOOKBACK_DAYS", 252))
        drawdown_line = format_drawdown_line(ticker, prev_close, lookback_days)
        if drawdown_line:
            lines.append(drawdown_line)

        # Mode indicator
        mode_icon = "📗" if strategy_mode == "LOC" else "🚨"
        mode_label = "Normal (LOC)" if strategy_mode == "LOC" else "CRASH (ATH DCA)"
        lines.append(f"• **Mode:** {mode_icon} {mode_label}")

        lines.append(f"• **Signals:** Buy[{buy_sig}] / Sell[{sell_sig}] | {reason}")

        rotation_due, elapsed_bd, exit_days = check_rotation_exit_signal(pos_cfg, today_ny)
        if rotation_due:
            lines.append(f"• 🔴 **[D+{exit_days} Rotation Maturity] Period expired — Review for sell! (Elapsed: {elapsed_bd} days)**")

        if strategy_mode == "LOC":
            # Normal mode: show LOC action line
            if sell_sig is True:
                lines.append("• 🚨 **[Warning] Risk area — Check LOC criteria conservatively**")
                lines.append(_format_loc_action_line(ticker, prev_close, cfg))
            else:
                lines.append(_format_loc_action_line(ticker, prev_close, cfg))

            # RSI+Volume composite buy signal (LOC mode only)
            rsi_vol_line = _check_rsi_volume_signal(ticker)
            if rsi_vol_line:
                lines.append(rsi_vol_line)
        else:
            # ATH_DCA (crash) mode: LOC paused, show notice
            lines.append("• 🔴 **[LOC Paused] — ATH DCA crash mode active**")
            lines.append(f"• 🎯 **[Action] LOC Buy:** **SUSPENDED** (ATH_DCA mode)")

        all_in_line = _format_all_in_line(ticker)
        if all_in_line:
            lines.append(all_in_line)

    # ── ATH Drawdown DCA Monitor ────────────────────────────────────
    ath_dca_lines = check_ath_dca_signals(cfg)
    if ath_dca_lines:
        lines.append("")
        lines.append("─" * 40)
        lines.append("📉 **ATH Drawdown DCA Monitor**")
        for line in ath_dca_lines:
            lines.append(line)

    # NOTE: sigma_messages (recompute/rotation-reset/error notices) are
    # intentionally NOT appended to the Discord content anymore — they're
    # printed to the console (see execute_dual_tactical_trader) so they still
    # show up in the GitHub Actions log, without cluttering the notification.

    return lines


# ═══════════════════════════════════════════════════════════
# Execution Loop
# ═══════════════════════════════════════════════════════════
def execute_dual_tactical_trader() -> None:
    """Run integrated macro signal & LOC automation (dual-mode)"""
    now_ny = datetime.now(ZoneInfo("America/New_York"))
    
    cfg = load_portfolio()
    webhook = os.environ.get("DISCORD_WEBHOOK") or cfg.get("DISCORD_WEBHOOK", "")
    user_id = os.environ.get("DISCORD_USER_ID") or cfg.get("DISCORD_USER_ID", "")

    reset_messages = reset_matured_rotation_positions(cfg, now_ny.date())
    status_messages = reset_messages + refresh_sigma_if_stale(cfg)

    # ── Dual-mode: evaluate and switch strategy modes ────────────────
    mode_messages = _evaluate_all_strategy_modes(cfg)
    status_messages.extend(mode_messages)

    # Keep these visible in the GitHub Actions run log even though they no
    # longer appear in the Discord notification content.
    for msg in status_messages:
        print(msg)

    # Build briefing (also runs ATH DCA monitor which may update config)
    briefing_lines = _build_briefing_lines(now_ny, cfg)

    # Persist ALL config changes (sigma updates, rotation resets, ATH DCA state, mode switches)
    save_portfolio(cfg)

    _send_discord(
        webhook_url=webhook, 
        user_id=user_id, 
        title=f"📋 AI & Semi Portfolio LOC Briefing", 
        content="\n".join(briefing_lines)
    )
    
    try:
        send_monthly_ping_if_due(cfg, webhook, user_id, now_ny)
    except Exception as e:
        print(f"⚠️ Error sending monthly ping: {e}")


if __name__ == "__main__":
    execute_dual_tactical_trader()