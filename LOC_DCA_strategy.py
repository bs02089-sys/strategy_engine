"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Sigma LOC DCA 통합 엔진 — 순수 LOC 지정가 5분할 매수
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
2026-08-16 재구성 — 단일 논리 원칙:
  이동평균선(MA 레짐 필터/MA20>MA60 정렬) · RSI+볼륨 · ATH 하락분할 DCA
  (비상 모드) · STAGE5 · 회복 재진입(RECOVERY_REENTRY) · 실시간 모니터
  (--ath-monitor) 를 전부 삭제하고, **하나의 논리**만 남긴다:

    전일 종가 × (1 − σ × ENTRY_MULTIPLIER) = LOC 매수가
    정규장에서 이 가격으로 LOC 지정가 주문 → 체결 여부는 증권앱 + 엑셀에서 관리

실전 엔진 (기본 실행):
  - 일일 Discord 브리핑: **LOC 매수가** · Sigma 자동 갱신 · 로테이션 초기화 ·
    전고점 대비 하락률
  - ⚠️ 체결 추적은 봇이 하지 않는다 (2026-08-16) — 사용자가 정규장에서 LOC 지정가
    주문을 걸고, 다음 날 증권앱에서 체결 여부를 확인해 엑셀에 기록한다.
    분할 예산/회차 표시는 엑셀이 단일 소스이므로 브리핑에 포함하지 않는다.

전략 모드 (백테스트/신호):
  - --backtest: 시그마 LOC 5분할 백테스트 (MA 필터 없음 — 단일 전략)
  - --signal: 실시간 신호 (종가 · LOC 매수가 · 오늘 LOC 도달 여부) — 콘솔용
  - --signal --discord: Discord 발송 | --all: 전 종목 단일 메시지 (수동 확인용)

Usage:
  python3 LOC_DCA_strategy.py                              # 일일 브리핑 (기본)
  python3 LOC_DCA_strategy.py --backtest                   # TQQQ 백테스트 (LOC 5분할)
  python3 LOC_DCA_strategy.py --signal                     # 오늘 신호 (TQQQ)
  python3 LOC_DCA_strategy.py --signal --discord --all     # 전 종목 신호 → Discord
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
    """
    webhook = os.environ.get("DISCORD_WEBHOOK") or cfg.get("DISCORD_WEBHOOK", "")
    user_id = os.environ.get("DISCORD_USER_ID") or cfg.get("DISCORD_USER_ID", "")
    return webhook, user_id


# ═══════════════════════════════════════════════════════════
# Sigma Auto-Update — By LOOKBACK_DAYS
# ═══════════════════════════════════════════════════════════

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
    Returns (sigma, method_actually_used).
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


_LEADING_SIGNALS = {"Yield Curve Inversion", "Fed Policy Cycle", "Valuation Overheat"}


def _signal_group(name: str) -> str:
    """group 필드가 없는 구버전 signal_report.json 을 위한 이름 기반 보정."""
    return "leading" if name in _LEADING_SIGNALS else "confirm"


def get_market_regime(filepath="signal_report.json") -> dict | None:
    """signal_report.json 기반 시장 국면 판정 — bear_market_signals.assess_regime 규칙 재사용.

    브리핑/신호에 'LOC_DCA vs 스윙 중 유리한 매수 조건'을 함께 표시한다 (2026-08-17).
    리포트가 없거나 파싱 실패 시 None (블록 생략).
    """
    try:
        from bear_market_signals import SignalResult, assess_regime
        if not os.path.exists(filepath):
            return None
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        results = [
            SignalResult(s["name"], int(s["score"]) > 0, int(s["score"]), s.get("detail", ""),
                         s.get("group") or _signal_group(s["name"]))
            for s in data.get("signals", [])
        ]
        if not results:
            return None
        return assess_regime(results)
    except Exception:
        return None


def calculate_final_loc(base_price: float) -> float:
    """Returns base LOC price without risk discount (disabled by user request)."""
    return base_price


# ═══════════════════════════════════════════════════════════
# LOC DCA — 순수 LOC 지정가 5분할 매수 (단일 논리)
# ═══════════════════════════════════════════════════════════
# 하나의 논리만 사용한다:
#   LOC 매수가 = 전일 종가 × (1 − σ × ENTRY_MULTIPLIER)
#   정규장에서 이 가격으로 LOC 지정가 주문 → 체결 여부는 증권앱 + 엑셀에서 관리.
#
# Config schema (per-position):
#   "LOC_DCA": {
#       "SPLITS": 5,           # 총 분할 수 (백테스트 기본값)
#       "BUY_AMOUNT": 10000    # 차수당 매수 금액 (백테스트 기본값)
#   }
#
# ⚠️ 체결 추적은 봇이 하지 않는다 (2026-08-16): 분할 예산/회차는 사용자의
#   엑셀이 단일 소스이며, 봇이 주문을 실제로 걸었는지 알 수 없어 자동 카운터는
#   현실과 어긋날 수 있기 때문. 브리핑은 LOC 매수가 신호만 제공한다.


# ═══════════════════════════════════════════════════════════
# Rotation Lifecycle (ROTATION_3M / END_DEC — 시간 제한 포지션)
# ═══════════════════════════════════════════════════════════

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

def _loc_action_line(ticker: str, prev_close: float, cfg: dict) -> str:
    """LOC 매수가 라인 — 단일 논리의 유일한 실행 신호.

    분할 회차/예산은 표시하지 않는다 (사용자 엑셀이 단일 소스 — 2026-08-16).
    """
    base_loc = calculate_loc_price(ticker, prev_close, cfg)
    final_loc = calculate_final_loc(base_loc)

    if base_loc != final_loc:
        return f"• 🎯 **[Action] LOC Buy:** ~~${base_loc:.2f}~~ ➡ **${final_loc:.2f}** (Risk Discount)"
    return f"• 🎯 **[Action] LOC Buy:** **${final_loc:.2f}**"


def _build_briefing_lines(now_ny: datetime, cfg: dict) -> list[str]:
    lines = [f"🌙 **U.S. Market LOC Portfolio Briefing** ({now_ny.strftime('%Y-%m-%d %H:%M %Z')})"]
    today_ny = now_ny.date()

    market_score = get_market_score()
    lines.append(f"📊 **Market Risk Score:** {market_score} / 14")
    # 국면 판정 — LOC_DCA vs 스윙 중 유리한 매수 조건 (bear_market_signals 규칙 재사용)
    regime = get_market_regime()
    if regime:
        lines.append(f"🎯 **[국면 판정] {regime['regime']} → {regime['favorite']} 매수 조건 유리**")
        lines.append(f"• 선행(고점 경고) {regime['leading']}/6 · 확인(하락 진행) {regime['confirm']}/8")
    lines.append("─" * 40)

    positions = cfg.get("POSITIONS", {})

    for ticker, pos_cfg in positions.items():
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

        # LOC 매수가 (단일 논리 — 유일한 실행 신호)
        lines.append(_loc_action_line(ticker, prev_close, cfg))

        rotation_due, elapsed_bd, exit_days = check_rotation_exit_signal(pos_cfg, today_ny)
        if rotation_due:
            lines.append(f"• 🔴 **[D+{exit_days} Rotation Maturity] Period expired — Review for sell! (Elapsed: {elapsed_bd} days)**")

    return lines


# ═══════════════════════════════════════════════════════════
# Execution Loop
# ═══════════════════════════════════════════════════════════
def execute_daily_briefing() -> None:
    """일일 브리핑 — 순수 LOC 5분할 운용 (기본 실행)"""
    now_ny = datetime.now(ZoneInfo("America/New_York"))

    cfg = load_portfolio()
    webhook, user_id = resolve_discord_config(cfg)

    status_messages = reset_matured_rotation_positions(cfg, now_ny.date()) + refresh_sigma_if_stale(cfg)

    # Keep these visible in the GitHub Actions run log even though they no
    # longer appear in the Discord notification content.
    for msg in status_messages:
        print(msg)

    briefing_lines = _build_briefing_lines(now_ny, cfg)

    # Persist ALL config changes (sigma updates, rotation resets, LOC DCA state)
    save_portfolio(cfg)

    _send_discord(
        webhook_url=webhook,
        user_id=user_id,
        title=f"📋 AI & Semi Portfolio Briefing (LOC 5분할)",
        content="\n".join(briefing_lines)
    )

    try:
        send_monthly_ping_if_due(cfg, webhook, user_id, now_ny)
    except Exception as e:
        print(f"⚠️ Error sending monthly ping: {e}")


# ═══════════════════════════════════════════════════════════
# 전략 모드 — 백테스트 + 실시간 신호
# ═══════════════════════════════════════════════════════════
INITIAL_CASH   = 50_000.0
BUY_AMOUNT     = 10_000.0
MAX_BUYS       = 5
LOOKBACK_DAYS  = 252
VOL_METHOD     = "EWMA"
EWMA_LAMBDA    = 0.94
DEFAULT_MULTIPLIER = 1.1
TEST_START  = date(2016, 8, 2)
TEST_END    = date(2026, 8, 2)
DATA_START  = "2013-12-01"


def load_config(ticker: str) -> dict:
    """portfolio_config.json에서 티커 설정 읽기 (없으면 기본값)."""
    try:
        with open("portfolio_config.json", "r", encoding="utf-8") as f:
            cfg = json.load(f)
        pos = cfg.get("POSITIONS", {}).get(ticker, {})
        return {
            "entry_multiplier": float(pos.get("ENTRY_MULTIPLIER", DEFAULT_MULTIPLIER)),
            "splits": int(pos.get("LOC_DCA", {}).get("SPLITS", MAX_BUYS)),
            "buy_amount": float(pos.get("LOC_DCA", {}).get("BUY_AMOUNT", BUY_AMOUNT)),
        }
    except Exception:
        return {
            "entry_multiplier": DEFAULT_MULTIPLIER,
            "splits": MAX_BUYS,
            "buy_amount": BUY_AMOUNT,
        }


# ══════════════════════════════════════════════════════════
# Backtest Engine (순수 LOC 5분할 — MA 필터 없음)
# ══════════════════════════════════════════════════════════
def backtest(df: pd.DataFrame,
             entry_multiplier: float = DEFAULT_MULTIPLIER,
             initial_cash: float = INITIAL_CASH,
             buy_amount: float = BUY_AMOUNT,
             max_buys: int = MAX_BUYS,
             fee_rate: float = 0.0) -> dict:
    """
    순수 LOC 5분할 DCA 백테스트 (단일 논리 — MA 필터/RSI/ATH_DCA 없음).

    규칙: 매일 loc = 전일 종가 × (1 − σ × 승수), 당일 저가 ≤ loc → 1차 매수
    ($10,000 기본), 최대 5차. 매도 없음 — 적립 전용.
    fee_rate → 매매 체결금액 대비 수수료(0.001 = 0.1%)
    """
    closes = df["Close"].to_numpy(dtype=float)
    lows = df["Low"].to_numpy(dtype=float)
    dates_idx = df.index
    n = len(df)

    cash = float(initial_cash)
    shares = 0.0
    buys = 0
    total_buys = 0
    total_spent = 0.0
    buy_log = []
    daily_values = []
    start_idx = LOOKBACK_DAYS

    for i in range(start_idx, n):
        prev_close = float(closes[i - 1])
        today_low = float(lows[i])
        today_close = float(closes[i])
        today_date: pd.Timestamp = dates_idx[i]

        # DCA 매수 — 당일 저가 ≤ LOC 면 1차 체결 (최대 max_buys)
        if cash >= buy_amount and buys < max_buys:
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
                total_spent += amt
                buy_log.append({
                    "date": today_date, "price": round(buy_price, 2),
                    "shares": round(amt / buy_price, 4), "amount": round(amt, 2),
                    "sigma": round(sigma, 4), "loc": round(loc_price, 2),
                    "cash_remaining": round(cash, 2), "type": "BUY",
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
        "total_return": round(total_ret, 2),
        "final_value": round(final_val, 2),
        "mdd": round(mdd, 2),
        "sharpe": round(sharpe, 2),
        "calmar": round(total_ret / abs(mdd), 2) if mdd != 0 else 0.0,
        "buys": total_buys,
        "total_spent": round(total_spent, 2),
        "remaining_cash": round(cash, 2),
        "final_shares": round(shares, 4),
        "buy_log": buy_log,
        "daily_values": daily_values,
    }


# ══════════════════════════════════════════════════════════
# Data & Signal
# ══════════════════════════════════════════════════════════
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


def current_signal(ticker: str, entry_multiplier: float) -> dict:
    """최신 데이터 기준 순수 LOC 신호 산출.

    loc = 전일 종가 × (1 − σ × 승수). 당일 저가 ≤ loc → "LOC 도달" 상태
    (체결 여부는 증권앱 확인 — 봇은 추적하지 않음, 2026-08-16).
    """
    df = load_data(ticker)
    closes = df["Close"]
    lows = df["Low"]

    last_close = float(closes.iloc[-1])
    prev_close = float(closes.iloc[-2])
    last_low = float(lows.iloc[-1])
    last_date = closes.index[-1].date()

    try:
        with open("portfolio_config.json", "r", encoding="utf-8") as f:
            cfg = json.load(f)
        pos = cfg.get("POSITIONS", {}).get(ticker, {})
        sigma = pos.get("DAILY_SIGMA")
    except Exception:
        sigma = None

    loc_price: float | None = None
    if sigma is not None:
        loc_price = _calculate_loc_from_sigma(prev_close, sigma, entry_multiplier)
    elif len(closes) >= LOOKBACK_DAYS + 1:
        lookback_window = closes.iloc[len(closes) - LOOKBACK_DAYS - 1: -1].reset_index(drop=True)
        sigma, _ = _calculate_volatility_from_closes(lookback_window, LOOKBACK_DAYS, VOL_METHOD, EWMA_LAMBDA)
        loc_price = _calculate_loc_from_sigma(prev_close, sigma, entry_multiplier)

    triggered = loc_price is not None and last_low <= loc_price
    if triggered:
        state = "TRIGGERED"
        action = "🟢 오늘 LOC 도달 — 체결 여부를 증권앱에서 확인"
    else:
        state = "WAIT"
        action = f"🟡 대기 — LOC ${loc_price:.2f} 이하 시 지정가 주문" if loc_price else "🟡 대기 — LOC 계산 불가"

    return {
        "ticker": ticker, "as_of": last_date,
        "close": last_close, "prev_close": prev_close,
        "today_low": last_low, "loc": loc_price,
        "sigma": sigma,
        "state": state, "action": action,
    }


# ══════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════
def parse_args(argv: list[str]) -> dict:
    opts = {"ticker": "TQQQ", "signal": False, "discord": False, "all": False,
            "fee": 0.0}
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
        i += 1
    return opts


def _signal_discord_block(sig: dict) -> str:
    """티커별 Discord 블록 — 종가(날짜), LOC 매수가, 오늘 LOC 도달 여부, 액션."""
    loc_part = f"LOC 매수: ${sig['loc']:.2f} | " if sig["loc"] else "LOC 매수: — | "
    lines = [
        f"**{sig['ticker']} LOC 5분할 전략 신호**",
        f"종가 ${sig['close']:.2f} ({sig['as_of']}) | 저가 ${sig['today_low']:.2f}",
    ]
    lines.append(f"{loc_part}상태: {sig['state']}")
    lines.append(f"▶ {sig['action']}")
    return "\n".join(lines)


def main():
    opts = parse_args(sys.argv[1:])
    ticker = opts["ticker"]
    cfg = load_config(ticker)
    fee = opts["fee"]

    # --discord는 --signal을 암시 (단독 사용 시 자동 적용)
    if opts["discord"] and not opts["signal"]:
        print("⚠️ --discord는 --signal과 함께 사용됩니다. --signal을 자동 적용합니다.")
        opts["signal"] = True

    # ── 실시간 신호 모드 ─────────────────────────────────────
    if opts["signal"]:
        if opts["all"]:
            # 전 종목 — portfolio_config.json의 POSITIONS 기준
            try:
                with open("portfolio_config.json", "r", encoding="utf-8") as f:
                    tickers = list(json.load(f).get("POSITIONS", {}).keys()) or ["TQQQ"]
            except Exception:
                tickers = ["TQQQ"]
        else:
            tickers = [ticker]

        discord_blocks = []
        for t in tickers:
            sig = _resolve_signal(t, opts)
            print("\n" + "═" * 72)
            print(f"  📡 {sig['ticker']} LOC 5분할 전략 — 현재 신호")
            print("═" * 72)
            print(f"  기준일        : {sig['as_of']}")
            print(f"  종가          : ${sig['close']:.2f} (전일 ${sig['prev_close']:.2f})")
            print(f"  당일 저가     : ${sig['today_low']:.2f}")
            if sig["loc"]:
                print(f"  LOC 매수      : ${sig['loc']:.2f}")
            print(f"  현재 상태     : {sig['state']}")
            print(f"\n  ▶ {sig['action']}")
            print("\n" + "═" * 72)
            discord_blocks.append(_signal_discord_block(sig))

        # ── Discord 발송 (--discord) — 전 종목 단일 메시지 ────────
        if opts["discord"] and discord_blocks:
            webhook, user_id = _resolve_discord()
            content = "\n\n".join(discord_blocks)
            print(content)  # Actions 로그 기록용 — 발송 실패/이미지 전달 시에도 확인 가능
            title = "📡 LOC 5분할 전략 신호 (전 종목)" if opts["all"] else f"📡 {tickers[0]} 신호"
            _send_discord(webhook, user_id, title, content)
        return

    # ── 백테스트 모드 (고정 검증 윈도우 사용) ──────────────────
    df = load_data(ticker, end=TEST_END)
    years = (df.index[-1] - df.index[0]).days / 365.25
    r = backtest(df, entry_multiplier=cfg["entry_multiplier"],
                 buy_amount=cfg["buy_amount"], max_buys=cfg["splits"], fee_rate=fee)

    print("\n" + "═" * 84)
    print(f"  📊 {ticker} — 순수 LOC 5분할 DCA 백테스트  |  ${INITIAL_CASH:,.0f} / {years:.1f}년")
    print("═" * 84)
    print(f"  설정: 승수 {cfg['entry_multiplier']} | 매수 ${cfg['buy_amount']:,.0f}×{cfg['splits']} "
          f"| 수수료 {fee*100:.2f}% | MA 필터 없음 (단일 논리)")
    print("─" * 84)
    print(f"  총수익률 {r['total_return']:+.1f}% | MDD {r['mdd']:.1f}% | Sharpe {r['sharpe']:.2f} "
          f"| Calmar {r['calmar']:.1f}")
    print(f"  최종 ${r['final_value']:,.0f} | 매수 {r['buys']}회 (총 ${r['total_spent']:,.0f}) "
          f"| 잔여 현금 ${r['remaining_cash']:,.0f} | 보유 {r['final_shares']:.2f}주")
    print("─" * 84)
    print(f"\n  📌 매도 규칙 없음 — 순수 적립 전용 (5차 소진 후 매수 중단)\n")
    print("═" * 84)


def _resolve_signal(ticker: str, opts: dict) -> dict:
    """티커별 순수 LOC 신호 dict 산출."""
    cfg = load_config(ticker)
    return current_signal(ticker, cfg["entry_multiplier"])


if __name__ == "__main__":
    # ── 통합 CLI 라우팅 ──────────────────────────────────────────
    strategy_flags = ("--signal", "--discord", "--all", "--backtest", "--ticker", "--fee")
    if any(flag in sys.argv for flag in strategy_flags):
        main()                         # 전략 CLI — 신호(--signal/--discord) / 백테스트(--backtest)
    else:
        execute_daily_briefing()       # 일일 Discord 브리핑 (기본)
