# ====================== Import ======================
import os
import sys
import logging
from datetime import datetime, timezone

import pandas as pd
import requests
import yfinance as yf
import pytz
from dotenv import load_dotenv

# 로깅 설정 (함수 밖에서는 정의만 수행)
logger = logging.getLogger(__name__)

# ====================== 핵심 환경 설정 ======================
def setup_environment():
    """환경 변수 로드 및 설정"""
    load_dotenv()
    config = {
        "webhook": os.getenv("DISCORD_WEBHOOK"),
        "user_id": os.getenv("DISCORD_USER_ID"),
        "ticker": os.getenv("TICKER", "SSO"),
        "kst": pytz.timezone('Asia/Seoul'),
        "force_run": os.getenv("FORCE_RUN", "false").lower() == "true"
    }
    return config

# ====================== 데이터 분석 로직 ======================
def get_market_data(ticker: str):
    """야후 파이낸스에서 시장 데이터를 가져와 변동성 및 목표가 계산"""
    try:
        ticker_obj = yf.Ticker(ticker)
        data = ticker_obj.history(period="130d", auto_adjust=True, timeout=30)

        if data.empty:
            raise ValueError(f"{ticker} 데이터 다운로드 실패")

        # 컬럼 소문자화 및 정리
        data.columns = [str(col).lower().replace(" ", "_") for col in data.columns]
        close_col = next((col for col in data.columns if 'close' in col), None)
        
        if close_col is None:
            raise KeyError(f"Close 컬럼을 찾을 수 없습니다.")

        close_prices = data[close_col].squeeze()

        # 변동성 계산 (20일 평균)
        daily_returns = close_prices.pct_change().dropna()
        rolling_std = daily_returns.rolling(window=20).std() * 100
        std_20d_avg = float(rolling_std.tail(20).mean())

        # 기본값 방어 로직
        if len(rolling_std) < 20 or pd.isna(std_20d_avg) or std_20d_avg <= 0:
            std_20d_avg = 1.8

        return {
            "prev_close": float(close_prices.iloc[-1]),
            "prev_date": data.index[-1].strftime('%Y-%m-%d'),
            "std_20d_avg": std_20d_avg,
        }

    except Exception as e:
        logger.error(f"시장 데이터 가져오기 실패: {e}")
        raise

def create_base_message(data: dict, kst_now: str, ticker: str):
    """디스코드에 보낼 메시지 텍스트 생성"""
    today_date = kst_now.split()[0]
    prev_close = data['prev_close']
    std = data['std_20d_avg']
    
    take_profit = prev_close * (1 + std / 100)
    buy_target = prev_close * (1 - std / 100)

    return (
        f"🔔 **{ticker} 시장 현황** ({today_date})\n\n"
        f"📍 **현재 시각** : {kst_now}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 **전일 종가** : ${prev_close:.2f}\n"
        f"📊 **20일 평균 변동성** : ±{std:.2f}%\n"
        f"🎯 익절 목표 : ${take_profit:.2f} (+{std:.2f}%)\n"
        f"🛒 매수 목표 : ${buy_target:.2f} (-{std:.2f}%)\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )

def send_discord_message(content: str, webhook_url: str, user_id: str):
    """디스코드 웹훅 전송"""
    if not webhook_url:
        logger.error("웹훅 URL이 없어 메시지를 전송할 수 없습니다.")
        return False

    mention = f"<@{user_id}>" if user_id else ""
    message = f"{mention}\n{content}"

    try:
        response = requests.post(
            webhook_url,
            json={"content": message},
            timeout=15
        )
        if response.status_code in [200, 204]:
            return True
        else:
            logger.error(f"Discord 전송 실패: {response.status_code}")
            return False
    except Exception as e:
        logger.error(f"Discord 통신 오류: {e}")
        return False

# ====================== 메인 실행부 ======================
def main():
    """스크립트의 실제 실행 로직"""
    config = setup_environment()
    kst_now = datetime.now(config["kst"]).strftime('%Y-%m-%d %H:%M:%S')
    utc_hour = datetime.now(timezone.utc).hour
    
    logger.info(f"실행 시작 | KST: {kst_now} | UTC Hour: {utc_hour}")

    try:
        # 데이터 수집
        data = get_market_data(config["ticker"])

        # 조건 체크 (FORCE_RUN이거나 특정 시간대인 경우)
        if config["force_run"] or utc_hour in [0, 10]:
            base_msg = create_base_message(data, kst_now, config["ticker"])
            success = send_discord_message(f"```\n{base_msg}```", config["webhook"], config["user_id"])
            
            if success:
                logger.info("✅ 트레이드 알림 전송 완료")
        else:
            logger.info(f"알림 생략 시간 (UTC {utc_hour}시)")

    except Exception as e:
        logger.error(f"스크립트 실행 중 오류 발생: {e}")

# 다른 파일에서 import 할 때는 실행되지 않도록 차단
if __name__ == "__main__":
    # 직접 실행 시 로깅 핸들러 추가
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s | %(levelname)s | %(message)s',
        handlers=[logging.StreamHandler(sys.stdout)]
    )
    
    # 작업 디렉토리 설정
    working_dir = os.path.dirname(os.path.abspath(__file__))
    if working_dir:
        os.chdir(working_dir)
        
    main()