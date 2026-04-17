import os
import numpy as np
import yfinance as yf
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

# ==================== 설정 ====================
load_dotenv()
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK")

TICKERS = ["BITU", "SOXL"]
LOOKBACK_DAYS = 252
KST = ZoneInfo("Asia/Seoul")

# yfinance 조용히 만들기
yf.utils.get_yf_logger().setLevel(40)   # ERROR 이상만

# ==================== 유틸 ====================
def kst_now_str():
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")

def send_discord(content: str):
    if not WEBHOOK_URL:
        print("WEBHOOK_URL 미설정")
        return
    try:
        requests.post(
            WEBHOOK_URL,
            json={"content": f"@everyone {content}"},
            timeout=10,
            headers={"Content-Type": "application/json"}
        )
    except Exception as e:
        print(f"Discord 전송 실패: {e}")

# ==================== 데이터 가져오기 ====================
def get_sigma_and_prev_close(symbol: str):
    try:
        # 3년치 데이터 한 번에 가져오기
        df = yf.download(
            symbol, 
            period="3y", 
            progress=False, 
            auto_adjust=True,
            timeout=15
        )
        
        if df.empty or len(df) < LOOKBACK_DAYS + 10:
            return None, None

        closes = df['Close'].values  # numpy array로 바로 변환
        
        # 로그 수익률
        log_returns = np.log(closes[1:] / closes[:-1])
        
        # 최근 252일 표준편차 (연율화는 안 함 → 일별 sigma)
        sigma = float(np.std(log_returns[-LOOKBACK_DAYS:]))
        
        prev_close = float(closes[-1])
        
        return sigma, prev_close
        
    except Exception as e:
        print(f"{symbol} 데이터 오류: {e}")
        return None, None


# ==================== 메인 ====================
def main():
    messages = []
    now_kst = kst_now_str()
    alert_count = 0

    for symbol in TICKERS:
        sigma, prev_close = get_sigma_and_prev_close(symbol)
        
        if sigma is None or prev_close is None:
            messages.append(f"❌ {symbol}: 데이터 로드 실패")
            continue

        thresh_1 = prev_close * (1 - sigma)      # 1σ 하방
        thresh_2 = prev_close * (1 - 2 * sigma)  # 2σ 하방

        msg = (
            f"📉 [{symbol} 매수 신호]\n"
            f"발생 시각: {now_kst}\n"
            f"전일 종가: ${prev_close:.2f}\n"
            f"1σ ({sigma*100:.2f}%) 가격: ${thresh_1:.2f}\n"
            f"2σ ({2*sigma*100:.2f}%) 가격: ${thresh_2:.2f}"
        )
        messages.append(msg)
        alert_count += 1

    # 실제 알림 보내기
    if messages:
        final_msg = "\n\n".join(messages)
        print(final_msg)
        send_discord(final_msg)
    else:
        print("오늘은 모든 종목 데이터 정상")

    # 월 1일 핑
    if datetime.now(KST).day == 1:
        send_discord(f"✅ Monthly Ping: 시스템 정상 작동 ({now_kst})")


if __name__ == "__main__":
    main()
