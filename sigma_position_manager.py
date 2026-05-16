import logging
import json
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

logger = logging.getLogger(__name__)


# ====================== 설정 로드 ======================
def load_config():
    load_dotenv()
    
    try:
        with open('config.json', 'r', encoding='utf-8') as f:
            config = json.load(f)
        logger.info("✅ config.json에서 설정을 불러왔습니다.")
        return config
    except FileNotFoundError:
        logger.error("❌ config.json 파일을 찾을 수 없습니다.")
        raise
    except json.JSONDecodeError:
        logger.error("❌ config.json 파일 형식이 잘못되었습니다.")
        raise


def setup_environment():
    config_dict = load_config()
    
    config = {
        "webhook": config_dict.get("DISCORD_WEBHOOK"),
        "user_id": config_dict.get("DISCORD_USER_ID"),
        "tickers": config_dict.get("TICKERS", ["TSLA"]),
        "positions": config_dict.get("POSITIONS", {"TSLA": 11}),
        "kst": pytz.timezone('Asia/Seoul'),
        "est": pytz.timezone('US/Eastern'),
    }
    return config


# ====================== 분할 매도 계산 ======================
def calculate_split_sell_targets(base_price: float, std_20d: float, shares: int):
    if shares <= 0:
        return []
    
    levels = [
        (0.9, "1단계 +0.9σ"),
        (1.3, "2단계 +1.3σ"),
        (1.8, "3단계 +1.8σ")
    ]
    
    split_plan = []
    remaining = shares
    per_level_base = max(1, shares // len(levels))
    
    for i, (sigma_mult, name) in enumerate(levels):
        qty = per_level_base if i < len(levels) - 1 else remaining
        if qty <= 0:
            break
        target_price = base_price * (1 + (std_20d * sigma_mult) / 100)
        split_plan.append({
            "level": name,
            "sigma": sigma_mult,
            "price": round(target_price, 2),
            "qty": qty
        })
        remaining -= qty
    return split_plan


# ====================== 기타 유틸 함수 ======================
def is_triple_witching_week(d):
    return (13 <= d.day <= 21) and (2 <= d.weekday() <= 4)


def get_vix_report():
    try:
        df_vix = yf.download("^VIX", period="2d", auto_adjust=True, progress=False)
        if not df_vix.empty:
            if isinstance(df_vix.columns, pd.MultiIndex):
                df_vix.columns = df_vix.columns.droplevel(1)
            vix_val = float(df_vix["Close"].iloc[-1])
            status = "안정" if vix_val <= 15 else "주의" if vix_val <= 25 else "공포" if vix_val <= 35 else "극단적 공포"
            return vix_val, f"{vix_val:.1f} ({status})"
    except:
        pass
    return 0.0, "N/A"


# ====================== 시장 데이터 분석 ======================
def get_combined_market_data(tickers: list, config: dict, est_tz: pytz.timezone):
    df = yf.download(tickers, period="130d", interval="1d", auto_adjust=True, progress=False)
    
    results = {}
    now_est = datetime.now(est_tz)
    
    m_open = now_est.replace(hour=9, minute=30, second=0, microsecond=0)
    m_close = now_est.replace(hour=16, minute=0, second=0, microsecond=0)
    is_regular_market = m_open <= now_est <= m_close and now_est.weekday() < 5

    is_witching = is_triple_witching_week(now_est.date()) and is_regular_market
    vix_val, vix_info = get_vix_report()

    for ticker in tickers:
        if len(tickers) > 1:
            ticker_df = pd.DataFrame({
                "Close": df["Close"][ticker],
                "Open": df["Open"][ticker]
            }).dropna()
        else:
            ticker_df = df.copy().dropna()

        if ticker_df.empty:
            continue

        if is_regular_market and len(ticker_df) >= 2:
            prev_close = float(ticker_df["Close"].iloc[-2])
            today_open = float(ticker_df["Open"].iloc[-1])
            base = today_open
        else:
            prev_close = float(ticker_df["Close"].iloc[-1])
            base = prev_close

        daily_returns = ticker_df["Close"].pct_change().dropna()
        std_20d = float(daily_returns.tail(20).std() * 100)
        if pd.isna(std_20d) or std_20d <= 0:
            std_20d = 2.0

        shares = config.get("positions", {}).get(ticker, 0)
        split_plan = calculate_split_sell_targets(base, std_20d, shares)

        results[ticker] = {
            "prev_close": prev_close,
            "std": std_20d,
            "split_sell_plan": split_plan,
            "total_shares": shares
        }
        
    return results, is_regular_market, vix_info


# ====================== 메시지 생성 ======================
def create_combined_message(results: dict, is_open: bool, kst_now: str, vix_info: str):
    mode = "🚀 실시간 모드" if is_open else "⏳ 장전 대기 모드"
    msg = f"🔔 **자산 관리 리포트 ({mode})**\n"
    msg += f"🎬 VIX : {vix_info}\n"
    
    for ticker, val in results.items():
        msg += f"\n━━━━━━━━━━━━━━━━━━━━━━\n"
        msg += f"📍 **종목 : {ticker}** (보유: {val.get('total_shares', 0)}주)\n"
        msg += f"📊 20일 변동성(1σ) : ±{val['std']:.2f}%\n"
        
        if val.get("split_sell_plan"):
            msg += f"📌 **3단계 분할 매도 계획**\n"
            for plan in val["split_sell_plan"]:
                msg += f"   • {plan['level']:18} → ${plan['price']:.2f}  ({plan['qty']}주)\n"
    
    msg += f"\n━━━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"⏰ 분석 시각: {kst_now}"
    return msg


# ====================== Discord 전송 ======================
def send_discord_message(content: str, webhook_url: str, user_id: str):
    if not webhook_url:
        return False
    mention = f"<@{user_id}>" if user_id else ""
    try:
        requests.post(webhook_url, json={"content": f"{mention}\n{content}"}, timeout=15)
        return True
    except:
        return False


# ====================== 메인 ======================
def main():
    config = setup_environment()
    kst_now = datetime.now(config["kst"]).strftime('%Y-%m-%d %H:%M:%S')
    
    now_est = datetime.now(config["est"])
    today_date = now_est.date()

    if now_est.weekday() >= 5:
        logger.info("📅 주말 휴장입니다.")
        return

    if holidays is not None:
        us_holidays = holidays.US(years=today_date.year)
        if today_date in us_holidays:
            logger.info(f"📅 미국 공휴일입니다.")
            return

    try:
        results, is_open, vix_info = get_combined_market_data(
            config["tickers"], config, config["est"]
        )
        
        if not results:
            return

        final_msg = create_combined_message(results, is_open, kst_now, vix_info)
        send_discord_message(f"```\n{final_msg}```", config["webhook"], config["user_id"])
        logger.info("✅ 알림 전송 완료")
    except Exception as e:
        logger.error(f"⚠️ 실행 오류: {e}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    main()
