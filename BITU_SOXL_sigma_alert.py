import os
import numpy as np
import yfinance as yf
import requests
from datetime import datetime, timedelta

WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK")

TICKERS = ["BITU", "SOXL"]
LOOKBACK_DAYS = 252

def kst_now_str():
    now = datetime.utcnow() + timedelta(hours=9)
    return now.strftime("%Y-%m-%d %H:%M:%S")

def send_discord(content: str):
    if not WEBHOOK_URL:
        print("❌ DISCORD_WEBHOOK secret 없음")
        return
    try:
        requests.post(
            WEBHOOK_URL,
            json={"content": f"@everyone {content}"},
            timeout=15,
            headers={"Content-Type": "application/json"}
        )
        print("✅ Discord 전송 완료")
    except Exception as e:
        print(f"Discord 전송 실패: {e}")

def get_sigma_and_prev_close(symbol: str):
    try:
        print(f"📥 {symbol} 데이터 다운로드 시도...")
        
        # 더 안정적으로 가져오기 위한 옵션 추가
        df = yf.download(
            symbol,
            period="3y",
            interval="1d",
            progress=False,
            auto_adjust=True,
            timeout=30,
            threads=False,
            repair=True,           # 새로 추가
            keepna=True
        )
        
        print(f"   → 다운로드 완료: {len(df)} rows, 마지막 날짜: {df.index[-1] if not df.empty else 'None'}")
        
        if df.empty or len(df) < LOOKBACK_DAYS + 30:
            print(f"   ❌ {symbol}: 데이터 부족")
            return None, None

        closes = df['Close'].values
        log_returns = np.log(closes[1:] / closes[:-1])
        sigma = float(np.std(log_returns[-LOOKBACK_DAYS:]))
        prev_close = float(closes[-1])
        
        print(f"   ✅ {symbol} 성공 | Prev Close: {prev_close:.2f} | Sigma: {sigma*100:.2f}%")
        return sigma, prev_close
        
    except Exception as e:
        print(f"   ❌ {symbol} 예외 발생: {e}")
        return None, None


def main():
    print(f"🚀 Sigma Alert 시작 - {kst_now_str()}\n")
    
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
    send_discord(final_msg)

if __name__ == "__main__":
    main()
