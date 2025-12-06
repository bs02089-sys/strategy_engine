import os
import pandas as pd
import numpy as np
import yfinance as yf
import requests
import subprocess
from dotenv import load_dotenv
from datetime import timedelta
from zoneinfo import ZoneInfo
from scipy.optimize import minimize, minimize_scalar

# ==================== 설정 ====================
TICKERS = ["PLTR", "AGQ"]
TEST_LOOKBACK_DAYS = 252 * 5
FEES = 0.00065

# ==================== .env 로드 ====================
load_dotenv()
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK")

# ==================== 디스코드 알림 ====================
def send_discord_message(content: str):
    if not WEBHOOK_URL:
        raise RuntimeError("❌ Webhook URL이 설정되지 않았습니다.")
    try:
        resp = requests.post(WEBHOOK_URL, json={"content": content}, timeout=10)
        if resp.status_code in (200, 204):
            print("✅ 디스코드 알림 전송 성공")
        else:
            print(f"❌ 디스코드 알림 실패: {resp.status_code} / {resp.text}")
    except Exception as e:
        print(f"❌ 디스코드 알림 예외: {e}")

# ==================== 데이터 로딩 ====================
def load_data():
    ny_now = pd.Timestamp.now(tz=ZoneInfo("America/New_York")).normalize().tz_localize(None)
    start_date = (ny_now - timedelta(days=TEST_LOOKBACK_DAYS + 7)).date()
    end_date = (ny_now + timedelta(days=1)).date()
    data = yf.download(TICKERS, start=start_date, end=end_date, auto_adjust=True, progress=False)["Close"]
    close = data.dropna()
    daily_return = close.pct_change().dropna()
    return close, daily_return

close, daily_return = load_data()

# ==================== 지표 계산 ====================
def portfolio_metrics(port_curve: pd.Series):
    years = len(port_curve) / 252
    cagr = port_curve.iloc[-1] ** (1 / years) - 1
    mdd = (port_curve / port_curve.cummax() - 1).min()
    daily = port_curve.pct_change().dropna()
    sharpe = (daily.mean() / daily.std()) * np.sqrt(252) if daily.std() != 0 else np.nan
    return {"CAGR": cagr, "MDD": mdd, "Sharpe": sharpe}

# ==================== 목표 비중 (MDD 기준) ====================
def optimize_weights_mdd(returns: pd.DataFrame):
    def objective(weights):
        port_ret = (returns * weights).sum(axis=1)
        curve = (1 + port_ret).cumprod()
        mdd = (curve / curve.cummax() - 1).min()
        return abs(mdd)
    cons = {'type': 'eq', 'fun': lambda w: np.sum(w) - 1}
    bounds = [(0, 1)] * len(TICKERS)
    init_guess = [0.5] * len(TICKERS)
    result = minimize(objective, init_guess, bounds=bounds, constraints=cons)
    return dict(zip(TICKERS, result.x))

# ==================== σ 및 거래횟수 계산 ====================
def calc_sigma_and_trades(returns: pd.DataFrame):
    sigma = {}
    trades = {}
    for t in TICKERS:
        if t not in returns.columns or returns[t].empty:
            sigma[t], trades[t] = np.nan, 0
            continue
        sigma[t] = returns[t].tail(252).std()
        total_events = (returns[t] < -sigma[t]).sum()
        trades[t] = total_events / 5
    return sigma, trades

# ==================== TP 백테스트 ====================
def backtest_sigma_multiplier(symbol: str, tp_multiplier: float,
                              start_ts: pd.Timestamp, end_ts: pd.Timestamp) -> pd.DataFrame:
    mask = (daily_return.index >= start_ts) & (daily_return.index <= end_ts)
    ret_slice = daily_return.loc[mask, symbol].sort_index()
    px_slice = close[symbol].reindex(ret_slice.index).astype(float)

    dates = ret_slice.index.to_numpy()
    ret_arr = ret_slice.to_numpy(dtype=float)
    price_arr = px_slice.to_numpy(dtype=float)

    trades = []
    sigma_1y = daily_return[symbol].tail(252).std()
    if sigma_1y is None or np.isnan(sigma_1y) or sigma_1y <= 0:
        return pd.DataFrame(columns=["entry_date", "exit_date", "ret_pct"])

    tp_pct = tp_multiplier * sigma_1y * 100.0
    n = len(dates)
    i = 0
    while i < n:
        if ret_arr[i] >= -sigma_1y:
            i += 1
            continue
        entry_idx = i
        entry_date = pd.Timestamp(dates[entry_idx])
        entry_px = price_arr[entry_idx]
        exit_idx = None
        j = entry_idx + 1
        while j < n:
            p2 = price_arr[j]
            ret_pct = (p2 / entry_px - 1.0) * 100.0
            if ret_pct >= tp_pct:
                exit_idx = j
                break
            j += 1
        if exit_idx is not None:
            exit_date = pd.Timestamp(dates[exit_idx])
            exit_px = price_arr[exit_idx]
            trades.append({
                "entry_date": entry_date,
                "exit_date": exit_date,
                "ret_pct": (exit_px / entry_px - 1.0) * 100.0
            })
            i = exit_idx + 1
        else:
            i = entry_idx + 1
    return pd.DataFrame(trades)

# ==================== k 최적화 ====================
def optimize_k(symbol: str, k_bounds=(1.0, 10.0)) -> float:
    start_ts = daily_return.index.max() - pd.Timedelta(days=252 * 5)
    end_ts = daily_return.index.max()
    def objective(k):
        df = backtest_sigma_multiplier(symbol, tp_multiplier=k, start_ts=start_ts, end_ts=end_ts)
        if df.empty:
            return 1e6
        curve = (1.0 + df["ret_pct"] / 100.0).cumprod()
        mdd = (curve / curve.cummax() - 1).min()
        return abs(mdd)
    res = minimize_scalar(objective, bounds=k_bounds, method="bounded")
    return float(res.x)

# ==================== 현재 값 추출 ====================
def get_latest_values(symbol: str):
    try:
        ret_today = float(daily_return[symbol].iloc[-1])
        current_price = float(close[symbol].iloc[-1])
        return ret_today, current_price
    except (IndexError, KeyError):
        return None, None

# ==================== 메시지 생성 ====================
def build_alert_messages():
    sigma, trades = calc_sigma_and_trades(daily_return)
    tw_mdd = optimize_weights_mdd(daily_return[TICKERS])
    now_kst = pd.Timestamp.now(tz=ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M:%S")
    messages = []

    for symbol in TICKERS:
        if symbol not in daily_return.columns or daily_return[symbol].empty:
            messages.append(f"❌ {symbol} 데이터 누락으로 분석 불가")
            continue

        k_mdd = optimize_k(symbol)
        tp_mdd_pct = k_mdd * sigma[symbol] * 100.0
        ret_today, current_price = get_latest_values(symbol)
        if ret_today is None or current_price is None:
            messages.append(f"❌ {symbol} 현재 값 추출 실패")
            continue

        condition_met = ret_today <= -sigma[symbol]
        ret_str = f"+{ret_today*100:.2f}%" if ret_today > 0 else f"{ret_today*100:.2f}%"
        sigma_down = current_price * (1 - sigma[symbol])

        message = (
            f"📉 [{symbol} 매수 신호 체크]\n"
            f"알림 발생 시각: {now_kst}\n"
            f"목표 비중(MDD): {tw_mdd[symbol]*100:.2f}%\n"
            f"1시그마 값: {sigma[symbol]*100:.2f}% (도달가격: ${sigma_down:.2f})\n"
            f"최근 5년 평균 거래횟수: {int(trades[symbol])}\n"
            f"현재 가격: ${current_price:.2f}\n"
            f"전일 대비: {ret_str}\n"
            f"매수 조건 충족: {'✅ Yes' if condition_met else '❌ No'}\n"
            f"최적화 TP(MDD): {tp_mdd_pct:.2f}%"
        )
        messages.append(message)

    return "\n\n".join(messages)

# ==================== 실행 ====================
if __name__ == "__main__":
    final_message = build_alert_messages()
    print(final_message)
    send_discord_message(final_message)

    # 자동 푸시 (원하면 주석 해제)
    # subprocess.run(["git", "add", "TQQQ_UGL_1sigma_alert.py"], check=True)
    # subprocess.run(["git", "commit", "-m", "Auto update alert script"], check=True)
    # subprocess.run(["git", "push", "origin", "main"], check=True)
