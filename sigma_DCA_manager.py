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


def get_period_high(ticker: str, lookback_days: int = 252, max_retries: int = 3) -> tuple[float | None, str | None]:
    """
    Fetches the previous high (전고점) over the lookback window, using the
    High column (true intraday high) rather than Close — 전고점 in
    Korean retail-investor usage typically means the highest price ever
    touched, not just the highest closing price. Same calendar-day
    buffering approach as _fetch_closes_for_lookback() so yfinance's
    calendar-day `period` still yields enough trading days.
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
            highs = hist['High'].dropna()
            if highs.empty:
                raise ValueError("No high price data.")
            recent_highs: pd.Series = highs[-lookback_days:] if len(highs) >= lookback_days else highs  # type: ignore[no-redef]
            peak_idx = recent_highs.idxmax()
            peak_price = float(recent_highs.loc[peak_idx])
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
    peak_price, peak_date_str = get_period_high(ticker, lookback_days)
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
    """Adjusts LOC price based on risk score."""
    score = get_market_score()
    if score >= 10: discount = 0.95
    elif score >= 6: discount = 0.98
    else: discount = 1.0
    return base_price * discount


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
# Briefing Builder
# ═══════════════════════════════════════════════════════════
def _build_briefing_lines(now_ny: datetime, cfg: dict) -> list[str]:
    lines = [f"🌙 **U.S. Market LOC Portfolio Briefing** ({now_ny.strftime('%Y-%m-%d %H:%M %Z')})"]
    today_ny = now_ny.date()
    
    market_score = get_market_score()
    lines.append(f"📊 **Market Risk Score:** {market_score} / 14")
    lines.append("─" * 40)

    positions = cfg.get("POSITIONS", {})

    for ticker, pos_cfg in positions.items():
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

        lines.append(f"• **Signals:** Buy[{buy_sig}] / Sell[{sell_sig}] | {reason}")

        rotation_due, elapsed_bd, exit_days = check_rotation_exit_signal(pos_cfg, today_ny)
        if rotation_due:
            lines.append(f"• 🔴 **[D+{exit_days} Rotation Maturity] Period expired — Review for sell! (Elapsed: {elapsed_bd} days)**")

        if sell_sig is True:
            lines.append("• 🚨 **[Warning] Risk area — Check LOC criteria conservatively**")
            lines.append(_format_loc_action_line(ticker, prev_close, cfg))
        else:
            lines.append(_format_loc_action_line(ticker, prev_close, cfg))

        all_in_line = _format_all_in_line(ticker)
        if all_in_line:
            lines.append(all_in_line)

    # NOTE: sigma_messages (recompute/rotation-reset/error notices) are
    # intentionally NOT appended to the Discord content anymore — they're
    # printed to the console (see execute_dual_tactical_trader) so they still
    # show up in the GitHub Actions log, without cluttering the notification.

    return lines


# ═══════════════════════════════════════════════════════════
# Execution Loop
# ═══════════════════════════════════════════════════════════
def execute_dual_tactical_trader() -> None:
    """Run integrated macro signal & LOC automation"""
    now_ny = datetime.now(ZoneInfo("America/New_York"))
    
    cfg = load_portfolio()
    webhook = os.environ.get("DISCORD_WEBHOOK") or cfg.get("DISCORD_WEBHOOK", "")
    user_id = os.environ.get("DISCORD_USER_ID") or cfg.get("DISCORD_USER_ID", "")

    reset_messages = reset_matured_rotation_positions(cfg, now_ny.date())
    status_messages = reset_messages + refresh_sigma_if_stale(cfg)
    save_portfolio(cfg)

    # Keep these visible in the GitHub Actions run log even though they no
    # longer appear in the Discord notification content.
    for msg in status_messages:
        print(msg)

    briefing_lines = _build_briefing_lines(now_ny, cfg)
    
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