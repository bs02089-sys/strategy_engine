import os
import pandas as pd
import numpy as np
import yfinance as yf
import requests
from dotenv import load_dotenv
from datetime import timedelta
from zoneinfo import ZoneInfo

# ==================== 설정 ====================
TICKERS = ["TQQQ", "SOXL"]
LOOKBACK_TRADING_DAYS = 252
FEES = 0.00065

# ==================== .env 로드 ====================
load_dotenv()
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK")

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
def load_data():
    now = pd.Timestamp.now(tz=ZoneInfo("Asia/Seoul")).normalize().tz_localize(None)
    start_date = (now - timedelta(days=LOOKBACK_TRADING_DAYS + 150)).date()
    end_date = (now + timedelta(days=1)).date()
    data = yf.download(TICKERS, start=start_date, end=end_date, auto_adjust=True, progress=False)
    close = data["Close"].reindex(columns=TICKERS).dropna(how="all")
    return close

close = load_data()

# ==================== σ 계산 ====================
def compute_sigma(close_series: pd.Series, window: int = LOOKBACK_TRADING_DAYS) -> float | None:
    s = pd.Series(close_series).dropna()
    returns = s.pct_change().dropna()
    if len(returns) < window + 1:
        return None
    sigma = returns.iloc[-window-1:-1].std()
    return float(sigma) if np.isfinite(sigma) else None

# ==================== 최적 TP 계산 (최근 1년 롤링, 그리드 탐색) ====================
def _find_signals_and_forward_path(series: pd.Series,
                                   lookback_window: int = 252,
                                   forward_days: int = 20) -> list[dict]:
    s = series.dropna()
    high = s  # 고가 데이터가 없으므로 종가로 대체
    events = []
    returns = s.pct_change().dropna()
    for i in range(lookback_window + 1, len(s) - 1):
        sigma = returns.iloc[i - lookback_window - 1:i - 1].std()
        if not np.isfinite(sigma) or sigma <= 0:
            continue
        prev_close = s.iloc[i - 1]
        today_close = s.iloc[i]
        threshold_2 = prev_close * (1 - 2 * sigma)
        if today_close <= threshold_2:
            j_end = min(i + 1 + forward_days, len(s))
            forward_high = high.iloc[i + 1:j_end]
            events.append({
                "date": s.index[i],
                "entry": today_close,
                "prev_close": prev_close,
                "sigma": float(sigma),
                "forward_high": forward_high.values,
            })
    return events

def optimize_tp_fixed_pct_for_symbol(close_series: pd.Series,
                                     tp_grid: np.ndarray | None = None,
                                     lookback_window: int = 252,
                                     forward_days: int = 20,
                                     fees: float = 0.00065) -> float | None:
    s = close_series.last("365D").dropna()
    if len(s) < lookback_window + 30:
        return None
    if tp_grid is None:
        tp_grid = np.round(np.arange(0.02, 0.2001, 0.005), 3)  # 2% ~ 20% 후보
    events = _find_signals_and_forward_path(s, lookback_window=lookback_window, forward_days=forward_days)
    if not events:
        return None
    best_tp, best_score = None, -np.inf
    for tp in tp_grid:
        total_return = 0.0
        trades = 0
        for ev in events:
            entry = ev["entry"]
            f_high = ev["forward_high"]
            tp_price = entry * (1 + tp)
            hit = np.any(f_high >= tp_price)
            if hit:
                pnl = tp - 2 * fees
                total_return += pnl
                trades += 1
        if total_return > best_score:
            best_score, best_tp = total_return, tp
    return float(best_tp) if best_tp is not None else None

# ==================== 전일 종가와 현재가 ====================
def get_prev_and_current_price(symbol: str):
    s = close[symbol].dropna()
    if len(s) < 2:
        return None, None
    prev_close = s.iloc[-2]
    current_price = s.iloc[-1]
    prev_close = prev_close.item() if hasattr(prev_close, "item") else float(prev_close)
    current_price = current_price.item() if hasattr(current_price, "item") else float(current_price)
    return prev_close, current_price

# ==================== 메시지 생성 ====================
def build_alert_messages():
    now_kst = pd.Timestamp.now(tz=ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M:%S")
    messages = []
    for symbol in TICKERS:
        if symbol not in close.columns or close[symbol].dropna().empty:
            messages.append(f"❌ {symbol} 데이터 누락으로 분석 불가")
            continue
        prev_close, current_price = get_prev_and_current_price(symbol)
        sigma = compute_sigma(close[symbol])
        if prev_close is None or current_price is None or sigma is None:
            messages.append(f"❌ {symbol} 시그마 계산 불가 (데이터 부족)")
            continue
        sigma2 = 2 * sigma
        threshold_2 = prev_close * (1 - sigma2)
        ret_today = (current_price / prev_close) - 1.0
        ret_str = f"+{ret_today*100:.2f}%" if ret_today > 0 else f"{ret_today*100:.2f}%"
        buy_signal = current_price <= threshold_2
        optimal_tp = optimize_tp_fixed_pct_for_symbol(close[symbol], lookback_window=LOOKBACK_TRADING_DAYS, forward_days=20, fees=FEES)
        tp_text = f"{optimal_tp*100:.2f}%" if optimal_tp else "❌ 계산 불가"
        message = (
            f"📉 [{symbol} 매수 신호 체크]\n"
            f"알림 발생 시각: {now_kst}\n"
            f"2σ (전일까지 252일): {sigma2*100:.2f}% (도달가격: ${threshold_2:.2f})\n"
            f"전일 종가: ${prev_close:.2f}\n"
            f"현재 가격: ${current_price:.2f}\n"
            f"전일 대비: {ret_str}\n"
            f"매수 조건 충족: {'✅ 2σ' if buy_signal else '❌ No'}\n"
            f"최적 TP (최근 1년 롤링): {tp_text}"
        )
        messages.append(message)
    return "\n\n".join(messages)

# ==================== 월간 Ping ====================
def monthly_ping():
    now_kst = pd.Timestamp.now(tz=ZoneInfo("Asia/Seoul"))
    if now_kst.day == 1:
        send_discord_message(f"✅ Monthly Ping: 시스템 정상 작동 중 ({now_kst.strftime('%Y-%m-%d %H:%M:%S')})")

# ==================== 실행 ====================
if __name__ == "__main__":
    final_message = build_alert_messages()
    print(final_message)
    send_discord_message(final_message)
    monthly_ping()
