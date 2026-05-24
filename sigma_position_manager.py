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
import matplotlib.pyplot as plt  # 차트 시각화용 추가

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
        "MULT_NORMAL": 0.85,
        "MULT_FEAR": 1.95,
        "MULT_EXTREME": 2.40,
    }

    # 공통 기본값 설정 (config.json에서 관리)
    default_values = cfg.get("DEFAULTS", {
        "SIGMA_DEFAULT": 2.0,
        "LAST_CAST_DATE": "2026-05-07",
        "ANNUAL_QUOTA": 24,   
        "MAX_DROP_PROTECTION": 0.10
    })

    return {
        "webhook": cfg.get("DISCORD_WEBHOOK") or os.getenv("DISCORD_WEBHOOK"),
        "user_id": cfg.get("DISCORD_USER_ID") or os.getenv("DISCORD_USER_ID"),
        "tickers": cfg.get("TICKERS", ["SOXL", "TSLA"]),
        "positions": cfg.get("POSITIONS", {}),
        "vix_long": {**default_vix, **cfg.get("VIX_CONFIG", {}).get("LONG",  {})},
        "vix_short": {**default_vix, **cfg.get("VIX_CONFIG", {}).get("SHORT", {})},
        "defaults": default_values,           
        "kst": pytz.timezone('Asia/Seoul'),
        "est": pytz.timezone('US/Eastern'),
        "full_cfg": cfg
    }
        

# ====================== 유틸 ======================
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
                   vix_cfg: dict, defaults: dict = None) -> dict:
    
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
        if is_today_data_present and is_open:
            today_open = float(ticker_df["Open"].iloc[-1])
        else:
            today_open = prev_close
    except Exception:
        today_open = prev_close

    base = today_open if (is_open and is_today_data_present) else prev_close
    gap_ratio = (today_open - prev_close) / prev_close if prev_close != 0 else 0.0

    hist_df = ticker_df.iloc[:-1] if is_today_data_present else ticker_df

    daily_ret = hist_df["Close"].pct_change().dropna()
    std_20d = float(daily_ret.tail(20).std() * 100)
    if pd.isna(std_20d) or std_20d <= 0:
        std_20d = float(defaults.get("SIGMA_DEFAULT", 2.0))

    mode = pos_cfg.get("MODE", "SHORT")
    shares = pos_cfg.get("TOTAL_SHARES", 0)

    vix_cfg = vix_cfg or {}
    v_extreme = vix_cfg.get("MULT_EXTREME")
    v_fear    = vix_cfg.get("MULT_FEAR")
    v_normal  = vix_cfg.get("MULT_NORMAL")
    v_high    = vix_cfg.get("LEVEL_HIGH")
    v_low     = vix_cfg.get("LEVEL_LOW")

    current_price = float(ticker_df["Close"].iloc[-1])

    result = {
        "mode": mode,
        "prev_close": prev_close,
        "today_open": today_open,
        "std": std_20d,
        "total_shares": shares,
        "current_price": current_price,
    }

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
            buy_name, sub_msg = f"-{v_fear}σ", "🔴 VIX 공포"
        elif is_open and gap_ratio < -0.001:
            gap_adjust = abs(gap_ratio) * 100
            rem = max(0, std_20d - gap_adjust)
            buy_target = today_open * (1 - rem / 100)
            buy_name = f"-{rem/std_20d:.1f}σ"
            sub_msg = "📉 갭 하락 보정"
        else:
            buy_target = prev_close * (1 - std_20d * v_normal / 100)
            buy_name, sub_msg = f"-{v_normal}σ", "✨ VIX 안정"

        result.update({
            "buy_target": buy_target,
            "buy_name": buy_name,
            "sub_msg": sub_msg,
            "split_sell_plan": calculate_split_sell_targets(base, std_20d, shares)
        })
        return result
    
    # LONG 모드
    log_ret = np.log(hist_df["Close"] / hist_df["Close"].shift(1)).dropna().tail(90)
    daily_sigma_val = float(log_ret.std(ddof=1)) if len(log_ret) > 0 else 0.04

    if vix_val >= v_high:
        multiplier = v_extreme
        vix_status_msg = "🔴🔴 VIX 극단적 공포"
    elif vix_val >= v_low:
        multiplier = v_fear
        vix_status_msg = "🔴 VIX 공포"
    else:
        multiplier = v_normal
        vix_status_msg = "✨ VIX 안정"            

    if is_open and gap_ratio < -0.03:
        gap_pct = abs(gap_ratio) * 100
        if gap_pct <= 5.0:
            adjusted_multiplier = 0.45
            gap_zone = f"-3%~-5% 구간"
        elif gap_pct <= 7.0:
            adjusted_multiplier = 0.25
            gap_zone = f"-5%~-7% 구간"
        elif gap_pct <= 10.0:
            adjusted_multiplier = 0.10
            gap_zone = f"-7%~-10% 구간"
        else:
            adjusted_multiplier = 0.0
            gap_zone = f"-10% 초과 구간"
        sub_msg_gap = f" (갭 {gap_ratio*100:.1f}% / {gap_zone} → 배수 {multiplier:.2f}→{adjusted_multiplier:.2f})"
    else:
        adjusted_multiplier = multiplier
        sub_msg_gap = ""

    long_buy = (today_open if is_open else prev_close) * float(np.exp(-daily_sigma_val * adjusted_multiplier))
    
    last_cast_str = pos_cfg.get("LAST_CAST_DATE", defaults.get("LAST_CAST_DATE", "2026-01-01"))
    try:
        last_cast_date = datetime.strptime(last_cast_str, "%Y-%m-%d").date()
    except Exception:
        last_cast_date = datetime(2026, 1, 1).date()

    days_since = (today_date - last_cast_date).days
    normal_std = 4.0 if "SOXL" in ticker.upper() else 2.5
    min_days_gate = 14 if std_20d > normal_std * 1.3 else 5
    is_time_gate_passed = days_since >= min_days_gate

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
        "buy_name": f"LONG 변동성 방어선 ({adjusted_multiplier:.2f}x)",
        "sub_msg": f"{vix_status_msg} ➔ {adjusted_multiplier:.2f}배수 하방{sub_msg_gap}",
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
    levels = [
        (0.85, "1단계 +0.85σ"),
        (1.95, "2단계 +1.95σ"),
        (2.40, "3단계 +2.40σ")
    ]
    per_level = max(1, shares // len(levels))
    plan = []
    remaining = shares
    for i, (mult, name) in enumerate(levels):
        qty = per_level if i < len(levels) - 1 else remaining
        if qty <= 0:
            break
        target_price = round(base_price * (1 + std_20d * mult / 100), 2)
        plan.append({"level": name, "price": target_price, "qty": qty})
        remaining -= qty
    return plan


# ====================== 📊 시각화 엔진 ======================
def generate_long_portfolio_chart(df: pd.DataFrame, config: dict, output_filename: str = "portfolio_trend.png"):
    """LONG 모드 계좌 전용 월별 누적수익률 및 계좌 평가액 추세 차트 생성"""
    positions_cfg = config.get("positions", {})
    long_tickers = [tk for tk, cfg in positions_cfg.items() if cfg.get("MODE") == "LONG"]
    
    if not long_tickers:
        logger.info("ℹ️ LONG 모드 종목이 없어 차트 생성을 스킵합니다.")
        return False

    # 데이터 프레임 단일/멀티 인덱스 예외 보정
    try:
        chart_data = pd.DataFrame(index=df.index)
        for tk in long_tickers:
            if isinstance(df.columns, pd.MultiIndex):
                chart_data[tk] = df.xs(tk, level=1, axis=1)["Close"]
            else:
                chart_data[tk] = df["Close"]
        chart_data = chart_data.dropna()
    except Exception as e:
        logger.error(f"❌ 차트 데이터 파싱 에러: {e}")
        return False

    if chart_data.empty:
        return False

    # 월별 말일 데이터 추출하여 월별 추세 트래킹
    monthly_df = chart_data.resample('ME').last()
    if len(monthly_df) < 2:
        # 데이터 일수가 너무 적은 경우 일별 데이터 그대로 사용
        monthly_df = chart_data
        x_labels = monthly_df.index.strftime('%m-%d')
        title_suffix = "(일별 추세)"
    else:
        x_labels = monthly_df.index.strftime('%Y-%m')
        title_suffix = "(월별 추세)"

    # 포트폴리오 메트릭 계산
    total_value_series = pd.Series(0.0, index=monthly_df.index)
    total_cost = 0.0
    
    # 각 종목별 수익률 및 누적 금액 합산
    returns_dict = {}
    for tk in long_tickers:
        pos = positions_cfg[tk]
        avg_price = float(pos.get("MY_AVG_PRICE", 0.0))
        shares = int(pos.get("TOTAL_SHARES", 0))
        
        if avg_price > 0 and shares > 0:
            # 월별 자산 가치 = 현재가 * 보유 주식 수
            total_value_series += monthly_df[tk] * shares
            total_cost += avg_price * shares
            # 종목별 수익률
            returns_dict[tk] = ((monthly_df[tk] - avg_price) / avg_price) * 100
        else:
            # 평단가 정보가 없는 경우 첫 거래일 기준 간이 수익률 계산
            returns_dict[tk] = ((monthly_df[tk] - monthly_df[tk].iloc[0]) / monthly_df[tk].iloc[0]) * 100

    # 종합 누적 수익률 (%)
    if total_cost > 0:
        portfolio_return = ((total_value_series - total_cost) / total_cost) * 100
    else:
        # 투자 금액 산출 불가 시 자산 단순 합산의 변동률 추종
        portfolio_return = total_value_series.pct_change().cumsum() * 100
        portfolio_return.iloc[0] = 0.0

    # 스타일 및 차트 그리기 (plt)
    plt.style.use('seaborn-v0_8-darkgrid' if 'seaborn-v0_8-darkgrid' in plt.style.available else 'default')
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 8), sharex=True)
    
    # [차트 1] 월별 누적 수익률 (%)
    for tk, ret_series in returns_dict.items():
        ax1.plot(x_labels, ret_series, marker='o', linestyle='--', alpha=0.6, label=f"{tk} 수익률")
    
    if total_cost > 0:
        ax1.plot(x_labels, portfolio_return, marker='s', color='#d62728', linewidth=2.5, label="종합 포트폴리오")
    ax1.axhline(0, color='black', linestyle='-', linewidth=0.8, alpha=0.5)
    ax1.set_title(f"📈 LONG 포트폴리오 누적 수익률 추세 {title_suffix}", fontsize=13, fontweight='bold')
    ax1.set_ylabel("수익률 (%)", fontsize=11)
    ax1.legend(loc="upper left")

    # [차트 2] 계좌 평가 금액 추세 ($)
    if total_cost > 0:
        ax2.fill_between(x_labels, total_value_series, total_cost, where=(total_value_series >= total_cost), 
                         interpolate=True, color='green', alpha=0.15, label='익절 구간')
        ax2.fill_between(x_labels, total_value_series, total_cost, where=(total_value_series < total_cost), 
                         interpolate=True, color='red', alpha=0.15, label='손절 구간')
        ax2.axhline(total_cost, color='blue', linestyle=':', alpha=0.7, label=f'총 투자금 (${total_cost:,.2f})')
        ax2.plot(x_labels, total_value_series, marker='o', color='#2ca02c', linewidth=2.5, label=f'계좌 평가액 (${total_value_series.iloc[-1]:,.2f})')
    else:
        # 기본 자산 가격 합산 그래프
        ax2.plot(x_labels, total_value_series, marker='o', color='#7f7f7f', label='자산 가격 지수 합산')
        
    ax2.set_title("💰 LONG 포트폴리오 계좌 자산 평가액 추세", fontsize=13, fontweight='bold')
    ax2.set_ylabel("자산 가치 ($)", fontsize=11)
    ax2.set_xlabel("기준 월 (Date)", fontsize=11)
    ax2.legend(loc="upper left")
    
    plt.xticks(rotation=30)
    plt.tight_layout()
    
    # 이미지 파일 저장
    plt.savefig(output_filename, dpi=150)
    plt.close()
    logger.info(f"📊 시각화 차트 생성 완료: {output_filename}")
    return True


# Git 동기화 함수
def sync_config_to_git(target_date: datetime.date):
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
        subprocess.run(["git", "commit", "-m", f"🤖 Auto-update config.json [{today_str}]"], check=True, timeout=10)

        if is_github_action:
            subprocess.run(["git", "push"], check=True, timeout=15)
        else:
            subprocess.run(["git", "push", "origin", "main"], check=True, timeout=15)
    except Exception as e:
        logger.error(f"❌ Git 처리 중 오류: {e}")


# ====================== 데이터 수집 ======================
def get_combined_market_data(tickers: list, config: dict, est_tz, target_date: datetime.date):
    df = yf.download(tickers, period="150d", interval="1d", progress=False, auto_adjust=True)
    if df.empty:
        logger.error("❌ yfinance 데이터 다운로드 실패")
        return {}, False, "N/A", df

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
    defaults = config.get("defaults", {})          

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
                defaults=defaults         
            )
        except Exception as e:
            logger.warning(f"⚠️ {ticker} 분석 실패: {e}")

    return results, is_open, vix_info, df


# ====================== 리포트 생성 ======================
def create_combined_message(results: dict, is_open: bool,
                            kst_now: str, vix_info: str, is_last_day: bool) -> str:
    mode_str = "🚀 실시간 모드" if is_open else "⏳ 장전 대기 모드"
    lines = [
        f"=== 🎯 매매엔진 통합 리포트 ({mode_str}) ===",
        f"🎬 {vix_info}",
    ]

    for ticker, v in results.items():
        if v is None:
            continue

        opt_mode = "📈 LONG (장기 적립)" if v.get("mode") == "LONG" else "⚡ SHORT (단기 타격)"
        
        lines += [
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            f"● 종목 : {ticker} [{opt_mode}] (보유량 : {v.get('total_shares', 0)}주)",
            f"● 현재가 : ${v.get('current_price', 0.0):.2f}",
            f"● 전일 종가 : ${v.get('prev_close', 0.0):.2f}",
        ]

        sub_msg = v.get('sub_msg', '')
        lines.append(f"● VIX 상태 : {sub_msg}")

        buy_target = v.get('buy_target', 0.0)
        current_price = v.get('current_price', 0.0)
        if is_open and current_price <= buy_target:
            lines.append(f"🛒 [매수 예정가] : ${buy_target:.2f} (🚨 [매수 시그널 포착] 실탄 집행!)")
        else:
            lines.append(f"🛒 [매수 예정가] : ${buy_target:.2f}")

        if v.get("mode") == "LONG":
            lines += [
                f"-----------------------------------------",
                f"📊 일간 평균 변동성 : ±{v.get('daily_sigma', 0.0):.2f}%",
                f"💡 적용 배수     : {v.get('multiplier', 0.0):.2f}x",
                f"⚙️ 타임 엔진     : {v.get('time_guard_info', '정보 없음')}",
                f"📊 집행 현황     : {v.get('current_casts', 0)}/{v.get('annual_quota', 24)}회",
            ]
            if v.get("my_avg_price", 0) > 0:
                lines.append(f"🍏 평단가         : ${v.get('my_avg_price'):.2f}")
        else:
            lines.append(f"📊 20일 변동성(1σ) : ±{v.get('std', 0.0):.2f}%")
            plan = v.get("split_sell_plan", [])
            if plan:
                lines.append("📌 **3단계 분할 매도 계획**")
                for p in plan:
                    lines.append(f"   • {p['level']:16} → ${p['price']:.2f}  ({p['qty']}주)")

    lines.append("-----------------------------------------")
    if is_last_day:
        lines += [
            "📡 **[🤖 디스코드 계정 만료 방지 생존 핑 발송 완료]**",
            "-----------------------------------------"
        ]
    
    lines.append(f"⏰ 통합 분석 관제탑 시각: {kst_now}")
    return "\n".join(lines)


# ====================== Discord 전송 ======================
def send_discord_message_with_file(content: str, webhook_url: str, user_id: str, file_path: str = None) -> bool:
    if not webhook_url:
        return False
    mention = f"<@{user_id}>\n" if user_id else ""
    try:
        # 💡 끝부분 f-string 닫는 따옴표(") 및 백틱 보정 완료
        payload = {
            "content": f"{mention}```\n{content}\n```"  
        }
        
        # 파일 첨부 여부에 따라 격리 전송 처리
        if file_path and os.path.exists(file_path):
            with open(file_path, 'rb') as f:
                files = {
                    "file": (os.path.basename(file_path), f, "image/png")
                }
                # multipart/form-data 형태로 payload와 file을 동시에 포스트
                r = requests.post(webhook_url, data=payload, files=files, timeout=20)
        else:
            r = requests.post(webhook_url, json=payload, timeout=15)
            
        return r.status_code in (200, 204)
    except Exception as e:
        logger.error(f"❌ 디스코드 파일 웹훅 전송 중 장애 발생: {e}")
        return False
    

# ====================== 메인 ======================
def main():
    chart_filename = "portfolio_trend.png"
    try:
        config = setup_environment()
        logger.info("✅ 설정 로드 완료")

        kst_now = datetime.now(config["kst"])
        now_est = datetime.now(config["est"])
        
        target_date = now_est.date()
        if now_est.weekday() == 6 and now_est.hour >= 18:
            target_date += timedelta(days=1)

        if target_date.weekday() >= 5 or is_us_holiday(target_date):
            logger.info(f"📅 {target_date}은(는) 휴장일입니다. 브리핑을 스킵합니다.")
            return

        is_last = is_last_business_day_of_month(target_date)

        # ==================== 데이터 분석 ====================
        results, is_open, vix_info, full_df = get_combined_market_data(
            config["tickers"], config, config["est"], target_date
        )

        if not results:
            logger.error("❌ 분석 결과가 없습니다.")
            return

        # ==================== 📊 LONG 모드 차트 빌드 시스템 가동 ====================
        has_chart = generate_long_portfolio_chart(full_df, config, chart_filename)

        # ==================== Git 동기화 ====================
        sync_config_to_git(target_date)

        # ==================== 리포트 생성 & Discord 전송 ====================
        kst_str = kst_now.strftime('%Y-%m-%d %H:%M:%S')
        msg = create_combined_message(results, is_open, kst_str, vix_info, is_last)
        
        # 차트 이미지(존재할 시)를 포함하여 디스코드 전송
        send_success = send_discord_message_with_file(
            content=msg, 
            webhook_url=config["webhook"], 
            user_id=config["user_id"], 
            file_path=chart_filename if has_chart else None
        )
        
        if send_success:
            logger.info("✅ Discord 알림 및 포트폴리오 차트 전송 완료")
        else:
            logger.warning("⚠️ Discord 전송 실패")

    except Exception as e:
        logger.error(f"💥 메인 실행 중 오류 발생: {e}")
    finally:
        # 실행 완료 후 생성된 로컬 임시 이미지 파괴 (서버 스토리지 보호)
        if os.path.exists(chart_filename):
            try:
                os.remove(chart_filename)
            except Exception:
                pass
  
                        
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    main()