import tempfile  
import logging
import json
import os
import subprocess
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import requests
import yfinance as yf
import pytz

logger = logging.getLogger(__name__)

# yfinance 임시 디렉토리 캐시 설정 (DB 잠김 및 TypeError 완벽 방지)
temp_cache_dir = tempfile.mkdtemp()
yf.set_tz_cache_location(temp_cache_dir)


# ====================== 설정 ======================
def load_config() -> dict:
    try:
        with open('config.json', 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        logger.error("❌ config.json 파일을 찾을 수 없습니다.")
        raise
    except json.JSONDecodeError:
        logger.error("❌ config.json 형식이 잘못되었습니다.")
        raise


def save_config(cfg: dict):
    with open('config.json', 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def setup_environment() -> dict:
    cfg = load_config()

    default_vix = {
        "LEVEL_LOW": 20.0,
        "LEVEL_HIGH": 30.0,
        "MULT_NORMAL": 0.85,
        "MULT_FEAR": 1.95,
        "MULT_EXTREME": 2.40,
    }

    default_values = cfg.get("DEFAULTS", {
        "SIGMA_DEFAULT": 2.0,
        "LAST_CAST_DATE": "2026-05-07",
        "ANNUAL_QUOTA": 24,
    })

    return {
        "webhook": cfg.get("DISCORD_WEBHOOK") or os.getenv("DISCORD_WEBHOOK"),
        "user_id": cfg.get("DISCORD_USER_ID") or os.getenv("DISCORD_USER_ID"),
        "tickers": cfg.get("TICKERS", ["SOXL"]),
        "positions": cfg.get("POSITIONS", {}),
        "vix_cfg": {**default_vix, **cfg.get("VIX_CONFIG", {})},
        "defaults": default_values,
        "kst": pytz.timezone('Asia/Seoul'),
        "est": pytz.timezone('US/Eastern'),
        "full_cfg": cfg
    }


# ====================== 자동 동기화 및 유틸 ======================
def sync_ledger_to_config():
    """ledger.json → config.json 포지션 정보 자동 동기화."""
    if not os.path.exists("ledger.json") or not os.path.exists("config.json"):
        logger.info("ℹ️ ledger.json 또는 config.json 파일이 없어 자동 동기화를 건너뜁니다.")
        return

    try:
        with open("ledger.json", "r", encoding="utf-8") as f:
            ledger = json.load(f)
        with open("config.json", "r", encoding="utf-8") as f:
            config = json.load(f)
    except Exception as e:
        logger.error(f"❌ 파일 로드 실패 (동기화 중단): {e}")
        return

    if "POSITIONS" not in config:
        config["POSITIONS"] = {}
    if "DEFAULTS" not in config:
        config["DEFAULTS"] = {}

    if isinstance(ledger, list):
        logger.info("ℹ️ ledger.json이 빈 리스트 또는 리스트 형식입니다. 동기화를 건너뜁니다.")
        return

    for ticker, info in ledger.items():
        action = info.get("action")
        mode   = info.get("mode")

        if action == "BUY":
            buy_target    = info.get("buy_target", 0.0)
            new_qty       = info.get("qty", 0)
            current_casts = info.get("current_casts", 1)
            tx_date       = info.get("date", "")

            if ticker in config["POSITIONS"]:
                old_pos          = config["POSITIONS"][ticker]
                
                if "TOTAL_SHARES_LONG" in old_pos or "TOTAL_SHARES_SHORT" in old_pos:
                    if mode == "LONG":
                        old_total = old_pos.get("TOTAL_SHARES_LONG", 0)
                        old_avg = old_pos.get("MY_AVG_PRICE_LONG", 0.0)
                    else:
                        old_total = old_pos.get("TOTAL_SHARES_SHORT", 0)
                        old_avg = old_pos.get("MY_AVG_PRICE_SHORT", 0.0)
                else:
                    old_total = old_pos.get("TOTAL_SHARES", 0)
                    old_avg = old_pos.get("MY_AVG_PRICE", 0.0)
                
                total_cost       = (old_total * old_avg) + (new_qty * buy_target)
                final_shares     = old_total + new_qty
                calculated_avg   = round(total_cost / final_shares, 4) if final_shares > 0 else buy_target
            else:
                final_shares   = new_qty
                calculated_avg = buy_target

            if ticker not in config["POSITIONS"]:
                config["POSITIONS"][ticker] = {}
            
            if mode == "LONG":
                config["POSITIONS"][ticker]["TOTAL_SHARES_LONG"] = final_shares
                config["POSITIONS"][ticker]["MY_AVG_PRICE_LONG"] = calculated_avg
            else:
                config["POSITIONS"][ticker]["TOTAL_SHARES_SHORT"] = final_shares
                config["POSITIONS"][ticker]["MY_AVG_PRICE_SHORT"] = calculated_avg
                config["POSITIONS"][ticker]["CURRENT_CASTS_SHORT"] = current_casts
                config["POSITIONS"][ticker]["ANNUAL_QUOTA_SHORT"] = config["DEFAULTS"].get("ANNUAL_QUOTA", 14)
            
            config["POSITIONS"][ticker]["LAST_CAST_DATE"] = tx_date
            if tx_date:
                config["DEFAULTS"]["LAST_CAST_DATE"] = tx_date

        elif action == "SELL":
            if ticker in config["POSITIONS"]:
                logger.info(f"📉 장부 확인 - {ticker} 전량 매도 완료: 포지션을 청산합니다.")
                del config["POSITIONS"][ticker]
            if info.get("date"):
                config["DEFAULTS"]["LAST_CAST_DATE"] = info.get("date")

    try:
        with open("config.json", "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        logger.info("✅ 장부(ledger) 기반 config.json 누적 연산 및 자동 동기화 성공!")
    except Exception as e:
        logger.error(f"❌ config.json 자동 저장 실패: {e}")


def get_vix_report() -> tuple[float, str]:
    try:
        df = yf.download("^VIX", period="2d", progress=False)
        if not df.empty:
            if isinstance(df.columns, pd.MultiIndex):
                df = df.droplevel(1, axis=1)
            v = float(df["Close"].iloc[-1])
            if v <= 15:
                emoji, status = "✨", "안정"
            elif v <= 20:
                emoji, status = "⚠️", "주의"
            elif 20 < v <= 30:
                emoji, status = "🔴", "공포"
            else:
                emoji, status = "🔴🔴", "극단적 공포"
            return v, f"{emoji} VIX {status} ({v:.1f})"
    except Exception as e:
        logger.warning(f"VIX 데이터 수집 실패: {e}")
    return 0.0, "N/A"


def is_us_holiday(d: datetime.date) -> bool:
    try:
        import holidays
        return d in holidays.US(years=d.year)
    except ImportError:
        return False


def is_last_business_day_of_month(today: datetime.date) -> bool:
    check = today + timedelta(days=1)
    while check.month == today.month:
        if check.weekday() < 5 and not is_us_holiday(check):
            return False
        check += timedelta(days=1)
    return True


def is_triple_witching_week(d: datetime.date) -> bool:
    if d.month not in [3, 6, 9, 12]:
        return False
    return 13 <= d.day <= 21 and 2 <= d.weekday() <= 4


# ====================== 핵심 분석 함수 ======================
def analyze_ticker(ticker: str, ticker_df: pd.DataFrame, pos_cfg: dict,
                   vix_val: float, is_open: bool, today_date: datetime.date,
                   vix_long_cfg: dict, vix_short_cfg: dict, defaults: dict = None) -> dict:

    defaults = defaults or {}

    if isinstance(today_date, datetime):
        today_date = today_date.date()

    last_row_date = ticker_df.index[-1]
    if hasattr(last_row_date, "date"):
        last_row_date = last_row_date.date()

    is_today_data_present = (last_row_date == today_date)

    try:
        if is_today_data_present and is_open and len(ticker_df) > 1:
            prev_close = float(ticker_df["Close"].iloc[-2])
        else:
            prev_close = float(ticker_df["Close"].iloc[-1])
    except Exception:
        prev_close = float(ticker_df["Close"].iloc[-1])

    try:
        today_open = float(ticker_df["Open"].iloc[-1]) if (is_today_data_present and is_open) else prev_close
    except Exception:
        today_open = prev_close

    base      = today_open if (is_open and is_today_data_present) else prev_close
    gap_ratio = (today_open - prev_close) / prev_close if prev_close != 0 else 0.0

    hist_df   = ticker_df.iloc[:-1] if is_today_data_present else ticker_df
    daily_ret = hist_df["Close"].pct_change().dropna()
    std_20d   = float(daily_ret.tail(20).std() * 100)
    if pd.isna(std_20d) or std_20d <= 0:
        std_20d = float(defaults.get("SIGMA_DEFAULT", 2.0))

    current_price = float(ticker_df["Close"].iloc[-1])

    # 데이터 취합용 딕셔너리 기초 생성
    result = {
        "prev_close": prev_close,
        "today_open": today_open,
        "current_price": current_price,
        "std": std_20d
    }

    # 🛠️ SHORT 모드 계산 로직 
    vs_extreme = vix_short_cfg.get("MULT_EXTREME", 2.4)
    vs_fear    = vix_short_cfg.get("MULT_FEAR", 1.95)
    vs_normal  = vix_short_cfg.get("MULT_NORMAL", 0.85)
    vs_high    = vix_short_cfg.get("LEVEL_HIGH", 30.0)
    vs_low     = vix_short_cfg.get("LEVEL_LOW", 20.0)
    shares_short = pos_cfg.get("TOTAL_SHARES_SHORT", pos_cfg.get("TOTAL_SHARES", 0)) 

    if vix_val >= vs_high:
        short_target = base * (1 - std_20d * vs_extreme / 100)
        short_buy_name, short_sub_msg = f"-{vs_extreme}σ", "🔴🔴 VIX 극단적 공포"
    elif is_triple_witching_week(today_date):
        if is_open and gap_ratio < -0.001:
            rem = max(0, std_20d * vs_fear + gap_ratio * 100)
            short_target = today_open * (1 - rem / 100)
            short_buy_name   = f"-{rem/std_20d:.1f}σ"
            short_sub_msg    = "🧙 세 마녀 주간 갭 하락 보정"
        else:
            short_target = prev_close * (1 - std_20d * vs_fear / 100)
            short_buy_name, short_sub_msg = f"-{vs_fear}σ", "🧙 세 마녀 주간"
    elif vix_val >= vs_low:
        short_target = base * (1 - std_20d * vs_fear / 100)
        short_buy_name, short_sub_msg = f"-{vs_fear}σ", "🔴 VIX 공포"
    elif is_open and gap_ratio < -0.001:
        gap_adjust = abs(gap_ratio) * 100
        rem        = max(0, std_20d + gap_adjust)
        short_target = today_open * (1 - rem / 100)
        short_buy_name   = f"-{rem/std_20d:.1f}σ"
        short_sub_msg    = "📉 갭 하락 보정"
    else:
        short_target = prev_close * (1 - std_20d * vs_normal / 100)
        short_buy_name, short_sub_msg = f"-{vs_normal}σ", "✨ VIX 안정"

    result.update({
        "short_shares": shares_short,
        "short_target": short_target,
        "short_buy_name": short_buy_name,
        "short_sub_msg": short_sub_msg,
        "short_current_casts": pos_cfg.get("CURRENT_CASTS_SHORT", 0),
        "short_annual_quota": pos_cfg.get("ANNUAL_QUOTA_SHORT", 14),
        "split_sell_plan": calculate_split_sell_targets(base, std_20d, shares_short)
    })

    # 🛠️ LONG 모드 계산 로직 
    vl_extreme = vix_long_cfg.get("MULT_EXTREME", 2.4)
    vl_fear    = vix_long_cfg.get("MULT_FEAR", 1.95)
    vl_normal  = vix_long_cfg.get("MULT_NORMAL", 0.85)
    vl_high    = vix_long_cfg.get("LEVEL_HIGH", 30.0)
    vl_low     = vix_long_cfg.get("LEVEL_LOW", 20.0)
    shares_long = pos_cfg.get("TOTAL_SHARES_LONG", pos_cfg.get("TOTAL_SHARES", 34))

    log_ret         = np.log(hist_df["Close"] / hist_df["Close"].shift(1)).dropna().tail(90)
    daily_sigma_val = float(log_ret.std(ddof=1)) if len(log_ret) > 0 else 0.04

    if vix_val >= vl_high:
        long_multiplier = vl_extreme
        long_vix_msg = "🔴🔴 VIX 극단적 공포"
    elif vix_val >= vl_low:
        long_multiplier = vl_fear
        long_vix_msg = "🔴 VIX 공포"
    else:
        long_multiplier = vl_normal
        long_vix_msg = "✨ VIX 안정"

    if is_open and gap_ratio < -0.03:
        gap_pct = abs(gap_ratio) * 100
        if gap_pct <= 5.0:
            adjusted_multiplier = 0.45
            gap_zone = "-3%~-5% 구간"
        elif gap_pct <= 7.0:
            adjusted_multiplier = 0.25
            gap_zone = "-5%~-7% 구간"
        elif gap_pct <= 10.0:
            adjusted_multiplier = 0.10
            gap_zone = "-7%~-10% 구간"
        else:
            adjusted_multiplier = 0.0
            gap_zone = "-10% 초과 구간"
        long_sub_msg = f"{long_vix_msg} ➔ {adjusted_multiplier:.2f}배수 하방 (갭 {gap_ratio*100:.1f}% / {gap_zone} → 배수 {long_multiplier:.2f}→{adjusted_multiplier:.2f})"
    else:
        adjusted_multiplier = long_multiplier
        long_sub_msg = f"{long_vix_msg} ➔ {adjusted_multiplier:.2f}배수 하방"

    long_target = (today_open if is_open else prev_close) * float(np.exp(-daily_sigma_val * adjusted_multiplier))
    last_cast_str = pos_cfg.get("LAST_CAST_DATE", defaults.get("LAST_CAST_DATE", "2026-01-01"))

    try:
        last_cast_date = datetime.strptime(last_cast_str, "%Y-%m-%d").date()
    except Exception:
        last_cast_date = datetime(2026, 1, 1).date()

    days_since    = (today_date - last_cast_date).days
    normal_std    = 4.0 if "SOXL" in ticker.upper() else 2.5
    min_days_gate = 14 if std_20d > normal_std * 1.3 else 5
    is_time_gate_passed = days_since >= min_days_gate

    if is_open and current_price <= long_target and not is_time_gate_passed:
        time_guard_status = "🔥 [시간 가드 강제 해제] 초저점 도달로 실탄 집행!"
        action_ment       = f"계산된 초저점 타깃가(${long_target:.2f})를 터치하여 기계적으로 매수를 집행합니다."
    elif is_time_gate_passed:
        time_guard_status = "🟢 [시간 가드 해제] 자유 매수 가능 주간"
        action_ment       = f"대기 기간을 충족하여 실탄 장전이 완료되었습니다. 오늘 본장 매수 저격가는 ${long_target:.2f}입니다."
    else:
        time_guard_status = f"⏳ [시간 가드 작동 중] {min_days_gate - days_since}일 대기 필요"
        action_ment       = f"조급한 실탄 고갈을 막기 위해 관망합니다. 가드 해제 후 유효 매수 예정가는 ${long_target:.2f}입니다."

    annual_quota_long = pos_cfg.get("ANNUAL_QUOTA_LONG", pos_cfg.get("ANNUAL_QUOTA", defaults.get("ANNUAL_QUOTA", 24)))
    current_casts_long = pos_cfg.get("CURRENT_CASTS_LONG", pos_cfg.get("CURRENT_CASTS", 0))
    exhaustion_rate = min(current_casts_long / max(annual_quota_long, 1) * 100, 100.0)

    result.update({
        "long_shares":    shares_long,
        "daily_sigma":    daily_sigma_val * 100,
        "multiplier":     long_multiplier,
        "long_target":    long_target,
        "long_buy_name":  f"LONG 변동성 방어선 ({adjusted_multiplier:.2f}x)",
        "long_sub_msg":   long_sub_msg,
        "time_guard_info":   time_guard_status,
        "time_guard_action": action_ment,
        "my_avg_price":   pos_cfg.get("MY_AVG_PRICE_LONG", pos_cfg.get("MY_AVG_PRICE", 0.0)),
        "long_current_casts": current_casts_long,
        "long_annual_quota":  annual_quota_long,
        "exhaustion_rate": exhaustion_rate,
    })

    return result


def calculate_split_sell_targets(base_price: float, std_20d: float, shares: int) -> list:
    if shares <= 0:
        return []
    levels    = [(0.85, "1단계 +0.85σ"), (1.95, "2단계 +1.95σ"), (2.40, "3단계 +2.40σ")]
    per_level = max(1, shares // len(levels))
    plan      = []
    remaining = shares
    for i, (mult, name) in enumerate(levels):
        qty = per_level if i < len(levels) - 1 else remaining
        if qty <= 0:
            break
        plan.append({"level": name, "price": round(base_price * (1 + std_20d * mult / 100), 2), "qty": qty})
        remaining -= qty
    return plan


# ====================== Git 동기화 ======================
def sync_config_to_git(target_date: datetime.date):
    today_str = target_date.strftime("%Y-%m-%d")
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain", "config.json"],
            capture_output=True, text=True, timeout=10
        )
        if not status.stdout.strip():
            logger.info("📝 config.json 변경사항이 없어 Git Commit을 생략합니다.")
            return

        if os.getenv("GITHUB_ACTIONS") == "true":
            logger.info("ℹ️ GitHub Actions 환경 감지: 파이썬 내부 푸시를 생략하고 워크플로우에 위임합니다.")
            return

        subprocess.run(["git", "config", "user.name",  "Automated Bot"],  check=True, timeout=10)
        subprocess.run(["git", "config", "user.email", "bot@example.com"], check=True, timeout=10)
        subprocess.run(["git", "add", "config.json"], check=True, timeout=10)
        subprocess.run(["git", "commit", "-m", f"Auto-update config.json [{today_str}]"], check=True, timeout=10)
        subprocess.run(["git", "push", "origin", "main"], check=True, timeout=15)
        logger.info("🚀 로컬 파워셸 기준 Git Push 성공.")
    except Exception as e:
        logger.error(f"❌ Git 처리 중 오류: {e}")


# ====================== 데이터 수집 ======================
def get_combined_market_data(tickers: list, config: dict, est_tz, target_date: datetime.date):
    df = yf.download(tickers, period="150d", interval="1d", progress=False, auto_adjust=True)
    if df.empty:
        logger.error("❌ yfinance 데이터 다운로드 실패")
        return {}, False, "N/A"

    now_est      = datetime.now(est_tz)
    market_open  = now_est.replace(hour=9,  minute=30, second=0, microsecond=0)
    market_close = now_est.replace(hour=16, minute=0,  second=0, microsecond=0)

    is_open = (
        market_open <= now_est <= market_close
        and target_date.weekday() < 5
        and not is_us_holiday(target_date)
        and target_date == now_est.date()
    )

    vix_val, vix_info  = get_vix_report()
    
    # 통합 딕셔너리 구성
    positions_cfg      = config.get("positions", {})
    
    # default_vix가 함수 내부에 선언되어 있지 않다면 아래처럼 안전하게 로컬 딕셔너리를 지정하거나,
    # setup_environment에서 넘겨받은 전체 config 구조를 활용하도록 세팅합니다.
    default_vix = {
        "LEVEL_LOW": 20.0, "LEVEL_HIGH": 30.0,
        "MULT_NORMAL": 0.85, "MULT_FEAR": 1.95, "MULT_EXTREME": 2.40
    }
    vix_cfg            = {**default_vix, **config.get("full_cfg", {}).get("VIX_CONFIG", {})}
    defaults           = config.get("defaults",  {})

    results = {}
    for ticker in tickers:
        try:
            t_df = (df.xs(ticker, level=1, axis=1)[["Close", "Open"]].dropna()
                    if isinstance(df.columns, pd.MultiIndex)
                    else df[["Close", "Open"]].dropna())

            if len(t_df) < 2:
                continue

            pos_cfg = positions_cfg.get(ticker, {})

            # 🌟 analyze_ticker를 호출할 때 롱/숏 개별 vix_cfg 대신 통합 vix_cfg를 통째로 넘겨줍니다.
            results[ticker] = analyze_ticker(
                ticker=ticker, ticker_df=t_df, pos_cfg=pos_cfg,
                vix_val=vix_val, is_open=is_open, today_date=target_date,
                vix_long_cfg=vix_cfg, vix_short_cfg=vix_cfg, defaults=defaults
            )
        except Exception as e:
            logger.warning(f"⚠️ {ticker} 분석 실패: {e}")

    return results, is_open, vix_info


# ====================== 리포트 생성 (롱/숏 통합 출력) ======================
def create_combined_message(results: dict, is_open: bool,
                            kst_now: str, vix_info: str, is_last_day: bool) -> str:
    mode_str = "🚀 실시간 모드" if is_open else "⏳ 장전 대기 모드"
    lines    = [
        f"=== 🎯 매매엔진 통합 리포트 ({mode_str}) ===",
        f"🎬 {vix_info}",
    ]

    for ticker, v in results.items():
        if v is None:
            continue
        
        current_price = v.get('current_price', 0.0)
        long_target   = v.get('long_target', 0.0)
        short_target  = v.get('short_target', 0.0)

        lines += [
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            f"● 종목 : {ticker}",
            f"● 현재가 : ${current_price:.2f}  (전일 종가 : ${v.get('prev_close', 0.0):.2f})",
            f"📊 20일 변동성(1σ) : ±{v.get('std', 0.0):.2f}%",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            "📈 [본인 계좌 - LONG 모드]",
            f"  • 보유량 : {v.get('long_shares', 0)}주 (평단가: ${v.get('my_avg_price', 0.0):.2f})" if v.get('my_avg_price', 0) > 0 else f"  • 보유량 : {v.get('long_shares', 0)}주",
            f"  • VIX 상태 : {v.get('long_sub_msg', '')}",
            f"  • 일간 평균 변동성 : ±{v.get('daily_sigma', 0.0):.2f}% / 배수 : {v.get('multiplier', 0.0):.2f}x",
            f"  • ⚙️ 타임 엔진 : {v.get('time_guard_info', '')}",
        ]
        
        if is_open and current_price <= long_target:
            lines.append(f"  • 🛒 매수 예정가 : ${long_target:.2f} (🚨 [LONG 매수 시그널 포착] 실탄 집행!)")
        else:
            lines.append(f"  • 🛒 매수 예정가 : ${long_target:.2f}")
            
        lines += [
            "-----------------------------------------",
            "⚡ [처형 계좌 - SHORT 모드]",
            f"  • 보유량 : {v.get('short_shares', 0)}주 (집행 현황: {v.get('short_current_casts', 0)}/{v.get('short_annual_quota', 14)}회)",
            f"  • VIX 상태 : {v.get('short_sub_msg', '')} ({v.get('short_buy_name', '')})",
        ]
        
        if is_open and current_price <= short_target:
            lines.append(f"  • 🛒 매수 예정가 : ${short_target:.2f} (🚨 [SHORT 매수 시그널 포착] 2배 가속!)")
        else:
            lines.append(f"  • 🛒 매수 예정가 : ${short_target:.2f}")

        plan = v.get("split_sell_plan", [])
        if plan:
            lines.append("  📌 **3단계 분할 매도 계획 (SHORT)**")
            for p in plan:
                lines.append(f"     • {p['level']:16} → ${p['price']:.2f}  ({p['qty']}주)")

    lines.append("-----------------------------------------")
    if is_last_day:
        lines += [
            "📡 **[🤖 디스코드 계정 만료 방지 생존 핑 발송 완료]**",
            "-----------------------------------------"
        ]
    lines.append(f"⏰ 통합 분석 관제탑 시각: {kst_now}")
    return "\n".join(lines)


# ====================== Discord 전송 ======================
def send_discord_message(content: str, webhook_url: str, user_id: str) -> bool:
    if not webhook_url:
        return False
    mention = f"<@{user_id}>\n\n" if user_id else ""
    try:
        payload = {"content": f"{mention}{content}"}
        r = requests.post(webhook_url, json=payload, timeout=15)
        return r.status_code in (200, 204)
    except Exception as e:
        logger.error(f"❌ 디스코드 웹훅 전송 중 장애 발생: {e}")
        return False


# ====================== 메인 ======================
def main():
    try:
        sync_ledger_to_config()
        config = setup_environment()
        logger.info("✅ 최신 설정 로드 및 연동 완료")

        kst_now = datetime.now(config["kst"])
        now_est = datetime.now(config["est"])

        target_date = now_est.date()
        if now_est.weekday() == 6 and now_est.hour >= 18:
            target_date += timedelta(days=1)

        if target_date.weekday() >= 5 or is_us_holiday(target_date):
            logger.info(f"📅 {target_date}은(는) 휴장일입니다. 브리핑을 스킵합니다.")
            return

        is_last = is_last_business_day_of_month(target_date)

        results, is_open, vix_info = get_combined_market_data(
            config["tickers"], config, config["est"], target_date
        )
        if not results:
            logger.error("❌ 분석 결과가 없습니다.")
            return

        sync_config_to_git(target_date)

        kst_str = kst_now.strftime('%Y-%m-%d %H:%M:%S')
        msg     = create_combined_message(results, is_open, kst_str, vix_info, is_last)

        def _pick(*vals):
            for v in vals:
                if v and str(v).strip():
                    return str(v).strip()
            return None

        discord_webhook_url = _pick(
            os.environ.get("DISCORD_WEBHOOK"),
            config.get("webhook"),
            config.get("DISCORD_WEBHOOK"),
            config.get("webhook_url"),
        )
        discord_user_id = _pick(
            os.environ.get("DISCORD_USER_ID"),
            config.get("user_id"),
            config.get("DISCORD_USER_ID"),
            config.get("USER_ID"),
        )

        if not discord_webhook_url:
            logger.warning("⚠️ DISCORD_WEBHOOK 값이 없습니다. config.json 또는 환경변수를 확인하세요.")

        send_success = send_discord_message(
            content=msg,
            webhook_url=discord_webhook_url,
            user_id=discord_user_id
        )

        if send_success:
            logger.info("✅ Discord 알림 전송 완료")
        else:
            logger.warning("⚠️ Discord 전송 실패 (웹훅 URL 및 환경 변수를 점검하세요)")

    except Exception as e:
        logger.error(f"💥 메인 실행 중 오류 발생: {e}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    main()