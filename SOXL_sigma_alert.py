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
# 1. 경로 및 본진 설정 (최우선 실행)
# ==========================================
CURRENT_FILE_PATH = os.path.abspath(__file__)
WORKING_DIR = os.path.dirname(CURRENT_FILE_PATH)
os.chdir(WORKING_DIR)
print(f"🚀 본진 설정 완료: {WORKING_DIR}")

# ==========================================
# 2. 환경 변수 및 설정 로드
# ==========================================
# 본진으로 이동 후 .env 로드 (가장 확실한 방법)
load_dotenv()
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK")
MASSIVE_API_KEY = os.getenv("MASSIVE_API_KEY")

# config.json 로드
config_path = os.path.join(WORKING_DIR, "config.json")
if os.path.exists(config_path):
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)
    print(f"✅ 설정 파일 로드 완료: {config_path}")
else:
    config = {
        "MY_AVG_PRICE": 100.0,
        "MY_TOTAL_SHARES": 500,
        "CURRENT_USED": 0,
        "LAST_RUN_TIME": "N/A"
    }
    print("⚠️ config.json 파일이 없어 기본값으로 시작합니다.")

# ==========================================
# 3. 전술 기준값 (상수)
# ==========================================
TICKERS = ["SOXL"]
MIN_SIGMA_BULL = 0.10
MAX_SIGMA_BEAR = 0.20
ANNUAL_QUOTA = 12

# [최초 획득 데이터 & 기준점]
MY_AVG_PRICE = config.get("MY_AVG_PRICE", 111.05)   # 내 자산의 뿌리
MY_TOTAL_SHARES = config.get("MY_TOTAL_SHARES", 163) # 내 자산의 뿌리
CURRENT_USED = config.get("CURRENT_USED", 0)         # 작전은 언제나 0부터 시작!

def get_data_backup(ticker):
    """yfinance 단독 정예 수집 시스템 (안정성 최우선)"""
    print(f"📡 {ticker} 데이터 수집 중 (via yfinance)...")
    try:
        # 2년치 데이터를 가져와서 분석에 충분한 양을 확보합니다.
        df = yf.download(ticker, period="2y", auto_adjust=True, progress=False)
        
        if not df.empty:
            # 데이터 정렬: 최신 날짜가 위로 오도록 (closes[0]이 오늘 종가)
            df = df.sort_index(ascending=False)
            
            # yfinance의 Multi-index 또는 복잡한 구조를 1차원 리스트로 깔끔하게 평탄화
            closes = df["Close"].dropna().values.flatten().tolist()
            
            if len(closes) >= 2:
                print(f"✅ 수집 성공: {len(closes)}개의 캔들을 확보했습니다.")
                return closes
            else:
                print("⚠️ 데이터 개수가 부족합니다.")
        else:
            print("⚠️ yfinance로부터 데이터를 가져오지 못했습니다. (Empty DataFrame)")
            
    except Exception as e:
        print(f"❌ 데이터 수집 중 치명적 오류 발생: {e}")
    
    return None

def calculate_rsi(closes, period=14):
    if len(closes) < period + 1: return 50.0
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

        # 시그마 로직
        if current_rsi >= 80:
            dynamic_sigma, sigma_note, status = 0.10, " (강한 과매수 → 10% 적용)", "🔥 불 마켓"
        elif current_rsi >= 70:
            log_returns = np.diff(np.log(closes[:41]))
            dynamic_sigma, sigma_note, status = np.std(log_returns), " (과매수 → 40D σ 적용)", "🔥 불 마켓"
        else:
            if is_bull:
                log_returns = np.diff(np.log(closes[:41]))
                dynamic_sigma, status = max(np.std(log_returns), MIN_SIGMA_BULL), "🔥 불 마켓"
                sigma_note = " (불 마켓 → 40D σ 적용)"
            else:
                log_returns = np.diff(np.log(closes[:252])) if len(closes) >= 252 else np.diff(np.log(closes))
                dynamic_sigma, status = min(np.std(log_returns), MAX_SIGMA_BEAR), "🛡️ 베어 마켓"
                sigma_note = " (베어 마켓 → 최근 1년 σ 적용)"

        p1, p2 = latest_price * (1 - dynamic_sigma), latest_price * (1 - 2 * dynamic_sigma)
        sentiment = "🚨 강한 과매수" if current_rsi >= 80 else "🔴 과매수" if current_rsi >= 70 else "✅ 과매도" if current_rsi <= 30 else "⚪ 중립"

        if current_rsi >= 80:
            loc_section = f"⚠️ **강한 과매수 구간**입니다\n   • -2σ까지 깊은 조정 시 고려\n   📍 추천 LOC: **${p2:.2f}** (100%)\n"
        else:
            loc_section = (f"🎯 **오늘의 낚시 포인트 (이중 그물 전략)**\n"
                           f"   📍 LOC 예약 1(-1σ): **${p1:.2f}** (100%)\n"
                           f"   📍 LOC 예약 2(-2σ): **${p2:.2f}** (100%)\n")

        bear_signal = (not is_bull) and (current_rsi < 40)
        bull_recovery = is_bull and (current_rsi >= 50) and (len(closes) > 200 and closes[1] <= ma200)
        
        rebalance_msg = "🚨 **베어 마켓 신호 감지**: SOXL 25% 매도 추천\n\n" if bear_signal else \
                        "✅ **불 마켓 전환 신호 감지**: 비중 50:50 조정 추천\n\n" if bull_recovery else ""

        return_val = ((latest_price - MY_AVG_PRICE) / MY_AVG_PRICE) * 100 if MY_AVG_PRICE > 0 else 0
        remaining = ANNUAL_QUOTA - CURRENT_USED

        msg = (
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 **{ticker} 자율주행 리포트**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ 현재 추세: {status} ({sentiment})\n"
            f"🌡️ 심리 지수: RSI {current_rsi:.1f}\n"
            f"💰 전일 종가: ${previous_close:.2f}\n"
            f"📍 적용 시그마(40D){sigma_note}: {dynamic_sigma*100:.2f}%\n\n"
            f"{rebalance_msg}{loc_section}\n"
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

# ==========================================
    # 4. GitHub 자동 업데이트 (전체 변경사항 푸시)
    # ==========================================
    config["LAST_RUN_TIME"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4, ensure_ascii=False)

    try:
        # [수정 포인트] "config.json" 대신 "." 을 사용합니다.
        # "." 은 현재 폴더 내의 모든 변경사항(코드 수정, 제이슨 수정 등)을 의미합니다.
        subprocess.run(["git", "add", "."], check=True, cwd=WORKING_DIR)
        
        subprocess.run(["git", "commit", "-m", f"Auto-update: {datetime.now().strftime('%Y-%m-%d %H:%M')}"], check=True, cwd=WORKING_DIR)
        subprocess.run(["git", "push", "origin", "main"], check=True, cwd=WORKING_DIR)
        print("✅ GitHub Push 성공 (코드 및 설정 전체 업데이트 완료)")
    except Exception as e:
        print(f"⚠️ GitHub Push 실패: {e}")
        
if __name__ == "__main__":
    main()