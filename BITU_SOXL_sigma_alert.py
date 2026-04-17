import os
import numpy as np
import yfinance as yf
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

# ==================== 설정 ====================
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK")   # .env 대신 GitHub Secret에서 직접 읽음

TICKERS = ["BITU", "SOXL"]
LOOKBACK_DAYS = 252
KST = ZoneInfo("Asia/Seoul")

# yfinance 로그 최소화
yf.utils.get_yf_logger().setLevel(40)

# ==================== 유틸 ====================
def kst_now_str():
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")

def send_discord(content: str):
    if not WEBHOOK_URL:
        print("❌ DISCORD_WEBHOOK secret이 설정되지 않았습니다.")
        return False
    try:
        requests.post(
            WEBHOOK_URL,
            json={"content": f"@everyone {content}"},
            timeout=15,
            headers={"Content-Type": "application/json"}
        )
        return True
    except Exception as e:
        print(f"Discord 전송 실패: {e}")
        return False


def get_sigma_and_prev_close(symbol: str):
    try:
        df = yf.download(
            symbol, 
            period="3y", 
            progress=False, 
            auto_adjust=True, 
            timeout=20,
            threads=False   # GitHub Actions에서 안정성 ↑
        )
        
        if df.empty or len(df) < LOOKBACK_DAYS + 30:
            print(f"{symbol}: 데이터 부족 (rows: {len(df)})")
            return None, None

        closes = df['Close'].values
        log_returns = np.log(closes[1:] / closes[:-1])
        sigma = float(np.std(log_returns[-LOOKBACK_DAYS:]))
        prev_close = float(closes[-1])
        
        return sigma, prev_close
        
    except Exception as e:
        print(f"{symbol} 데이터 다운로드 실패: {e}")
        return None, None


# ==================== 메인 ====================
def main():
    print(f"🚀 Sigma Alert 시작 - {kst_now_str()}")
    
    messages = []
    
    for symbol in TICKERS:
        sigma, prev_close = get_sigma_and_prev_close(symbol)
        
        if sigma is None or prev_close is None:
            messages.append(f"❌ {symbol}: 데이터 로드 실패")
            continue

        thresh_1 = prev_close * (1 - sigma)
        thresh_2 = prev_close * (1 - 2 * sigma)

        msg = (
            f"📉 **{symbol} 매수 지정가 추천**\n"
            f"📅 {kst_now_str()} (KST)\n"
            f"전일 종가: `${prev_close:.2f}`\n"
            f"1σ 가격: `${thresh_1:.2f}`\n"
            f"2σ 가격: `${thresh_2:.2f}`"
        )
        messages.append(msg)

    final_msg = "\n\n".join(messages)
    print(final_msg)
    
    if messages:
        send_discord(final_msg)
    else:
        send_discord("⚠️ 모든 종목 데이터 로드 실패")

    print("✅ 스크립트 실행 완료")


if __name__ == "__main__":
    main()
