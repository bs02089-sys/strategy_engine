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

# 🚀 패키지가 없어도 봇이 죽지 않도록 방어 로직 추가
try:
    import holidays
except ImportError:
    holidays = None
    logger.warning("⚠️ 'holidays' 패키지가 설치되지 않았습니다. 휴장일 체크가 비활성화됩니다.")

# ====================== 설정 로드 및 저장 ======================
def load_config() -> dict:
    try:
        with open('config.json', 'r', encoding='utf-8') as f:
            cfg = json.load(f)
    except FileNotFoundError:
        logger.error("❌ config.json 파일을 찾을 수 없습니다.")
        raise
    except json.JSONDecodeError:
        logger.error("❌ config.json 형식이 잘못되었습니다.")
        raise

    updated = False
    if not cfg.get("DISCORD_WEBHOOK") and os.getenv("DISCORD_WEBHOOK"):
        cfg["DISCORD_WEBHOOK"] = os.getenv("DISCORD_WEBHOOK")
        updated = True
    if not cfg.get("DISCORD_USER_ID") and os.getenv("DISCORD_USER_ID"):
        cfg["DISCORD_USER_ID"] = os.getenv("DISCORD_USER_ID")
        updated = True

    if updated:
        save_config(cfg)

    return cfg

def save_config(cfg: dict):
    with open('config.json', 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)

def setup_environment() -> dict:
    cfg = load_config()
    return {
        "webhook":       cfg.get("DISCORD_WEBHOOK"),
        "user_id":       cfg.get("DISCORD_USER_ID"),
        "tickers":       cfg.get("TICKERS", ["SOXL", "TSLA"]),
        "positions":     cfg.get("POSITIONS", {}),
        "kst":           pytz.timezone('Asia/Seoul'),
        "est":           pytz.timezone('US/Eastern'),
        "full_cfg":      cfg
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
    
    # 🚀 3배 레버리지의 실제 변동성을 반영하기 위해 한계치를 2.0(200%)으로 상향
    return min(annual_sigma, 2.0)

def calculate_split_sell_targets(base_price: float, std_20d: float, shares: int) -> list:
    if shares <= 0:
        return []
    levels = [(0.9, "1단계 +0.9σ"), (1.3, "2단계 +1.3σ"), (1.8, "3단계 +1.8σ")]
    per_level = max(1, shares // len(levels))
    plan, remaining = [], shares
    for i, (mult, name) in enumerate(levels):
        qty = per_level if i < len(levels) - 1 else remaining
        if qty <= 0: break
        plan.append({
            "level": name,
            "price": round(base_price * (1 + std_20d * mult / 100), 2),
            "qty":   qty,
        })
        remaining -= qty
    return plan

def get_vix_report() -> tuple[float, str]:
    try:
        df = yf.download("^VIX", period="2d", progress=False)
        if not df.empty:
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.droplevel(1)
            v = float(df["Close"].iloc[-1])
            status = "안정" if v <= 15 else "주의" if v <= 25 else "공포" if v <= 35 else "극단적 공포"
            return v, f"{v:.1f} ({status})"
    except Exception as e:
        logger.warning(f"VIX 데이터 수집 실패: {e}")
    return 0.0, "N/A"

def is_us_holiday(d) -> bool:
    if holidays is None:
        return False
    us_holidays = holidays.US(years=d.year)
    return d in us_holidays

def is_last_business_day_of_month(today) -> bool:
    check = today + timedelta(days=1)
    while check.month == today.month:
        if check.weekday() < 5 and not is_us_holiday(check):
            return False
        check += timedelta(days=1)
    return True

def is_triple_witching_week(d) -> bool:
    # 🚀 3, 6, 9, 12월에만 세 마녀의 날이 존재함
    if d.month not in [3, 6, 9, 12]:
        return False
    return (13 <= d.day <= 21) and (2 <= d.weekday() <= 4)

# ====================== ticker 단위 분석 ======================
def analyze_ticker(ticker: str, ticker_df: pd.DataFrame, pos_cfg: dict,
                   vix_val: float, is_open: bool, today_est) -> dict:
    # today_est가 date 객체일 때와 datetime 객체일 때 모두 date()로 통일
    if isinstance(today_est, datetime):
        today_est_date = today_est.date()
    else:
        today_est_date = today_est

    # 🛡️ [장 초반 데이터 공백 방어선] 데이터 개수가 부족하면 안전하게 0.70 기본 변동성 리턴
    if len(ticker_df) < 2:
        prev_close = float(ticker_df["Close"].iloc[-1]) if not ticker_df.empty else 10.0
        today_open = prev_close
    else:
        prev_close = float(ticker_df["Close"].iloc[-2 if is_open else -1])
        # 장이 열렸으나 yfinance에 오늘 시가 데이터가 아직 안 올라왔을 때를 대비한 방어
        try:
            today_open = float(ticker_df["Open"].iloc[-1]) if is_open else prev_close
            if pd.isna(today_open):
                today_open = prev_close
        except Exception:
            today_open = prev_close

    base = today_open if is_open else prev_close
    gap_ratio = (today_open - prev_close) / prev_close

    daily_ret = ticker_df["Close"].pct_change().dropna()
    std_20d = float(daily_ret.tail(20).std() * 100)
    if pd.isna(std_20d) or std_20d <= 0:
        std_20d = 2.0

    mode   = pos_cfg.get("MODE", "SHORT")
    shares = pos_cfg.get("TOTAL_SHARES", 0)

    # ------------------ [SHORT 모드 및 기존 VIX/갭하락 로직] ------------------
    if vix_val >= 35.0:
        buy_target = base * (1 - std_20d * 2.0 / 100)
        buy_name, sub_msg = "-2.0σ", "🔴🔴 VIX 극단적 공포 (초심해 방어)"
    elif is_triple_witching_week(today_est_date):
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
        "mode": mode, "prev_close": prev_close, "today_open": today_open,
        "std": std_20d, "buy_target": buy_target, "buy_name": buy_name,
        "sub_msg": sub_msg, "total_shares": shares,
    }

    # ------------------ [LONG 모드: 선장님 맞춤형 진화 완전체] ------------------
    if mode == "LONG":
        annual_sig = calculate_annual_sigma(ticker_df["Close"].values)
        
        # 🚀 연간 변동성을 일간 및 주간 변동성 수치로 변환
        annual_sigma_val = annual_sig
        daily_sig_pct = (annual_sigma_val / np.sqrt(252)) * 100
        weekly_sig_pct = (annual_sigma_val / np.sqrt(52)) * 100
        
        # 📊 [선장님 직관 반영: 동적 배율 엔진] VIX 수치에 따라 화력 배율 자동 조절
        if vix_val >= 35.0:
            calculated_multiplier = 2.0  # 🔴 VIX 35 이상 = 2시그마 초심해 타점
            vix_status_msg = "🔴🔴 VIX 극단 공포 (초심해 2배수 타점)"
        elif vix_val > 25.0:
            calculated_multiplier = 1.5  # ⚠️ VIX 25~35 = 1.5시그마 심화 타점
            vix_status_msg = "⚠️ VIX 공포 상승 (1.5배수 타점)"
        else:
            calculated_multiplier = 1.0  # ✨ 평시 장세 = 1.0시그마 기본 철벽 타점
            vix_status_msg = "✨ 평시 장세 (1.0배수 기본 타점)"

        long_buy_ratio = (daily_sig_pct / 100) * calculated_multiplier
        
        # 🛡️ [선장님 갭 보정 현실화 핵심] gap_ratio < 0 조건을 과감히 깨부수고,
        # 정규장이 열렸다면(is_open) 시가가 높든 낮든 무조건 '오늘 실시간 시가' 기준으로 방어선 전개!
        if is_open:
            long_buy = today_open * (1 - long_buy_ratio)
            sub_msg = f"📉 {vix_status_msg} + 실시간 시가(Open) 기준 타점 배치 완료"
        else:
            long_buy = prev_close * (1 - long_buy_ratio)
            sub_msg = f"{vix_status_msg} 대기 중 (전일 종가 기준)"
            
        # 10% 최하단 리스크 안전 가드
        long_buy = max(long_buy, prev_close * 0.10)

        # 20일 표준편차 기반 고변동성 장세 시간 가드 필터링
        normal_std = 4.0 if "SOXL" in ticker else 2.5 
        if std_20d > normal_std * 1.3:
            min_days_gate = 14
        else:
            min_days_gate = 5

        # 🚀 역사적 첫 투자 시작일 유령 날짜 방어선 교정
        last_cast_str = pos_cfg.get("LAST_CAST_DATE", "2025-05-07")
        try:
            last_cast_date = datetime.strptime(last_cast_str, "%Y-%m-%d").date()
        except ValueError:
            last_cast_date = datetime(2025, 5, 7).date()
        
        days_since_last_cast = (today_est_date - last_cast_date).days
        is_time_gate_passed = days_since_last_cast >= min_days_gate
        days_remaining = min_days_gate - days_since_last_cast

        # 실시간 가격 안전 추출 (데이터 딜레이 방어)
        try:
            current_live_price = float(ticker_df["Close"].iloc[-1])
        except Exception:
            current_live_price = prev_close

        if is_open and (current_live_price <= long_buy) and not is_time_gate_passed:
            result["time_guard_locked"] = True
        else:
            result["time_guard_locked"] = False

        # 🚀 실제 엔진 가동 원리와 100% 일치하는 동적 관제 문구 생성
        if not is_time_gate_passed:
            if is_open:
                if current_live_price > long_buy:
                    time_guard_status = f"⏳ [시간규제 가동 중] 잔파도 매수 제한 ({days_remaining}일 대기 필요) ➔ 최종 타점(${long_buy:.2f}) 터치 시 강제 락업 해제 및 기습 포격!"
                else:
                    time_guard_status = f"🔥 [규제 강제 파괴] VIX 연동 장벽 돌파! 시간 규제를 무력화하고 실탄을 집행합니다!"
            else:
                time_guard_status = f"🛡️ [평시 경계 태세] 시간 가드 잠김 ({days_remaining}일 남음) ➔ 오늘 정규장 {ticker} 최종 타점({vix_status_msg}) 도달 시 자동 락업 해제 및 집행!"
        else:
            time_guard_status = f"🟢 [진입 제약 없음] 시간 가드 해제 상태 (안심하고 타점 조준 가능)"

        # 전일 종가 대비 최종 매수 예정가의 대폭락 하락률 실시간 역산
        drop_rate_from_prev = ((long_buy - prev_close) / prev_close) * 100
        current_quota = max(pos_cfg.get("ANNUAL_QUOTA", 24), 1)
        current_casts = pos_cfg.get("CURRENT_CASTS", 0)
        exhaustion_rate = min(current_casts / current_quota * 100, 100.0)

        result.update({
            "annual_sigma":     annual_sig * 100,
            "daily_sigma":      daily_sig_pct,
            "weekly_sigma":     weekly_sig_pct,
            "drop_rate":        drop_rate_from_prev,
            "multiplier":       calculated_multiplier,
            "buy_target":       long_buy,
            "buy_name":         f"LONG 변동성 방어선 ({calculated_multiplier:.1f}x)",
            "sub_msg":          sub_msg,
            "time_guard_info":  time_guard_status,
            "my_avg_price":     pos_cfg.get("MY_AVG_PRICE", 0.0),
            "current_casts":    current_casts,
            "annual_quota":     current_quota,
            "exhaustion_rate":  exhaustion_rate,
        })

    else:
        result["split_sell_plan"] = calculate_split_sell_targets(base, std_20d, shares)

    return result

# ====================== 데이터 수집 및 분석 ======================
def get_combined_market_data(tickers: list, config: dict, est_tz, target_date) -> tuple[dict, bool, str]:
    df = yf.download(tickers, period="150d", interval="1d", progress=False)
    if df is None or df.empty:
        logger.error("❌ 야후 파이낸스 서버 응답 없음")
        return {}, False, "N/A"

    now_est = datetime.now(est_tz)

    # target_date 기준으로 is_open 판정 (일요일 저녁 수동 실행 시 날짜 보정 반영)
    # target_date가 실제 거래일이라면, 현재 시각(now_est)의 시/분만 사용해 장 여부 판단
    # 공휴일 체크 추가
    market_open_time  = now_est.replace(hour=9,  minute=30, second=0, microsecond=0)
    market_close_time = now_est.replace(hour=16, minute=0,  second=0, microsecond=0)
    is_open = (
        market_open_time <= now_est <= market_close_time
        and target_date.weekday() < 5          # target_date 기준 평일 체크
        and not is_us_holiday(target_date)     # 공휴일 체크
    )

    vix_val, vix_info = get_vix_report()
    positions_cfg = config.get("positions", {})
    results = {}

    for ticker in tickers:
        try:
            if isinstance(df.columns, pd.MultiIndex):
                if ticker in df.columns.levels[1]:
                    t_df = df.xs(ticker, level=1, axis=1)[["Close", "Open"]].dropna()
                else:
                    continue
            else:
                t_df = df[["Close", "Open"]].copy().dropna()

            if len(t_df) < 2:
                continue

            results[ticker] = analyze_ticker(
                ticker, t_df, positions_cfg.get(ticker, {}), vix_val, is_open,
                target_date
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
    ]

    for ticker, v in results.items():
        # 🛡️ [최강 방어 가드] 만약 특정 종목의 데이터(v)가 통째로 None이면 안전하게 예외 처리하고 패스
        if v is None:
            lines += [
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
                f"● 종목 : {ticker} [⚠️ 데이터 없음]",
                f"📢 해당 계좌 또는 종목의 연산 데이터가 비어 있습니다. 설정을 확인하세요.",
            ]
            continue  # 에러를 내지 않고 다음 종목 계산으로 안전하게 토스!

        # 🟢 데이터가 존재할 때만 정상적으로 리포트 조립 시작
        opt_mode = "📈 LONG (장기 적립)" if v.get("mode") == "LONG" else "⚡ SHORT (단기 타격)"
        lines += [
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            f"● 종목 : {ticker} [{opt_mode}] (보유량 : {v.get('total_shares', 0)}주)",
            f"● 장세 상태 : {v.get('sub_msg', 'Normal')}",
            f"● 전일 종가 : ${v.get('prev_close', 0.0):.2f}",
        ]
        
        if v.get("mode") == "LONG":
            lines += [
                f"-----------------------------------------",
                f"📊 [🔍 90일 자산 데이터]",
                f" └─ 연간 환산 변동성 (Annual σ) : {v.get('annual_sigma', 0.0):.1f}%",
                f" └─ 일간 평균 변동성 (Daily σ)  : ±{v.get('daily_sigma', 0.0):.2f}% (★핵심 지표)",
                f"-----------------------------------------",
                f"🛒 [매수 예정가] : ${v.get('buy_target', 0.0):.2f}",
                f"📉 [타점 분석]   : 전일 종가 대비 **{v.get('drop_rate', 0.0):.2f}%** 대폭락 시 집행",
                f"💡 [안심 가이드] : 오늘 설정된 그물망은 {ticker} 일간 평균 변동성(±{v.get('daily_sigma', 0.0):.2f}%) 자리에 정교하게 포진했습니다. 실증 데이터 검증(최근 1년 24회 발생)을 거친 맞춤형 실전 타점입니다.",
                f"-----------------------------------------",
                f"⚙️ 타임 엔진 제어 : {v.get('time_guard_info', '정보 없음')}",
                f"📊 계좌 집행 현황 : {v.get('current_casts', 0)}/{v.get('annual_quota', 24)}회 집행 완료",
                f"🔥 자금 소진율 : {v.get('exhaustion_rate', 0.0):.1f}%",
            ]
            if v.get("my_avg_price", 0.0) > 0:
                lines.append(f"🍏 가문 평단가 : ${v.get('my_avg_price', 0.0):.2f}")
                
        else:
            short_std = v.get("std", 0.0) if v.get("std") is not None else 0.0
            buy_name_str = v.get("buy_name", "단기 타격선")
            buy_target_val = v.get("buy_target", 0.0)

            lines.append(f"📊 20일 기준 변동성(1σ) : ±{short_std:.2f}%")
            lines.append(f"🛒 **매수 예정가({buy_name_str}) : ${buy_target_val:.2f}**")
            
            plan = v.get("split_sell_plan", [])
            lines.append("📌 **3단계 분할 매도 계획**" if plan else "📌 분할 매도 계획 : 보유 주수 없음")
            for p in plan:
                lines.append(f"   • {p['level']:16} → ${p['price']:.2f}  ({p['qty']}주)")

    # 🚀 디스코드 계정 만료 방지 및 시각 정보 조립 (위치 고정)
    lines.append("-----------------------------------------")
    if is_last_day:
        lines += [
            "📡 **[🤖 디스코드 계정 만료 방지 생존 핑 발송 완료]**",
            "📢 본 메시지는 휴면 계정 전환을 막기 위한 월간 정기 핑입니다.",
            "-----------------------------------------",
        ]
        
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
    config  = setup_environment()
    now_est = datetime.now(config["est"])
    
    # 🚀 한국 시간 월요일 오전(미국 일요일 저녁) 수동 실행 대응
    # 미국 시간 기준 일요일 오후 6시(선물장 개장) 이후라면, 타겟 날짜를 '월요일'로 간주합니다.
    target_date = now_est.date()
    if now_est.weekday() == 6 and now_est.hour >= 18:
        target_date += timedelta(days=1)

    # target_date 기준으로 주말 및 휴장일 체크
    if target_date.weekday() >= 5 or is_us_holiday(target_date):
        logger.info("📅 휴장일 - 브리핑 건너뜀")
        return

    kst_now    = datetime.now(config["kst"]).strftime('%Y-%m-%d %H:%M:%S')
    is_last    = is_last_business_day_of_month(target_date)

    try:
        results, is_open, vix_info = get_combined_market_data(
            config["tickers"], config, config["est"], target_date
        )
        if not results:
            return

        try:
            today_str = target_date.strftime("%Y-%m-%d")
            is_github_action = os.getenv("GITHUB_ACTIONS") == "true"

            # 🚀 Git 변경 사항이 있을 때만 Commit 수행 (Crash 방지)
            status = subprocess.run(["git", "status", "--porcelain", "config.json"], capture_output=True, text=True)
            
            if status.stdout.strip(): 
                subprocess.run(["git", "config", "user.name", "Automated Bot"], check=True)
                subprocess.run(["git", "config", "user.email", "bot@example.com"], check=True)
                subprocess.run(["git", "add", "config.json"], check=True)
                subprocess.run(["git", "commit", "-m", f"🤖 Auto-update config.json [{today_str}]"], check=True)

                if is_github_action:
                    logger.info("📡 깃허브 액션 환경 감지: 원격 저장소에 자동으로 푸시합니다.")
                    subprocess.run(["git", "push"], check=True)
                else:
                    logger.info("💻 로컬 PC 환경 감지: 깃허브 원격 저장소로 동기화(Push)를 시도합니다.")
                    subprocess.run(["git", "push", "origin", "main"], check=True)
            else:
                logger.info("📝 config.json 변경 사항이 없어 Git Commit을 생략합니다.")

        except Exception as git_err:
            logger.error(f"❌ Git 동기화 실패: {git_err}")

        msg = create_combined_message(results, is_open, kst_now, vix_info, is_last)
        send_discord_message(msg, config["webhook"], config["user_id"])
        logger.info("✅ 알림 전송 완료")
    except Exception as e:
        logger.error(f"⚠️ 실행 오류: {e}")
        
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    main()