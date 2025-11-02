import requests
from config.config import load_api_key_twelve_data

def get_live_price(ticker: str) -> float:
    """
    단일 종목의 실시간 가격을 Twelve Data API로 조회
    """
    api_key = load_api_key_twelve_data()
    if not api_key:
        print(f"⚠️ API 키 없음 → 실시간 가격 조회 불가")
        return 0.0

    try:
        url = f"https://api.twelvedata.com/price?symbol={ticker}&apikey={api_key}"
        response = requests.get(url, timeout=3)
        response.raise_for_status()
        data = response.json()

        if "price" in data:
            return float(data["price"])
        else:
            print(f"⚠️ {ticker} 가격 조회 실패: {data}")
            return 0.0

    except Exception as e:
        print(f"❌ {ticker} 가격 조회 오류: {e}")
        return 0.0