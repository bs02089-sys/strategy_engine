import os
import sys
import json
import numpy as np
import warnings
import requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# Windows 인코딩 에러 방지
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

warnings.filterwarnings('ignore', category=FutureWarning)


# ===================================================================
# 설정 로드
# ===================================================================
def load_config():
    try:
        with open("config.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print("❌ config.json 파일을 찾을 수 없습니다.")
        sys.exit(1)
    except json.JSONDecodeError:
        print("❌ config.json 파일 형식이 잘못되었습니다.")
        sys.exit(1)


# ===================================================================
# 시장 모드 판별 (뉴욕 시간 기준)
# ===================================================================
def get_market_mode():
    now_ny = datetime.now(ZoneInfo("America/New_York"))
    hour = now_ny.hour + now_ny.minute / 60.0
    
    if hour < 9.5:
        return "장전", now_ny
    elif hour < 16.0:
        return "장중", now_ny
    else:
        return "장후", now_ny


# ===================================================================
# 실시간 데이터 조회 (안정성 강화)
# ===================================================================
def get_realtime_data(mode):
    try:
        soxl = yf.Ticker("SOXL")
        vix = yf.Ticker("^VIX")

        hist = soxl.history(period="3d", auto_adjust=False)
        if len(hist) < 2:
            print("⚠️ SOXL 역사적 데이터 부족")
            return None, None, None, None

        now_ny = datetime.now(ZoneInfo("America/New_York"))
        today_date = now_ny.date()
        is_today_in_hist = (hist.index[-1].date() == today_date)

        # 전일 종가
        prev_close = float(hist['Close'].iloc[-2] if is_today_in_hist else hist['Close'].iloc[-1])

        # 당일 시가
        if mode == "장전":
            current_open = prev_close
        else:
            try:
                f_open = soxl.fast_info.open
                current_open = float(f_open) if f_open is not None else float(hist['Open'].iloc[-1] if is_today_in_hist else prev_close)
            except:
                current_open = float(hist['Open'].iloc[-1] if is_today_in_hist else prev_close)

        # 현재가 & VIX
        try:
            current_price = float(soxl.fast_info.last_price)
            current_vix = float(vix.fast_info.last_price)
        except:
            current_price = float(hist['Close'].iloc[-1] if is_today_in_hist else prev_close)
            current_vix = float(vix.history(period="1d")['Close'].iloc[-1])

        return float(current_vix), float(prev_close), float(current_open), float(current_price)

    except Exception as e:
        print(f"❌ 데이터 조회 실패: {e}")
        return None, None, None, None


# ===================================================================
# 디스코드 메시지 전송
# ===================================================================
def send_discord_message(webhook_url, user_id, title, content):
    if not webhook_url:
        print("⚠️ DISCORD_WEBHOOK이 설정되어 있지 않습니다.")
        return

    mention = f"<@{user_id}> " if user_id else ""
    payload = {
        "content": mention,
        "embeds": [{
            "title": title,
            "description": content,
            "color": 15158332 if "🚨" in title else 3447003,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }]
    }

    try:
        res = requests.post(webhook_url, json=payload, timeout=15)
        if res.status_code == 204:
            print("✅ 디스코드 알림 전송 성공")
        else:
            print(f"❌ 디스코드 전송 실패 ({res.status_code})")
            print(res.text)
    except Exception as e:
        print(f"❌ 디스코드 전송 중 오류: {e}")


# ===================================================================
# 메인 실행 함수
# ===================================================================
def execute_dual_tactical_trader():
    mode, now_ny = get_market_mode()
    MODE_EMOJI = {"장전": "🌙", "장중": "☀️", "장후": "🌆"}

    print("======================================================================")
    print(f"📡 SOXL_VIX_SIGMA.py  {MODE_EMOJI[mode]} {mode} 모드")
    print(f"🕒 뉴욕 시간: {now_ny.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print("======================================================================\n")

    # ==================== 설정 로드 ====================
    cfg = load_config()
    pos_cfg = cfg["POSITIONS"]["SOXL"]
    vix_cfg = cfg["VIX_CONFIG"]["LONG"]

    # 환경변수 우선 → config.json fallback
    webhook_url = os.environ.get("DISCORD_WEBHOOK") or cfg.get("DISCORD_WEBHOOK", "")
    user_id     = os.environ.get("DISCORD_USER_ID") or cfg.get("DISCORD_USER_ID", "")

    # ==================== 디버그 출력 ====================
    print("🔍 [환경변수 확인]")
    print(f"   Webhook : {'✅ 로드됨' if webhook_url else '❌ 없음'}")
    print(f"   User ID : {'✅ 로드됨' if user_id else '❌ 없음'}\n")
    # ====================================================

    # 파라미터 로드
    SIGMA             = vix_cfg["FIXED_SIGMA"]
    MULT_NORMAL       = vix_cfg["MULT_NORMAL"]
    MULT_FEAR         = vix_cfg["MULT_FEAR"]
    MULT_EXTREME      = vix_cfg["MULT_EXTREME"]
    TAKE_PROFIT_RATIO = vix_cfg["TAKE_PROFIT_RATIO"]

    # 실시간 데이터
    current_vix, prev_close, current_open, current_price = get_realtime_data(mode)
    if current_vix is None:
        print("❌ 데이터를 가져올 수 없어 종료합니다.")
        return

    price_label = "현재가 (실시간)" if mode == "장중" else "전일 종가"

    print(f"📌 {price_label}: ${current_price:.2f}")
    print(f"📌 당일 시가 : ${current_open:.2f}")
    print(f"📌 전일 종가 : ${prev_close:.2f}")
    print(f"📌 VIX       : {current_vix:.2f}\n")

    # ===================================================================
    # 전략 계산
    # ===================================================================
    gap_ratio = (current_open - prev_close) / prev_close if prev_close != 0 else 0

    base_mult = (
        MULT_EXTREME if current_vix >= cfg["VIX_CONFIG"]["LEVEL_HIGH"] else
        MULT_FEAR if current_vix >= cfg["VIX_CONFIG"]["LEVEL_LOW"] else
        MULT_NORMAL
    )

    if gap_ratio >= -0.03:
        adj_mult = base_mult
    elif gap_ratio >= -0.05:
        adj_mult = 0.45
    elif gap_ratio >= -0.07:
        adj_mult = 0.25
    elif gap_ratio >= -0.10:
        adj_mult = 0.10
    else:
        adj_mult = 0.0

    target_price_today = current_open * np.exp(-SIGMA * adj_mult)

    discord_report = []
    any_triggered = False

    header = (
        f"**{MODE_EMOJI[mode]} {mode} 모드** | {now_ny.strftime('%Y-%m-%d %H:%M %Z')}\n"
        f"• {price_label}: ${current_price:.2f}\n"
        f"• 당일시가: ${current_open:.2f}\n"
        f"• 전일종가: ${prev_close:.2f}\n"
    )
    discord_report.append(header)

    # ====================== LONG ======================
    print("🟢 [LONG] 매수 계좌")
    shares_long = pos_cfg["TOTAL_SHARES_LONG"]
    avg_long = pos_cfg["MY_AVG_PRICE_LONG"]

    long_msg = "**🟢 [LONG] 매수 계좌 현황**\n"
    if shares_long > 0 and avg_long > 0:
        ret_long = (current_price - avg_long) / avg_long
        long_msg += f"• 수량: {shares_long}주 / 평단: ${avg_long:.4f}\n"
        long_msg += f"• 수익률: {ret_long*100:+.2f}%\n"

        if ret_long >= TAKE_PROFIT_RATIO:
            long_msg += "🚨 **[익절 발동] 전량 매도 청산 권장!**\n"
            any_triggered = True
    else:
        long_msg += "• 보유 물량 없음 (매수 대기)\n"

    long_msg += f"• LOC 매수 예정가: ${target_price_today:.2f}\n"
    discord_report.append(long_msg)

    # ====================== SHORT ======================
    print("🔵 [SHORT] 매도 계좌")
    shares_short = pos_cfg["TOTAL_SHARES_SHORT"]
    avg_short = pos_cfg["MY_AVG_PRICE_SHORT"]

    short_msg = "**🔵 [SHORT] 매도 계좌 현황**\n"
    if shares_short > 0 and avg_short > 0:
        ret_short = (current_price - avg_short) / avg_short
        tp_price = avg_short * (1 + TAKE_PROFIT_RATIO)

        short_msg += f"• 수량: {shares_short}주 / 평단: ${avg_short:.4f}\n"
        short_msg += f"• 수익률: {ret_short*100:+.2f}%\n"

        if ret_short >= TAKE_PROFIT_RATIO:
            short_msg += "🚨 **[익절 발동] 전량 매도 청산 권장!**\n"
            any_triggered = True

        short_msg += f"• 익절 지정가: ${tp_price:.2f}\n"
    else:
        short_msg += "• 보유 물량 없음\n"

    discord_report.append(short_msg)

    # ====================== 디스코드 전송 ======================
    full_content = "\n----------------------------------------\n".join(discord_report)
    full_content += f"\n\n• **VIX**: {current_vix:.2f}"

    if any_triggered:
        title = "🚨 [긴급] 익절 목표 돌파 - 전량 청산 권장"
    elif mode == "장중":
        title = "☀️ [장중 브리핑]"
    elif mode == "장전":
        title = "🌙 [장전 브리핑]"
    else:
        title = "🌆 [장후 브리핑]"

    send_discord_message(webhook_url, user_id, title, full_content)


# ===================================================================
if __name__ == "__main__":
    # yfinance는 여기서 import (필요할 때만)
    import yfinance as yf
    execute_dual_tactical_trader()