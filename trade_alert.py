import os
import logging
from datetime import datetime

import pandas as pd
import requests
import yfinance as yf
import pytz
from dotenv import load_dotenv

# 로깅 설정
logger = logging.getLogger(__name__)

# ====================== 핵심 환경 설정 ======================
def setup_environment():
    """환경 변수 로드 및 설정"""
    load_dotenv()
    config = {
        "webhook": os.getenv("DISCORD_WEBHOOK"),
        "user_id": os.getenv("DISCORD_USER_ID"),
        "ticker": os.getenv("TICKER", "IONQ"),
        "kst": pytz.timezone('Asia/Seoul'),
        "est": pytz.timezone('US/Eastern'),
    }
    return config

# ====================== 데이터 분석 로직 ======================
def get_market_data(ticker: str, est_tz: pytz.timezone):
    """시장 데이터를 가져와 정규장 시간 체크 및 하이브리드 타점 계산"""
    try:
        # 시그마 계산을 위해 충분한 데이터 로드
        ticker_obj = yf.Ticker(ticker)
        df = ticker_obj.history(period="130d", auto_adjust=True, timeout=30)

        if df.empty:
            raise ValueError(f"{ticker} 데이터 다운로드 실패")

        # [1] 미국 현지 시간 판별 및 장 상태 정밀 체크
        # ------------------------------------------
        now_est = datetime.now(est_tz)
        
        # 정규장 시간 정의 (09:30 ~ 16:00)
        m_open = now_est.replace(hour=9, minute=30, second=0, microsecond=0)
        m_close = now_est.replace(hour=16, minute=0, second=0, microsecond=0)
        
        # 실제 '정규장 중'인지 여부 (영업일 & 시간대)
        is_regular_market = m_open <= now_est <= m_close and now_est.weekday() < 5

        if is_regular_market:
            # 실시간 장 중 모드
            mode_msg = "🚀 장 개시 후 (실시간 보정)"
            prev_close = float(df["Close"].iloc[-2])
            today_open = float(df["Open"].iloc[-1])
            base_price = today_open
            is_market_open = True
        else:
            # 장 개시 전 또는 마감 후 모드
            mode_msg = "⏳ 장 개시 전 (대기/전일 기준)"
            prev_close = float(df["Close"].iloc[-1])
            today_open = prev_close  # 갭 계산용 (장전엔 0%가 됨)
            base_price = prev_close
            is_market_open = False
        # ------------------------------------------

        # [2] 변동성 계산 (20일 평균)
        daily_returns = df["Close"].pct_change().dropna()
        rolling_std = daily_returns.rolling(window=20).std() * 100
        std_20d_avg = float(rolling_std.tail(20).mean())

        if pd.isna(std_20d_avg) or std_20d_avg <= 0:
            std_20d_avg = 1.8

        # [3] 하이브리드 타점 계산
        gap_ratio = (today_open - prev_close) / prev_close
        
        # 장 중이면서 갭 하락 시에만 타점 하향 보정
        if is_market_open and gap_ratio < 0:
            rem_std = max(0, std_20d_avg + (gap_ratio * 100))
            buy_target = today_open * (1 - rem_std / 100)
            sub_msg = f"📉 갭 하락 보정 (-{abs(gap_ratio*100):.2f}% 반영)"
        else:
            buy_target = prev_close * (1 - std_20d_avg / 100)
            sub_msg = "📈 기존 타점 유지"

        take_profit = prev_close * (1 + std_20d_avg / 100)

        return {
            "prev_close": prev_close,
            "today_open": today_open,
            "gap_ratio": gap_ratio,
            "std": std_20d_avg,
            "buy_target": buy_target,
            "take_profit": take_profit,
            "mode_msg": mode_msg,
            "sub_msg": sub_msg,
            "is_market_open": is_market_open
        }

    except Exception as e:
        logger.error(f"시장 데이터 분석 실패: {e}")
        raise

def create_base_message(data: dict, kst_now: str, ticker: str):
    """디스코드 리포트 텍스트 생성"""
    return (
        f"🔔 **{ticker} 전략 알림**\n"
        f"📍 {data['mode_msg']}\n"
        f"📍 {data['sub_msg']}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 전일 종가 : ${data['prev_close']:.2f}\n"
        f"🚀 금일 시가 : ${data['today_open']:.2f} (Gap: {data['gap_ratio']*100:+.2f}%)\n"
        f"📊 평균 변동성 : ±{data['std']:.2f}%\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🛒 **매수 타점 : ${data['buy_target']:.2f}**\n"
        f"🎯 **익절 목표 : ${data['take_profit']:.2f}**\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏰ 분석시각: {kst_now}"
    )

def send_discord_message(content: str, webhook_url: str, user_id: str):
    """디스코드 웹훅 전송"""
    if not webhook_url: return False
    mention = f"<@{user_id}>" if user_id else ""
    try:
        requests.post(webhook_url, json={"content": f"{mention}\n{content}"}, timeout=15)
        return True
    except: return False

# ====================== 메인 실행부 ======================
# ====================== 메인 실행부 수정 ======================
def main():
    config = setup_environment()
    kst_now = datetime.now(config["kst"]).strftime('%Y-%m-%d %H:%M:%S')
    
    try:
        # 1. 시장 데이터 분석 및 하이브리드 타점 계산 (EST 시간대 전달)
        data = get_market_data(config["ticker"], config["est"])

        # 2. 메시지 생성 및 전송
        base_msg = create_base_message(data, kst_now, config["ticker"])
        
        # 디스코드 박스 형태를 위해 백틱을 안전하게 감쌉니다.
        final_content = f"```\n{base_msg}```"
        
        # 메시지 전송 실행
        success = send_discord_message(final_content, config["webhook"], config["user_id"])
        
        if success:
            logger.info(f"✅ [{config['ticker']}] 알림 전송 완료 (KST {kst_now})")
        else:
            logger.error("❌ 알림 전송 실패")

    except Exception as e:
        logger.error(f"⚠️ 스크립트 실행 중 오류 발생: {e}")
        
                
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    main()
