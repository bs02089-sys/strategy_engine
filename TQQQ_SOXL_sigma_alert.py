import os
import numpy as np
import requests
import time
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK")
MASSIVE_API_KEY = os.getenv("MASSIVE_API_KEY")   

TICKERS = ["TQQQ", "SOXL"]
LOOKBACK_DAYS = 252

# ==================== 유틸 ====================
def kst_now_str():
    now = datetime.utcnow() + timedelta(hours=9)
    return now.strftime("%Y-%m-%d %H:%M:%S")

def send_discord(content: str):
    if not WEBHOOK_URL:
        print("⚠️ DISCORD_WEBHOOK이 설정되지 않았습니다.")
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
        print(f"❌ Discord 전송 실패: {e}")

# ==================== Massive API 데이터 가져오기 ====================
def get_sigma_and_prev_close(symbol: str):
    if not MASSIVE_API_KEY:
        print(f"❌ {symbol}: MASSIVE_API_KEY가 .env에 없습니다.")
        return None, None

    for attempt in range(3):
        try:
            print(f"📥 {symbol} Massive API 데이터 요청 중... ({attempt+1}/3)")
            
            # 최근 3년치 일봉 데이터 요청
            end_date = datetime.now().strftime("%Y-%m-%d")
            start_date = (datetime.now() - timedelta(days=1100)).strftime("%Y-%m-%d")
            
            url = f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range/1/day/{start_date}/{end_date}"
            
            params = {
                "adjusted": "true",
                "sort": "asc",
                "limit": 50000
            }
            
            headers = {"Authorization": f"Bearer {MASSIVE_API_KEY}"}
            
            resp = requests.get(url, params=params, headers=headers, timeout=30)
            
            if resp.status_code != 200:
                print(f"   HTTP {resp.status_code}: {resp.text[:200]}")
                time.sleep(8)
                continue
                
            data = resp.json()
            
            if "results" not in data or not data["results"]:
                print(f"   ❌ 결과 없음")
                time.sleep(5)
                continue
                
            results = data["results"]
            print(f"   → {len(results)} 개 데이터 수신")
            
            # 종가 추출
            closes = np.array([bar["c"] for bar in results])
            
            if len(closes) < LOOKBACK_DAYS + 30:
                print(f"   ❌ 데이터 부족 ({len(closes)} rows)")
                return None, None
                
            # 로그 수익률 기반 sigma 계산
            log_returns = np.log(closes[1:] / closes[:-1])
            sigma = float(np.std(log_returns[-LOOKBACK_DAYS:]))
            prev_close = float(closes[-1])
            
            print(f"   ✅ {symbol} 성공 | 종가: {prev_close:.2f} | σ: {sigma*100:.2f}%")
            return sigma, prev_close
            
        except Exception as e:
            print(f"   시도 {attempt+1} 실패: {e}")
            time.sleep(10)
    
    print(f"   ❌ {symbol} 최종 실패")
    return None, None

# ==================== 메인 ====================
def main():
    if not MASSIVE_API_KEY:
        print("❌ MASSIVE_API_KEY가 설정되지 않았습니다. .env 파일을 확인하세요.")
        return
        
    print(f"🚀 Massive API Sigma Alert 시작 - {kst_now_str()}\n")
    
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
            f"1σ 가격: `${thresh_1:.2f}` ← 추천 지정가\n"
            f"2σ 가격: `${thresh_2:.2f}` ← 강력 매수 지정가"
        )
        messages.append(msg)

    final_msg = "\n\n".join(messages)
    print("="*60)
    print(final_msg)
    print("="*60 + "\n")
    
    send_discord(final_msg)

if __name__ == "__main__":
    main()
