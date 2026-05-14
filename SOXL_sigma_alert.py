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

# 설정 파일 로드 (config.json이 없을 때만 0으로 초기화)
if os.path.exists(config_path):
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)
else:
    config = {
        "MY_AVG_PRICE": 0.0,
        "CURRENT_USED": 0,
        "ANNUAL_QUOTA": 20,
        "LAST_RUN_TIME": "N/A"
    }

MY_AVG_PRICE = config.get("MY_AVG_PRICE", 0.0)
CURRENT_USED = config.get("CURRENT_USED", 0)
ANNUAL_QUOTA = config.get("ANNUAL_QUOTA", 20)

# ==========================================
# 2. 핵심 계산 함수
# ==========================================
def calculate_annual_sigma(closes, window):
    closes = np.array(closes).flatten().astype(float)
    closes = closes[~np.isnan(closes)]
    if len(closes) < window + 1:
        window = len(closes) - 1
    
    window_closes = closes[-(window + 1):]
    log_returns = np.diff(np.log(window_closes))
    log_returns = log_returns[np.isfinite(log_returns)]
    
    if len(log_returns) < 5: return 0.60
    return np.std(log_returns, ddof=1) * np.sqrt(252)

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
    if not WEBHOOK_URL: return
    ping = f"<@{DISCORD_USER_ID}>\n" if DISCORD_USER_ID else ""
    try:
        requests.post(WEBHOOK_URL, json={"content": ping + message}, timeout=15)
    except Exception as e:
        print(f"❌ 전송 오류: {e}")

# ==========================================
# 3. 메인 전략 실행
# ==========================================
def main():
    ticker = "SOXL"
    try:
        # 최근 5일치 데이터를 가져와 시점 판별
        df = yf.download(ticker, period="5d", auto_adjust=True, progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.droplevel(1)
        df = df.dropna(subset=["Close", "Open"])
    except Exception as e:
        print(f"데이터 오류: {e}")
        return

    # [1] 미국 현지 시간 판별 (뉴욕 기준)
    tz_est = pytz.timezone('US/Eastern')
    now_est = datetime.now(tz_est)
    
    # 미국 정규장 시간 정의 (09:30 ~ 16:00)
    m_open = now_est.replace(hour=9, minute=30, second=0, microsecond=0)
    m_close = now_est.replace(hour=16, minute=0, second=0, microsecond=0)

    # 영업일이며, 현재 정규장 시간 내인지 확인
    is_regular_market = m_open <= now_est <= m_close and now_est.weekday() < 5

    # [2] 기준가 및 모드 설정
    if is_regular_market:
        # 장 중: 오늘 시가를 기준으로 타점 계산
        mode_msg = "🚀 **장 개시 후 (실시간)**"
        prev_close = float(df["Close"].iloc[-2].item())
        today_open = float(df["Open"].iloc[-1].item())
        base = today_open
        gap_ratio = (today_open - prev_close) / prev_close
        price_info = f"✅ 전일 종가 : ${prev_close:.2f}\n✅ 오늘 시가 : ${today_open:.2f} ({gap_ratio:+.2f}%)"
    else:
        # 장 전/후: 가장 최근 종가를 '전일 종가'로 설정
        mode_msg = "⏳ **장 개시 전 (대기)**"
        prev_close = float(df["Close"].iloc[-1].item())
        base = prev_close
        gap_ratio = 0
        price_info = f"✅ 전일 종가 : ${prev_close:.2f}"

    # [3] 변동성 분석 및 시그마 타점 계산
    closes = df["Close"].values
    sigma_90 = calculate_annual_sigma(closes, 90)
    sigma_252 = calculate_annual_sigma(closes, 252)
    sigma_30 = calculate_annual_sigma(closes, 30)
    vol_ratio = sigma_30 / sigma_252 if sigma_252 > 0 else 1.0
    daily_vol = sigma_90 / np.sqrt(252)
    
    t_0_5 = base * (1 - daily_vol * 0.5)
    t_1_0 = base * (1 - daily_vol * 1.0)
    t_2_0 = base * (1 - daily_vol * 2.0)
    t_2_5 = base * (1 - daily_vol * 2.5)
    target_profit = base * (1 + daily_vol * 1.0)

    # [4] 시장 판단 (VIX) 및 수익률
    vix_val, vix_info = get_vix_report()
    KST = pytz.timezone('Asia/Seoul')
    profit_loss = ((base - MY_AVG_PRICE) / MY_AVG_PRICE * 100) if MY_AVG_PRICE > 0 else 0

    # [5] ★ 원칙 기반 타점 선정 ★
    if vix_val >= 35.0:
        regime, target_name, recommend_price = "🔴🔴 **VIX 비상**", "-2.5σ", t_2_5
        guidance = "⚠️ 투매 발생! -2.5σ에서 거물을 낚으세요."
    elif is_regular_market and gap_ratio <= -0.01:
        regime, target_name, recommend_price = "📉 **갭하락 발생**", "-0.5σ", t_0_5
        guidance = "💡 낮게 시작했습니다! -0.5σ에서 낚으세요."
    elif vol_ratio >= 1.30:
        regime, target_name, recommend_price = "🔴 **고변동성**", "-2.0σ", t_2_0
        guidance = "📉 변동성 확대! 깊은 타점(-2.0σ) 대기."
    else:
        regime, target_name, recommend_price = "🟢 **정상 변동성**", "-1.0σ", t_1_0
        guidance = "🚀 안정적 흐름. -1.0σ에 그물을 치세요."

    # [6] 리포트 생성 및 전송
    report = [
        f"━━━━━━━━━━━━━━━━━━━━",
        f"📊 **{ticker} 원칙 매매 리포트**",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"{mode_msg} | {regime}",
        f"{price_info}",
        f"📈 내 수익률 : {profit_loss:+.2f}%",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"🎯 **오늘의 집행 타점**",
        f"👉 **{target_name} : ${recommend_price:.2f}**", 
        f"\n📍 익절 목표(+1.0σ) : ${target_profit:.2f}",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"📉 VIX 상태 : {vix_info}",
        f"🔎 가이드: {guidance}",
        f"◆ 탄약 : {CURRENT_USED}/{ANNUAL_QUOTA} 발",
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