import os
import json
import numpy as np
import pandas as pd
import requests
import yfinance as yf
import pytz
from datetime import datetime
from dotenv import load_dotenv

# ==========================================
# 1. 환경 설정
# ==========================================
load_dotenv()

WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK")
DISCORD_USER_ID = os.getenv("DISCORD_USER_ID")

WORKING_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(WORKING_DIR)

config_path = os.path.join(WORKING_DIR, "config.json")

if os.path.exists(config_path):
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)
else:
    config = {
        "MY_AVG_PRICE": 0.0,
        "CURRENT_USED": 0,
        "ANNUAL_QUOTA": 20,
        "HOLD_DATE": "2028-05-07",
        "LAST_RUN_TIME": "N/A"
    }

MY_AVG_PRICE = config.get("MY_AVG_PRICE", 0.0)
CURRENT_USED = config.get("CURRENT_USED", 0)
ANNUAL_QUOTA = config.get("ANNUAL_QUOTA", 20)
HOLD_DATE = config.get("HOLD_DATE", "2028-05-07")

# ==========================================
# 2. 보조 함수
# ==========================================
def calculate_annual_sigma(closes, window):
    # 멀티인덱스 방지 및 1차원 배열 강제화
    closes = np.array(closes).flatten().astype(float)
    closes = closes[~np.isnan(closes)]
    
    if len(closes) < window + 1:
        window = len(closes) - 1
    
    window_closes = closes[-(window + 1):]
    log_returns = np.diff(np.log(window_closes))
    
    # 이상치 제거 및 유효 데이터 체크
    log_returns = log_returns[np.isfinite(log_returns)]
    if len(log_returns) < 5:
        return 0.60  # 기본값
    
    daily_sigma = np.std(log_returns, ddof=1)
    annual_sigma = daily_sigma * np.sqrt(252)
    return annual_sigma

def get_vix_report():
    try:
        df_vix = yf.download("^VIX", period="2d", auto_adjust=True, progress=False)
        if not df_vix.empty:
            if isinstance(df_vix.columns, pd.MultiIndex):
                df_vix.columns = df_vix.columns.droplevel(1)
            vix_val = float(df_vix["Close"].iloc[-1].item())
            status = "안정" if vix_val <= 15 else "주의" if vix_val <= 25 else "공포" if vix_val <= 35 else "극단적 공포"
            return vix_val, f"{vix_val:.1f} ({status})"
    except:
        pass
    return 0.0, "N/A"

def send_discord(message):
    if not WEBHOOK_URL:
        return
    ping = f"<@{DISCORD_USER_ID}>\n" if DISCORD_USER_ID else ""
    try:
        requests.post(WEBHOOK_URL, json={"content": ping + message}, timeout=15)
    except Exception as e:
        print(f"❌ 전송 오류: {e}")

# ==========================================
# 3. 메인 로직
# ==========================================
def main():
    ticker = "SOXL"
    try:
        df = yf.download(ticker, period="2y", auto_adjust=True, progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.droplevel(1)
        df = df.dropna(subset=["Close"])
    except Exception as e:
        print(f"데이터 오류: {e}")
        return

    # 장 상태 판단
    tz_est = pytz.timezone('US/Eastern')
    today_est = datetime.now(tz_est).date()
    last_row_date = df.index[-1].date()

    if last_row_date < today_est:
        prev_close = float(df["Close"].iloc[-1].item())
        today_open = prev_close
        mode_msg = "⏳ **장 개시 전**"
        is_market_open = False
    else:
        prev_close = float(df["Close"].iloc[-2].item())
        today_open = float(df["Open"].iloc[-1].item())
        mode_msg = "🚀 **장 개시 후**"
        is_market_open = True

    # 시그마 계산 (90일 메인)
    closes = df["Close"].values
    sigma_main = calculate_annual_sigma(closes, 90)
    sigma_short = calculate_annual_sigma(closes, 30)
    sigma_long = calculate_annual_sigma(closes, 252)
    ratio = sigma_short / sigma_long if sigma_long > 0 else 1.0

    # 💡 직관적 타점 계산 핵심: 연간 시그마를 일일 시그마로 변환
    daily_vol = sigma_main / np.sqrt(252)
    base = today_open if is_market_open else prev_close

    # 타점 = 기준가 - (기준가 * 일일변동성 * 배수)
    t_1_0 = base * (1 - daily_vol * 1.0)
    t_1_2 = base * (1 - daily_vol * 1.2)
    t_1_5 = base * (1 - daily_vol * 1.5)
    t_2_0 = base * (1 - daily_vol * 2.0)
    t_2_5 = base * (1 - daily_vol * 2.5)
    
    target_profit = base * (1 + daily_vol * 1.0)

    # 리포트 작성
    vix_val, vix_info = get_vix_report()
    profit_loss = ((base - MY_AVG_PRICE) / MY_AVG_PRICE * 100) if MY_AVG_PRICE > 0 else 0
    KST = pytz.timezone('Asia/Seoul')

    # 시장 판단 로직 (생략 없이 유지)
    if vix_val >= 35.0:
        regime, guidance, aggression = "🔴🔴 **극단 공포**", "⚠️ -2.5σ 대기", "극보수적"
    elif ratio >= 1.50:
        regime, guidance, aggression = "🔴 극고변동성", "📉 -1.5σ ~ -2.0σ 분할", "보수적"
    else:
        regime, guidance, aggression = "🟢 정상 범위", "🚀 -1.0σ ~ -1.2σ 매수", "공격적"

    report = [
        f"━━━━━━━━━━━━━━━━━━━━",
        f"📊 **{ticker} 변동성 전략 리포트**",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"{mode_msg} | {regime}",
        f"📊 **연환산 σ** : {sigma_main*100:.2f}% (일일: {daily_vol*100:.2f}%)",
        f"✅ 전일 종가 : ${prev_close:.2f} ({profit_loss:+.2f}%)",
        f"🚀 기준가 : ${base:.2f}",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"📍 -1.0σ 타점 : **${t_1_0:.2f}**",
        f"📍 -1.2σ 타점 : **${t_1_2:.2f}**",
        f"📍 -1.5σ 타점 : **${t_1_5:.2f}**",
        f"📍 -2.0σ 타점 : **${t_2_0:.2f}**",
        f"📍 +1.0σ 목표 : **${target_profit:.2f}**",
        f"📉 VIX : {vix_info}",
        f"\n🔎 가이드: {guidance} ({aggression})",
        f"◆ 탄약 : {CURRENT_USED}/{ANNUAL_QUOTA} 발",
        f"⏰ {datetime.now(KST).strftime('%Y-%m-%d %H:%M')}"
    ]

    final_report = "\n".join(report)
    print(final_report)
    send_discord(final_report)

    # 업데이트
    config["LAST_RUN_TIME"] = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4, ensure_ascii=False)

if __name__ == "__main__":
    main()