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

    # VIX 기본값
    default_vix = {
        "LEVEL_LOW": 20.0,
        "LEVEL_HIGH": 30.0,
        "MULT_NORMAL": 0.60,
        "MULT_FEAR": 1.95,
        "MULT_EXTREME": 2.45,
    }

    # 공통 기본값 설정 (config.json에서 관리)
    default_values = cfg.get("DEFAULTS", {
        "SIGMA_DEFAULT": 2.0,
        "LAST_CAST_DATE": "2026-05-07",
        "ANNUAL_QUOTA": 24,
        "MAX_DROP_PROTECTION": 0.10
    })

    return {
        "webhook":       cfg.get("DISCORD_WEBHOOK"),
        "user_id":       cfg.get("DISCORD_USER_ID"),
        "tickers":       cfg.get("TICKERS", ["SOXL", "TSLA"]),
        "positions":     cfg.get("POSITIONS", {}),
        "vix_long":      {**default_vix, **cfg.get("VIX_CONFIG", {}).get("LONG",  {})},
        "vix_short":     {**default_vix, **cfg.get("VIX_CONFIG", {}).get("SHORT", {})},
        "defaults":      default_values,           # ← 추가
        "kst":           pytz.timezone('Asia/Seoul'),
        "est":           pytz.timezone('US/Eastern'),
        "full_cfg":      cfg
    }
        

# ====================== 유틸 ======================
def get_vix_report() -> tuple[float, str]:
    try:
        df = yf.download("^VIX", period="2d", progress=False)
        if not df.empty:
            if isinstance(df.columns, pd.MultiIndex):
                df = df.droplevel(1, axis=1)
            v = float(df["Close"].iloc[-1])
            status = "안정" if v <= 15 else "주의" if v <= 20 else "공포" if v <= 30 else "극단적 공포"
            return v, f"{v:.1f} ({status})"
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
                   vix_cfg: dict, defaults: dict = None) -> dict:
    
    """ticker별 매수 타점 및 전략 분석 (SHORT / LONG 모드 통합)"""
    
    defaults = defaults or {}

    # 날짜 타입 보정
    if isinstance(today_date, datetime):
        today_date = today_date.date()

    # ==================== 가격 데이터 안전 추출 ====================
    try:
        prev_close = float(ticker_df["Close"].iloc[-2 if (is_open and len(ticker_df) > 1) else -1])
    except Exception:
        prev_close = float(ticker_df["Close"].iloc[-1])

    try:
        today_open = float(ticker_df["Open"].iloc[-1]) if is_open and len(ticker_df) > 0 else prev_close
    except Exception:
        today_open = prev_close

    base = today_open if is_open else prev_close
    gap_ratio = (today_open - prev_close) / prev_close if prev_close != 0 else 0.0

    # ==================== 20일 변동성 계산 ====================
    daily_ret = ticker_df["Close"].pct_change().dropna()
    std_20d = float(daily_ret.tail(20).std() * 100)
    if pd.isna(std_20d) or std_20d <= 0:
        std_20d = float(defaults.get("SIGMA_DEFAULT", 2.0))

    mode = pos_cfg.get("MODE", "SHORT")
    shares = pos_cfg.get("TOTAL_SHARES", 0)

    # ------------------ VIX 파라미터 추출 (config.json 우선) ------------------
    vix_cfg = vix_cfg or {}
    
    v_extreme = vix_cfg.get("MULT_EXTREME")
    v_fear    = vix_cfg.get("MULT_FEAR")
    v_normal  = vix_cfg.get("MULT_NORMAL")
    v_high    = vix_cfg.get("LEVEL_HIGH")
    v_low     = vix_cfg.get("LEVEL_LOW")

    # ==================== 결과 기본 구조 ====================
    result = {
        "mode": mode,
        "prev_close": prev_close,
        "today_open": today_open,
        "std": std_20d,
        "total_shares": shares,
    }

    # ====================== SHORT 모드 ======================
    if mode != "LONG":
        if vix_val >= v_high:
            buy_target = base * (1 - std_20d * v_extreme / 100)
            buy_name, sub_msg = f"-{v_extreme}σ", "🔴🔴 VIX 극단적 공포"
        elif is_triple_witching_week(today_date):
            if is_open and gap_ratio < -0.001:
                rem = max(0, std_20d * v_fear + gap_ratio * 100)
                buy_target = today_open * (1 - rem / 100)
                buy_name = f"-{rem/std_20d:.1f}σ"
                sub_msg = "🧙 세 마녀 주간 갭 하락 보정"
            else:
                buy_target = prev_close * (1 - std_20d * v_fear / 100)
                buy_name, sub_msg = f"-{v_fear}σ", "🧙 세 마녀 주간"
        elif vix_val >= v_low:
            buy_target = base * (1 - std_20d * v_fear / 100)
            buy_name, sub_msg = f"-{v_fear}σ", "⚠️ VIX 공포 상승"
        elif is_open and gap_ratio < -0.001:
            rem = max(0, std_20d + gap_ratio * 100)
            buy_target = today_open * (1 - rem / 100)
            buy_name = f"-{rem/std_20d:.1f}σ"
            sub_msg = "📉 갭 하락 보정"
        else:
            buy_target = prev_close * (1 - std_20d * v_normal / 100)
            buy_name, sub_msg = f"-{v_normal}σ", "📈 평시 안정 장세"

        result.update({
            "buy_target": buy_target,
            "buy_name": buy_name,
            "sub_msg": sub_msg,
            "split_sell_plan": calculate_split_sell_targets(base, std_20d, shares)
        })
        return result

    # ====================== LONG 모드 ======================
    # 90일 로그 수익률 기반 daily sigma
    log_ret = np.log(ticker_df["Close"] / ticker_df["Close"].shift(1)).dropna().tail(90)
    daily_sigma_val = float(log_ret.std(ddof=1)) if len(log_ret) > 0 else 0.04

    if vix_val >= v_high:
        multiplier = v_extreme
        vix_status_msg = "🔴🔴 VIX 극단 공포 장세"
    elif vix_val >= v_low:
        multiplier = v_fear
        vix_status_msg = "⚠️ VIX 공포 상승 장세"
    else:
        multiplier = v_normal
        vix_status_msg = "✨ 평시 안정 장세"

    long_buy_ratio = float(np.exp(-daily_sigma_val * multiplier))
    long_buy = (today_open if is_open else prev_close) * long_buy_ratio
    
    # config.json에서 최대 하락 보호 비율 가져오기
    max_drop_protection = defaults.get("MAX_DROP_PROTECTION", 0.10)
    long_buy = max(long_buy, prev_close * max_drop_protection)

    # 시간 가드 로직
    last_cast_str = pos_cfg.get("LAST_CAST_DATE", defaults.get("LAST_CAST_DATE", "2026-01-01"))
    try:
        last_cast_date = datetime.strptime(last_cast_str, "%Y-%m-%d").date()
    except Exception:
        last_cast_date = datetime(2026, 1, 1).date()

    days_since = (today_date - last_cast_date).days
    normal_std = 4.0 if "SOXL" in ticker.upper() else 2.5
    min_days_gate = 14 if std_20d > normal_std * 1.3 else 5
    is_time_gate_passed = days_since >= min_days_gate

    current_price = float(ticker_df["Close"].iloc[-1])

    if is_open and current_price <= long_buy and not is_time_gate_passed:
        time_guard_status = "🔥 [시간 가드 강제 해제] 실탄 집행!"
    elif is_time_gate_passed:
        time_guard_status = "🟢 [시간 가드 해제] 자유 매수 가능"
    else:
        time_guard_status = f"⏳ [시간 가드 작동 중] {min_days_gate - days_since}일 남음"

    annual_quota = pos_cfg.get("ANNUAL_QUOTA", defaults.get("ANNUAL_QUOTA", 24))

    result.update({
        "daily_sigma": daily_sigma_val * 100,
        "multiplier": multiplier,
        "buy_target": long_buy,
        "buy_name": f"LONG 변동성 방어선 ({multiplier:.2f}x)",
        "sub_msg": f"{vix_status_msg} ➔ {multiplier:.2f}배수 하방",
        "time_guard_info": time_guard_status,
        "my_avg_price": pos_cfg.get("MY_AVG_PRICE", 0.0),
        "current_casts": pos_cfg.get("CURRENT_CASTS", 0),
        "annual_quota": annual_quota,
        "exhaustion_rate": min(pos_cfg.get("CURRENT_CASTS", 0) / max(annual_quota, 1) * 100, 100.0),
    })

    return result


def calculate_split_sell_targets(base_price: float, std_20d: float, shares: int) -> list:
    if shares <= 0:
        return []
    levels = [(0.9, "1단계 +0.9σ"), (1.3, "2단계 +1.3σ"), (1.8, "3단계 +1.8σ")]
    per_level = max(1, shares // len(levels))
    plan = []
    remaining = shares
    for i, (mult, name) in enumerate(levels):
        qty = per_level if i < len(levels) - 1 else remaining
        if qty <= 0:
            break
        plan.append({
            "level": name,
            "price": round(base_price * (1 + std_20d * mult / 100), 2),
            "qty": qty,
        })
        remaining -= qty
    return plan


# Git 동기화 함수
def sync_config_to_git(target_date: datetime.date):
    """config.json 변경사항을 Git에 커밋 & 푸시"""
    today_str = target_date.strftime("%Y-%m-%d")
    is_github_action = os.getenv("GITHUB_ACTIONS") == "true"

    try:
        status = subprocess.run(
            ["git", "status", "--porcelain", "config.json"], 
            capture_output=True, text=True, timeout=10
        )
        
        if not status.stdout.strip():
            logger.info("📝 config.json 변경사항이 없어 Git Commit을 생략합니다.")
            return

        subprocess.run(["git", "config", "user.name", "Automated Bot"], check=True, timeout=10)
        subprocess.run(["git", "config", "user.email", "bot@example.com"], check=True, timeout=10)
        
        subprocess.run(["git", "add", "config.json"], check=True, timeout=10)
        subprocess.run(["git", "commit", "-m", f"🤖 Auto-update config.json [{today_str}]"], 
                      check=True, timeout=10)

        if is_github_action:
            logger.info("📡 GitHub Actions 환경 감지: 원격 저장소에 자동으로 푸시합니다.")
            subprocess.run(["git", "push"], check=True, timeout=15)
        else:
            logger.info("💻 로컬 PC 환경 감지: 깃허브 원격 저장소로 동기화(Push)를 시도합니다.")
            subprocess.run(["git", "push", "origin", "main"], check=True, timeout=15)

    except subprocess.TimeoutExpired:
        logger.error("⏰ Git 명령어 실행 시간이 초과되었습니다.")
    except subprocess.CalledProcessError as e:
        logger.error(f"❌ Git 명령어 실행 실패: {e}")
    except Exception as e:
        logger.error(f"❌ Git 처리 중 예상치 못한 오류: {e}")


# ====================== 데이터 수집 ======================
def get_combined_market_data(tickers: list, config: dict, est_tz, target_date: datetime.date):
    df = yf.download(tickers, period="150d", interval="1d", progress=False, auto_adjust=True)
    if df.empty:
        logger.error("❌ yfinance 데이터 다운로드 실패")
        return {}, False, "N/A"

    now_est = datetime.now(est_tz)
    market_open = now_est.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now_est.replace(hour=16, minute=0, second=0, microsecond=0)

    is_open = (
        market_open <= now_est <= market_close
        and target_date.weekday() < 5
        and not is_us_holiday(target_date)
        and target_date == now_est.date()
    )

    vix_val, vix_info = get_vix_report()
    positions_cfg = config.get("positions", {})
    vix_long_cfg = config.get("vix_long", {})
    vix_short_cfg = config.get("vix_short", {})
    defaults = config.get("defaults", {})          # ← 추가

    results = {}
    for ticker in tickers:
        try:
            if isinstance(df.columns, pd.MultiIndex):
                t_df = df.xs(ticker, level=1, axis=1)[["Close", "Open"]].dropna()
            else:
                t_df = df[["Close", "Open"]].dropna()

            if len(t_df) < 2:
                continue

            pos_cfg = positions_cfg.get(ticker, {})
            mode = pos_cfg.get("MODE", "SHORT")
            vix_cfg = vix_long_cfg if mode == "LONG" else vix_short_cfg

            results[ticker] = analyze_ticker(
                ticker=ticker,
                ticker_df=t_df,
                pos_cfg=pos_cfg,
                vix_val=vix_val,
                is_open=is_open,
                today_date=target_date,
                vix_cfg=vix_cfg,
                defaults=defaults          # ← defaults 전달
            )
        except Exception as e:
            logger.warning(f"⚠️ {ticker} 분석 실패: {e}")

    return results, is_open, vix_info


# ====================== 리포트 생성 ======================
def create_combined_message(results: dict, is_open: bool,
                            kst_now: str, vix_info: str, is_last_day: bool) -> str:
    mode_str = "🚀 실시간 모드" if is_open else "⏳ 장전 대기 모드"
    lines = [
        f"=== 🎯 매매엔진 통합 리포트 ({mode_str}) ===",
        f"🎬 VIX 지수 : {vix_info}",
        ""
    ]

    if not results:
        lines.append("⚠️ 분석 결과가 없습니다.")
        return "\n".join(lines)

    for ticker, v in results.items():
        if v is None:
            lines += [
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
                f"● 종목 : {ticker} [⚠️ 데이터 없음]",
            ]
            continue

        opt_mode = "📈 LONG (장기 적립)" if v.get("mode") == "LONG" else "⚡ SHORT (단기 타격)"
        
        lines += [
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            f"● 종목 : {ticker} [{opt_mode}] (보유량 : {v.get('total_shares', 0)}주)",
            f"● 전일 종가 : ${v.get('prev_close', 0.0):.2f}",
            f"● 장세 상태 : {v.get('sub_msg', 'Normal')}",
            f"🛒 [매수 예정가] : ${v.get('buy_target', 0.0):.2f}",
        ]
        
        if v.get("mode") == "LONG":
            lines += [
                f"-----------------------------------------",
                f"📊 일간 평균 변동성 : ±{v.get('daily_sigma', 0.0):.2f}%",
                f"💡 배수 : {v.get('multiplier', 0.0):.2f}x",
                f"⚙️ 타임 엔진 : {v.get('time_guard_info', '정보 없음')}",
                f"📊 집행 현황 : {v.get('current_casts', 0)}/{v.get('annual_quota', 24)}회",
            ]
            if v.get("my_avg_price"):
                lines.append(f"🍏 평단가 : ${v.get('my_avg_price'):.2f}")
        else:
            lines.append(f"📊 20일 변동성(1σ) : ±{v.get('std', 0.0):.2f}%")
            plan = v.get("split_sell_plan", [])
            if plan:
                lines.append("📌 **3단계 분할 매도 계획**")
                for p in plan:
                    lines.append(f"   • {p['level']:16} → ${p['price']:.2f}  ({p['qty']}주)")

    lines.append("-----------------------------------------")
    if is_last_day:
        lines.append("📡 **[🤖 디스코드 계정 만료 방지 생존 핑 발송 완료]**")
    
    lines.append(f"⏰ 통합 분석 관제탑 시각: {kst_now}")
    return "\n".join(lines)


# ====================== Discord 전송 ======================
def send_discord_message(content: str, webhook_url: str, user_id: str) -> bool:
    if not webhook_url:
        return False
    mention = f"<@{user_id}>\n" if user_id else ""
    try:
        payload = {
            "content": f"{mention}```\n{content}\n```"
        }
        r = requests.post(webhook_url, json=payload, timeout=15)
        return r.status_code in (200, 204)
    except Exception:
        return False


# ====================== 메인 ======================
def main():
    try:
        config = setup_environment()
        logger.info("✅ 설정 로드 완료")

        kst_now = datetime.now(config["kst"])
        now_est = datetime.now(config["est"])
        
        # 타겟 날짜 결정
        target_date = now_est.date()
        if now_est.weekday() == 6 and now_est.hour >= 18:  # 토요일 저녁
            target_date += timedelta(days=1)

        # 휴장일 스킵
        if target_date.weekday() >= 5 or is_us_holiday(target_date):
            logger.info(f"📅 {target_date}은(는) 휴장일입니다. 브리핑을 스킵합니다.")
            return

        is_last = is_last_business_day_of_month(target_date)

        # ==================== 데이터 분석 ====================
        results, is_open, vix_info = get_combined_market_data(
            config["tickers"], config, config["est"], target_date
        )

        if not results:
            logger.error("❌ 분석 결과가 없습니다.")
            return

        # ==================== Git 동기화 (기존 로직 최대한 유지) ====================
        try:
            today_str = target_date.strftime("%Y-%m-%d")
            is_github_action = os.getenv("GITHUB_ACTIONS") == "true"

            status = subprocess.run(
                ["git", "status", "--porcelain", "config.json"], 
                capture_output=True, text=True, timeout=10
            )
            
            if status.stdout.strip(): 
                subprocess.run(["git", "config", "user.name", "Automated Bot"], check=True, timeout=10)
                subprocess.run(["git", "config", "user.email", "bot@example.com"], check=True, timeout=10)
                subprocess.run(["git", "add", "config.json"], check=True, timeout=10)
                subprocess.run(["git", "commit", "-m", f"🤖 Auto-update config.json [{today_str}]"], check=True, timeout=10)

                if is_github_action:
                    logger.info("📡 GitHub Actions 환경 감지: 원격 저장소에 자동으로 푸시합니다.")
                    subprocess.run(["git", "push"], check=True, timeout=15)
                else:
                    logger.info("💻 로컬 PC 환경 감지: 깃허브 원격 저장소로 동기화(Push)를 시도합니다.")
                    subprocess.run(["git", "push", "origin", "main"], check=True, timeout=15)
            else:
                logger.info("📝 config.json 변경 사항이 없어 Git Commit을 생략합니다.")

        except Exception as git_err:
            logger.warning(f"⚠️ Git 동기화 실패 (계속 진행): {git_err}")

        # ==================== 리포트 생성 & Discord 전송 ====================
        kst_str = kst_now.strftime('%Y-%m-%d %H:%M:%S')
        msg = create_combined_message(results, is_open, kst_str, vix_info, is_last)
        
        send_success = send_discord_message(msg, config["webhook"], config["user_id"])
        
        if send_success:
            logger.info("✅ Discord 알림 전송 완료")
        else:
            logger.warning("⚠️ Discord 전송 실패")

    except Exception as e:
        logger.error(f"💥 메인 실행 중 오류 발생: {e}")
  
                        
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    main()  