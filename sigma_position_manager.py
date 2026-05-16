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
def load_config() -> dict:
    load_dotenv()
    try:
        with open('config.json', 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        logger.error("❌ config.json 파일을 찾을 수 없습니다.")
        raise
    except json.JSONDecodeError:
        logger.error("❌ config.json 형식이 잘못되었습니다.")
        raise


def setup_environment() -> dict:
    cfg = load_config()
    return {
        "webhook":   cfg.get("DISCORD_WEBHOOK"),
        "user_id":   cfg.get("DISCORD_USER_ID"),
        "tickers":   cfg.get("TICKERS", ["TSLA"]),
        "positions": cfg.get("POSITIONS", {}),
        "kst":       pytz.timezone('Asia/Seoul'),
        "est":       pytz.timezone('US/Eastern'),
    }


# ====================== 유틸 함수 ======================
def calculate_annual_sigma(closes, window: int = 90) -> float:
    """90일 로그수익률 기반 연환산 시그마 (표본표준편차, ddof=1)"""
    arr = np.array(closes).flatten().astype(float)
    arr = arr[~np.isnan(arr)]
    window = min(window, len(arr) - 1)
    if window < 5:
        return 0.70
    log_ret = np.diff(np.log(arr[-(window + 1):]))
    log_ret = log_ret[np.isfinite(log_ret)]
    if len(log_ret) < 5:
        return 0.70
    daily_sigma = float(np.std(log_ret, ddof=1))
    annual_sigma = daily_sigma * np.sqrt(252)
    # ✅ σ > 1.0(100%) 방지 캡
    return min(annual_sigma, 0.80)


def calculate_split_sell_targets(base_price: float, std_20d: float, shares: int) -> list:
    """단기(SHORT)용 3단계 분할 매도 계획"""
    if shares <= 0:
        return []
    levels = [(0.9, "1단계 +0.9σ"), (1.3, "2단계 +1.3σ"), (1.8, "3단계 +1.8σ")]
    per_level = max(1, shares // len(levels))
    plan, remaining = [], shares
    for i, (mult, name) in enumerate(levels):
        qty = per_level if i < len(levels) - 1 else remaining
        if qty <= 0:
            break
        plan.append({
            "level": name,
            "price": round(base_price * (1 + std_20d * mult / 100), 2),
            "qty":   qty,
        })
        remaining -= qty
    return plan


def get_vix_report() -> tuple[float, str]:
    try:
        df = yf.download("^VIX", period="2d", auto_adjust=True, progress=False)
        if not df.empty:
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.droplevel(1)
            v = float(df["Close"].iloc[-1])
            status = "안정" if v <= 15 else "주의" if v <= 25 else "공포" if v <= 35 else "극단적 공포"
            return v, f"{v:.1f} ({status})"
    except Exception:
        pass
    return 0.0, "N/A"


def is_us_holiday(d) -> bool:
    if holidays is None:
        return False
    return d in holidays.US(years=d.year)


def is_last_business_day_of_month(today) -> bool:
    """오늘이 이번 달 마지막 영업일인지 판별"""
    check = today + timedelta(days=1)
    while check.month == today.month:
        if check.weekday() < 5 and not is_us_holiday(check):
            return False
        check += timedelta(days=1)
    return True


def is_triple_witching_week(d) -> bool:
    return (13 <= d.day <= 21) and (2 <= d.weekday() <= 4)


# ====================== ticker 단위 분석 ======================
def analyze_ticker(ticker: str, ticker_df: pd.DataFrame, pos_cfg: dict,
                   vix_val: float, is_open: bool) -> dict:
    """ticker 1개의 매수/매도 전략 계산"""
    prev_close = float(ticker_df["Close"].iloc[-2 if is_open else -1])
    today_open = float(ticker_df["Open"].iloc[-1]) if is_open else prev_close
    base = today_open if is_open else prev_close
    gap_ratio = (today_open - prev_close) / prev_close

    daily_ret = ticker_df["Close"].pct_change().dropna()
    std_20d = float(daily_ret.tail(20).std() * 100)
    if pd.isna(std_20d) or std_20d <= 0:
        std_20d = 2.0

    mode   = pos_cfg.get("MODE", "SHORT")
    shares = pos_cfg.get("TOTAL_SHARES", 0)

    # ── 공통 매수 타점 (SHORT·LONG 둘 다 기본값으로 사용) ──
    if vix_val >= 35.0:
        buy_target = base * (1 - std_20d * 2.0 / 100)
        buy_name, sub_msg = "-2.0σ", "🔴🔴 VIX 극단적 공포 (초심해 방어)"
    elif is_triple_witching_week(datetime.now().date()):
        if is_open and gap_ratio < 0:
            rem = max(0, std_20d * 1.5 + gap_ratio * 100)
            buy_target = today_open * (1 - rem / 100)
            buy_name = f"-{rem/std_20d:.1f}σ"
            sub_msg = "🧙 세 마녀 주간 갭 하락 보정"
        else:
            buy_target = prev_close * (1 - std_20d * 1.5 / 100)
            buy_name, sub_msg = "-1.5σ", "🧙 세 마녀 주간 하단 그물 대기"
    elif vix_val > 25.0:
        buy_target = base * (1 - std_20d * 1.5 / 100)
        buy_name, sub_msg = "-1.5σ", "⚠️ VIX 공포지수 상승 (타점 심화)"
    elif is_open and gap_ratio < 0:
        rem = max(0, std_20d + gap_ratio * 100)
        buy_target = today_open * (1 - rem / 100)
        buy_name = f"-{rem/std_20d:.1f}σ"
        sub_msg = "📉 갭 하락 보정 반영"
    else:
        buy_target = prev_close * (1 - std_20d / 100)
        buy_name, sub_msg = "-1.0σ", "📈 기존 시그마 유지"

    result = {
        "mode": mode, "prev_close": prev_close, "std": std_20d,
        "buy_target": buy_target, "buy_name": buy_name,
        "sub_msg": sub_msg, "total_shares": shares,
    }

    # ── LONG 전용 ──
    if mode == "LONG":
        annual_sig = calculate_annual_sigma(ticker_df["Close"].values)
        # ✅ 버그 수정: annual_sig는 소수(0~0.8), /100 불필요
        long_buy = prev_close * (1 - annual_sig * 1.5)
        long_buy = max(long_buy, prev_close * 0.10)   # 최소 현재가의 10% 방어

        result.update({
            "annual_sigma":    annual_sig * 100,       # 표시용 %
            "buy_target":      long_buy,
            "buy_name":        "장기 적립 방어선",
            "my_avg_price":    pos_cfg.get("MY_AVG_PRICE", 0.0),
            "current_casts":   pos_cfg.get("CURRENT_CASTS", 0),
            "annual_quota":    pos_cfg.get("ANNUAL_QUOTA", 20),
            "exhaustion_rate": pos_cfg.get("CURRENT_CASTS", 0) / max(pos_cfg.get("ANNUAL_QUOTA", 20), 1) * 100,
        })

    # ── SHORT 전용 ──
    elif mode == "SHORT":
        result["split_sell_plan"] = calculate_split_sell_targets(base, std_20d, shares)

    return result


# ====================== 데이터 수집 및 분석 ======================
def get_combined_market_data(tickers: list, config: dict, est_tz) -> tuple[dict, bool, str]:
    df = yf.download(tickers, period="150d", interval="1d",
                     auto_adjust=True, progress=False)
    if df is None or df.empty:
        logger.error("❌ 야후 파이낸스 서버 응답 없음")
        return {}, False, "N/A"

    now_est = datetime.now(est_tz)
    is_open = (
        now_est.replace(hour=9, minute=30, second=0, microsecond=0) <= now_est
        <= now_est.replace(hour=16, minute=0, second=0, microsecond=0)
        and now_est.weekday() < 5
    )
    vix_val, vix_info = get_vix_report()
    positions_cfg = config.get("positions", {})
    results = {}

    for ticker in tickers:
        try:
            if len(tickers) > 1:
                t_df = pd.DataFrame({
                    "Close": df["Close"][ticker],
                    "Open":  df["Open"][ticker],
                }).dropna()
            else:
                t_df = df[["Close", "Open"]].copy().dropna()

            if len(t_df) < 2:
                continue

            results[ticker] = analyze_ticker(
                ticker, t_df, positions_cfg.get(ticker, {}), vix_val, is_open
            )
        except Exception as e:
            logger.warning(f"⚠️ {ticker} 분석 실패: {e}")

    return results, is_open, vix_info


# ====================== 리포트 생성 ======================
def create_combined_message(results: dict, is_open: bool,
                            kst_now: str, vix_info: str, is_last_day: bool) -> str:
    mode_str = "🚀 실시간 모드" if is_open else "⏳ 장전 대기 모드"
    lines = [
        f"🔔 **통합 자산 관리 시스템 리포트 ({mode_str})**",
        f"🎬 VIX : {vix_info}",
    ]

    for ticker, v in results.items():
        opt_mode = "📈 장기적립" if v["mode"] == "LONG" else "⚡ 단기타격"
        lines += [
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"📍 **종목 : {ticker}** [{opt_mode}] (보유: {v['total_shares']}주)",
            f"📍 상태 : {v['sub_msg']}",
            f"💰 전일 종가 : ${v['prev_close']:.2f}",
        ]
        if v["mode"] == "LONG":
            lines += [
                f"📊 90일 연간 변동성(σ) : ±{v['annual_sigma']:.2f}%",
                f"🛒 **매수 예정가({v['buy_name']}) : ${v['buy_target']:.2f}**",
                f"📊 계좌 집행 현황 : {v['current_casts']}/{v['annual_quota']}회",
                f"🔥 자금 소진율 : {v['exhaustion_rate']:.1f}%",
            ]
            if v["my_avg_price"] > 0:
                lines.append(f"🍏 평단가 : ${v['my_avg_price']:.2f}")
        else:
            lines.append(f"📊 20일 변동성(1σ) : ±{v['std']:.2f}%")
            lines.append(f"🛒 **매수 예정가({v['buy_name']}) : ${v['buy_target']:.2f}**")
            plan = v.get("split_sell_plan", [])
            lines.append("📌 **3단계 분할 매도 계획**" if plan else "📌 분할 매도 계획 : 보유 주수 없음")
            for p in plan:
                lines.append(f"   • {p['level']:16} → ${p['price']:.2f}  ({p['qty']}주)")

    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    if is_last_day:
        lines += [
            "📡 **[🤖 디스코드 계정 만료 방지 생존 핑 발송 완료]**",
            "📢 본 메시지는 휴면 계정 전환을 막기 위한 월간 정기 핑입니다.",
            "━━━━━━━━━━━━━━━━━━━━━━",
        ]
    lines.append(f"⏰ 분석 시각: {kst_now}")
    return "\n".join(lines)


# ====================== Discord 전송 ======================
def send_discord_message(content: str, webhook_url: str, user_id: str) -> bool:
    if not webhook_url:
        return False
    mention = f"<@{user_id}>\n" if user_id else ""
    try:
        r = requests.post(webhook_url,
                          json={"content": f"{mention}```\n{content}```"},
                          timeout=15)
        return r.status_code in (200, 204)
    except Exception:
        return False


# ====================== 메인 ======================
def main():
    config  = setup_environment()
    now_est = datetime.now(config["est"])
    today   = now_est.date()

    # ✅ 중복 제거: 주말·공휴일 체크 한 곳에서만
    if now_est.weekday() >= 5 or is_us_holiday(today):
        logger.info("📅 휴장일 - 브리핑 건너뜀")
        return

    kst_now    = datetime.now(config["kst"]).strftime('%Y-%m-%d %H:%M:%S')
    is_last    = is_last_business_day_of_month(today)

    try:
        results, is_open, vix_info = get_combined_market_data(
            config["tickers"], config, config["est"]
        )
        if not results:
            return
        msg = create_combined_message(results, is_open, kst_now, vix_info, is_last)
        send_discord_message(msg, config["webhook"], config["user_id"])
        logger.info("✅ 알림 전송 완료")
    except Exception as e:
        logger.error(f"⚠️ 실행 오류: {e}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    main()