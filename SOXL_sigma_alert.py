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

print(f"🔍 DEBUG: WEBHOOK_URL loaded = {bool(WEBHOOK_URL)}")

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
    closes = np.array(closes, dtype=float)
    closes = closes[~np.isnan(closes)]
    if len(closes) < window + 10:
        window = max(30, len(closes) - 10)
    
    # 윈도우 구간 로그 수익률
    window_closes = closes[-(window + 1):]
    log_returns = np.diff(np.log(window_closes))
    log_returns = log_returns[np.isfinite(log_returns)]
    
    if len(log_returns) < 15:
        return 0.40
    
    # ✅ ddof=1: 표본표준편차 (금융 표준)
    daily_sigma = np.std(log_returns, ddof=1)
    annual_sigma = daily_sigma * np.sqrt(252)
    
    # ✅ 디버그 출력 (검증용)
    print(f"  [{window}일 윈도우] 일별σ={daily_sigma*100:.2f}%, 연환산σ={annual_sigma*100:.2f}%")
    
    return annual_sigma


def get_vix_report():
    try:
        df_vix = yf.download("^VIX", period="2d", auto_adjust=True, progress=False)
        if not df_vix.empty:
            # ✅ 수정 1: 멀티인덱스 처리
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
        print("⚠️ DISCORD_WEBHOOK이 설정되지 않았습니다.")
        return
    ping = f"<@{DISCORD_USER_ID}>\n" if DISCORD_USER_ID else ""
    try:
        response = requests.post(WEBHOOK_URL, json={"content": ping + message}, timeout=15)
        if response.status_code == 204:
            print("✅ Discord 전송 성공")
        else:
            print(f"⚠️ Discord 전송 실패: {response.status_code}")
    except Exception as e:
        print(f"❌ Discord 전송 오류: {e}")


# ==========================================
# 3. 메인
# ==========================================
def main():
    ticker = "SOXL"

    try:
        df = yf.download(ticker, period="2y", auto_adjust=True, progress=False)
        if df.empty:
            print("❌ SOXL 데이터 다운로드 실패")
            return

        print(f"📥 yfinance 다운로드 완료: {len(df)} rows")
        print(f"컬럼 목록: {list(df.columns)}")

        # ✅ 수정 1: 멀티인덱스 처리 (최신 yfinance 대응)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.droplevel(1)

        # 안전한 NaN 제거
        drop_cols = [col for col in ["Close", "Open"] if col in df.columns]
        if drop_cols:
            df = df.dropna(subset=drop_cols)
        else:
            df = df.dropna()

    except Exception as e:
        print(f"yfinance 오류: {e}")
        return

    # 시간대 처리
    tz_est = pytz.timezone('US/Eastern')
    today_est = datetime.now(tz_est).date()
    last_row_date = df.index[-1].date()

    if last_row_date < today_est:
        prev_close = float(df["Close"].iloc[-1].item())
        today_open = prev_close
        mode_msg = "⏳ **장 개시 전: 전일 종가 기준**"
        is_market_open = False
    else:
        prev_close = float(df["Close"].iloc[-2].item())
        today_open = float(df["Open"].iloc[-1].item())
        mode_msg = "🚀 **장 개시 후**"
        is_market_open = True

    closes = df["Close"].values

    sigma_short = calculate_annual_sigma(closes, 30)
    sigma_main  = calculate_annual_sigma(closes, 90)
    sigma_long  = calculate_annual_sigma(closes, 252)
    ratio = sigma_short / sigma_long if sigma_long > 0 else 1.0

    gap_ratio = (today_open - prev_close) / prev_close
    base = prev_close if not is_market_open else today_open

    # 타점 계산
    t_1_0 = base * (1 - sigma_main)
    t_1_2 = base * (1 - sigma_main * 1.2)
    t_1_5 = base * (1 - sigma_main * 1.5)
    t_2_0 = base * (1 - sigma_main * 2.0)
    t_2_5 = base * (1 - sigma_main * 2.5)

    target_profit = prev_close * (1 + sigma_main)

    vix_val, vix_info = get_vix_report()

    # Regime 판단
    if vix_val >= 35.0:
        regime = "🔴🔴 **VIX 극단 공포**"
        guidance = "⚠️ 극단적 공포 구간. -2.5σ 이하에서 극소량 LOC 매수만 고려하세요."
        aggression = "극보수적"
        recommend = f"-2.5σ (${t_2_5:.2f})"
    elif ratio >= 1.50:
        regime = "🔴 극고변동성"
        guidance = "📉 변동성 매우 높음. -1.5σ ~ -2.0σ 구간에서 분할 매수 추천"
        aggression = "보수적"
        recommend = f"-1.5σ (${t_1_5:.2f}) ~ -2.0σ"
    elif ratio >= 1.20:
        regime = "🟡 고변동성"
        guidance = "📍 변동성 확대 중. -1.2σ ~ -1.5σ에서 분할 매수 추천"
        aggression = "중립"
        recommend = f"-1.2σ (${t_1_2:.2f}) ~ -1.5σ"
    else:
        regime = "🟢 저~중변동성"
        guidance = "🚀 안정 구간. -1.0σ ~ -1.2σ에서 적극 매수 추천"
        aggression = "공격적"
        recommend = f"-1.0σ (${t_1_0:.2f}) ~ -1.2σ"

    # 가이드 메시지
    if vix_val >= 35:
        guide_msg = f"🔥 **VIX 극단 공포! {recommend}까지 기다리세요**"
    elif is_market_open:
        if today_open <= t_2_0 * 0.99:
            guide_msg = "🔥 **-2.0σ 터치! 대량 분할 매수 고려**"
        elif today_open <= t_1_5:
            guide_msg = "🔥 **-1.5σ 근접! 분할 매수**"
        elif today_open <= t_1_2:
            guide_msg = "🔥 **-1.2σ 터치! 매수 고려**"
        elif today_open <= t_1_0:
            guide_msg = "🔥 **-1.0σ 터치! 매수**"
        else:
            guide_msg = f"⏳ 타점 대기 중 (추천: {recommend})"
    else:
        guide_msg = "💤 미 개장 전입니다."

    # 리포트
    profit_loss = ((today_open - MY_AVG_PRICE) / MY_AVG_PRICE * 100) if MY_AVG_PRICE > 0 else 0
    KST = pytz.timezone('Asia/Seoul')

    report = [
        f"━━━━━━━━━━━━━━━━━━━━",
        f"📊 **{ticker} 3단계 변동성 전략 리포트**",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"{mode_msg} | {regime}",
        f"📊 **90일 σ (메인)** : {sigma_main*100:.2f}%",
        f"✅ 전일 종가 : ${prev_close:.2f} ({profit_loss:+.2f}%)",
        f"🚀 금일 시가 : ${today_open:.2f} (갭 {gap_ratio*100:+.1f}%)",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"📍 -1.0σ : ${t_1_0:.2f}",
        f"📍 -1.2σ : ${t_1_2:.2f}",
        f"📍 -1.5σ : ${t_1_5:.2f}",
        f"📍 -2.0σ : ${t_2_0:.2f}",
        f"📍 -2.5σ : ${t_2_5:.2f} (VIX 극단용)",
        f"📍 +1.0σ 목표 : ${target_profit:.2f}",
        f"📉 단기/장기 σ 비율 : {ratio:.2f}",
        f"📉 VIX : {vix_info}",
        f"\n🔎 시장 판단",
        f"{guidance}",
        f"🎯 매수 공격성 : {aggression}",
        f"\n{guide_msg}",
        f"◆ {HOLD_DATE}까지 보유, 익절은 없다!",
        f"◆ 탄약 : {CURRENT_USED}/{ANNUAL_QUOTA} 발",
        # ✅ 수정 3: LAST_RUN_TIME을 KST 기준으로 저장
        f"⏰ {datetime.now(KST).strftime('%Y-%m-%d %H:%M')}"
    ]

    final_report = "\n".join(report)

    print("\n" + "="*60)
    print(final_report)
    print("="*60 + "\n")

    send_discord(final_report)

    # config 업데이트 (✅ 수정 3: KST 기준)
    config["LAST_RUN_TIME"] = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4, ensure_ascii=False)


if __name__ == "__main__":
    main()