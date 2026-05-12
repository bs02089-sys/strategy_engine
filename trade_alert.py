# ====================== Import ======================
# 1. 표준 라이브러리
import os
import sys
import logging
from datetime import datetime, timezone

# 2. 서드파티 라이브러리
import pandas as pd
import requests
import yfinance as yf
import pytz                               # ← 추가됨
from dotenv import load_dotenv

# 3. .env 로드 (환경변수 설정)
load_dotenv()

# 4. 환경변수 읽기
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK")
DISCORD_USER_ID = os.getenv("DISCORD_USER_ID")
TICKER = os.getenv("TICKER", "SSO")

# 필수 환경변수 체크
if not DISCORD_WEBHOOK:
    raise ValueError("❌ DISCORD_WEBHOOK 환경변수가 설정되지 않았습니다.")

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# 작업 디렉토리 고정
WORKING_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(WORKING_DIR)

KST = pytz.timezone('Asia/Seoul')


def get_market_data(ticker: str):
    """시장 데이터 다운로드 - GitHub Actions 최적화 버전"""
    try:
        data = yf.download(
            ticker,
            period="130d",
            auto_adjust=True,
            progress=False,
            timeout=30,
            threads=False,
        )

        if data.empty:
            raise ValueError(f"{ticker} 데이터를 가져올 수 없습니다. (empty)")

        # ==================== 컬럼 구조 방어 코드 ====================
        # MultiIndex인 경우 처리
        if isinstance(data.columns, pd.MultiIndex):
            data = data.droplevel(0, axis=1)   # ticker 레벨 제거

        # 컬럼명을 소문자로 정규화 (가끔 대소문자 문제 발생)
        data.columns = [col.lower() if isinstance(col, str) else col for col in data.columns]

        # Close 컬럼 찾기 (다양한 경우 대응)
        close_col = None
        for col in data.columns:
            if str(col).lower() in ['close', 'adj close', 'adj_close']:
                close_col = col
                break

        if close_col is None:
            raise KeyError(f"Close 컬럼을 찾을 수 없습니다. 현재 컬럼: {list(data.columns)}")

        close_prices = data[close_col].squeeze()

        # ============================================================

        daily_returns = close_prices.pct_change().dropna()
        rolling_std = daily_returns.rolling(window=20).std() * 100
        std_20d_avg = float(rolling_std.tail(20).mean())

        if len(rolling_std) < 20 or pd.isna(std_20d_avg) or std_20d_avg <= 0:
            logger.warning("변동성 계산 데이터 부족 → 기본값 1.8% 사용")
            std_20d_avg = 1.8

        # 현재가 가져오기 (2가지 방법 백업)
        ticker_obj = yf.Ticker(ticker)
        current_price = ticker_obj.fast_info.get("last_price")
        if not current_price or pd.isna(current_price):
            hist = ticker_obj.history(period="2d")
            current_price = float(hist["Close"].iloc[-1])

        prev_close = float(close_prices.iloc[-1])
        prev_date = data.index[-1].strftime('%Y-%m-%d')

        return {
            "prev_close": prev_close,
            "prev_date": prev_date,
            "current_price": float(current_price),
            "take_profit": prev_close * (1 + std_20d_avg / 100),
            "buy_target": prev_close * (1 - std_20d_avg / 100),
            "std_20d_avg": std_20d_avg,
        }

    except Exception as e:
        logger.error(f"시장 데이터 가져오기 실패: {e}")
        logger.error(f"받은 컬럼 정보: {list(data.columns) if 'data' in locals() else 'N/A'}")
        raise
        

def create_base_message(data: dict, kst_now: str, ticker: str):
    """최종 개선 버전"""
    today_date = kst_now.split()[0]
    
    # 현재가 기준 목표까지 거리 계산
    to_tp = (data['take_profit'] - data['current_price']) / data['current_price'] * 100
    to_buy = (data['buy_target'] - data['current_price']) / data['current_price'] * 100

    return (
        f"🔔 **{ticker} 시장 현황** ({today_date})\n\n"
        f"📍 **현재 시각** : {kst_now}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 전일 종가   : ${data['prev_close']:.2f}\n"           # ← latest_price → prev_close 수정
        f"📊 **현재가**   : ${data['current_price']:.2f}\n"
        f"🎯 익절 목표   : ${data['take_profit']:.2f}   (+{to_tp:.2f}%)\n"
        f"🛒 매수 목표   : ${data['buy_target']:.2f}   ({to_buy:.2f}%)\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )
            

def send_discord_message(content: str):
    """디스코드 웹훅 전송 - 디버깅 강화 버전"""
    mention = f"<@{DISCORD_USER_ID}>" if DISCORD_USER_ID else ""
    message = f"{mention}\n{content}" if mention else content

    try:
        response = requests.post(
            DISCORD_WEBHOOK,
            json={"content": message},
            timeout=15,
            headers={"Content-Type": "application/json"}
        )
        
        logger.info(f"Discord 응답 코드: {response.status_code}")
        logger.info(f"Discord 응답 내용: {response.text[:200]}")  # ← 디버깅용
        
        if response.status_code == 204:
            logger.info("✅ Discord 메시지 전송 성공 (No Content)")
            return True
        else:
            logger.error(f"❌ Discord 전송 실패: {response.status_code} - {response.text}")
            return False

    except requests.exceptions.RequestException as e:
        logger.error(f"❌ Discord 요청 예외 발생: {e}")
        return False
    except Exception as e:
        logger.error(f"❌ Discord 전송 중 알 수 없는 오류: {e}")
        return False

# ====================== 메인 로직 ======================
def main():
    kst_now = datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')
    utc_hour = datetime.now(timezone.utc).hour
    
    force_run = os.getenv("FORCE_RUN", "false").lower() == "true"

    logger.info(f"스크립트 실행 | KST: {kst_now} | UTC Hour: {utc_hour} | FORCE_RUN: {force_run}")

    try:
        data = get_market_data(TICKER)

        # FORCE_RUN이 True이면 무조건 전송
        if force_run or utc_hour in [0, 10]:
            base_msg = create_base_message(data, kst_now, TICKER)
            success = send_discord_message(f"```\n{base_msg}```")
            
            if success:
                logger.info("✅ Discord 알림 전송 완료")
            else:
                logger.error("❌ Discord 알림 전송 실패")
        else:
            logger.info(f"스케줄 외 시간 (UTC {utc_hour}시) - 알림 생략")

    except Exception as e:
        logger.error(f"스크립트 실행 중 오류: {e}")
        raise

if __name__ == "__main__":
    main()