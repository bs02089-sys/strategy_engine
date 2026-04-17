import os
import numpy as np
import yfinance as yf
import requests
from datetime import datetime

# ==================== 설정 ====================
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK")

TICKERS = ["BITU", "SOXL"]
LOOKBACK_DAYS = 252

# ==================== 유틸 ====================
def kst_now_str():
    # tzinfo 없이 단순 KST (UTC+9) 사용 → zoneinfo 문제 회피
    now = datetime.utcnow()
    kst = now.replace(tzinfo=None) + timedelta(hours=9)
    return kst.strftime("%Y-%m-%d %H:%M:%S")

def send_discord(content: str):
    if not WEBHOOK_URL:
        print("❌ DISCORD_WEBHOOK secret이 없습니다.")
        return
    try:
        requests.post(
            WEBHOOK_URL,
            json={"content": f"@everyone {content}"},
            timeout=15,
            headers={"Content-Type": "application/json"}
        )
        print("Discord 전송 완료")
    except Exception as e:
        print(f"Discord 전송 실패: {e}")

# ==================== 데이터 가져오기 ====================
def get_sigma_and_prev_close(symbol: str):
    try:
        print(f"{symbol} 데이터 다운로드 시작...")
        
        df = yf.download(
            symbol,
            period="3y",
            progress=False,
            auto_adjust=True,
            timeout=25,
            threads=False
        )
        
        print(f"{symbol} 데이터 로드 완료: {len(df)} rows")
        
        if df.empty or len(df) < LOOKBACK_DAYS + 30:
            print(f"{symbol}: 데이터 부족")
            return None, None

        closes = df['Close'].values
        log_returns = np.log(closes[1:] / closes[:-1])
        sigma = float(np.std(log_returns[-LOOKBACK_DAYS:]))
        prev_close = float(closes[-1])
        
        return sigma, prev_close
        
    except Exception as e:
        print(f"{symbol} 오류 발생: {e}")
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
    print("\n" + final_msg + "\n")
    
    send_discord(final_msg)

    print("✅ 스크립트 실행 완료")


if __name__ == "__main__":
    from datetime import timedelta   # 여기서 import
    main()
