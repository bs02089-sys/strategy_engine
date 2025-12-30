import os
import pandas as pd
import numpy as np
import yfinance as yf
import requests
from dotenv import load_dotenv
from datetime import timedelta
from zoneinfo import ZoneInfo

# ==================== 설정 ====================
TICKERS = ["TQQQ", "SSO", "QLD"]
LOOKBACK_TRADING_DAYS = 252
TIMEZONE = ZoneInfo("Asia/Seoul")

# ==================== .env 로드 ====================
load_dotenv()
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK")

# ==================== 유틸 ====================
def kst_now_naive_date():
    return pd.Timestamp.now(tz=TIMEZONE).normalize().tz_localize(None).date()

def kst_now_str():
    return pd.Timestamp.now(tz=TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")

def is_us_market_open_now() -> bool:
    now_kst = pd.Timestamp.now(tz=TIMEZONE)
    now_et = now_kst.tz_convert("America/New_York")
    return now_et.time() >= pd.Timestamp("09:30").time() and now_et.time() <= pd.Timestamp("16:00").time()

# ==================== 디스코드 알림 ====================
def send_discord_message(content: str):
    if not WEBHOOK_URL:
        raise RuntimeError("❌ Webhook URL이 설정되지 않았습니다.")
    try:
        resp = requests.post(WEBHOOK_URL, json={"content": f"@everyone {content}"}, timeout=10)
        if resp.status_code in (200, 204):
            print("✅ 디스코드 알림 전송 성공")
        else:
            print(f"❌ 디스코드 알림 실패: {resp.status_code} / {resp.text}")
    except Exception as e:
        print(f"❌ 디스코드 알림 예외: {e}")

# ==================== 데이터 로딩 ====================
def load_data_multi(tickers: list[str]) -> pd.DataFrame:
    now_date = kst_now_naive_date()
    start_date = (pd.Timestamp(now_date) - timedelta(days=LOOKBACK_TRADING_DAYS + 150)).date()
    end_date = (pd.Timestamp(now_date) + timedelta(days=1)).date()

    data = yf.download(tickers, start=start_date, end=end_date, auto_adjust=True, progress=False)
    if isinstance(data.columns, pd.MultiIndex):
        close = data["Close"].reindex(columns=tickers)
    else:
        close = data.reindex(columns=["Close"])
        close.columns = tickers[:1]
    close = close.dropna(how="all")

    for sym in tickers:
        if sym not in close.columns or close[sym].dropna().empty:
            print(f"⚠️ {sym} 멀티 다운로드 누락. 개별 재다운로드 시도.")
            solo = yf.download(sym, start=start_date, end=end_date, auto_adjust=True, progress=False)
            if "Close" in solo.columns and not solo["Close"].dropna().empty:
                close[sym] = solo["Close"]
            else:
                print(f"❌ {sym} 개별 재다운로드 실패 또는 데이터 없음.")

    close = close.reindex(columns=tickers)
    return close

def load_data() -> pd.DataFrame:
    return load_data_multi(TICKERS)

close = load_data()

# ==================== σ 계산 (오늘 제외) ====================
def compute_sigma(close_series: pd.Series, window: int = LOOKBACK_TRADING_DAYS) -> float | None:
    s = pd.Series(close_series).dropna()
    returns = s.pct_change().dropna()
    if len(returns) < window + 1:
        return None
    sigma = returns.iloc[-window-1:-1].std()
    return float(sigma) if np.isfinite(sigma) else None

# ==================== 현재가 조회 ====================
def get_current_price(symbol: str) -> float | None:
    try:
        tk = yf.Ticker(symbol)
        lp = getattr(getattr(tk, "fast_info", None), "last_price", None)
        if lp is not None and np.isfinite(lp) and lp > 0:
            return float(lp)
        hist = tk.history(period="1d", interval="1m", auto_adjust=True)
        if isinstance(hist, pd.DataFrame) and not hist.empty:
            last_row = hist.iloc[-1]
            for col in ["Close", "Adj Close", "Open"]:
                if col in hist.columns and pd.notnull(last_row.get(col)):
                    val = float(last_row.get(col))
                    if np.isfinite(val) and val > 0:
                        return val
        return None
    except Exception as e:
        print(f"⚠️ {symbol} 현재가 조회 실패: {e}")
        return None

# ==================== 전일 종가와 현재가 ====================
def get_prev_and_current_price(symbol: str) -> tuple[float | None, float | None]:
    if symbol not in close.columns:
        return None, None
    s = close[symbol].dropna()
    if len(s) < 1:
        return None, None

    # 전일 종가 = 가장 최근 종가
    prev_close = float(s.iloc[-1])

    if is_us_market_open_now():
        current_price = get_current_price(symbol)
        if current_price is None:
            current_price = prev_close  # fallback
    else:
        current_price = prev_close  # 장외에는 전일 종가로 표시

    return prev_close, current_price

# ==================== 메시지 생성 ====================
def build_alert_messages() -> str:
    now_kst = kst_now_str()
    messages: list[str] = []

    for symbol in TICKERS:
        if symbol not in close.columns or close[symbol].dropna().empty:
            messages.append(f"❌ {symbol} 데이터 누락으로 분석 불가")
            continue

        prev_close, current_price = get_prev_and_current_price(symbol)
        sigma = compute_sigma(close[symbol])

        if prev_close is None or current_price is None or sigma is None:
            messages.append(f"❌ {symbol} 시그마 계산 불가 (데이터 부족)")
            continue

        sigma2 = 2.0 * sigma
        threshold_2 = prev_close * (1.0 - sigma2)

        ret_today = (current_price / prev_close) - 1.0
        ret_str = f"+{ret_today * 100:.2f}%" if ret_today > 0 else f"{ret_today * 100:.2f}%"
        buy_signal = current_price <= threshold_2

        message = (
            f"📉 [{symbol} 매수 신호 체크]\n"
            f"알림 발생 시각: {now_kst}\n"
            f"2σ (전일까지 {LOOKBACK_TRADING_DAYS}일): {sigma2 * 100:.2f}% (도달가격: ${threshold_2:.2f})\n"
            f"전일 종가: ${prev_close:.2f}\n"
            f"현재 가격: ${current_price:.2f}\n"
            f"전일 대비: {ret_str}\n"
            f"매수 조건 충족: {'✅ 2σ' if buy_signal else '❌ No'}"
        )
        messages.append(message)

    return "\n\n".join(messages)

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
