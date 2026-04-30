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
    # iloc[-1]로 접근 후 확실하게 스칼라로 변환
    return float(100 - (100 / (1 + rs.iloc[-1])))

def main():
    global CURRENT_USED
    for ticker in TICKERS:
        df = get_data_backup(ticker)
        if df is None or len(df) < 252:
            continue

        # [경고 박멸 핵심] .iloc[n] 대신 .take([n])이나 .values[n]을 사용하여 
        # NumPy 스칼라를 직접 float으로 변환하는 방식 채택
        latest_price = float(df["Close"].values[0])
        previous_close = float(df["Close"].values[1])
        closes_list = df["Close"].values.flatten().tolist()
        
        current_rsi = calculate_rsi(df)
        
        # 이동평균 계산 시에도 안전하게 단일 값으로 추출
        ma200_val = df["Close"].head(200).mean()
        ma120_val = df["Close"].head(120).mean()
        
        # 만약 결과가 Series라면 첫 요소를, 아니면 그대로 float 변환
        ma200 = float(ma200_val.iloc[0]) if isinstance(ma200_val, pd.Series) else float(ma200_val)
        ma120 = float(ma120_val.iloc[0]) if isinstance(ma120_val, pd.Series) else float(ma120_val)
        
        env_res_25 = ma120 * 1.25
        env_sup_25 = ma120 * 0.75
        
        # 1. 추세 판단 및 시그마 산출
        is_bull = (latest_price > ma200) and (current_rsi >= 50)
        
        if current_rsi >= 80:
            dynamic_sigma, status = 0.10, "🔥 상승장 (불 마켓)"
        elif current_rsi >= 70:
            log_returns = np.diff(np.log(closes_list[:41]))
            dynamic_sigma, status = np.std(log_returns), "🔥 상승장 (불 마켓)"
        else:
            if is_bull:
                log_returns = np.diff(np.log(closes_list[:41]))
                dynamic_sigma, status = max(np.std(log_returns), MIN_SIGMA_BULL), "🔥 상승장 (불 마켓)"
            else:
                log_returns = np.diff(np.log(closes_list[:252]))
                dynamic_sigma, status = min(np.std(log_returns), MAX_SIGMA_BEAR), "🛡️ 하락장 (베어 마켓)"

        p1, p2 = latest_price * (1 - dynamic_sigma), latest_price * (1 - 2 * dynamic_sigma)
        
        # 2. [자동 감지] 조기 매도 신호
        exit_ready = (current_rsi >= 80) and (latest_price >= env_res_25)
        bear_signal = (not is_bull) and (current_rsi < 40)
        bull_recovery = is_bull and (current_rsi >= 50) and (previous_close <= ma200)
        
        sentiment = "🚨 강한 과매수" if current_rsi >= 80 else "🔴 과매수" if current_rsi >= 70 else "✅ 과매도" if current_rsi <= 30 else "⚪ 중립"
        env_touched = (latest_price <= env_sup_25)

        # 3. 리포트 메시지 구성
        header_title = "🚀 [즉시 익절 실행 권고]" if exit_ready else f"📊 {ticker} 시그마 전술 리포트 V3.5"
        
        rebalance_msg = "🚨 **하락장 대응**: SOXL 비중 25% 축소 추천\n\n" if bear_signal else \
                        "✅ **추세 전환**: 비중 50:50 재조정 추천\n\n" if bull_recovery else ""

        if exit_ready:
            sell_guide = "🔥 **[긴급] 과열 신호 포착! 전량 익절 후 시즌 리셋 권장**"
        elif CURRENT_USED >= ANNUAL_QUOTA:
            sell_guide = "🏁 **[종료] 12회 완료. 자산 80%를 SPYM으로 전환하십시오.**"
        else:
            sell_guide = "⏳ 현재 시즌 운용 중 (기계적 매수 대기)"

        return_val = ((latest_price - MY_AVG_PRICE) / MY_AVG_PRICE) * 100 if MY_AVG_PRICE > 0 else 0

        msg = (
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"**{header_title}**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ 현재 추세: {status} ({sentiment})\n"
            f"🌡️ 시장 심리: RSI {current_rsi:.1f}\n"
            f"💰 현재 가격: ${latest_price:.2f} (수익률: {return_val:+.2f}%)\n"
            f"📍 시그마 변동성: {dynamic_sigma*100:.2f}%\n\n"
            f"📈 **엔벨로프 분석**\n"
            f"  - 저항선(25%): ${env_res_25:.2f}\n"
            f"  - 지지선(25%): ${env_sup_25:.2f} {'⚠️ 터치!' if env_touched else ''}\n\n"
            f"{rebalance_msg}"
            f"🎯 **오늘의 매수 타점**\n"
            f"📍 1차 예약(-1σ): **${p1:.2f}**\n"
            f"📍 2차 예약(-2σ): **${p2:.2f}**\n\n"
            f"📊 **시즌 현황: {CURRENT_USED}/{ANNUAL_QUOTA}회**\n"
            f"🛑 **전술적 매도 가이드**\n"
            f"  {sell_guide}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⏰ 보고 시각: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        )

        print(msg)
        if WEBHOOK_URL: requests.post(WEBHOOK_URL, json={"content": msg})

    config["LAST_RUN_TIME"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4, ensure_ascii=False)
    
    try:
        subprocess.run(["git", "add", "."], cwd=WORKING_DIR)
        subprocess.run(["git", "commit", "-m", f"V3.5 Pure Scalar Fix: {datetime.now().strftime('%Y-%m-%d %H:%M')}"], cwd=WORKING_DIR)
        subprocess.run(["git", "push", "origin", "main"], cwd=WORKING_DIR)
    except: pass
        
if __name__ == "__main__":
    main()