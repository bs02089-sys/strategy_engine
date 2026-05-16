import os
import logging
from datetime import datetime

import pandas as pd
import requests
import yfinance as yf
import pytz
from dotenv import load_dotenv

try:
    import holidays
except ImportError:
    holidays = None

# 로깅 설정
logger = logging.getLogger(__name__)

# ====================== 핵심 환경 설정 ======================
def setup_environment():
    """환경 변수 로드 및 멀티 티커 설정"""
    load_dotenv()
    
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

# ====================== 데이터 분석 및 판별 로직 ======================
def is_triple_witching_week(d):
    """매월 세 번째 금요일(세마녀의 날)이 포함된 주간인지 판별"""
    # 💡 세 번째 금요일(15일~21일) 주간의 변동성이 커지는 수, 목, 금요일 집중 모니터링 구간
    # (세 번째 금요일이 가장 빠를 때가 15일이므로 해당 주 수요일은 13일이 됩니다)
    is_witching_range = (13 <= d.day <= 21) and (2 <= d.weekday() <= 4)
    return is_witching_range

def get_combined_market_data(tickers: list, est_tz: pytz.timezone):
    try:
        # [1] 데이터 다운로드
        df = yf.download(tickers, period="130d", interval="1d", auto_adjust=True, timeout=30, progress=False)
        
        if df is None or df.empty:
            logger.error("❌ 야후 파이낸스 서버 응답 없음")
            return {}, False

        results = {}
        now_est = datetime.now(est_tz)
        
        # 정규장 시간 판별
        m_open = now_est.replace(hour=9, minute=30, second=0, microsecond=0)
        m_close = now_est.replace(hour=16, minute=0, second=0, microsecond=0)
        is_regular_market = m_open <= now_est <= m_close and now_est.weekday() < 5

        # 실제 본 정규장 중(is_regular_market)일 때만 세마녀 보정을 활성화합니다.
        is_witching = is_triple_witching_week(now_est.date()) and is_regular_market

        for ticker in tickers:
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

            # [2] 변동성 계산 (std_20d = 1.0σ)
            daily_returns = ticker_df["Close"].pct_change().dropna()
            std_20d = float(daily_returns.tail(20).std() * 100)

            if pd.isna(std_20d) or std_20d <= 0:
                std_20d = 2.0 

            gap_ratio = (today_open - prev_close) / prev_close

            # [3] 매수 예정가 계산
            if is_witching:
                base_sigma = std_20d * 1.5
                if is_regular_market and gap_ratio < 0:
                    rem_std = max(0, base_sigma + (gap_ratio * 100))
                    buy_target = today_open * (1 - rem_std / 100)
                    sigma_mult = rem_std / std_20d if std_20d > 0 else 0
                    buy_name = f"-{sigma_mult:.1f}σ"
                    sub_msg = "🧙 세 마녀 주간 갭 하락 보정"
                else:
                    buy_target = prev_close * (1 - base_sigma / 100)
                    buy_name = "-1.5σ"
                    sub_msg = "🧙 세 마녀 주간 하단 그물 대기"
            else:
                if is_regular_market and gap_ratio < 0:
                    rem_std = max(0, std_20d + (gap_ratio * 100))
                    buy_target = today_open * (1 - rem_std / 100)
                    sigma_mult = rem_std / std_20d if std_20d > 0 else 0
                    buy_name = f"-{sigma_mult:.1f}σ"
                    sub_msg = "📉 갭 하락 보정 반영"
                else:
                    buy_target = prev_close * (1 - std_20d / 100)
                    buy_name = "-1.0σ"
                    sub_msg = "📈 기존 시그마 유지"

            # [4] 매도 예정가 계산
            if gap_ratio >= 0.02:
                sell_target = base * (1 + (std_20d * 1.5) / 100)
                sell_name = "+1.5σ"
            else:
                sell_target = base * (1 + std_20d / 100)
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
    mode = "🚀 실시간 모드" if is_open else "⏳ 장전 대기 모드"
    msg = f"🔔 **자산 관리 리포트 ({mode})**\n"
    
    for ticker, val in results.items():
        msg += f"\n━━━━━━━━━━━━━━━━━━━━━━\n"
        msg += f"📍 **종목 : {ticker}**\n"
        msg += f"📍 {val['sub_msg']}\n"
        msg += f"💰 전일 종가 : ${val['prev_close']:.2f}\n"
        msg += f"📊 20일 변동성(1σ) : ±{val['std']:.2f}%\n"
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

def main():
    config = setup_environment()
    kst_now = datetime.now(config["kst"]).strftime('%Y-%m-%d %H:%M:%S')
    
    now_est = datetime.now(config["est"])
    today_date = now_est.date()

    # [방어막 1] 주말 방어
    if now_est.weekday() >= 5:
        logger.info("📅 주말 휴장으로 인해 알림을 발송하지 않습니다.")
        return

    # [방어막 2] 미국 연방 공휴일 방어 (정밀 알맹이 로직)
    if holidays is not None:
        us_holidays = holidays.US(years=today_date.year)
        if today_date in us_holidays:
            holiday_name = us_holidays.get(today_date)
            logger.info(f"📅 미국 공휴일 [{holiday_name}]로 인한 휴장입니다. 얼럿을 발송하지 않습니다.")
            return

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