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

from datetime import datetime, date
from zoneinfo import ZoneInfo   
from typing import List, Tuple, Optional

# ====================== Encoding ======================
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

# ====================== Config ======================
CONFIG_PATH = "portfolio_config.json"
DISCORD_TITLE_LIMIT = 256
DISCORD_CONTENT_LIMIT = 4096

# ====================== I/O ======================
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


# ====================== Sigma ======================
def log_sigma_update(ticker: str, sigma: float, today: date) -> None:
    file_path = "sigma_history.csv"
    file_exists = os.path.isfile(file_path)
    with open(file_path, 'a', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(['Date', 'Ticker', 'Sigma'])
        writer.writerow([today.strftime("%Y-%m-%d"), ticker, round(sigma, 4)])


def _fetch_closes_for_lookback(ticker: str, lookback_days: int, max_retries: int = 3):
    buffer = max(30, int(lookback_days * 0.6) + 30)
    for attempt in range(max_retries):
        try:
            hist = yf.Ticker(ticker).history(period=f"{lookback_days + buffer}d", interval="1d")
            closes = hist['Close'].dropna()
            if len(closes) >= lookback_days:
                return closes
        except Exception:
            if attempt == max_retries - 1:
                raise
            time.sleep(2)
    raise RuntimeError(f"{ticker} data fetch failed")


def _calculate_ewma_sigma(closes, lookback_days: int, ewma_lambda: float) -> float:
    log_returns = np.log(closes / closes.shift(1)).dropna()[-lookback_days:]
    arr = np.asarray(log_returns)
    var = float(arr[0] ** 2)
    for r in arr[1:]:
        var = ewma_lambda * var + (1 - ewma_lambda) * (r ** 2)
    return float(np.sqrt(var))


def recompute_sigma_for_ticker(ticker: str, pos: dict, today: date) -> float:
    lookback = int(pos.get("LOOKBACK_DAYS", 252))
    ewma_lambda = float(pos.get("EWMA_LAMBDA", 0.94))

    closes = _fetch_closes_for_lookback(ticker, lookback)
    sigma = _calculate_ewma_sigma(closes, lookback, ewma_lambda)
    new_sigma = round(sigma, 4)

    pos["DAILY_SIGMA"] = new_sigma
    pos["LAST_SIGMA_UPDATE"] = today.strftime("%Y-%m-%d")
    log_sigma_update(ticker, new_sigma, today)
    return new_sigma


def refresh_sigma_if_stale(cfg: dict) -> List[str]:
    messages = []
    today = datetime.now(ZoneInfo("America/New_York")).date()
    
    for ticker, pos in cfg.get("POSITIONS", {}).items():
        last_update = datetime.strptime(pos.get("LAST_SIGMA_UPDATE", "2000-01-01"), "%Y-%m-%d").date()
        if (today - last_update).days < 90:
            continue
        try:
            new_sigma = recompute_sigma_for_ticker(ticker, pos, today)
            messages.append(f"📊 {ticker} sigma updated: {new_sigma:.4f}")
        except Exception as e:
            messages.append(f"⚠️ {ticker} sigma update failed: {e}")
    return messages


# ====================== LOC Price ======================
def get_prev_close(ticker: str) -> Tuple[Optional[float], str]:
    """전일 종가 조회 (더 안정적으로 개선)"""
    try:
        # 더 긴 기간으로 조회하여 주말/공휴일 대응
        hist = yf.Ticker(ticker).history(period="15d", interval="1d")
        if hist.empty:
            raise ValueError("No data")

        closes = hist['Close'].dropna()
        if closes.empty:
            raise ValueError("No valid closes")

        # 가장 최근 종가 사용
        prev_close = float(closes.iloc[-1])
        date_str = closes.index[-1].date().strftime("%m-%d")
        
        if np.isnan(prev_close) or prev_close <= 0:
            raise ValueError("Invalid price")

        return prev_close, date_str

    except Exception as e:
        print(f"⚠️ {ticker} 가격 조회 실패: {e}")
        return None, "N/A"
    

def calculate_loc_price(ticker: str, prev_close: float, cfg: dict) -> float:
    positions = cfg.get("POSITIONS", {})
    pos = positions.get(ticker, {})
    multiplier = pos.get("ENTRY_MULTIPLIER", 1.41)
    sigma = pos.get("DAILY_SIGMA", 0.03)
    return round(prev_close * (1 - sigma * multiplier), 2)


# ====================== Discord ======================
def _send_discord(webhook: str, user_id: str, title: str, content: str):
    if not webhook:
        return
    payload = {
        "content": f"<@{user_id}>" if user_id else "",
        "embeds": [{"title": title[:DISCORD_TITLE_LIMIT], "description": content[:DISCORD_CONTENT_LIMIT], "color": 3447003}]
    }
    try:
        requests.post(webhook, json=payload, timeout=10)
    except Exception as e:
        print(f"⚠️ Discord 전송 실패: {e}")


# ====================== Main ======================
def execute_dual_tactical_trader():
    # New York 타임존 사용
    now_ny = datetime.now(ZoneInfo("America/New_York"))
    cfg = load_portfolio()
    
    webhook = os.environ.get("DISCORD_WEBHOOK") or cfg.get("DISCORD_WEBHOOK", "")
    user_id = os.environ.get("DISCORD_USER_ID") or cfg.get("DISCORD_USER_ID", "")

    # Sigma 업데이트
    for msg in refresh_sigma_if_stale(cfg):
        print(msg)
    
    save_portfolio(cfg)

    # Briefing
    lines = [f"🌙 **LOC Portfolio Briefing** ({now_ny.strftime('%Y-%m-%d %H:%M %Z')})"]
    lines.append("─" * 40)

    for ticker, pos in cfg.get("POSITIONS", {}).items():
        prev_close, date_str = get_prev_close(ticker)
        
        if prev_close is None or np.isnan(prev_close):
            lines.append(f"\n🔹 **{ticker}** — 가격 조회 실패 ⚠️")
            continue

        loc_price = calculate_loc_price(ticker, prev_close, cfg)
        
        invest_type = pos.get("INVEST_TYPE", "")
        meta = f" | {invest_type}" if invest_type else ""

        lines.append(f"\n🔹 **{ticker}** (전일종가: ${prev_close:.2f} | {date_str}{meta})")
        lines.append(f"• 🎯 **LOC Buy:** **${loc_price:.2f}**")
        lines.append("• 📌 매일 무조건 LOC 지정가 매수")
        
    _send_discord(webhook, user_id, "📋 LOC 매수 브리핑", "\n".join(lines))


if __name__ == "__main__":
    execute_dual_tactical_trader()