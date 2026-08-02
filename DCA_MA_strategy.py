"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Sigma DCA 통합 엔진 (완결판) — 실전 운용 + 백테스트 + 신호
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
기존 sigma_DCA_manager.py(실전 엔진) + DCA_MA_strategy.py(백테스트/신호)를
하나로 통합한 단일 파일입니다 (2026-08-02).

실전 엔진 (기본 실행):
  - 일일 Discord 브리핑: LOC 목표가 · Sigma 자동 갱신 · 전고점 50% 청산 ·
    ATH 하락분할 DCA · MA 레짐 필터 · 로테이션 초기화 · 시장 바닥 단계
  - --ath-monitor: 장중 실시간 ATH DCA 알림 (cron-job.org → repository_dispatch)

전략 모드 (백테스트/신호):
  - --backtest: 시그마 DCA + MA 레짐 필터 백테스트 (티커별 기본 설정)
  - --signal: 실시간 신호 (종가+날짜 · LOC 매수가 · ATH 대비 MDD · 비상 트리거)
  - --signal --discord: Discord 발송 | --all: 전 종목(TQQQ+SOXL) 단일 메시지

Usage:
  python3 DCA_MA_strategy.py                              # 일일 브리핑 (기본)
  python3 DCA_MA_strategy.py --ath-monitor                # 장중 실시간 모니터
  python3 DCA_MA_strategy.py --backtest                   # TQQQ 백테스트 (MA20 lump)
  python3 DCA_MA_strategy.py --backtest --ticker SOXL     # SOXL 백테스트
  python3 DCA_MA_strategy.py --signal                     # 오늘 신호 (TQQQ)
  python3 DCA_MA_strategy.py --signal --discord --all     # 전 종목 신호 → Discord
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


def resolve_discord_config(cfg: dict) -> tuple[str, str]:
    """Resolve Discord webhook URL and user ID from config with env var override.

    Environment variables (GitHub Actions secrets) take precedence over
    file-based values so credentials can be kept out of version control.
    Shared by both DCA_MA_strategy.py and MarketStageSystem.py.
    """
    webhook = os.environ.get("DISCORD_WEBHOOK") or cfg.get("DISCORD_WEBHOOK", "")
    user_id = os.environ.get("DISCORD_USER_ID") or cfg.get("DISCORD_USER_ID", "")
    return webhook, user_id


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


def _is_stage5_trigger(raw) -> bool:
    """Check if a trigger value is the special 'STAGE5' keyword,
    meaning the trigger fires when MarketStageSystem reports bottom
    stage 5 for the ticker — the market bottom confirmation becomes
    the buy signal for this split."""
    if raw is None:
        return False
    if isinstance(raw, str):
        return raw.strip().upper() == "STAGE5"
    return False


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


def check_ath_dca_signals(cfg: dict,
                          realtime_prices: dict | None = None,
                          alerts_only: bool = False) -> list[str]:
    """
    Evaluate ATH drawdown DCA triggers for every position that has
    ATH_DCA.ENABLED == true.

    Supports two trigger types:
      - "PCT": Percentage-based (e.g. "-60%") — fires when ATH drawdown
        meets or exceeds the configured threshold.
      - "STAGE5": Market-stage based — fires when MarketStageSystem
        reports bottom stage 5 for the ticker, confirming the market
        bottom. This serves as the final split trigger in the ATH DCA
        3-split emergency mode.

    For each ticker:
      1. Compute rolling All-Time High from 1 year of Close data.
      2. Calculate current drawdown % from that ATH.
      3. For every TRIGGER_N, evaluate the trigger type:
         - PCT: check drawdown vs threshold
         - STAGE5: check get_bottom_stage(ticker) == 5
      4. If trigger fires AND split not yet used → emit BUY ALERT
         and persist the split number.
      5. For PCT triggers only: emit "imminent" warning when
         drawdown is within 5 percentage points.

    State is persisted via pos["ATH_DCA_USED_SPLITS"] (caller must
    save the config after this function returns).

    Optional parameters (used by the realtime --ath-monitor mode):
      - realtime_prices: {ticker: current_price} overrides the yfinance
        last-close so drawdown is measured against live prices (Finnhub).
      - alerts_only: emit ONLY actionable alerts (🚨 trigger / 📡 imminent
        within 5%p) and suppress recurring status lines. Imminent alerts
        are deduplicated via pos["ATH_DCA_IMMINENT_SENT"] so frequent
        polls don't spam — a warning re-sends only when the gap narrows
        by >= 1.0%p since the previous alert.
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
            pos["ATH_DCA_IMMINENT_SENT"] = {}
            pos.pop("ATH_DCA_ENTERED_ON", None)  # fresh cycle — restart recovery clock
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

        # Parse triggers: supports both percentage ("-30%") and STAGE5 keyword
        triggers: dict[int, tuple[str, float]] = {}
        for i in range(1, total_splits + 1):
            raw = ath_dca.get(f"TRIGGER_{i}")
            if _is_stage5_trigger(raw):
                triggers[i] = ("STAGE5", 0.0)
            else:
                val = _parse_ath_trigger(raw)
                if val is not None and 0 < val < 1:
                    triggers[i] = ("PCT", val)

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
            # Realtime mode: prefer live price (Finnhub) over the yfinance
            # last-close so intraday trigger/imminent events fire now.
            if realtime_prices and ticker in realtime_prices:
                current_price = float(realtime_prices[ticker])
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
            trigger_type, threshold = triggers[split_num]
            if split_num in used:
                continue

            if trigger_type == "STAGE5":
                # ── Stage 5 Bottom Trigger ────────────────────────
                # Fires when MarketStageSystem confirms bottom stage 5.
                # This integrates the former standalone _format_all_in_line()
                # into the ATH DCA split system as the final split trigger.
                if get_bottom_stage(ticker) == 5:
                    used.append(split_num)
                    changed = True
                    triggered_this_run = True
                    pos.get("ATH_DCA_IMMINENT_SENT", {}).pop(str(split_num), None)
                    # Use the actual current drawdown as the effective threshold
                    effective_threshold = current_dd
                    target_price = round(rolling_ath_val * (1 - effective_threshold), 2)
                    messages.append(
                        f"🚨 **{ticker} ATH {split_num}차 DCA 매수 신호! 🔥🔥 [Stage 5 Bottom Confirmed]**\n"
                        f"   • ATH: \\${rolling_ath_val:.2f}\n"
                        f"   • 현재 DD: {current_dd_pct:.1f}%\n"
                        f"   • **시장 바닥(Stage 5) 감지 → 마지막 분할 매수 실행!**\n"
                        f"   • 현재가: \\${current_price:.2f}\n"
                        f"   • 목표가: \\${target_price:.2f} (이하)\n"
                        f"   • **매수 실행 권장!** (잔여: {total_splits - len(used)}/{total_splits}차)"
                    )
            else:
                # ── Percentage-based Trigger ──────────────────────
                gap_pct = (threshold - current_dd) * 100

                if current_dd >= threshold:
                    used.append(split_num)
                    changed = True
                    triggered_this_run = True
                    pos.get("ATH_DCA_IMMINENT_SENT", {}).pop(str(split_num), None)
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
                    # Realtime mode: dedupe so an imminent warning isn't
                    # re-sent every poll — re-alert only when the gap has
                    # narrowed by >= 1.0%p since the previous alert.
                    if alerts_only:
                        sent = pos.setdefault("ATH_DCA_IMMINENT_SENT", {})
                        prev_gap = sent.get(str(split_num))
                        if prev_gap is not None and gap_pct >= prev_gap - 1.0:
                            continue
                        sent[str(split_num)] = round(gap_pct, 2)
                    messages.append(
                        f"📡 **{ticker} ATH {split_num}차 DCA 임박!**\n"
                        f"   • ATH: \\${rolling_ath_val:.2f}\n"
                        f"   • 현재 DD: {current_dd_pct:.1f}% (목표: -{threshold*100:.0f}%)\n"
                        f"   • 추가 {gap_pct:.1f}%p 하락 시 트리거"
                    )

        # Persist state if changed
        if changed:
            pos["ATH_DCA_USED_SPLITS"] = sorted(used)

        # Realtime mode: only trigger/imminent alerts belong here — cycle
        # tracking, re-entry detection and status lines are owned by the
        # nightly briefing (which saves them right after this call).
        if alerts_only:
            continue

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
                    pos["ATH_DCA_IMMINENT_SENT"] = {}  # clear stale dedup state
                    pos.pop("ATH_DCA_ENTERED_ON", None)  # new cycle — restart recovery clock
                    changed = True
                    messages.append(
                        f"🔄 **{ticker} ATH DCA 사이클 재시작 준비 완료!**\n"
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
                        f"✅ **{ticker} ATH {total_splits}차 DCA 전체 완료 (사이클 재시작 대기)**\n"
                        f"   • 현재 ATH: \\${rolling_ath_val:.2f}\n"
                        f"   • 사이클 재시작 조건: 신규 ATH > \\${cycle_ath_f:.2f}\n"
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
                    f"   • 신규 ATH 갱신 시 사이클 재시작 대기"
                )
                continue  # skip remaining status lines

        # Status line (skip if a trigger just fired this run to avoid
        # redundancy — the trigger alert already explains the state)
        remaining = [s for s in triggers if s not in used]
        if triggered_this_run:
            continue  # skip duplicate status line

        if not used:
            first_type, first_threshold = triggers[1]
            if first_type == "PCT":
                next_gap = (first_threshold - current_dd) * 100
                messages.append(
                    f"📡 **{ticker} ATH 1차 DCA 임박!**\n"
                    f"   • ATH: \\${rolling_ath_val:.2f}\n"
                    f"   • 현재 DD: {current_dd_pct:.1f}%\n"
                    f"   • 1차(-{first_threshold*100:.0f}%) 까지: {-next_gap:+.1f}%p"
                )
            else:  # STAGE5 — no percentage gap to show
                messages.append(
                    f"📡 **{ticker} ATH 1차 DCA 임박!**\n"
                    f"   • ATH: \\${rolling_ath_val:.2f}\n"
                    f"   • 현재 DD: {current_dd_pct:.1f}%\n"
                    f"   • 1차(Stage 5 바닥 감지 시) 대기 중"
                )
        elif remaining:
            nxt = remaining[0]
            nxt_type, nxt_threshold = triggers[nxt]
            if nxt_type == "PCT":
                next_gap = (nxt_threshold - current_dd) * 100
                messages.append(
                    f"📊 **{ticker} ATH {nxt}차 DCA**\n"
                    f"   • ATH: \\${rolling_ath_val:.2f}\n"
                    f"   • 실행: {len(used)}/{total_splits}차 ✅\n"
                    f"   • 다음({nxt}차): 추가 {next_gap:+.1f}%p 하락 시"
                )
            else:  # STAGE5
                messages.append(
                    f"📊 **{ticker} ATH {nxt}차 DCA**\n"
                    f"   • ATH: \\${rolling_ath_val:.2f}\n"
                    f"   • 실행: {len(used)}/{total_splits}차 ✅\n"
                    f"   • 다음({nxt}차): Stage 5 바닥 감지 시 트리거"
                )

    return messages


# ═══════════════════════════════════════════════════════════
# MarketStageSystem Integration — Stage 5 → ATH DCA 3차 Trigger
# ═══════════════════════════════════════════════════════════
# Same loose, file-based coupling pattern as get_market_score() above:
# MarketStageSystem.py owns market_state.json and writes to it independently
# on its own schedule; this script only ever reads it. No import dependency
# between the two codebases, so either can be changed/redeployed without
# touching the other.
#
# get_bottom_stage() is consumed by check_ath_dca_signals() when a
# TRIGGER_N is set to "STAGE5" — Stage 5 (market bottom confirmation)
# becomes the trigger for the corresponding ATH DCA split.

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


# ==============================================================================
# Trend Signal Engine
#
# MA 기반 추세 신호로 ROTATION_3M/END_DEC 자산의 매수/매도
# 타이밍을 결정합니다. VIX(공포지수)는 사용하지 않습니다.
# (VIX는 bear_market_signals.py에서 별도 처리)
#
# 모든 함수는 execute_dual_tactical_trader()에서 사용 중이므로
# 삭제하지 마십시오.
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
    """
    Returns NYSE holidays in [start_date, end_date] as a datetime64[D] array.

    Uses the calendar's `valid_days()` API instead of the removed `holidays()`
    method: pandas_market_calendars 5.x changed `nyse.holidays()` to return a
    CustomBusinessDay offset object (no longer a DatetimeIndex), which silently
    broke the old conversion and made business-day counting ignore holidays.
    Holidays here = all weekdays in range minus valid trading days, which is
    exactly the complement np.busday_count() needs.
    """
    try:
        import pandas_market_calendars as mcal
        nyse = mcal.get_calendar("NYSE")
        start64 = np.datetime64(start_date)
        end64 = np.datetime64(end_date) + np.timedelta64(1, "D")  # inclusive end
        all_days = np.arange(start64, end64, dtype="datetime64[D]")
        all_weekdays = all_days[np.is_busday(all_days)]
        valid = np.array(
            nyse.valid_days(start_date=start_date, end_date=end_date),
            dtype="datetime64[D]",
        )
        return np.setdiff1d(all_weekdays, valid)
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
#   LOC → ATH_DCA: ATH drawdown >= TRIGGER_1 (crash detected)  [automatic]
#   ATH_DCA → LOC: Recovery re-entry (backtest-validated rule) [automatic]
#                  DD narrowed to DD_RATIO × TRIGGER_1 + MA20>MA60
#                  + MIN_DAYS business days elapsed (bear-trap filter).
#                  Manual override (STRATEGY_MODE="LOC") still works.
#
# Recovery re-entry preserves ATH_DCA_USED_SPLITS, so if the market
# re-crashes the existing automatic LOC→ATH_DCA switch resumes and the
# unused 2차/3차 splits continue from the reserved cash (safety net).


def _compute_ath_drawdown(ticker: str) -> tuple[float, float, float] | None:
    """
    Fetch 1y Close history and compute (current_dd, rolling_ath, current_price).
    Returns None on data failure. Shared by the LOC→ATH_DCA switch check and
    the recovery re-entry check (single yfinance fetch, single source of truth).
    """
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period="1y", interval="1d", auto_adjust=True)
        if hist.empty:
            return None
        closes = hist["Close"].dropna()
        if len(closes) < 20:
            return None
        current_price = float(closes.iloc[-1])
        rolling_ath = float(closes.expanding().max().iloc[-1])
        if rolling_ath <= 0:
            return None
        current_dd = (rolling_ath - current_price) / rolling_ath
        return current_dd, rolling_ath, current_price
    except Exception as exc:
        print(f"  ⚠️ {ticker} drawdown fetch failed: {exc}")
        return None


def _fetch_ma_alignment(ticker: str) -> tuple[float, float, float] | None:
    """
    Fetch 120d Close history and compute (current_price, ma20, ma60).
    Returns None on data failure or insufficient history. Same MA20/MA60
    methodology as check_macro_and_technical_signals().
    """
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period="120d", interval="1d", auto_adjust=False)
        if hist.empty:
            return None
        closes = hist["Close"].dropna()
        if len(closes) < 60:
            return None
        current_price = float(closes.iloc[-1])
        ma20 = float(closes.rolling(window=20).mean().iloc[-1])
        ma60 = float(closes.rolling(window=60).mean().iloc[-1])
        return current_price, ma20, ma60
    except Exception as exc:
        print(f"  ⚠️ {ticker} MA fetch failed: {exc}")
        return None


def _check_recovery_reentry(ticker: str, pos: dict) -> str | None:
    """
    Evaluate the backtest-validated recovery re-entry rule for a position
    currently in ATH_DCA (crash) mode.

    Returns a human-readable reason string when the position should switch
    back to LOC, or None when it should stay in ATH_DCA.

    Conditions (validated by backtest, 2026-08-02):
      1. At least one ATH split still unused (2차/3차 reserved as safety net)
      2. >= MIN_DAYS business days elapsed since ATH_DCA entry (bear-trap
         filter; the 2024-08 V-recovery backtest fired on D+43)
      3. Drawdown narrowed to <= DD_RATIO × TRIGGER_1 (recovery confirmed)
      4. MA20 > MA60 (bullish alignment) when MA_CONFIRM is true

    Per-position config (defaults shown):
      "RECOVERY_REENTRY": {
        "ENABLED": false,    # OPT-IN — true enables automatic re-entry
        "DD_RATIO": 0.5,     # DD must narrow to 50%% of TRIGGER_1
        "MIN_DAYS": 30,      # business days since crash entry
        "MA_CONFIRM": true   # also require MA20 > MA60
      }
    State (auto-managed): pos["ATH_DCA_ENTERED_ON"] = "YYYY-MM-DD"

    User chose full LOC resume (no budget cap) — on re-entry the normal
    20-split LOC ladder runs again; 2차/3차 reserves stay available in
    ATH_DCA_USED_SPLITS for a re-crash.
    """
    ath_dca = pos.get("ATH_DCA", {})
    rec = pos.get("RECOVERY_REENTRY", {})
    if not rec.get("ENABLED", False):
        return None

    # 1) Require at least one split still reserved
    total_splits = int(ath_dca.get("SPLITS", 3))
    used = pos.get("ATH_DCA_USED_SPLITS", []) or []
    if not isinstance(used, list):
        used = []
    remaining = [s for s in range(1, total_splits + 1) if s not in used]
    if not remaining:
        return None  # all splits used — wait for a fresh ATH cycle

    # 2) Minimum elapsed business days since crash entry
    today = datetime.now(ZoneInfo("America/New_York")).date()
    entered_str = pos.get("ATH_DCA_ENTERED_ON")
    if not entered_str:
        # First evaluation with recovery enabled — start the clock now
        pos["ATH_DCA_ENTERED_ON"] = today.strftime("%Y-%m-%d")
        return None
    try:
        entered_date = datetime.strptime(entered_str, "%Y-%m-%d").date()
    except ValueError:
        pos["ATH_DCA_ENTERED_ON"] = today.strftime("%Y-%m-%d")
        return None
    min_days = int(rec.get("MIN_DAYS", 30))
    if min_days < 1:
        min_days = 30  # guard against misconfigured bear-trap filter
    elapsed = business_days_elapsed(entered_date, today)
    if elapsed < min_days:
        return None

    # 3) Drawdown narrowed to DD_RATIO × TRIGGER_1
    trigger_1 = _parse_ath_trigger(ath_dca.get("TRIGGER_1"))
    if trigger_1 is None:
        return None
    dd_info = _compute_ath_drawdown(ticker)
    if dd_info is None:
        return None
    current_dd, _, _ = dd_info
    dd_ratio = float(rec.get("DD_RATIO", 0.5))
    if not 0 < dd_ratio < 1:
        dd_ratio = 0.5  # guard against misconfigured DD_RATIO
    if current_dd > trigger_1 * dd_ratio:
        return None

    # 4) MA20 > MA60 (bullish alignment) when MA_CONFIRM
    if rec.get("MA_CONFIRM", True):
        ma_info = _fetch_ma_alignment(ticker)
        if ma_info is None:
            return None
        price, ma20, ma60 = ma_info
        if not (price > ma20 > ma60):
            return None

    return (f"DD {current_dd*100:.1f}% ≤ {dd_ratio*100:.0f}%×T1({trigger_1*100:.0f}%)"
            f" + MA20>MA60 (D+{elapsed}, 예비분 {len(remaining)}차 보존)")


def _evaluate_strategy_mode(ticker: str, pos: dict) -> str:
    """
    Evaluate whether a position's STRATEGY_MODE should switch.
    Returns the new mode ("LOC" or "ATH_DCA") without writing the mode
    itself — the caller persists STRATEGY_MODE. NOTE: this function MAY
    record the state field ATH_DCA_ENTERED_ON into pos as a side effect
    (recovery re-entry clock); the caller's save_portfolio() persists it.

    When automatic recovery re-entry fires, the reason string (DD/MA/D+N)
    is stored in the transient key pos["_RECOVERY_REASON"] so callers can
    surface it in notifications; _build_briefing_lines() consumes it and
    pops it before save_portfolio().

    LOC → ATH_DCA transition:
      1. ATH_DCA.ENABLED must be true
      2. Current ATH drawdown >= TRIGGER_1 threshold
      (on switch, records ATH_DCA_ENTERED_ON for the recovery clock)

    ATH_DCA → LOC transition:
      Automatic recovery re-entry (_check_recovery_reentry) — backtest-
      validated rule that switches back to LOC once the market has
      recovered (DD narrowed + MA bullish + min days elapsed). Manual
      override (STRATEGY_MODE="LOC") still works as before.
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
        dd_info = _compute_ath_drawdown(ticker)
        if dd_info is None:
            return "LOC"
        current_dd, _, _ = dd_info

        if current_dd >= trigger_1_raw:
            # Record crash-entry date so the recovery re-entry clock starts
            # (only when switching INTO crash mode, not on re-evaluation)
            today = datetime.now(ZoneInfo("America/New_York")).date()
            pos["ATH_DCA_ENTERED_ON"] = today.strftime("%Y-%m-%d")
            print(f"🔄 {ticker}: LOC → ATH_DCA mode switch (DD={current_dd*100:.1f}% >= T1={trigger_1_raw*100:.0f}%)")
            return "ATH_DCA"
        return "LOC"

    else:  # current_mode == "ATH_DCA"
        # Automatic recovery re-entry: switch back to LOC once the market
        # has recovered (backtest-validated). Preserves unused splits so
        # 2차/3차 resume on a re-crash. Falls back to staying in crash mode
        # (legacy manual-only behavior) when conditions aren't met.
        reason = _check_recovery_reentry(ticker, pos)
        if reason is not None:
            # Transient — consumed by _build_briefing_lines (Discord Mode
            # line) and popped before save_portfolio, so it never persists.
            pos["_RECOVERY_REASON"] = reason
            print(f"🔄 {ticker}: ATH_DCA → LOC 비상 모드 종료 ({reason})")
            return "LOC"
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
            if current_mode == "ATH_DCA" and new_mode == "LOC":
                # ATH_DCA → LOC happens automatically via recovery re-entry
                # (or manual override) — include the reason for transparency.
                detail = f" — {pos.get('_RECOVERY_REASON', '')}" if pos.get("_RECOVERY_REASON") else ""
                messages.append(
                    f"🔄 **{ticker}: {current_mode} → {new_mode} 모드 전환 (비상 모드 종료){detail}**"
                )
            else:
                messages.append(
                    f"🔄 **{ticker}: {current_mode} → {new_mode} 모드 전환**"
                )
        # Ensure field exists even if no change
    return messages


RECOVERY_NUDGE_MILESTONES = (5, 1)  # remaining business days that fire a one-time 🔔 pre-alert


def _recovery_clock(pos_cfg: dict, today_ny: date) -> tuple[int, int, int, date] | None:
    """Return (elapsed_bd, min_days, remaining, entered_date) for a
    recovery-enabled ATH_DCA position, or None when the recovery wait
    monitor does not apply (LOC mode / RECOVERY_REENTRY disabled /
    ATH_DCA_ENTERED_ON missing or unparseable).

    Shared by the nightly briefing wait line (_recovery_wait_line) and the
    realtime D-5/D-1 pre-alert (_recovery_nudge_line) so both use the exact
    same bear-trap clock.
    """
    if str(pos_cfg.get("STRATEGY_MODE", "LOC")).upper() == "LOC":
        return None
    rec_block = pos_cfg.get("RECOVERY_REENTRY", {})
    if not rec_block.get("ENABLED", False):
        return None
    entered_str = pos_cfg.get("ATH_DCA_ENTERED_ON")
    if not entered_str:
        return None
    try:
        entered_date = datetime.strptime(entered_str, "%Y-%m-%d").date()
    except ValueError:
        return None
    min_days = int(rec_block.get("MIN_DAYS", 30))
    if min_days < 1:
        min_days = 30
    elapsed_bd = business_days_elapsed(entered_date, today_ny)
    return elapsed_bd, min_days, min_days - elapsed_bd, entered_date


def _recovery_wait_line(ticker: str, pos_cfg: dict, today_ny: date) -> str | None:
    """Return the recovery re-entry wait-monitor line for a position, or None.

    Shown ONLY while the bear-trap clock is still running:
      - strategy_mode is ATH_DCA (not LOC)
      - RECOVERY_REENTRY.ENABLED is true
      - ATH_DCA_ENTERED_ON is set and parseable
      - business_days_elapsed < MIN_DAYS

    Shows both the business-day countdown (the bear-trap clock) and the
    calendar days since crash entry, so the remaining wait is visible in
    both units. Positions already past the wait (e.g. TQQQ D+90) return
    None, so the nightly briefing and the --ath-monitor realtime channel
    stay clean once re-entry is actually possible.
    """
    clock = _recovery_clock(pos_cfg, today_ny)
    if clock is None:
        return None
    elapsed_bd, min_days, remaining, entered_date = clock
    if remaining <= 0:
        return None
    cal_days = (today_ny - entered_date).days
    return (
        f"• ⏳ **{ticker} 비상 모드 종료 대기:** D+{elapsed_bd}/{min_days} "
        f"영업일 (남은 {remaining}영업일 | 진입 {entered_date.strftime('%Y-%m-%d')}, "
        f"경과 {cal_days}일)"
    )


def _recovery_nudge_line(ticker: str, pos_cfg: dict, today_ny: date) -> str | None:
    """Return a one-time 're-entry imminent' pre-alert (🔔), or None.

    Fires once per milestone in RECOVERY_NUDGE_MILESTONES (D-5 and D-1
    business days remaining) via the persisted ATH_DCA_NUDGE_SENT list, so
    a 5-10min realtime scheduler can't repeat it. If the bear-trap clock
    resets (remaining jumps above the max milestone — e.g. a fresh crash
    cycle), stale marks are cleared so the next countdown re-fires.
    """
    clock = _recovery_clock(pos_cfg, today_ny)
    if clock is None:
        return None
    elapsed_bd, min_days, remaining, _ = clock
    if remaining > max(RECOVERY_NUDGE_MILESTONES):
        pos_cfg.pop("ATH_DCA_NUDGE_SENT", None)  # fresh clock — reset stale marks
        return None
    if remaining not in RECOVERY_NUDGE_MILESTONES:
        return None
    sent = pos_cfg.setdefault("ATH_DCA_NUDGE_SENT", [])
    if remaining in sent:
        return None
    sent.append(remaining)
    return (
        f"🔔 **{ticker} 비상 모드 종료 임박!** 남은 대기 {remaining}영업일 "
        f"(D+{elapsed_bd}/{min_days}) — MIN_DAYS 클럭이 곧 끝납니다. "
        f"회복 신호 시 LOC 모드 자동 복귀 예정."
    )


# ═══════════════════════════════════════════════════════════
# MA 레짐 필터 (Moving-Average Regime Filter) — 백테스트 검증 반영
# ═══════════════════════════════════════════════════════════
# DCA_MA_strategy.py에서 검증된
# 레짐 필터를 실전에 반영한 것:
#   - LOC 모드      : MA 하향 돌파 → 전량 청산 + 매수 금지
#                     MA 상향 돌파 → TQQQ: 전액 재매수 / SOXL: DCA 재개
#   - ATH_DCA 비상 모드: MA 필터 OFF (분할 매수 진행 중 개입 안 함)
#   - 비상 모드 종료(리커버리 리엔트리) → LOC 복귀 후 MA 필터 재활성
#
# Config schema (per-position):
#   "MA_FILTER": {
#       "ENABLED": true,
#       "MA_DAYS": 20,          # TQQQ 20 / SOXL 250
#       "REENTRY": "lump",      # TQQQ: "lump"(전액), SOXL: "dca_reset"(DCA 재개)
#       "REENTRY_PCT": 1.0       # lump 전액 비율 (선택)
#   }
# State (auto-managed): pos["MA_FILTER_STATE"] = {"regime", "since"}

def _drop_unsettled_today_bar(closes: pd.Series) -> pd.Series:
    """장중(당일 미체결) 상태면 마지막 바(오늘)를 제외한 확정 종가만 반환.

    get_prev_close()와 동일한 정산(settle) 기준을 사용해, 실시간 모니터가
    장중에 돌아도 인트라데이 가격으로 거짓 크로스가 발생하지 않도록 한다.
    야간 브리핑(장 마감 후 실행)에서는 당일 확정 종가가 그대로 쓰인다.
    """
    now_ny = datetime.now(ZoneInfo("America/New_York"))
    today_ny = now_ny.date()
    market_close_settled_at = datetime.combine(
        today_ny,
        dtime(NY_MARKET_CLOSE_HOUR, NY_MARKET_CLOSE_MINUTE),
        tzinfo=ZoneInfo("America/New_York"),
    ) + timedelta(minutes=NY_CLOSE_SETTLE_BUFFER_MINUTES)
    if now_ny >= market_close_settled_at:
        return closes
    last_idx = closes.index[-1]
    last_date = last_idx.date() if isinstance(last_idx, pd.Timestamp) else pd.Timestamp(last_idx).date()
    if last_date == today_ny and len(closes) >= 2:
        return closes.iloc[:-1]
    return closes


def _fetch_ma_closes(ticker: str, ma_days: int, max_retries: int = 3):
    """MA 계산용 종가 조회 — 백테스트와 동일하게 auto_adjust=True(조정 종가).

    yfinance의 `period`는 달력일 기준이라, 확정 거래일 수를 보장하기 위해
    달력일 버퍼를 더한다 (기존 _fetch_closes_for_lookback()와 동일 패턴).
    """
    buffer_days = max(30, int(ma_days * 0.6) + 30)
    period_days = ma_days + buffer_days
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            stock = yf.Ticker(ticker)
            hist = stock.history(period=f"{period_days}d", interval="1d", auto_adjust=True)
            if hist.empty:
                raise ValueError("Data empty.")
            closes = hist["Close"].dropna()
            if len(closes) < ma_days:
                raise ValueError(f"Insufficient data ({len(closes)}/{ma_days}).")
            closes = _drop_unsettled_today_bar(closes)
            if len(closes) < ma_days:
                raise ValueError(f"Insufficient settled data ({len(closes)}/{ma_days}).")
            return closes
        except Exception as e:
            last_err = e
            if attempt < max_retries:
                time.sleep(2.0)
    raise RuntimeError(f"❌ {ticker} MA data fetch failed after {max_retries} attempts") from last_err


def _check_ma_filter(ticker: str, pos: dict) -> dict:
    """
    MA 레짐 필터를 평가한다. pos["MA_FILTER_STATE"]를 갱신하며
    (호출자가 save_portfolio로 영속화) 크로스 이벤트는 전환 시 1회만 감지한다.

    Returns:
      enabled        : MA_FILTER.ENABLED
      ma_days, close, ma_val, ma_pct
      regime         : "above" | "below" | None
      days_in_regime : 현재 레짐 지속 영업일 수
      crossed_down   : 이번 평가에서 하향 돌파 (신규 이벤트)
      crossed_up     : 이번 평가에서 상향 돌파 (신규 이벤트)
      reentry        : "lump" | "dca_reset"
      reentry_pct    : lump 재진입 비율
      suspended      : ATH_DCA 비상 모드면 True (필터 OFF — 상태만 추적)
    """
    base: dict = {
        "enabled": False, "ma_days": 0, "close": 0.0, "ma_val": 0.0,
        "ma_pct": 0.0, "regime": None, "days_in_regime": 0,
        "crossed_down": False, "crossed_up": False,
        "reentry": "dca_reset", "reentry_pct": 1.0, "suspended": False,
    }
    mf = pos.get("MA_FILTER", {})
    if not mf.get("ENABLED", False):
        return base

    ma_days = int(mf.get("MA_DAYS", 20))
    if ma_days < 2:
        return base
    base["ma_days"] = ma_days
    base["reentry"] = str(mf.get("REENTRY", "dca_reset"))
    base["reentry_pct"] = float(mf.get("REENTRY_PCT", 1.0))
    base["enabled"] = True
    base["suspended"] = str(pos.get("STRATEGY_MODE", "LOC")).upper() == "ATH_DCA"

    closes = _fetch_ma_closes(ticker, ma_days)
    ma_series = closes.rolling(ma_days).mean()
    close_now = float(closes.iloc[-1])
    ma_now = float(ma_series.iloc[-1])
    if not np.isfinite(ma_now) or ma_now <= 0:
        return base

    regime = "above" if close_now > ma_now else "below"
    base["close"] = round(close_now, 2)
    base["ma_val"] = round(ma_now, 2)
    base["ma_pct"] = round((close_now - ma_now) / ma_now * 100, 2)
    base["regime"] = regime

    # 설정 변경(MA 일수/재진입 방식) 감지 시 상태 리셋 — 이전 MA 기간의
    # 레짐이 남아 첫 평가에서 허위 크로스 알림을 내는 것을 방지
    # (ATH_DCA의 CONFIG_FINGERPRINT 패턴과 동일한 방식).
    cfg_fp = f"{ma_days}|{base['reentry']}|{base['reentry_pct']}"
    if pos.get("MA_FILTER_CONFIG_FINGERPRINT") != cfg_fp:
        pos["MA_FILTER_CONFIG_FINGERPRINT"] = cfg_fp
        state = pos["MA_FILTER_STATE"] = {}
    else:
        state = pos.setdefault("MA_FILTER_STATE", {})

    stored = state.get("regime")
    today = datetime.now(ZoneInfo("America/New_York")).date()
    if stored != regime:
        state["regime"] = regime
        state["since"] = today.strftime("%Y-%m-%d")
    if stored == "above" and regime == "below":
        base["crossed_down"] = True
    elif stored == "below" and regime == "above":
        base["crossed_up"] = True

    since_str = state.get("since")
    if since_str:
        try:
            since = datetime.strptime(since_str, "%Y-%m-%d").date()
            base["days_in_regime"] = business_days_elapsed(since, today)
        except ValueError:
            base["days_in_regime"] = 0
    return base


def _ma_filter_lines(info: dict) -> list[str]:
    """MA 레짐 상태/크로스 알림 라인 생성 (일일 브리핑·실시간 모니터 공용)."""
    if not info.get("enabled") or not info.get("regime"):
        return []
    ma_days = info["ma_days"]
    regime = info["regime"]
    label = "MA 위 (보유/매수 가능)" if regime == "above" else "MA 아래 (매수 금지)"
    icon = "🟢" if regime == "above" else "🟡"

    if info.get("suspended"):
        return [f"• 📉 **MA{ma_days} 레짐 (참고):** {icon} {label} — 비상 모드 중 MA 필터 OFF"]

    lines = [
        f"• 📉 **MA{ma_days} 레짐:** 종가 ${info['close']:.2f} vs MA{ma_days} "
        f"${info['ma_val']:.2f} ({info['ma_pct']:+.1f}%) | {icon} {label} "
        f"(레짐 {info.get('days_in_regime', 0)}일)"
    ]
    if info.get("crossed_down"):
        lines.append(
            f"🚨 **MA{ma_days} 하향 돌파 — 전량 청산 + 매수 금지!** "
            f"(종가 ${info['close']:.2f} < MA{ma_days} ${info['ma_val']:.2f})"
        )
    if info.get("crossed_up"):
        if info.get("reentry") == "lump":
            pct = float(info.get("reentry_pct", 1.0))
            verb = "전액 재매수" if pct >= 1.0 else f"{pct*100:.0f}% 재매수"
            lines.append(
                f"💰 **MA{ma_days} 상향 돌파 — {verb} 신호!** "
                f"(종가 ${info['close']:.2f} > MA{ma_days} ${info['ma_val']:.2f})"
            )
        else:
            lines.append(
                f"🔄 **MA{ma_days} 상향 돌파 — DCA 재개 신호!** "
                f"(종가 ${info['close']:.2f} > MA{ma_days} ${info['ma_val']:.2f})"
            )
    return lines


def _build_briefing_lines(now_ny: datetime, cfg: dict) -> list[str]:
    lines = [f"🌙 **U.S. Market LOC Portfolio Briefing** ({now_ny.strftime('%Y-%m-%d %H:%M %Z')})"]
    today_ny = now_ny.date()
    
    market_score = get_market_score()
    lines.append(f"📊 **Market Risk Score:** {market_score} / 14")
    lines.append("─" * 40)

    positions = cfg.get("POSITIONS", {})

    for ticker, pos_cfg in positions.items():
        # Consume the transient recovery-reentry reason FIRST so it is always
        # removed from pos regardless of downstream continue/exception paths
        # (prevents _RECOVERY_REASON leaking into portfolio_config.json).
        recovery_reason = pos_cfg.pop("_RECOVERY_REASON", None)

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

        # Mode indicator (+ recovery re-entry reason if it just fired —
        # already popped at the top of the loop, never reaches config)
        mode_icon = "📗" if strategy_mode == "LOC" else "🚨"
        mode_label = "일반 모드 (LOC)" if strategy_mode == "LOC" else "비상 모드 (ATH DCA)"
        mode_line = f"• **Mode:** {mode_icon} {mode_label}"
        if recovery_reason:
            mode_line += f" | 🔄 **비상 모드 종료** ({recovery_reason})"
        lines.append(mode_line)

        # Recovery re-entry wait monitor — shown ONLY while the bear-trap
        # clock is still running (elapsed < MIN_DAYS). Positions already past
        # the wait (e.g. TQQQ D+90) display nothing, so the briefing stays
        # clean once re-entry is actually possible.
        wait_line = _recovery_wait_line(ticker, pos_cfg, today_ny)
        if wait_line:
            lines.append(wait_line)

        # ── MA 레짐 필터 (백테스트 검증 반영: LOC 모드 활성 / 비상 모드 OFF) ──
        ma_info = {"enabled": False}
        try:
            ma_info = _check_ma_filter(ticker, pos_cfg)
            lines.extend(_ma_filter_lines(ma_info))
        except Exception as e:
            print(f"⚠️ {ticker} MA filter check failed: {e}")
        ma_blocked = bool(ma_info.get("enabled") and not ma_info.get("suspended")
                          and ma_info.get("regime") == "below")

        lines.append(f"• **Signals:** Buy[{buy_sig}] / Sell[{sell_sig}] | {reason}")

        rotation_due, elapsed_bd, exit_days = check_rotation_exit_signal(pos_cfg, today_ny)
        if rotation_due:
            lines.append(f"• 🔴 **[D+{exit_days} Rotation Maturity] Period expired — Review for sell! (Elapsed: {elapsed_bd} days)**")

        if strategy_mode == "LOC":
            if ma_blocked:
                # MA 레짐 아래: 매수 금지 — LOC/RSI 매수 신호 생략
                lines.append(f"• 🚫 **매수 금지 — MA{ma_info.get('ma_days')} 아래 레짐 (현금 대기)**")
            else:
                # Normal mode: show LOC action line
                if sell_sig is True:
                    lines.append("• 🚨 **[Warning] Risk area — Check LOC criteria conservatively**")
                lines.append(_format_loc_action_line(ticker, prev_close, cfg))
                # RSI+Volume composite buy signal (LOC mode only)
                rsi_vol_line = _check_rsi_volume_signal(ticker)
                if rsi_vol_line:
                    lines.append(rsi_vol_line)
        else:
            # ATH_DCA (crash) mode: show LOC price for reference
            lines.append(_format_loc_action_line(ticker, prev_close, cfg))

    # ── ATH Drawdown DCA Monitor (includes Stage 5 All-In as split trigger) ──
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
# Realtime ATH DCA Monitor — --ath-monitor mode
# ═══════════════════════════════════════════════════════════
# GitHub Actions `schedule` cron is best-effort and can be delayed by
# minutes to hours at peak load, so realtime (1~5min) alerting is driven
# by an external scheduler (cron-job.org) that POSTs a GitHub
# `repository_dispatch` event every N minutes → this lightweight mode.
#
# Pipeline:
#   cron-job.org (exact N-min alarm)
#     → POST /repos/{owner}/{repo}/dispatches  (event_type: ath-dca-monitor)
#     → workflow runs: python DCA_MA_strategy.py --ath-monitor
#     → Finnhub /quote realtime price override (fallback: yfinance close)
#     → check_ath_dca_signals(alerts_only=True) → 🚨/📡 only, deduped
#     → Discord webhook (same secrets as the nightly briefing)

def _fetch_finnhub_quote(ticker: str, api_key: str) -> float | None:
    """Fetch the real-time current price from Finnhub /quote (free tier).

    Returns None on failure so the monitor falls back to yfinance's last
    close rather than aborting the whole poll.
    """
    url = f"https://finnhub.io/api/v1/quote?symbol={ticker}&token={api_key}"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        price = _safe_float(data.get("c"))  # "c" = current price
        if price is not None and price > 0:
            print(f"✅ Finnhub {ticker}: ${price:.2f}")
            return price
    except Exception as e:
        print(f"⚠️ Finnhub quote fetch failed for {ticker}: {e}")
    return None


def run_ath_dca_monitor() -> None:
    """Lightweight realtime ATH DCA alerting (--ath-monitor mode).

    Sends ONLY actionable alerts to Discord:
      - 🚨 TRIGGER_1/2/3 fired (매수 신호)
      - 📡 imminent warning (drawdown within 5%p of the next trigger)
      - 🔄 ATH DCA config-change reset (rare + important)
      - ⏳ recovery re-entry wait rollover (once per business day)
      - 🔔 recovery re-entry imminent (one-time at D-5 / D-1 remaining)
      - 🚨/💰/🔄 MA 레짐 크로스 (하향 돌파 → 전량 청산, 상향 돌파 → 재매수/DCA 재개;
        LOC 모드에서만 작동, 비상 모드 중에는 필터 OFF)

    Recurring status lines / cycle tracking are skipped (alerts_only=True)
    and imminent warnings are deduplicated per (ticker, split) via
    ATH_DCA_IMMINENT_SENT persisted in the config, so a 5-min scheduler
    can't spam the channel with the same warning every poll.
    """
    cfg = load_portfolio()
    webhook, user_id = resolve_discord_config(cfg)

    # Realtime price override via Finnhub. Env-var ONLY on purpose — the
    # config file is committed to the repo, so a key stored there would
    # leak into git history. Set FINNHUB_API_KEY as a GitHub Actions secret
    # (and locally via export).
    api_key = os.environ.get("FINNHUB_API_KEY", "")
    realtime_prices: dict[str, float] = {}
    if api_key:
        for ticker, pos in cfg.get("POSITIONS", {}).items():
            ath_dca = pos.get("ATH_DCA", {})
            if ath_dca.get("ENABLED", False):
                price = _fetch_finnhub_quote(ticker, api_key)
                if price is not None:
                    realtime_prices[ticker] = price
    else:
        print("⚠️ FINNHUB_API_KEY not set — using yfinance last close (may lag intraday).")

    # Only alerts (🚨 trigger / 📡 imminent); suppress status lines.
    messages = check_ath_dca_signals(
        cfg, realtime_prices=realtime_prices or None, alerts_only=True
    )

    # Recovery re-entry wait monitor (⏳) — once per business day. The
    # countdown only rolls over on business days, so dedupe via
    # ATH_DCA_WAIT_SENT ("YYYY-MM-DD|D+X/N") to keep a 5-10min poll
    # scheduler from re-sending the identical line all day long.
    today_ny = datetime.now(ZoneInfo("America/New_York")).date()
    for ticker, pos in cfg.get("POSITIONS", {}).items():
        wait_line = _recovery_wait_line(ticker, pos, today_ny)
        if wait_line is None:
            pos.pop("ATH_DCA_WAIT_SENT", None)  # wait over / not eligible — clear stale state
            continue
        sig = f"{today_ny}|{wait_line}"
        if pos.get("ATH_DCA_WAIT_SENT") == sig:
            continue  # already sent for today's countdown value
        pos["ATH_DCA_WAIT_SENT"] = sig
        messages.append(wait_line)

    # Re-entry imminent pre-alert (🔔) — one-time at D-5 / D-1 remaining
    # business days, deduped per milestone via ATH_DCA_NUDGE_SENT.
    for ticker, pos in cfg.get("POSITIONS", {}).items():
        nudge_line = _recovery_nudge_line(ticker, pos, today_ny)
        if nudge_line:
            messages.append(nudge_line)

    # MA 레짐 크로스 알림 (LOC 모드에서만 작동 — 비상 모드 중에는 필터 OFF).
    # 크로스는 레짐 전환 시 1회만 감지되고 MA_FILTER_STATE가 영속화되므로
    # 5~10분 폴링 스케줄러가 같은 신호를 반복 발송하지 않는다.
    for ticker, pos in cfg.get("POSITIONS", {}).items():
        try:
            ma_info = _check_ma_filter(ticker, pos)
            cross_lines = [l for l in _ma_filter_lines(ma_info)
                           if l.startswith(("🚨", "💰", "🔄"))]
            if cross_lines:
                messages.append("\n".join(cross_lines))
        except Exception as e:
            print(f"⚠️ {ticker} MA filter check failed: {e}")

    # Persist ATH_DCA_USED_SPLITS + ATH_DCA_IMMINENT_SENT + ATH_DCA_WAIT_SENT
    # + ATH_DCA_NUDGE_SENT + MA_FILTER_STATE dedup state so the next poll
    # knows what was already emitted.
    save_portfolio(cfg)

    # Only actionable alerts belong in the realtime channel: 🚨 trigger /
    # 📡 imminent, 🔄 config-change reset (rare + important — a changed
    # trigger resets split state), ⏳ wait-period rollover (once per
    # business day), and 🔔 re-entry imminent (once per milestone). The wait
    # line starts with a "• ⏳" bullet so match the emoji anywhere, not just
    # startswith. ⚠️ fetch-failure messages are dropped here (the nightly
    # briefing reports them) so a flaky yfinance fetch can't spam Discord
    # every 5-10 minutes.
    alerts = [
        m for m in messages
        if m.startswith(("🚨", "📡", "🔄", "🔔", "💰")) or "⏳" in m
    ]
    if not alerts:
        print("✅ No ATH DCA alerts (trigger/imminent/wait-rollover) this poll.")
        return

    for msg in alerts:
        print(msg.splitlines()[0])

    _send_discord(
        webhook_url=webhook,
        user_id=user_id,
        title=f"🚨 ATH DCA Realtime Alert",
        content="\n\n".join(alerts),
    )


# ═══════════════════════════════════════════════════════════
# Execution Loop
# ═══════════════════════════════════════════════════════════
def execute_dual_tactical_trader() -> None:
    """Run integrated macro signal & LOC automation (dual-mode)"""
    now_ny = datetime.now(ZoneInfo("America/New_York"))
    
    cfg = load_portfolio()
    webhook, user_id = resolve_discord_config(cfg)

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




# ═══════════════════════════════════════════════════════════
# 전략 모드 — 백테스트 + 실시간 신호 (기존 DCA_MA_strategy.py)
# ═══════════════════════════════════════════════════════════
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

    ATH/MDD 계산은 _compute_ath_drawdown과 동일 방법론
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
        # 부호: "MA의 종가 대비 위치" — MA가 종가 위(+) / 아래(-)
        f"MA{ma_days} ${sig['ma']:.2f} ({-sig['distance_pct']:+.1f}%)",
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
            print(f"  MA{md}        : ${sig['ma']:.2f}  (종가 대비 {-sig['distance_pct']:+.1f}%)")
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
    # ── 통합 CLI 라우팅 ──────────────────────────────────────────
    strategy_flags = ("--signal", "--discord", "--all", "--backtest",
                      "--ticker", "--ma", "--reentry", "--reentry-pct", "--fee")
    if "--ath-monitor" in sys.argv:
        run_ath_dca_monitor()          # 장중 실시간 ATH DCA 모니터
    elif any(flag in sys.argv for flag in strategy_flags):
        main()                         # 전략 CLI — 신호(--signal/--discord) / 백테스트(--backtest)
    else:
        execute_dual_tactical_trader() # 일일 Discord 브리핑 (기본)
