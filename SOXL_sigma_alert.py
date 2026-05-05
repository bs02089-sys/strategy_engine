import os
import json
import numpy as np
import requests
import yfinance as yf
from datetime import datetime, timedelta
from dotenv import load_dotenv

# ==========================================
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
        "ANNUAL_QUOTA": 20,
        "CURRENT_USED": 0,
        "LAST_RUN_TIME": "N/A"
    }

# 전략 파라미터 고정 (연간 개념 일치)
ANNUAL_QUOTA = 20
CURRENT_USED = config.get("CURRENT_USED", 0)
MY_AVG_PRICE = config.get("MY_AVG_PRICE", 0.0)

# ==========================================
# 2. 보조 함수 (VIX 및 메시지 전송)
# ==========================================
def get_vix_report():
    """VIX 지수 수집 및 상태 판별"""
    try:
        df_vix = yf.download("^VIX", period="2d", auto_adjust=True, progress=False)
        if not df_vix.empty:
            vix_val = float(df_vix["Close"].iloc[-1].item())
            if vix_val <= 15: status = "안정"
            elif vix_val <= 25: status = "주의"
            elif vix_val <= 35: status = "공포"
            else: status = "극단적 공포"
            return f"{vix_val:.1f} ({status})"
    except: pass
    return "N/A"

def send_discord(message):
    """디스코드 메시지 전송"""
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
    
    # [개념 일치] 연간 변동성 추출을 위해 2년치 데이터 수집
    df = yf.download(ticker, period="2y", auto_adjust=True, progress=False)
    if df.empty:
        print(f"❌ {ticker} 데이터를 가져올 수 없습니다.")
        return

    # 데이터 정리
    closes = df["Close"].values.flatten()
    latest_close = float(closes[-1].item())
    prev_close = float(closes[-2].item())
    
    # [핵심] 최근 252거래일(약 1년) 로그 수익률 기반 연간 평균 변동성(1σ) 계산
    sample_size = min(len(closes) - 1, 252)
    log_returns = np.diff(np.log(closes[-(sample_size + 1):]))
    sigma_val = np.std(log_returns)
    
    # -1σ 가격: 전일 종가 대비 연간 평균 변동성 하단
    target_price = prev_close * (1 - sigma_val)
    
    # 수익률 및 보유 기한 계산
    profit_loss = ((latest_close - MY_AVG_PRICE) / MY_AVG_PRICE * 100) if MY_AVG_PRICE > 0 else 0
    hold_date = (datetime.now() + timedelta(days=730)).strftime('%Y년 %m월 %d일')
    vix_info = get_vix_report()

    # 매수 신호 판정
    is_buy_signal = latest_close <= target_price
    guide_msg = "🔥 **신호 감지: -1σ가격 터치! 매수하세요.**" if is_buy_signal else "⏳매수 대기중"

# 메시지
    report = [
        f"━━━━━━━━━━━━━━━━━━━━",
        f"📊 **{ticker} 전략 리포트**",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"✅ 전일 종가 : ${latest_close:.2f} ({profit_loss:+.2f}%)",
        f"📍 -1σ 타점 : ${target_price:.2f}",
        f"📍 1σ (연평균) : {sigma_val*100:.2f}%",
        f"📉 VIX 지수 : {vix_info}",
        f"\n🎯 전략 지침",
        f"{guide_msg}",
        f"◆ 감정 배제, 신호 진입\n"
        f"◆ 시장 비명, 연간 20발, 기쁨의 한 발\n"
        f"◆ {hold_date}까지 보유, 익절은 없다!\n"
        f"◆ MDD 77%라는 훈장, 수익률 275%의 황금열쇠\n"
        f"\n📊 시즌 탄약 : {CURRENT_USED}/{ANNUAL_QUOTA} 발\n"
        f"━━━━━━━━━━━━━━━━━━━━",       
        f"⏰ 시각: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    ]
    
    final_report = "\n".join(report)
    print(final_report)
    send_discord(final_report)

    # 설정 업데이트
    config["LAST_RUN_TIME"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4, ensure_ascii=False)

if __name__ == "__main__":
    main()