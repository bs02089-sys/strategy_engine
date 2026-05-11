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


# ====================== 함수 정의 ======================
def get_market_data(ticker: str):
    """시장 데이터 다운로드 및 계산"""
    try:
        data = yf.download(ticker, period="130d", auto_adjust=True, progress=False)

        if data.empty:
            raise ValueError(f"{ticker} 데이터를 가져올 수 없습니다.")

        close_prices = data["Close"].squeeze()
        daily_returns = close_prices.pct_change().dropna()
        rolling_std = daily_returns.rolling(window=20).std() * 100
        std_20d_avg = float(rolling_std[-20:].mean())

        if len(rolling_std) < 20 or pd.isna(std_20d_avg):
            logger.warning("변동성 계산 데이터 부족 → 기본값 1.8% 사용")
            std_20d_avg = 1.8

        prev_close = float(close_prices.iloc[-1])
        prev_date = data.index[-1].strftime('%Y-%m-%d')
        current_price = float(yf.Ticker(ticker).fast_info["last_price"])

        return {
            "prev_close": prev_close,
            "prev_date": prev_date,
            "current_price": current_price,
            "take_profit": prev_close * (1 + std_20d_avg / 100),
            "buy_target": prev_close * (1 - std_20d_avg / 100),
            "std_20d_avg": std_20d_avg,
        }

    except Exception as e:
        logger.error(f"시장 데이터 가져오기 실패: {e}")
        raise


def create_base_message(data: dict, kst_now: str, ticker: str):
    """최종 개선 버전 - 레이아웃 조정"""
    today_date = kst_now.split()[0]
    
    # 현재가 기준 목표까지 거리 계산
    to_tp = (data['take_profit'] - data['current_price']) / data['current_price'] * 100
    to_buy = (data['current_price'] - data['buy_target']) / data['current_price'] * 100

    return (
        f"🔔 **{ticker} 시장 현황** ({today_date})\n\n"
        f"📍 **현재 시각** : {kst_now}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 전일 종가 : ${data['prev_close']:.2f}\n"
        f"📊 현 재 가  : ${data['current_price']:.2f}\n"
        f"🎯 익절 목표 : ${data['take_profit']:.2f} (+{to_tp:.2f}%)\n"
        f"🛒 매수 목표 : ${data['buy_target']:.2f}  ({to_buy:.2f}%)\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )
    

def send_discord_message(content: str):
    """디스코드 웹훅 전송"""
    mention = f"<@{DISCORD_USER_ID}>" if DISCORD_USER_ID else ""
    message = f"{mention}\n{content}" if mention else content

    try:
        response = requests.post(
            DISCORD_WEBHOOK,
            json={"content": message},
            timeout=10
        )
        response.raise_for_status()
        logger.info("✅ Discord 메시지 전송 성공")
        return True
    except Exception as e:
        logger.error(f"❌ Discord 전송 실패: {e}")
        return False


# ====================== 메인 로직 ======================
def main():
    kst_now = datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')
    utc_hour = datetime.now(timezone.utc).hour
    
    force_run = os.getenv("FORCE_RUN", "false").lower() == "true"

    logger.info(f"스크립트 실행 | KST: {kst_now} | UTC Hour: {utc_hour} | FORCE_RUN: {force_run}")

    try:
        data = get_market_data(TICKER)

        if force_run or utc_hour == 0:
            base_msg = create_base_message(data, kst_now, TICKER)
            send_discord_message(f"```\n{base_msg}```")
            logger.info("✅ 오전 현황 알림 전송 완료")

        elif utc_hour == 10:
            if data["current_price"] >= data["take_profit"]:
                alert_type = "🔴 익절 알림"
                alert_line = f"🔥 현재가 ${data['current_price']:.2f} → 익절 목표 ${data['take_profit']:.2f} 도달!"
            elif data["current_price"] <= data["buy_target"]:
                alert_type = "🟢 매수 알림"
                alert_line = f"💰 현재가 ${data['current_price']:.2f} → 매수 목표 ${data['buy_target']:.2f} 도달!"
            else:
                logger.info(f"조건 미충족 | 현재가: ${data['current_price']:.2f}")
                return

            base_msg = create_base_message(data, kst_now, TICKER)
            message = f"```\n{base_msg}\n{'─'*30}\n{alert_line}\n{'═'*30}\n```"
            send_discord_message(message)
            logger.info(f"✅ {alert_type} 전송 완료")

        else:
            logger.info(f"스케줄 외 실행 시간 (UTC {utc_hour}시)")

    except Exception as e:
        logger.error(f"스크립트 실행 중 오류 발생: {e}")
        raise


if __name__ == "__main__":
    main()