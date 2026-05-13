import os
import json
import numpy as np
import requests
import yfinance as yf
import pytz
from datetime import datetime
from dotenv import load_dotenv

# 1. 환경 설정 및 경로 지정
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
        "LAST_RUN_TIME": "N/A"
    }

MY_AVG_PRICE = config.get("MY_AVG_PRICE", 0.0)
CURRENT_USED = config.get("CURRENT_USED", 0)
ANNUAL_QUOTA = config.get("ANNUAL_QUOTA", 20)

# ==========================================
# 2. 보조 함수
# ==========================================
def get_vix_report():
    try:
        df_vix = yf.download("^VIX", period="2d", auto_adjust=True, progress=False)
        if not df_vix.empty:
            vix_val = float(df_vix["Close"].iloc[-1].item())
            if vix_val <= 15: status = "안정"
            elif vix_val <= 25: status = "주의"
            elif vix_val <= 35: status = "공포"
            else: status = "극단적 공포"
            return vix_val, f"{vix_val:.1f} ({status})"
    except: pass
    return 0.0, "N/A"

def get_order_strategy(vix_val):
    if vix_val >= 35:
        return "📌 매수 방식 : LOC 매수 추천 (극단적 공포 → 종가 하락 가능성 높음)"
    else:
        return "📌 매수 방식 : 지정가 매수 추천 (장중 저점 체결 유리)"

def send_discord(message):
    if not WEBHOOK_URL: return
    ping = f"<@{DISCORD_USER_ID}>\n" if DISCORD_USER_ID else ""
    try:
        requests.post(WEBHOOK_URL, json={"content": ping + message}, timeout=10)
    except: pass

# ==========================================
# 3. 메인 전략 실행 (시간대별 데이터 무결성 확보)
# ==========================================
def main():
    ticker = "SOXL"
    
    # 2년치 데이터 (시그마 계산용)
    df = yf.download(ticker, period="2y", auto_adjust=True, progress=False)
    if df.empty:
        print(f"❌ {ticker} 데이터를 가져올 수 없습니다.")
        return

    # [시간대별 데이터 추출 로직 보정]
    # ------------------------------------------
    last_row_time = df.index[-1]
    today_date = datetime.now(pytz.timezone('US/Eastern')).date()

    if last_row_time.date() < today_date:
        # 상황 1: 아직 오늘 장이 열리기 전 (한국 시간 낮~오후)
        prev_close = float(df["Close"].iloc[-1].item())  # 마지막 줄이 어제의 확정 종가
        today_open = prev_close  # 아직 시가가 없으므로 종가로 임시 세팅
        mode_msg = "⏳ **장 개시 전: 전일 데이터 기준 (시가 미반영)**"
        is_market_open = False
    else:
        # 상황 2: 오늘 장이 개시됨 (밤 시간대)
        prev_close = float(df["Close"].iloc[-2].item())  # 뒤에서 두 번째 줄이 어제의 확정 종가
        today_open = float(df["Open"].iloc[-1].item())   # 마지막 줄이 오늘의 실시간 시가
        mode_msg = "🚀 **장 개시 후: 하이브리드 전략 적용 중**"
        is_market_open = True
    # ------------------------------------------

    # 연간 σ 계산 (최근 252거래일 로그 수익률 기준)
    closes = df["Close"].values.flatten()
    sample_size = min(len(closes) - 1, 252)
    log_returns = np.diff(np.log(closes[-(sample_size + 1):]))
    sigma_val = np.std(log_returns)

    # 매수 타점 계산 (하이브리드 로직)
    gap_ratio = (today_open - prev_close) / prev_close
    
    if is_market_open and gap_ratio < 0:
        # 갭 하락 시: 시가에서 잔여 변동성만큼 추가 대기
        rem_1s = max(0, sigma_val + gap_ratio) 
        rem_2s = max(0, (sigma_val * 2) + gap_ratio)
        target_price_1s = today_open * (1 - rem_1s)
        target_price_2s = today_open * (1 - rem_2s)
        sub_msg = f"📉 갭 하락 보정 적용 (타점 하향)"
    else:
        # 갭 상승 또는 장 개시 전: 전일 종가 기준 고정
        target_price_1s = prev_close * (1 - sigma_val)
        target_price_2s = prev_close * (1 - (sigma_val * 2))
        sub_msg = f"📈 갭 상승/장전 (종가 기준 유지)"

    target_profit_1s = prev_close * (1 + sigma_val)

    # 수익률 계산 및 리포트 작성
    profit_loss = ((today_open - MY_AVG_PRICE) / MY_AVG_PRICE * 100) if MY_AVG_PRICE > 0 else 0
    hold_date = "2028년 05월 07일"
    vix_val, vix_info = get_vix_report()
    order_strategy = get_order_strategy(vix_val)

    # 가이드 메시지 판정
    if is_market_open:
        if today_open <= target_price_2s:
            guide_msg = "🔥 **신호 감지: -2σ 터치! 2회분 매수**"
        elif today_open <= target_price_1s:
            guide_msg = "🔥 **신호 감지: -1σ 터치! 매수**"
        else:
            guide_msg = "⏳ 타점 대기 중"
    else:
        guide_msg = "💤 미 증시 개장 전입니다."

    KST = pytz.timezone('Asia/Seoul')
    report = [
        f"━━━━━━━━━━━━━━━━━━━━",
        f"📊 **{ticker} 전략 리포트**",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"{mode_msg}",
        f"{sub_msg}",
        f"✅ 전일 종가 : ${prev_close:.2f}",
        f"🚀 금일 시가 : ${today_open:.2f} (갭: {gap_ratio*100:+.2f}%)",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"📍 -1σ 매수 타점 : ${target_price_1s:.2f}",
        f"📍 -2σ 매수 타점 : ${target_price_2s:.2f}",
        f"📍 +1σ 매도 목표 : ${target_profit_1s:.2f}",
        f"📍 1σ (연평균)   : {sigma_val*100:.2f}%",
        f"📉 VIX 지수 : {vix_info}",
        f"{order_strategy}",
        f"\n🎯 전략 지침",
        f"{guide_msg}",
        f"◆ MDD 77%는 훈장, 수익률 275%는 결과",
        f"◆ {hold_date}까지 보유, 익절은 없다!",
        f"\n📊 시즌 탄약 : {CURRENT_USED}/{ANNUAL_QUOTA} 발",
        f"⏰ 시각: {datetime.now(KST).strftime('%Y-%m-%d %H:%M')}"
    ]

    final_report = "\n".join(report)
    print(final_report)
    send_discord(final_report)

    # config 업데이트
    config["LAST_RUN_TIME"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4, ensure_ascii=False)

if __name__ == "__main__":
    main()