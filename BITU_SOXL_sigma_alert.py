import os
import pandas as pd
import numpy as np
import yfinance as yf
import requests
from dotenv import load_dotenv
from zoneinfo import ZoneInfo

# ==================== 설정 ====================
TICKERS = ["BITU", "SOXL"]   
LOOKBACK_TRADING_DAYS = 252
TIMEZONE = ZoneInfo("Asia/Seoul")
ET = ZoneInfo("America/New_York")

# ==================== .env 로드 ====================
load_dotenv()
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK")

# ==================== 유틸 ====================
def kst_now_str():
    return pd.Timestamp.now(tz=TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")

def now_et():
    return pd.Timestamp.now(tz=ET)

def is_us_market_open_now() -> bool:
    nyt = now_et().time()
    return nyt >= pd.Timestamp("09:30").time() and nyt <= pd.Timestamp("16:00").time()

# ==================== 디스코드 알림 ====================
def send_discord_message(content: str):
    if not WEBHOOK_URL:
        raise RuntimeError("❌ Webhook URL이 설정되지 않았습니다.")
    try:
        resp = requests.post(WEBHOOK_URL, json={"content": f"@everyone {content}"}, timeout=10)
        if resp.status_code not in (200, 204):
            print(f"❌ 디스코드 알림 실패: {resp.status_code} / {resp.text}")
    except Exception as e:
        print(f"❌ 디스코드 알림 예외: {e}")

# ==================== σ 계산용 과거 종가 ====================
def load_close_series(symbol: str) -> pd.Series:
    df = yf.download(symbol, period="3y", auto_adjust=True, progress=False)
    if "Close" in df.columns:
        s = df["Close"]
        if isinstance(s, pd.DataFrame):
            s = s.iloc[:, 0]
        return s.dropna()
    return pd.Series(dtype=float)

close_map: dict[str, pd.Series] = {sym: load_close_series(sym) for sym in TICKERS}

# ==================== σ 계산 (로그수익률 기반) ====================
def compute_sigma(close_series: pd.Series, window: int = LOOKBACK_TRADING_DAYS) -> float | None:
    s = close_series.dropna()
    # 로그수익률로 변경
    returns = np.log(s / s.shift(1)).dropna()
    if len(returns) < window:
        return None
    sigma = returns.iloc[-window:].std()
    return float(sigma) if np.isfinite(sigma) else None

# ==================== 전일 종가 (ET 기준) ====================
def get_previous_close_et(symbol: str) -> float | None:
    try:
        tk = yf.Ticker(symbol)
        h = tk.history(period="10d", interval="1d", auto_adjust=False)
        if not isinstance(h, pd.DataFrame) or h.empty or "Close" not in h.columns:
            return None
        h = h.tz_localize(ET) if h.index.tz is None else h.tz_convert(ET)
        h = h.dropna(subset=["Close"])
        if h.empty:
            return None

        last_idx = h.index[-1]
        last_date = last_idx.date()
        today_et = now_et().date()

        if is_us_market_open_now() and last_date == today_et:
            if len(h) < 2:
                return None
            return float(h["Close"].iloc[-2])
        else:
            return float(h["Close"].iloc[-1])
    except Exception as e:
        print(f"⚠️ {symbol} 전일 종가 추출 실패: {e}")
        return None

# ==================== 메시지 생성 ====================
def build_alert_messages() -> str:
    now_kst = kst_now_str()
    messages: list[str] = []

    for symbol in TICKERS:
        prev_close = get_previous_close_et(symbol)
        sigma = compute_sigma(close_map.get(symbol, pd.Series(dtype=float)))
        if prev_close is None or sigma is None:
            messages.append(f"❌ {symbol} 시그마/가격 계산 불가 (데이터 부족)")
            continue
        threshold_1 = prev_close * (1.0 - sigma)
        threshold_2 = prev_close * (1.0 - 2 * sigma)
        message = (
            f"📉 [{symbol} 매수 신호]\n"
            f"알림 발생 시각: {now_kst}\n"
            f"전일 종가: ${prev_close:.2f}\n"
            f"1σ {sigma * 100:.2f}% 도달 가격: ${threshold_1:.2f}\n"
            f"2σ {2 * sigma * 100:.2f}% 도달 가격: ${threshold_2:.2f}"
        )
        messages.append(message)
    
    return "\n".join(messages)

# ==================== 월간 Ping ====================
def monthly_ping():
    now_kst = pd.Timestamp.now(tz=TIMEZONE)
    if now_kst.day == 1:
        send_discord_message(f"✅ Monthly Ping: 시스템 정상 작동 중 ({now_kst.strftime('%Y-%m-%d %H:%M:%S')})")

# ==================== 실행 ====================
if __name__ == "__main__":
    final_message = build_alert_messages()
    print(final_message)
    send_discord_message(final_message)
    monthly_ping()
