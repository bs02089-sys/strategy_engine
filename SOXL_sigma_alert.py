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
    closes = np.array(closes).flatten().astype(float)
    closes = closes[~np.isnan(closes)]
    
    if len(closes) < window + 1:
        window = len(closes) - 1
    
    window_closes = closes[-(window + 1):]
    log_returns = np.diff(np.log(window_closes))
    log_returns = log_returns[np.isfinite(log_returns)]
    
    if len(log_returns) < 5:
        return 0.60
    
    daily_sigma = np.std(log_returns, ddof=1)
    return daily_sigma * np.sqrt(252)

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

    # 변동성 계산
    closes = df["Close"].values
    sigma_main = calculate_annual_sigma(closes, 90)
    sigma_short = calculate_annual_sigma(closes, 30)
    sigma_long = calculate_annual_sigma(closes, 252)
    ratio = sigma_short / sigma_long if sigma_long > 0 else 1.0
    
    daily_vol = sigma_main / np.sqrt(252)
    base = today_open if is_market_open else prev_close

    # 타점 리스트 (직관적 차감 방식)
    t_1_0 = base * (1 - daily_vol * 1.0)
    t_1_2 = base * (1 - daily_vol * 1.2)
    t_1_5 = base * (1 - daily_vol * 1.5)
    t_2_0 = base * (1 - daily_vol * 2.0)
    t_2_5 = base * (1 - daily_vol * 2.5)
    target_profit = base * (1 + daily_vol * 1.0)

    # VIX 정보 가져오기
    vix_val, vix_info = get_vix_report()
    KST = pytz.timezone('Asia/Seoul')
    profit_loss = ((base - MY_AVG_PRICE) / MY_AVG_PRICE * 100) if MY_AVG_PRICE > 0 else 0

    # --- 시장 상황별 타점 결정 로직 ---
    if vix_val >= 35.0:
        regime = "🔴🔴 **VIX 극단 공포**"
        recommend_price = t_2_5
        target_name = "-2.5σ (비상 매수)"
        guidance = "⚠️ 시장 투매 구간입니다. -2.5σ 아래에서만 입질하세요."
    elif ratio >= 1.30:
        regime = "🔴 **고변동성 (주의)**"
        recommend_price = t_2_0
        target_name = "-2.0σ (방어 매수)"
        guidance = "📉 변동성 확대 중입니다. 깊은 타점(-2.0σ)에 그물을 치세요."
    else:
        regime = "🟢 **정상 변동성**"
        recommend_price = t_1_0
        target_name = "-1.0σ (추세 매수)"
        guidance = "🚀 흐름이 안정적입니다. -1.0σ부터 적극적으로 대응하세요."

    # 리포트 구성
    report = [
        f"━━━━━━━━━━━━━━━━━━━━",
        f"📊 **{ticker} 실전 전략 리포트**",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"{mode_msg} | {regime}",
        f"📊 **90일 σ** : {sigma_main*100:.1f}% (일일 {daily_vol*100:.2f}%)",
        f"✅ 기준가 : ${base:.2f} ({profit_loss:+.2f}%)",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"🎯 **오늘의 집중 타점**",
        f"👉 **{target_name} : ${recommend_price:.2f}**", 
        f"\n📍 보조 타점",
        f"- 1단계(-1.2σ) : ${t_1_2:.2f}",
        f"- 2단계(-1.5σ) : ${t_1_5:.2f}",
        f"📍 목표 수익(+1.0σ) : ${target_profit:.2f}",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"📉 VIX : {vix_info}",
        f"🔎 가이드: {guidance}",
        f"◆ 탄약 : {CURRENT_USED}/{ANNUAL_QUOTA} 발",
        f"⏰ {datetime.now(KST).strftime('%Y-%m-%d %H:%M')}"
    ]

    final_report = "\n".join(report)
    print(final_report)
    send_discord(final_report)

    # 설정 저장
    config["LAST_RUN_TIME"] = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4, ensure_ascii=False)

if __name__ == "__main__":
    main()