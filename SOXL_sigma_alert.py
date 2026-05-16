import os
import json
import numpy as np
import pandas as pd
import requests
import yfinance as yf
import pytz
from datetime import datetime
from dotenv import load_dotenv

try:
    import holidays
except ImportError:
    holidays = None

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
        "MY_AVG_PRICE": 0.0,
        "CURRENT_CASTS": 0,
        "ANNUAL_QUOTA": 20,
        "LAST_RUN_TIME": "N/A"
    }

MY_AVG_PRICE = config.get("MY_AVG_PRICE", 0.0)
CURRENT_CASTS = config.get("CURRENT_CASTS", 0)
ANNUAL_QUOTA = config.get("ANNUAL_QUOTA", 20)

# ==========================================
# 2. 핵심 계산 및 판별 함수
# ==========================================
def calculate_annual_sigma(closes, window=90):
    closes = np.array(closes).flatten().astype(float)
    closes = closes[~np.isnan(closes)]
    if len(closes) < window + 1: window = len(closes) - 1
    
    window_closes = closes[-(window + 1):]
    log_returns = np.diff(np.log(window_closes))
    log_returns = log_returns[np.isfinite(log_returns)]
    
    if len(log_returns) < 5: return 0.70
    return np.std(log_returns, ddof=1) * np.sqrt(252)

def is_triple_witching_week(d):
    """매월 세 번째 금요일(세마녀의 날)이 포함된 주간인지 판별"""
    # 💡 세 번째 금요일(15일~21일) 주간의 변동성이 커지는 수, 목, 금요일 집중 모니터링 구간
    # (세 번째 금요일이 가장 빠를 때가 15일이므로 해당 주 수요일은 13일이 됩니다)
    is_witching_range = (13 <= d.day <= 21) and (2 <= d.weekday() <= 4)
    return is_witching_range

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
    if not WEBHOOK_URL: return
    ping = f"<@{DISCORD_USER_ID}>\n" if DISCORD_USER_ID else ""
    try:
        requests.post(WEBHOOK_URL, json={"content": ping + message}, timeout=15)
    except Exception as e: print(f"❌ 전송 오류: {e}")

# ==========================================
# 3. 메인 전략 실행
# ==========================================
def main():
    ticker = "SOXL"
    
    tz_est = pytz.timezone('US/Eastern')
    now_est = datetime.now(tz_est)
    today_date = now_est.date()

    # [방어막 1] 주말(토, 일) 체크
    if now_est.weekday() >= 5:
        print("📅 오늘은 즐거운 주말입니다. 미국 시장이 열리지 않습니다.")
        return

    # [방어막 2] 미국 연방 공휴일 체크 (정밀 알맹이 로직)
    if holidays is not None:
        us_holidays = holidays.US(years=today_date.year)
        if today_date in us_holidays:
            holiday_name = us_holidays.get(today_date)
            holiday_msg = f"📅 오늘은 미국 공휴일 [{holiday_name}]로 인해 휴장입니다. 편안한 하루 되세요!"
            print(holiday_msg)
            send_discord(f"━━━━━━━━━━━━━━━━━━━━\n📊 **{ticker} 휴장 안내**\n━━━━━━━━━━━━━━━━━━━━\n{holiday_msg}")
            return

    try:
        df = yf.download(ticker, period="130d", auto_adjust=True, progress=False)
        if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.droplevel(1)
        df = df.dropna(subset=["Close", "Open"])
    except Exception as e:
        print(f"데이터 오류: {e}"); return

    m_open = now_est.replace(hour=9, minute=30, second=0, microsecond=0)
    m_close = now_est.replace(hour=16, minute=0, second=0, microsecond=0)
    is_regular_market = m_open <= now_est <= m_close and now_est.weekday() < 5

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

    closes = df["Close"].values
    sigma_90 = calculate_annual_sigma(closes, 90)
    sigma_252 = calculate_annual_sigma(closes, 252)
    sigma_30 = calculate_annual_sigma(closes, 30)
    vol_ratio = sigma_30 / sigma_252 if sigma_252 > 0 else 1.0
    daily_vol = sigma_90 / np.sqrt(252)
    
    vix_val, vix_info = get_vix_report()
    
    # 세마녀 주간이더라도 본 정규장 중일 때만 특수 시그마 보정을 발동합니다.
    is_witching = is_triple_witching_week(today_date) and is_regular_market

    # [4] 매수 및 매도 예정가 결정 로직
    if vix_val >= 35.0:
        regime, t_name, recommend_buy = "🔴🔴 **VIX 비상**", "-2.5σ", base * (1 - daily_vol * 2.5)
        guidance = "⚠️ 역사적 기회! 최저점 월척을 낚으세요."
    elif is_witching:
        regime, t_name, recommend_buy = "🧙 **세 마녀 주간**", "-2.5σ", base * (1 - daily_vol * 2.5)
        guidance = "📉 마녀의 심술! 변동성이 크니 아주 깊은 곳(-2.5σ) 대기."
    elif is_regular_market and gap_ratio <= -0.01:
        regime, t_name, recommend_buy = "📉 **갭하락 (기회)**", "-1.5σ", base * (1 - daily_vol * 1.5)
        guidance = "💡 갭하락 날입니다. 평소보다 깊은 -1.5σ에서 대기!"
    elif vol_ratio >= 1.30:
        regime, t_name, recommend_buy = "🔴 **고변동성**", "-2.0σ", base * (1 - daily_vol * 2.0)
        guidance = "📉 변동성 폭발! 심해(-2.0σ)에 그물을 치세요."
    else:
        regime, t_name, recommend_buy = "🟢 **정상 변동성**", "-1.5σ", base * (1 - daily_vol * 1.5)
        guidance = "🚀 평범한 하락은 거릅니다. -1.5σ 월척만 노리세요."

    # 매도 예정가 결정
    if vix_val <= 15.0 or (not is_regular_market and base > prev_close * 1.02):
        sell_name, recommend_sell = "+2.0σ", base * (1 + daily_vol * 2.0)
    else:
        sell_name, recommend_sell = "+1.5σ", base * (1 + daily_vol * 1.5)

    KST = pytz.timezone('Asia/Seoul')

    # [5] 리포트 생성
    report = [
        f"━━━━━━━━━━━━━━━━━━━━",
        f"📊 **{ticker} 매매 전략[*월척 낚시 모드]**",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"{mode_msg} | {regime}",
        f"{price_info}",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"🎯 **오늘의 낚시 포인트**",
        f"👉 **매수 예정가({t_name}) : ${recommend_buy:.2f}**", 
        f"👉 **매도 예정가({sell_name}) : ${recommend_sell:.2f}**",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"📉 VIX : {vix_info}",
        f"🔎 가이드: {guidance}",
        f"◆ 낚시 횟수 : {CURRENT_CASTS}/{ANNUAL_QUOTA} 회",
        f"⏰ {datetime.now(KST).strftime('%m/%d %H:%M')}"
    ]

    final_report = "\n".join(report)
    print(final_report)
    send_discord(final_report)

    config["LAST_RUN_TIME"] = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4, ensure_ascii=False)

if __name__ == "__main__":
    main()