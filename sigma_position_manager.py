import logging
import json
from datetime import datetime, timedelta

import numpy as np
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
        logger.info("✅ config.json에서 설정을 성공적으로 불러왔습니다.")
        return config
    except FileNotFoundError:
        logger.error("❌ config.json 파일을 찾을 수 없습니다.")
        raise
    except json.JSONDecodeError:
        logger.error("❌ config.json 파일 형식이 잘못되었습니다.")
        raise


def setup_environment():
    config_dict = load_config()
    return {
        "webhook": config_dict.get("DISCORD_WEBHOOK"),
        "user_id": config_dict.get("DISCORD_USER_ID"),
        "tickers": config_dict.get("TICKERS", ["TSLA"]),
        "positions": config_dict.get("POSITIONS", {}),
        "kst": pytz.timezone('Asia/Seoul'),
        "est": pytz.timezone('US/Eastern'),
    }


# ====================== 📈 장기(LONG)용 연간 시그마 계산 ======================
def calculate_annual_sigma(closes, window=90):
    closes = np.array(closes).flatten().astype(float)
    closes = closes[~np.isnan(closes)]
    if len(closes) < window + 1: 
        window = len(closes) - 1
    
    window_closes = closes[-(window + 1):]
    log_returns = np.diff(np.log(window_closes))
    log_returns = log_returns[np.isfinite(log_returns)]
    
    if len(log_returns) < 5: 
        return 0.70
    return float(np.std(log_returns, ddof=1) * np.sqrt(252))


# ====================== ⚡ 단기(SHORT)용 분할 매도 계산 ======================
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


# ====================== 📅 매월 마지막 영업일 판별기 ======================
def is_last_business_day_of_month(today_date):
    """
    오늘이 이번 달의 '마지막 영업일(주말 및 미국 공휴일 제외)'인지 판별합니다.
    """
    current_month = today_date.month
    test_date = today_date + timedelta(days=1)
    
    # 내일부터 이번 달 말일까지 검사해서 남아있는 영업일이 있는지 확인
    while test_date.month == current_month:
        # 주말이 아니고 공휴일도 아니라면, 아직 이번 달에 영업일이 더 남은 것임!
        if test_date.weekday() < 5:
            if holidays is not None:
                us_holidays = holidays.US(years=test_date.year)
                if test_date not in us_holidays:
                    return False
            else:
                return False
        test_date += timedelta(days=1)
        
    return True


# ====================== 시장 정보 및 일정 판별 유틸 ======================
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


# ====================== 하이브리드 대통합 데이터 분석 ======================
def get_combined_market_data(tickers: list, config: dict, est_tz: pytz.timezone):
    df = yf.download(tickers, period="150d", interval="1d", auto_adjust=True, progress=False)

    if df is None or df.empty:
        logger.error("❌ 야후 파이낸스 서버 응답 없음")
        return {}, False, "N/A"

    results = {}
    now_est = datetime.now(est_tz)

    m_open = now_est.replace(hour=9, minute=30, second=0, microsecond=0)
    m_close = now_est.replace(hour=16, minute=0, second=0, microsecond=0)
    is_regular_market = m_open <= now_est <= m_close and now_est.weekday() < 5

    is_witching = is_triple_witching_week(now_est.date()) and is_regular_market
    vix_val, vix_info = get_vix_report()

    positions_setting = config.get("positions", {})

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
            today_open = prev_close
            base = prev_close

        # 변동성 계산 (std_20d = 1.0σ)
        daily_returns = ticker_df["Close"].pct_change().dropna()
        std_20d = float(daily_returns.tail(20).std() * 100)
        if pd.isna(std_20d) or std_20d <= 0:
            std_20d = 2.0

        gap_ratio = (today_open - prev_close) / prev_close

        # 개별 종목 포지션 세팅값 획득 및 운용 모드(MODE) 분기 처리
        t_info = positions_setting.get(ticker, {})
        mode = t_info.get("MODE", "SHORT")
        shares = t_info.get("TOTAL_SHARES", 0)

        # 공통 매수 그물망 기본 산출
        if vix_val >= 35.0:
            base_sigma = std_20d * 2.0
            sub_msg = "🔴🔴 VIX 극단적 공포 (초심해 방어)"
            buy_target = base * (1 - base_sigma / 100)
            buy_name = "-2.0σ"
        elif is_witching:
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
        elif vix_val > 25.0:
            base_sigma = std_20d * 1.5
            sub_msg = "⚠️ VIX 공포지수 상승 (타점 심화)"
            buy_target = base * (1 - base_sigma / 100)
            buy_name = "-1.5σ"
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

        ticker_result = {
            "mode": mode,
            "prev_close": prev_close,
            "std": std_20d,
            "buy_target": buy_target,
            "buy_name": buy_name,
            "sub_msg": sub_msg,
            "total_shares": shares
        }

        # ════════════ 장기(LONG) 모드 전용 연산 ════════════
        if mode == "LONG":
            current_casts = t_info.get("CURRENT_CASTS", 0)
            annual_quota = t_info.get("ANNUAL_QUOTA", 20)
            my_avg_price = t_info.get("MY_AVG_PRICE", 0.0)
            
            annual_sig = calculate_annual_sigma(ticker_df["Close"].values, window=90)
            long_buy_target = prev_close * (1 - (annual_sig * 1.5) / 100)
            
            exhaustion_rate = (current_casts / annual_quota) * 100 if annual_quota > 0 else 0.0
            ticker_result.update({
                "current_casts": current_casts,
                "annual_quota": annual_quota,
                "my_avg_price": my_avg_price,
                "exhaustion_rate": exhaustion_rate,
                "annual_sigma": annual_sig * 100,
                "buy_target": long_buy_target,
                "buy_name": "장기 적립 방어선"
            })
            
        # ════════════ 단기(SHORT) 모드 전용 연산 ════════════
        elif mode == "SHORT":
            split_plan = calculate_split_sell_targets(base, std_20d, shares)
            ticker_result.update({
                "split_sell_plan": split_plan
            })

        results[ticker] = ticker_result
        
    return results, is_regular_market, vix_info


# ====================== 대통합 시각화 리포트 생성 ======================
def create_combined_message(results: dict, is_open: bool, kst_now: str, vix_info: str, is_last_day: bool):
    mode_str = "🚀 실시간 모드" if is_open else "⏳ 장전 대기 모드"
    msg = f"🔔 **통합 자산 관리 시스템 리포트 ({mode_str})**\n"
    msg += f"🎬 VIX : {vix_info}\n"
    
    for ticker, val in results.items():
        msg += f"\n━━━━━━━━━━━━━━━━━━━━━━\n"
        
        opt_mode = "📈 장기적립" if val["mode"] == "LONG" else "⚡ 단기타격"
        msg += f"📍 **종목 : {ticker}** [{opt_mode}] (보유: {val['total_shares']}주)\n"
        msg += f"📍 상태 : {val['sub_msg']}\n"
        msg += f"💰 전일 종가 : ${val['prev_close']:.2f}\n"
        
        if val["mode"] == "LONG":
            msg += f"📊 90일 연간 변동성(σ) : ±{val['annual_sigma']:.2f}%\n"
            msg += f"🛒 **매수 예정가({val['buy_name']}) : ${val['buy_target']:.2f}**\n"
            msg += f"📊 계좌 집행 현황 : {val['current_casts']}/{val['annual_quota']}회\n"
            msg += f"🔥 자금 소진율 : {val['exhaustion_rate']:.1f}%\n"
            if val['my_avg_price'] > 0:
                msg += f"🍏 평단가 : ${val['my_avg_price']:.2f}\n"

        elif val["mode"] == "SHORT":
            msg += f"📊 20일 변동성(1σ) : ±{val['std']:.2f}%\n"
            msg += f"🛒 **매수 예정가({val['buy_name']}) : ${val['buy_target']:.2f}**\n"
            if val.get("split_sell_plan"):
                msg += f"📌 **3단계 분할 매도 계획**\n"
                for plan in val["split_sell_plan"]:
                    msg += f"   • {plan['level']:16} → ${plan['price']:.2f}  ({plan['qty']}주)\n"
            else:
                msg += f"📌 **분할 매도 계획** : 보유 주수가 없습니다.\n"
    
    msg += f"\n━━━━━━━━━━━━━━━━━━━━━━\n"
    
    # 💡 월말 마지막 영업일 생존 핑 안내
    if is_last_day:
        msg += f"📡 **[🤖 디스코드 계정 만료 방지 생존 핑 발송 완료]**\n"
        msg += f"📢 본 메시지는 휴면 계정 전환을 막기 위한 월간 정기 핑입니다.\n"
        msg += f"━━━━━━━━━━━━━━━━━━━━━━\n"
        
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


# ====================== 메인 로직 ======================
def main():
    config = setup_environment()
    kst_now = datetime.now(config["kst"]).strftime('%Y-%m-%d %H:%M:%S')
    
    now_est = datetime.now(config["est"])
    today_date = now_est.date()

    if now_est.weekday() >= 5:
        logger.info("📅 주말 휴장으로 브리핑을 건너뜁니다.")
        return

    if holidays is not None:
        us_holidays = holidays.US(years=today_date.year)
        if today_date in us_holidays:
            logger.info(f"📅 미국 공휴일 휴장입니다.")
            return

    # 💡 오늘이 이번 달의 마지막 영업일인지 판별
    is_last_day = is_last_business_day_of_month(today_date)

    try:
        results, is_open, vix_info = get_combined_market_data(
            config["tickers"], config, config["est"]
        )
        
        if not results:
            return

        # 마지막 영업일 여부(is_last_day)를 함께 전달하여 메시지 생성
        final_msg = create_combined_message(results, is_open, kst_now, vix_info, is_last_day)
        send_discord_message(f"```\n{final_msg}```", config["webhook"], config["user_id"])
        logger.info("✅ 대통합 자산 관리 알림 전송 완료")
    except Exception as e:
        logger.error(f"⚠️ 실행 오류 발생: {e}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    main()