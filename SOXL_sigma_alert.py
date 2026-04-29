import os
import json
import numpy as np
import requests
import yfinance as yf
import pandas as pd
import subprocess
from datetime import datetime
from dotenv import load_dotenv

# 1. 환경 설정 및 경로 최적화
load_dotenv()
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK")
MASSIVE_API_KEY = os.getenv("MASSIVE_API_KEY")

# 작업 디렉토리 고정
WORKING_DIR = "C:/Users/bs020/strategy_engine"
os.chdir(WORKING_DIR)

TICKERS = ["SOXL"]
MIN_SIGMA_BULL = 0.10
MAX_SIGMA_BEAR = 0.20

# 설정 파일 로드 (config.json)
config_path = os.path.join(WORKING_DIR, "config.json")
with open(config_path, "r", encoding="utf-8") as f:
    config = json.load(f)

MY_AVG_PRICE = config.get("MY_AVG_PRICE", 100.00)
MY_TOTAL_SHARES = config.get("MY_TOTAL_SHARES", 500)
ANNUAL_QUOTA = 12
CURRENT_USED = config.get("CURRENT_USED", 0)

def get_data_backup(ticker):
    """1차 Massive API -> 2차 yfinance 이중 수집 시스템"""
    # [1단계] Massive API 시도
    url = f"https://api.massiveapi.com/v1/market/candles"
    params = {"symbol": ticker, "interval": "d", "limit": 300, "apikey": MASSIVE_API_KEY}
    try:
        res = requests.get(url, params=params, timeout=7)
        if res.status_code == 200:
            data = res.json()
            if data and 'candles' in data and len(data['candles']) > 0:
                closes = [float(item['close']) for item in data['candles']]
                return closes[::-1] # 최신이 [0]
    except Exception: pass

    # [2단계] yfinance 우회 시도
    try:
        df = yf.download(ticker, period="2y", auto_adjust=True, progress=False)
        if not df.empty:
            df = df.sort_index(ascending=False)
            return df["Close"].dropna().tolist()
    except Exception: pass
    return None

def calculate_rsi(closes, period=14):
    """안전한 RSI 계산 (데이터 부족 시 50 반환)"""
    if len(closes) < period + 1: return 50.0
    # 최신순 데이터를 계산을 위해 다시 과거순으로 정렬
    target_data = closes[:60][::-1]
    df = pd.DataFrame({'close': target_data})
    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-10)
    return (100 - (100 / (1 + rs))).iloc[-1]

def main():
    global CURRENT_USED
    for ticker in TICKERS:
        closes = get_data_backup(ticker)
        if not closes or len(closes) < 2:
            print(f"⚠️ {ticker} 데이터를 수집할 수 없습니다.")
            continue

        latest_price = closes[0]
        previous_close = closes[1]
        current_rsi = calculate_rsi(closes)
        ma200 = sum(closes[:200]) / 200 if len(closes) >= 200 else latest_price
        is_bull = (latest_price > ma200) and (current_rsi >= 50)

        # --- 사령관의 전술 시그마 로직 ---
        if current_rsi >= 80:
            dynamic_sigma = 0.10
            sigma_note = " (강한 과매수 → 10% 적용)"
            status = "🔥 불 마켓"
        elif current_rsi >= 70:
            log_returns = np.diff(np.log(closes[:41]))
            dynamic_sigma = np.std(log_returns)
            sigma_note = " (과매수 → 40D σ 적용)"
            status = "🔥 불 마켓"
        else:
            if is_bull:
                log_returns = np.diff(np.log(closes[:41]))
                dynamic_sigma = max(np.std(log_returns), MIN_SIGMA_BULL)
                status = "🔥 불 마켓"
                sigma_note = " (불 마켓 → 40D σ 적용)"
            else:
                log_returns = np.diff(np.log(closes[:252])) if len(closes) >= 252 else np.diff(np.log(closes))
                dynamic_sigma = min(np.std(log_returns), MAX_SIGMA_BEAR)
                status = "🛡️ 베어 마켓"
                sigma_note = " (베어 마켓 → 최근 1년 σ 적용)"

        p1 = latest_price * (1 - dynamic_sigma)
        p2 = latest_price * (1 - 2 * dynamic_sigma)
        sentiment = "🚨 강한 과매수" if current_rsi >= 80 else "🔴 과매수" if current_rsi >= 70 else "✅ 과매도" if current_rsi <= 30 else "⚪ 중립"

        # 타점 섹션 (RSI 80 이상 시 p2만 노출)
        if current_rsi >= 80:
            loc_section = f"⚠️ **강한 과매수 구간**입니다\n   • -2σ까지 깊은 조정 시 고려\n   📍 추천 LOC: **${p2:.2f}** (100%)\n"
        else:
            loc_section = (
                f"🎯 **오늘의 낚시 포인트 (이중 그물 전략)**\n"
                f"   📍 LOC 예약 1(-1σ): **${p1:.2f}** (100%)\n"
                f"   📍 LOC 예약 2(-2σ): **${p2:.2f}** (100%)\n"
            )

        # 리밸런싱 및 수익률 계산
        bear_signal = (not is_bull) and (current_rsi < 40)
        bull_recovery = is_bull and (current_rsi >= 50) and (len(closes) > 200 and closes[1] <= ma200)
        
        rebalance_msg = ""
        if bear_signal: rebalance_msg = "🚨 **베어 마켓 신호 감지**: SOXL 25% 매도 추천\n\n"
        elif bull_recovery: rebalance_msg = "✅ **불 마켓 전환 신호 감지**: 비중 50:50 조정 추천\n\n"

        return_val = ((latest_price - MY_AVG_PRICE) / MY_AVG_PRICE) * 100 if MY_AVG_PRICE > 0 else 0
        remaining = ANNUAL_QUOTA - CURRENT_USED

        # 최종 메시지 구성
        msg = (
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 **{ticker} 자율주행 리포트**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ 현재 추세: {status} ({sentiment})\n"
            f"🌡️ 심리 지수: RSI {current_rsi:.1f}\n"
            f"💰 전일 종가: ${previous_close:.2f}\n"
            f"📍 적용 시그마(40D){sigma_note}: {dynamic_sigma*100:.2f}%\n\n"
            f"{rebalance_msg}"
            f"{loc_section}\n"
            f"📊 **시즌 낚시 현황: {CURRENT_USED}/{ANNUAL_QUOTA}회** (잔여: {remaining}회)\n"
            f"📌 규칙: 연간 12회 동일 금액 분할 매수\n\n"
            f"🛑 **매도 규칙**\n"
            f"   (현재 수익률: **{return_val:+.2f}%**)\n"
            f"   ✅ 12회 완료 후 80% SPYM 전환\n"
            f"   ✅ 남은 20%로 다시 12회 재시작\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⏰ 시각: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
            f"*(데이터 백업 시스템 가동 중)*"
        )

        print(msg)
        if WEBHOOK_URL: requests.post(WEBHOOK_URL, json={"content": msg})

    # GitHub 자동 업데이트
    config["LAST_RUN_TIME"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4, ensure_ascii=False)

    try:
        subprocess.run(["git", "add", "config.json"], check=True, cwd=WORKING_DIR)
        subprocess.run(["git", "commit", "-m", f"Auto-update: {datetime.now()}"], check=True, cwd=WORKING_DIR)
        subprocess.run(["git", "push", "origin", "main"], check=True, cwd=WORKING_DIR)
        print("✅ GitHub Push 성공")
    except Exception as e:
        print(f"⚠️ GitHub Push 실패: {e}")

if __name__ == "__main__":
    main()