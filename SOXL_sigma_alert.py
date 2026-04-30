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
    config = {"MY_AVG_PRICE": 111.05, "MY_TOTAL_SHARES": 163, "CURRENT_USED": 1, "LAST_RUN_TIME": "N/A"}

# ==========================================
# 3. 기준값 (상수)
# ==========================================
TICKERS = ["SOXL"]
MIN_SIGMA_BULL = 0.10
MAX_SIGMA_BEAR = 0.20
ANNUAL_QUOTA = 12

MY_AVG_PRICE = config.get("MY_AVG_PRICE", 111.05)
CURRENT_USED = config.get("CURRENT_USED", 1)

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
    last_rs = rs.iloc[-1]
    if hasattr(last_rs, "__iter__"): last_rs = last_rs[0]
    return float(100 - (100 / (1 + last_rs)))

def main():
    global CURRENT_USED
    for ticker in TICKERS:
        df = get_data_backup(ticker)
        if df is None or len(df) < 252:
            continue

        close_series = df["Close"].squeeze()
        latest_price = float(close_series.iloc[0])
        closes_list = close_series.values.flatten().tolist()
        
        current_rsi = calculate_rsi(df)
        
        ma200_raw = df["Close"].head(200).mean()
        ma120_raw = df["Close"].head(120).mean()
        ma200 = float(ma200_raw.iloc[0] if hasattr(ma200_raw, "iloc") else ma200_raw)
        ma120 = float(ma120_raw.iloc[0] if hasattr(ma120_raw, "iloc") else ma120_raw)
        
        env_res_25 = ma120 * 1.25
        env_sup_25 = ma120 * 0.75
        
        is_bull = (latest_price > ma200) and (current_rsi >= 50)
        
        if current_rsi >= 80:
            dynamic_sigma, status = 0.10, "불 마켓"
        elif current_rsi >= 70:
            log_returns = np.diff(np.log(closes_list[:41]))
            dynamic_sigma, status = np.std(log_returns), "불 마켓"
        else:
            if is_bull:
                log_returns = np.diff(np.log(closes_list[:41]))
                dynamic_sigma, status = max(np.std(log_returns), MIN_SIGMA_BULL), "불 마켓"
            else:
                log_returns = np.diff(np.log(closes_list[:252]))
                dynamic_sigma, status = min(np.std(log_returns), MAX_SIGMA_BEAR), "베어 마켓"

        p1, p2 = latest_price * (1 - dynamic_sigma), latest_price * (1 - 2 * dynamic_sigma)
        
        is_rsi_over = (current_rsi >= 80)
        is_env_over = (latest_price >= env_res_25)
        exit_ready = is_rsi_over and is_env_over
        
        sentiment = "과매수" if current_rsi >= 70 else "과매도" if current_rsi <= 30 else "중립"
        env_touched = (latest_price <= env_sup_25)

        # 리포트 메시지
        header_title = "🚀 [즉시 익절 권고]" if exit_ready else f"**{ticker} 리포트**"
        
        # 1. 매도 판독 결과 
        sell_reasoning = f"  - RSI 80 돌파: {'✅ 달성' if is_rsi_over else '❌ 미달 ('+str(round(current_rsi,1))+')'}\n" \
                         f"  - 엔벨로프 상단 터치: {'✅ 달성' if is_env_over else '❌ 미달 ('+str(round(latest_price,2))+' < '+str(round(env_res_25,2))+')'}"

        # 2. 매도 규칙 (사령관님 요청 최적화)
        sell_rules = f"  💎 \"12회 완료\" 또는 \"RSI 80 돌파 AND 엔벨로프 상단 터치\" 시 SPYM 전환\n" \
                     f"  🔄 \"남은 20%로 12회 재시작\""

        if exit_ready:
            sell_guide = "🔥 **[ACTION] 지금 즉시 수익을 확정하십시오!**"
        elif CURRENT_USED >= ANNUAL_QUOTA:
            sell_guide = "🏁 **[종료] 연간 12회 완료. SPYM 전환 시점입니다.**"
        else:
            sell_guide = "⏳ **시즌 운용 중**"

        return_val = ((latest_price - MY_AVG_PRICE) / MY_AVG_PRICE) * 100 if MY_AVG_PRICE > 0 else 0

        msg = (
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 {header_title}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ 현재 추세: {status}({sentiment})\n"
            f"  🌡️ 시장 심리: RSI {current_rsi:.1f}\n"
            f"  💰 현재 가격: ${latest_price:.2f} ({return_val:+.2f}%)\n"
            f"  📍 시그마: {dynamic_sigma*100:.2f}%\n\n"
            f"📈 **엔벨로프 분석**\n"
            f"  🔺 저항선(25%): ${env_res_25:.2f}\n"
            f"  🔻 지지선(25%): ${env_sup_25:.2f} {'⚠️ 터치!' if env_touched else ''}\n\n"
            f"🎯 **오늘의 매수 타점**\n"
            f"  📍 1차 예약(-1σ): **${p1:.2f}**\n"
            f"  📍 2차 예약(-2σ): **${p2:.2f}**\n\n"
            f"📊 **시즌 현황: {CURRENT_USED}/{ANNUAL_QUOTA}회**\n"
            f"🛑 **매도 판독 결과**\n"
            f"{sell_reasoning}\n"
            f"📝 **매도 규칙**\n"
            f"{sell_rules}\n"
            f"➡️ **가이드**: {sell_guide}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⏰ 보고: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        )

        print(msg)
        if WEBHOOK_URL: requests.post(WEBHOOK_URL, json={"content": msg})

    config["LAST_RUN_TIME"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4, ensure_ascii=False)
    
    try:
        subprocess.run(["git", "add", "."], cwd=WORKING_DIR)
        subprocess.run(["git", "commit", "-m", f"V4.2 Strategy Polished: {datetime.now().strftime('%Y-%m-%d %H:%M')}"], cwd=WORKING_DIR)
        subprocess.run(["git", "push", "origin", "main"], cwd=WORKING_DIR)
    except: pass
        
if __name__ == "__main__":
    main()