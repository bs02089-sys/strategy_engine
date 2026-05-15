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
# 1. 환경 설정 및 데이터 로드 
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
        "MY_AVG_PRICE": 0.0,    # 초기값
        "CURRENT_CASTS": 0,      # 초기값 
        "ANNUAL_QUOTA": 20,     # 초기값
        "LAST_RUN_TIME": "N/A"
    }

MY_AVG_PRICE = config.get("MY_AVG_PRICE", 0.0)
CURRENT_CASTS = config.get("CURRENT_CASTS", 0)
ANNUAL_QUOTA = config.get("ANNUAL_QUOTA", 20)  # 초기값

# ==========================================
# 2. 핵심 계산 함수
# ==========================================
def calculate_annual_sigma(closes, window=90): # 90일 윈도우 고정
    closes = np.array(closes).flatten().astype(float)
    closes = closes[~np.isnan(closes)]
    if len(closes) < window + 1: window = len(closes) - 1
    
    window_closes = closes[-(window + 1):]
    log_returns = np.diff(np.log(window_closes))
    log_returns = log_returns[np.isfinite(log_returns)]
    
    if len(log_returns) < 5: return 0.70 # 기본 변동성값 상향
    return np.std(log_returns, ddof=1) * np.sqrt(252)

def get_vix_report():
    try:
        df_vix = yf.download("^VIX", period="2d", auto_adjust=True, progress=False)
        if not df_vix.empty:
            if isinstance(df_vix.columns, pd.MultiIndex): df_vix.columns = df_vix.columns.droplevel(1)
            vix_val = float(df_vix["Close"].iloc[-1].item())
            status = "안정" if vix_val <= 15 else "주의" if vix_val <= 25 else "공포" if vix_val <= 35 else "극단적 공포"
            return vix_val, f"{vix_val:.1f} ({status})"
    except: pass
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
# 3. 메인 전략 실행 (대왕 고기 모드)
# ==========================================
def main():
    ticker = "SOXL"
    try:
        df = yf.download(ticker, period="5d", auto_adjust=True, progress=False)
        if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.droplevel(1)
        df = df.dropna(subset=["Close", "Open"])
    except Exception as e:
        print(f"데이터 오류: {e}")
        return

    # [1] 미국 현지 시간 및 장 상태 판별 (시공간 동기화)
    tz_est = pytz.timezone('US/Eastern')
    now_est = datetime.now(tz_est)
    m_open = now_est.replace(hour=9, minute=30, second=0, microsecond=0)
    m_close = now_est.replace(hour=16, minute=0, second=0, microsecond=0)
    is_regular_market = m_open <= now_est <= m_close and now_est.weekday() < 5

    # [2] 대왕 고기 포인트 기준 설정
    if is_regular_market:
        mode_msg = "🚀 **장 개시 후 (실시간)**"
        prev_close = float(df["Close"].iloc[-2].item())
        today_open = float(df["Open"].iloc[-1].item())
        base = today_open
        gap_ratio = (today_open - prev_close) / prev_close
        price_info = f"✅ 전일 종가 : ${prev_close:.2f}\n✅ 오늘 시가 : ${today_open:.2f} ({gap_ratio:+.2f}%)"
    else:
        mode_msg = "⏳ **장 개시 전 (대기)**"
        prev_close = float(df["Close"].iloc[-1].item())
        base = prev_close
        gap_ratio = 0
        price_info = f"✅ 전일 종가 : ${prev_close:.2f}"

    # [3] 변동성 분석 및 심화 캐스팅 포인트 계산
    closes = df["Close"].values
    sigma_90 = calculate_annual_sigma(closes, 90)
    sigma_252 = calculate_annual_sigma(closes, 252)
    sigma_30 = calculate_annual_sigma(closes, 30)
    vol_ratio = sigma_30 / sigma_252 if sigma_252 > 0 else 1.0
    daily_vol = sigma_90 / np.sqrt(252)
    
    # 캐스팅 포인트 심화 적용 (-1.0 -> -1.5 / -0.5 -> -1.0)
    t_1_0 = base * (1 - daily_vol * 1.0)
    t_1_5 = base * (1 - daily_vol * 1.5)
    t_2_0 = base * (1 - daily_vol * 2.0)
    t_2_5 = base * (1 - daily_vol * 2.5)
    target_profit = base * (1 + daily_vol * 1.5) # 익절가도 1.5시그마로 상향

    vix_val, vix_info = get_vix_report()
    KST = pytz.timezone('Asia/Seoul')

    # [4] ★ 대왕 고기 선별 로직 ★
    if vix_val >= 35.0:
        regime, target_name, recommend_price = "🔴🔴 **VIX 비상**", "-2.5σ", t_2_5
        guidance = "⚠️ 역사적 기회! 대왕 고기(-2.5σ)를 낚으세요."
    elif is_regular_market and gap_ratio <= -0.01:
        regime, target_name, recommend_price = "📉 **갭하락 (기회)**", "-1.0σ", t_1_0
        guidance = "💡 갭하락 날입니다. 평소보다 깊은 -1.0σ에서 대기!"
    elif vol_ratio >= 1.30:
        regime, target_name, recommend_price = "🔴 **고변동성**", "-2.0σ", t_2_0
        guidance = "📉 변동성 폭발! 심해(-2.0σ)에 그물을 치세요."
    else:
        regime, target_name, recommend_price = "🟢 **정상 변동성**", "-1.5σ", t_1_5
        guidance = "🚀 평범한 하락은 거릅니다. -1.5σ 대왕 고기만 노리세요."

    # [5] 리포트 생성
    report = [
        f"━━━━━━━━━━━━━━━━━━━━",
        f"📊 **{ticker} 원칙 매매 [대왕고기 모드]**",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"{mode_msg} | {regime}",
        f"{price_info}",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"🎯 **오늘의 캐스팅 포인트**",
        f"👉 **{target_name} : ${recommend_price:.2f}**", 
        f"\n📍 익절 목표(+1.5σ) : ${target_profit:.2f}",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"📉 VIX 상태 : {vix_info}",
        f"🔎 가이드: {guidance}",
        f"◆ 캐스팅 : {CURRENT_CASTS}/{ANNUAL_QUOTA} 회",
        f"⏰ {datetime.now(KST).strftime('%m/%d %H:%M')}"
    ]

    final_report = "\n".join(report)
    print(final_report)
    send_discord(final_report)

    # 실행 기록 업데이트
    config["LAST_RUN_TIME"] = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4, ensure_ascii=False)

if __name__ == "__main__":
    main()