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
    """환경 변수 로드 및 멀티 티커 설정"""
    load_dotenv()
    
    # 환경변수에서 TICKERS를 가져오되, 없으면 기본값 설정
    ticker_env = os.getenv("TICKERS", "TSLA,IONQ")
    ticker_list = [t.strip().upper() for t in ticker_env.split(",")]

    config = {
        "webhook": os.getenv("DISCORD_WEBHOOK"),
        "user_id": os.getenv("DISCORD_USER_ID"),
        "tickers": ticker_list,
        "kst": pytz.timezone('Asia/Seoul'),
        "est": pytz.timezone('US/Eastern'),
    }
    return config

# ====================== 데이터 분석 로직 ======================
def get_combined_market_data(tickers: list, est_tz: pytz.timezone):
    """멀티인덱스를 활용하여 여러 종목의 타점을 한 번에 계산"""
    try:
        # 여러 종목 데이터 일괄 다운로드(멀티인덱스 자동 생성)
        df = yf.download(tickers, period="130d", interval="1d", auto_adjust=True, timeout=30)
        
        if df.empty:
            raise ValueError("데이터 다운로드 실패")

        results = {}
        now_est = datetime.now(est_tz)
        
        # 정규장 시간 판별 (09:30 ~ 16:00)
        m_open = now_est.replace(hour=9, minute=30, second=0, microsecond=0)
        m_close = now_est.replace(hour=16, minute=0, second=0, microsecond=0)
        is_regular_market = m_open <= now_est <= m_close and now_est.weekday() < 5

        for ticker in tickers:
            # 멀티인덱스에서 특정 티커 데이터 추출
            # 한 종목만 넣었을 때와 여러 종목 넣었을 때의 인덱스 구조 대응
            if len(tickers) > 1:
                ticker_df = pd.DataFrame({
                    "Close": df["Close"][ticker],
                    "Open": df["Open"][ticker]
                }).dropna()
            else:
                ticker_df = df.copy().dropna()

            if is_regular_market:
                mode_msg = "🚀 장 개시 후 (실시간 보정)"
                prev_close = float(ticker_df["Close"].iloc[-2])
                today_open = float(ticker_df["Open"].iloc[-1])
            else:
                mode_msg = "⏳ 장 개시 전 (대기/전일 기준)"
                prev_close = float(ticker_df["Close"].iloc[-1])
                today_open = prev_close

            # 변동성 계산(20일 시그마 반영)
            daily_returns = ticker_df["Close"].pct_change().dropna()
            std_20d = float(daily_returns.tail(20).std() * 100)

            if pd.isna(std_20d) or std_20d <= 0:
                std_20d = 2.0  # 기본 방어값

            # 하이브리드 시그마 계산
            gap_ratio = (today_open - prev_close) / prev_close
            
            # 장 중이면서 갭 하락 시에만 시그마 하향 보정
            if is_regular_market and gap_ratio < 0:
                rem_std = max(0, std_20d + (gap_ratio * 100))
                buy_target = today_open * (1 - rem_std / 100)
                sub_msg = f"📉 갭 하락 보정 (-{abs(gap_ratio*100):.2f}% 반영)"
            else:
                buy_target = prev_close * (1 - std_20d / 100)
                sub_msg = "📈 기존 시그마 유지"

            results[ticker] = {
                "prev_close": prev_close,
                "today_open": today_open,
                "gap": gap_ratio * 100,
                "std": std_20d,
                "buy_target": buy_target,
                "mode_msg": mode_msg,
                "sub_msg": sub_msg
            }
            
        return results, is_regular_market

    except Exception as e:
        logger.error(f"데이터 분석 오류: {e}")
        raise

def create_combined_message(results: dict, is_open: bool, kst_now: str):
    """자산 관리 리포트 메시지 생성"""
    mode = "🚀 실시간 모드" if is_open else "⏳ 장전 대기 모드"
    msg = f"🔔 **자산 관리 리포트 ({mode})**\n"
    
    for ticker, val in results.items():
        msg += f"\n━━━━━━━━━━━━━━━━━━━━━━\n"
        msg += f"📍 **종목: {ticker}**\n"
        msg += f"📍 {val['sub_msg']}\n"
        msg += f"💰 전일 종가: ${val['prev_close']:.2f}\n"
        msg += f"📊 20일 변동성: ±{val['std']:.2f}%\n"
        msg += f"🛒 **매수 시그마(σ): ${val['buy_target']:.2f}**\n"
    
    msg += f"\n━━━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"⏰ 분석 시각: {kst_now}"
    return msg

def send_discord_message(content: str, webhook_url: str, user_id: str):
    if not webhook_url: return False
    mention = f"<@{user_id}>" if user_id else ""
    try:
        requests.post(webhook_url, json={"content": f"{mention}\n{content}"}, timeout=15)
        return True
    except: return False

# ====================== 메인 실행부 ======================
def main():
    config = setup_environment()
    kst_now = datetime.now(config["kst"]).strftime('%Y-%m-%d %H:%M:%S')
    
    try:
        # 멀티 데이터 분석
        results, is_open = get_combined_market_data(config["tickers"], config["est"])

        # 통합 메시지 생성
        final_msg = create_combined_message(results, is_open, kst_now)
        
        # 전송
        success = send_discord_message(f"```\n{final_msg}```", config["webhook"], config["user_id"])
        
        if success:
            logger.info(f"✅ 알림 전송 완료")
        else:
            logger.error("❌ 전송 실패")

    except Exception as e:
        logger.error(f"⚠️ 실행 오류: {e}")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    main()