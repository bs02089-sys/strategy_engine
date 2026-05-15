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
    try:
        # [1] 데이터 다운로드
        df = yf.download(tickers, period="130d", interval="1d", auto_adjust=True, timeout=30)
        
        # 데이터가 제대로 안 왔을 때 에러 방지
        if df is None or df.empty:
            logger.error("❌ 야후 파이낸스 서버 응답 없음 (데이터 비어있음)")
            return {}, False

        # 멀티인덱스 컬럼 존재 여부 체크 (에러 방어)
        if len(tickers) > 1 and "Close" not in df.columns:
            logger.error("❌ 데이터 컬럼이 올바르지 않습니다.")
            return {}, False

        results = {}
        now_est = datetime.now(est_tz)
        
        # 정규장 시간 판별 (09:30 ~ 16:00)
        m_open = now_est.replace(hour=9, minute=30, second=0, microsecond=0)
        m_close = now_est.replace(hour=16, minute=0, second=0, microsecond=0)
        is_regular_market = m_open <= now_est <= m_close and now_est.weekday() < 5

        for ticker in tickers:
            # 멀티인덱스 대응 데이터 추출
            if len(tickers) > 1:
                ticker_df = pd.DataFrame({
                    "Close": df["Close"][ticker],
                    "Open": df["Open"][ticker]
                }).dropna()
            else:
                ticker_df = df.copy().dropna()

            if ticker_df.empty: continue

            if is_regular_market:
                prev_close = float(ticker_df["Close"].iloc[-2])
                today_open = float(ticker_df["Open"].iloc[-1])
                base = today_open
            else:
                prev_close = float(ticker_df["Close"].iloc[-1])
                today_open = prev_close
                base = prev_close

            # [2] 변동성 계산 (가족 조언용 20일 시그마 적용)
            daily_returns = ticker_df["Close"].pct_change().dropna()
            std_20d = float(daily_returns.tail(20).std() * 100)

            if pd.isna(std_20d) or std_20d <= 0:
                std_20d = 2.0  # 기본 방어값

            # [3] 매수 예정가 계산 (하이브리드 갭 하락 보정)
            gap_ratio = (today_open - prev_close) / prev_close
            
            if is_regular_market and gap_ratio < 0:
                rem_std = max(0, std_20d + (gap_ratio * 100))
                buy_target = today_open * (1 - rem_std / 100)
                buy_name = f"-{std_20d:.1f}σ (보정)"
                sub_msg = f"📉 갭 하락 보정 반영"
            else:
                buy_target = prev_close * (1 - std_20d / 100)
                buy_name = f"-{std_20d:.1f}σ"
                sub_msg = "📈 기존 시그마 유지"

            # [4] 매도 예정가 계산 (과열 시 +2.0σ, 평상시 +1.5σ)
            # 단기 급등 시(2% 이상)에는 목표가를 높게 잡음
            if gap_ratio >= 0.02:
                sell_target = base * (1 + (std_20d * 2.0 / 1.5) / 100) # 비율상 2.0시그마 수준
                sell_name = "+2.0σ"
            else:
                sell_target = base * (1 + std_20d / 100) # 1.0시그마 수준(단기 대응용)
                sell_name = "+1.0σ"

            results[ticker] = {
                "prev_close": prev_close,
                "std": std_20d,
                "buy_target": buy_target,
                "buy_name": buy_name,
                "sell_target": sell_target,
                "sell_name": sell_name,
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
        msg += f"📍 **종목 : {ticker}**\n"
        msg += f"📍 {val['sub_msg']}\n"
        msg += f"💰 전일 종가 : ${val['prev_close']:.2f}\n"
        msg += f"📊 20일 변동성 : ±{val['std']:.2f}%\n"
        msg += f"🛒 매수 예정가({val['buy_name']}) : ${val['buy_target']:.2f}\n"
        msg += f"💰 매도 예정가({val['sell_name']}) : ${val['sell_target']:.2f}\n"
    
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
        results, is_open = get_combined_market_data(config["tickers"], config["est"])
        if not results: return

        final_msg = create_combined_message(results, is_open, kst_now)
        send_discord_message(f"```\n{final_msg}```", config["webhook"], config["user_id"])
        logger.info(f"✅ 알림 전송 완료")

    except Exception as e:
        logger.error(f"⚠️ 실행 오류: {e}")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    main()