import os
import json
import numpy as np
import requests
import yfinance as yf
import pandas as pd
import subprocess
from datetime import datetime
from dotenv import load_dotenv

# ==========================================
# 1. 경로 및 본진 설정
# ==========================================
CURRENT_FILE_PATH = os.path.abspath(__file__)
WORKING_DIR = os.path.dirname(CURRENT_FILE_PATH)
os.chdir(WORKING_DIR)

# ==========================================
# 2. 환경 변수 및 설정 로드
# ==========================================
load_dotenv()
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK")

config_path = os.path.join(WORKING_DIR, "config.json")
if os.path.exists(config_path):
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)
else:
    config = {"MY_AVG_PRICE": 111.05, "MY_TOTAL_SHARES": 163, "CURRENT_USED": 0, "LAST_RUN_TIME": "N/A"}

# ==========================================
# 3. 전술 기준값 (상수)
# ==========================================
TICKERS = ["SOXL"]
MIN_SIGMA_BULL = 0.10
MAX_SIGMA_BEAR = 0.20
ANNUAL_QUOTA = 12

MY_AVG_PRICE = config.get("MY_AVG_PRICE", 111.05)
CURRENT_USED = config.get("CURRENT_USED", 0)

def get_data_backup(ticker):
    try:
        df = yf.download(ticker, period="2y", auto_adjust=True, progress=False)
        if not df.empty:
            return df.sort_index(ascending=False)
    except Exception as e:
        print(f"❌ 데이터 수집 오류: {e}")
    return None

def calculate_rsi(df, period=14):
    closes = df["Close"].values.flatten()
    if len(closes) < period + 1: return 50.0
    delta = pd.Series(closes[::-1]).diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-10)
    return float((100 - (100 / (1 + rs))).iloc[-1])

def main():
    global CURRENT_USED
    for ticker in TICKERS:
        df = get_data_backup(ticker)
        if df is None or len(df) < 252:
            print(f"⚠️ {ticker} 데이터 부족")
            continue

        # [ValueError 방지] 스칼라 값으로 확실히 추출
        latest_price = float(df["Close"].iloc[0])
        previous_close = float(df["Close"].iloc[1])
        closes_list = df["Close"].values.flatten().tolist()
        
        current_rsi = calculate_rsi(df)
        ma200 = float(df["Close"].head(200).mean())
        ma120 = float(df["Close"].head(120).mean())
        
        # 엔벨로프 상단/하단 계산
        env_res_25 = ma120 * 1.25
        env_sup_25 = ma120 * 0.75
        env_sup_27 = ma120 * 0.73
        
        # 1. 정교한 추세 판단 및 시그마 산출 (사령관님 기존 로직 복구)
        is_bull = (latest_price > ma200) and (current_rsi >= 50)
        
        if current_rsi >= 80:
            dynamic_sigma, status = 0.10, "🔥 불 마켓"
        elif current_rsi >= 70:
            log_returns = np.diff(np.log(closes_list[:41]))
            dynamic_sigma, status = np.std(log_returns), "🔥 불 마켓"
        else:
            if is_bull:
                log_returns = np.diff(np.log(closes_list[:41]))
                dynamic_sigma, status = max(np.std(log_returns), MIN_SIGMA_BULL), "🔥 불 마켓"
            else:
                log_returns = np.diff(np.log(closes_list[:252]))
                dynamic_sigma, status = min(np.std(log_returns), MAX_SIGMA_BEAR), "🛡️ 베어 마켓"

        # 2. 매수 타점 설정 (40:60 비율 유지)
        p1 = latest_price * (1 - dynamic_sigma)
        p2 = latest_price * (1 - 2 * dynamic_sigma)
        
        # 3. 매도 및 리밸런싱 신호 로직 복구
        exit_signal = (current_rsi >= 80) and (latest_price >= env_res_25)
        bear_signal = (not is_bull) and (current_rsi < 40)
        bull_recovery = is_bull and (current_rsi >= 50) and (len(df) > 200 and float(df["Close"].iloc[1]) <= ma200)
        
        sentiment = "🚨 강한 과매수" if current_rsi >= 80 else "🔴 과매수" if current_rsi >= 70 else "✅ 과매도" if current_rsi <= 30 else "⚪ 중립"
        env_touched = (latest_price <= env_sup_25)

        # 4. 메시지 구성
        rebalance_msg = "🚨 **베어 마켓 신호**: SOXL 25% 매도 추천\n\n" if bear_signal else \
                        "✅ **불 마켓 전환 신호**: 비중 50:50 조정 추천\n\n" if bull_recovery else ""

        sell_guide = "🚨 **[전략적 탈출]** 엔벨 상단+RSI 80 돌파! 조기 익절 추천" if exit_signal else \
                     "🏁 **[시즌 종료]** 12회 집행 완료! 80% SPYM 전환" if CURRENT_USED >= ANNUAL_QUOTA else \
                     "⏳ 현재 시즌 운용 중"

        if current_rsi >= 80:
            loc_section = (f"⚠️ **강한 과매수 구간**\n"
                           f"📍추천 LOC: **${p2:.2f}** (100%)\n")
        else:
            loc_section = (f"🎯 **오늘의 낚시 포인트 (40:60)**\n"
                           f"📍LOC 예약 1(-1σ): **${p1:.2f}** (40%)\n"
                           f"📍LOC 예약 2(-2σ): **${p2:.2f}** (60%)\n")

        return_val = ((latest_price - MY_AVG_PRICE) / MY_AVG_PRICE) * 100 if MY_AVG_PRICE > 0 else 0

        msg = (
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 **{ticker} 시그마 통합 리포트 V3.1**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ 현재 추세: {status} ({sentiment})\n"
            f"🌡️ 심리 지수: RSI {current_rsi:.1f}\n"
            f"💰 현재 가격: ${latest_price:.2f}\n"
            f"📍 적용 시그마: {dynamic_sigma*100:.2f}%\n\n"
            f"📈 **엔벨로프(120일) 상하단**\n"
            f"  - 25% 저항선: ${env_res_25:.2f}\n"
            f"  - 25% 지지선: ${env_sup_25:.2f} {'⚠️ 터치!' if env_touched else ''}\n\n"
            f"{rebalance_msg}"
            f"{loc_section}\n"
            f"📊 **시즌 낚시 현황: {CURRENT_USED}/{ANNUAL_QUOTA}회**\n"
            f"🛑 **매도 규칙 (수익률: {return_val:+.2f}%)**\n"
            f"  {sell_guide}\n"
            f"  ✅ 12회 완료 후 80% SPYM 전환\n"
            f"  ✅ RSI 80 & 엔벨 상단 시 조기 익절\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⏰ 시각: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        )

        print(msg)
        if WEBHOOK_URL: requests.post(WEBHOOK_URL, json={"content": msg})

    config["LAST_RUN_TIME"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4, ensure_ascii=False)
    
    try:
        subprocess.run(["git", "add", "."], cwd=WORKING_DIR)
        subprocess.run(["git", "commit", "-m", f"V3.1 Full Recovery: {datetime.now().strftime('%Y-%m-%d %H:%M')}"], cwd=WORKING_DIR)
        subprocess.run(["git", "push", "origin", "main"], cwd=WORKING_DIR)
    except: pass
        
if __name__ == "__main__":
    main()