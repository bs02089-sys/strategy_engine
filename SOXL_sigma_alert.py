import os
import json
import numpy as np
import requests
import yfinance as yf
import pytz
from datetime import datetime, timedelta
from dotenv import load_dotenv

# 1. 환경 설정 및 경로 지정
# ==========================================
load_dotenv()
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK")
DISCORD_USER_ID = os.getenv("DISCORD_USER_ID")

# 스크립트 위치 기준으로 작업 디렉토리 고정
WORKING_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(WORKING_DIR)
config_path = os.path.join(WORKING_DIR, "config.json")

# 설정 파일 로드 (없으면 기본값 생성)
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

# 전략 파라미터
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
# 3. 메인 전략 실행 (SOXL 연간 시그마 전략)
# ==========================================
def main():
    ticker = "SOXL"

    df = yf.download(ticker, period="2y", auto_adjust=True, progress=False)
    if df.empty:
        print(f"❌ {ticker} 데이터를 가져올 수 없습니다.")
        return

    closes = df["Close"].values.flatten()
    latest_close = float(closes[-1].item())

    # 연간 σ 계산
    sample_size = min(len(closes) - 1, 252)
    log_returns = np.diff(np.log(closes[-(sample_size + 1):]))
    sigma_val = np.std(log_returns)

    # 매수/매도 타점 계산
    target_price_1s = latest_close * (1 - sigma_val)
    target_price_2s = latest_close * (1 - (sigma_val * 2))
    target_profit_1s = latest_close * (1 + sigma_val)

    # 수익률 계산
    profit_loss = ((latest_close - MY_AVG_PRICE) / MY_AVG_PRICE * 100) if MY_AVG_PRICE > 0 else 0
    hold_date = (datetime.now() + timedelta(days=730)).strftime('%Y년 %m월 %d일')

    vix_val, vix_info = get_vix_report()
    order_strategy = get_order_strategy(vix_val)

    # 매수 신호 판정
    if latest_close <= target_price_2s:
        guide_msg = "🔥 **신호 감지: -2σ가격 터치! 2회분을 매수하세요.**"
    elif latest_close <= target_price_1s:
        guide_msg = "🔥 **신호 감지: -1σ가격 터치! 매수하세요.**"
    else:
        guide_msg = "⏳ 매수 대기중"

    KST = pytz.timezone('Asia/Seoul')
    report = [
        f"━━━━━━━━━━━━━━━━━━━━",
        f"📊 **{ticker} 전략 리포트**",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"✅ 전일 종가 : ${latest_close:.2f} ({profit_loss:+.2f}%)",
        f"📍 -1σ 매수 타점 : ${target_price_1s:.2f}",
        f"📍 -2σ 매수 타점 : ${target_price_2s:.2f}",
        f"📍 +1σ 매도 목표 : ${target_profit_1s:.2f}  (처형 연말 참고)",
        f"📍 1σ (연평균)   : {sigma_val*100:.2f}%",
        f"📉 VIX 지수 : {vix_info}",
        f"{order_strategy}",
        f"\n🎯 전략 지침",
        f"{guide_msg}",
        f"◆ 감정 배제, 신호 진입",
        f"◆ 시장 비명, 연간 20발, 기쁨의 한 발",
        f"◆ {hold_date}까지 보유, 익절은 없다!",
        f"◆ MDD 77%라는 훈장, 수익률 275%의 황금열쇠",
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