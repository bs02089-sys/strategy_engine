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
import requests
import yfinance as yf
from datetime import datetime, timezone, date
from zoneinfo import ZoneInfo


# ====================== Encoding Settings ======================
if hasattr(sys.stdout, "reconfigure"):
    getattr(sys.stdout, "reconfigure")(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    getattr(sys.stderr, "reconfigure")(encoding="utf-8")
    
CONFIG_PATH            = "portfolio_config.json"
_DISCORD_TITLE_LIMIT   = 256
_DISCORD_CONTENT_LIMIT = 4096
SYSTEM_TAG             = "[STAT]"


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
        pos.setdefault("ENTRY_MULTIPLIER", 1.41)
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
    
def get_prev_close(ticker: str) -> tuple[float | None, str]:
    """
    Reliable previous close lookup using yfinance (with 3 retries)
    """
    print(f"🔍 Starting price lookup for {ticker}...")
    now_ny = datetime.now(ZoneInfo("America/New_York"))
    today_ny = now_ny.date()
    
    for attempt in range(1, 4):
        try:
            print(f"   → Trying yfinance history ({attempt}/3)...")
            stock = yf.Ticker(ticker)
            hist = stock.history(period="15d", interval="1d", auto_adjust=False, rounding=True)
            
            if not hist.empty:
                close_series = hist['Close'].dropna()
                if not close_series.empty:
                    last_idx = close_series.index[-1]
                    last_date = last_idx.date() if hasattr(last_idx, "date") else last_idx
                    
                    # Logic: If during trading hours, use index -2, otherwise index -1
                    if last_date == today_ny and now_ny.hour < 16 and len(close_series) >= 2:
                        prev_close = float(close_series.iloc[-2])
                        prev_date = close_series.index[-2].date()
                        date_str = prev_date.strftime("%m-%d")
                    else:
                        prev_close = float(close_series.iloc[-1])
                        date_str = last_date.strftime("%m-%d")
                        
                    print(f"✅ {ticker} yfinance success: ${prev_close:.2f} ({date_str})")
                    return prev_close, date_str

            print(f"   ⚠️ Attempt {attempt}: empty data returned.")

        except Exception as e:
            print(f"   ⚠️ Attempt {attempt} failed: {e}")

        # Backoff regardless of whether we hit an exception or just got
        # empty data back — previously the sleep only ran on exceptions.
        if attempt < 3:
            time.sleep(2.0)

    # Info fallback
    try:
        print(f"   → Trying yfinance info fallback...")
        info = yf.Ticker(ticker).info
        for key in ["previousClose", "regularMarketPreviousClose", "currentPrice", "regularMarketPrice"]:
            price = _safe_float(info.get(key))
            if price is not None and not np.isnan(price):
                print(f"✅ {ticker} info success: ${price:.2f} (key: {key})")
                return price, "N/A"
    except Exception as e:
        print(f"   ⚠️ Info fallback failed: {e}")

    print(f"❌ All price lookup methods failed for {ticker}")
    return None, "N/A"


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
    """
    pos_cfg = cfg.setdefault("POSITIONS", {}).setdefault(ticker, {})
    multiplier = pos_cfg.get("ENTRY_MULTIPLIER", 1.41)
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


# ==============================================================================
# Macro/Technical Signal Engine
# ==============================================================================

def get_current_vix() -> float | None:
    try:
        vix_hist = yf.Ticker("^VIX").history(period="5d", interval="1d")
        if vix_hist.empty:
            return None
        vix_closes = vix_hist['Close'].dropna()
        if vix_closes.empty:
            return None
        return float(vix_closes.iloc[-1])
    except Exception:
        return None


def check_macro_and_technical_signals(ticker: str, pos_cfg: dict, current_vix: float | None) -> tuple[bool, bool, str]:
    """
    Evaluates Buy/Sell signals based on investment type, price trends, and VIX.
    """
    if current_vix is None:
        return False, False, "VIX data delay (neutral)"

    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period="120d", interval="1d", auto_adjust=False)
        
        if hist.empty:
            return False, False, "Price data delay (neutral)"

        closes = hist['Close'].dropna()
        if len(closes) < 60:
            return False, False, f"Insufficient 60-day data ({len(closes)})"
            
        current_price = float(closes.iloc[-1])
        ma20 = float(closes.rolling(window=20).mean().iloc[-1])
        ma60 = float(closes.rolling(window=60).mean().iloc[-1])
    except Exception as e:
        return False, False, f"API delay ({e})"

    invest_type = str(pos_cfg.get("INVEST_TYPE", "")).upper()

    # 1. Rotation/End-of-Cycle Assets -> Only enter when in a steady uptrend
    if invest_type in {"ROTATION_3M", "END_DEC"}:
        buy_signal = bool(current_price > ma20 and current_price > ma60 and current_vix < 20)
        sell_signal = bool(current_price < ma60 or current_vix > 25)
        reason = f"Uptrend (VIX: {current_vix:.1f})" if buy_signal else "Mixed trend or risk management"
    
    # 2. Infrastructure/DCA Assets -> Enter unless VIX indicates panic
    else:
        buy_signal = bool(current_vix < 23)
        sell_signal = bool(current_vix > 28)
        reason = "AI infra cycle valid" if buy_signal else "Global macro risk"

    return buy_signal, sell_signal, reason


def _get_nyse_holidays(start_date: date, end_date: date) -> np.ndarray | None:
    try:
        import pandas_market_calendars as mcal
    except ImportError:
        return None

    try:
        nyse = mcal.get_calendar("NYSE")
        all_holidays = np.array(nyse.holidays(), dtype="datetime64[D]")
        start64 = np.datetime64(start_date)
        end64 = np.datetime64(end_date)
        return all_holidays[(all_holidays >= start64) & (all_holidays <= end64)]
    except Exception:
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
    except ValueError:
        return False, 0, 0

    exit_days = int(pos_cfg.get("ROTATION_EXIT_DAYS", pos_cfg.get("LOOKBACK_DAYS", 63)))
    elapsed_bd = business_days_elapsed(start_date, today)
    return elapsed_bd >= exit_days, elapsed_bd, exit_days


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
        return f"• 🎯 {SYSTEM_TAG} **[Action] LOC Buy:** ~~${base_loc:.2f}~~ ➡️ **${final_loc:.2f}** (Risk Discount)"
    return f"• 🎯 {SYSTEM_TAG} **[Action] LOC Buy:** **${final_loc:.2f}**"


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
    _send_discord(webhook, user_id, f"🗓️ {SYSTEM_TAG} Monthly Operation Ping", msg)
    
    cfg["LAST_MONTHLY_PING"] = today_ym
    save_portfolio(cfg)


# ═══════════════════════════════════════════════════════════
# Briefing Builder
# ═══════════════════════════════════════════════════════════
def _build_briefing_lines(now_ny: datetime, cfg: dict) -> list[str]:
    lines = [f"🌙 {SYSTEM_TAG} **U.S. Market LOC Portfolio Briefing** ({now_ny.strftime('%Y-%m-%d %H:%M %Z')})"]
    today_ny = now_ny.date()
    
    market_score = get_market_score()
    lines.append(f"📊 **Market Risk Score:** {market_score} / 14")
    lines.append("─" * 40)

    positions = cfg.get("POSITIONS", {})
    current_vix = get_current_vix()

    for ticker, pos_cfg in positions.items():
        buy_sig, sell_sig, reason = check_macro_and_technical_signals(ticker, pos_cfg, current_vix)
        
        prev_close, last_date_str = get_prev_close(ticker)
        if prev_close is None:
            lines.append(f"\n🔹 **{ticker}** — Price lookup failed ⚠️")
            continue

        position_meta = format_position_meta(pos_cfg, today_ny)
        lines.append(f"\n🔹 **{ticker}** (Close: ${prev_close:.2f} | {last_date_str}{position_meta})")
        lines.append(f"• **Signals:** Buy[{buy_sig}] / Sell[{sell_sig}] | {reason}")

        rotation_due, elapsed_bd, exit_days = check_rotation_exit_signal(pos_cfg, today_ny)
        if rotation_due:
            lines.append(f"• 🔴 **[D+{exit_days} Rotation Maturity] Period expired — Review for sell! (Elapsed: {elapsed_bd} days)**")

        if sell_sig is True:
            invest_type = str(pos_cfg.get("INVEST_TYPE", "")).upper()
            if invest_type in {"ROTATION_3M", "END_DEC"}:
                lines.append("• 🚨 **[Warning] Risk area — Check LOC criteria conservatively**")
                lines.append(_format_loc_action_line(ticker, prev_close, cfg))
            else:
                lines.append("• 🚨 **[Warning] Risk area — Review LOC execution probability**")
                lines.append(_format_loc_action_line(ticker, prev_close, cfg))
        elif buy_sig is True:
            lines.append(_format_loc_action_line(ticker, prev_close, cfg))
        else:
            lines.append("• 🟡 **[Note] Neutral area — No active buy signal, but mechanical LOC order remains valid**")
            lines.append(_format_loc_action_line(ticker, prev_close, cfg))

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
    sigma_messages = reset_messages + refresh_sigma_if_stale(cfg)
    save_portfolio(cfg) 

    # Keep these visible in the GitHub Actions run log even though they no
    # longer appear in the Discord notification content.
    for msg in sigma_messages:
        print(f"📝 [System Log] {msg}")

    briefing_lines = _build_briefing_lines(now_ny, cfg)
    
    _send_discord(
        webhook_url=webhook, 
        user_id=user_id, 
        title=f"📋 {SYSTEM_TAG} AI & Semi Portfolio LOC Briefing", 
        content="\n".join(briefing_lines)
    )
    
    try:
        send_monthly_ping_if_due(cfg, webhook, user_id, now_ny)
    except Exception as e:
        print(f"⚠️ Error sending monthly ping: {e}")


if __name__ == "__main__":
    execute_dual_tactical_trader()