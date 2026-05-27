import os
import sys
import json
import numpy as np
import warnings
import requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

warnings.filterwarnings('ignore', category=FutureWarning)


def load_config():
    with open("config.json", "r", encoding="utf-8") as f:
        return json.load(f)


def get_market_mode():
    now_ny = datetime.now(ZoneInfo("America/New_York"))
    hour = now_ny.hour + now_ny.minute / 60.0
    if hour < 9.5:
        return "장전", now_ny
    elif hour < 16.0:
        return "장중", now_ny
    else:
        return "장후", now_ny


def get_realtime_data(mode):
    try:
        import yfinance as yf
        soxl = yf.Ticker("SOXL")
        vix = yf.Ticker("^VIX")
        hist = soxl.history(period="3d", auto_adjust=False)

        if len(hist) < 2:
            return None, None, None, None

        now_ny = datetime.now(ZoneInfo("America/New_York"))
        is_today = (hist.index[-1].date() == now_ny.date())

        prev_close = float(hist['Close'].iloc[-2] if is_today else hist['Close'].iloc[-1])

        if mode == "장전":
            current_open = prev_close
        else:
            current_open = float(getattr(soxl.fast_info, 'open', None) or 
                               (hist['Open'].iloc[-1] if is_today else prev_close))

        current_price = float(soxl.fast_info.last_price)
        current_vix = float(vix.fast_info.last_price)

        return float(current_vix), float(prev_close), float(current_open), float(current_price)
    except:
        return None, None, None, None


def send_discord_message(webhook_url, user_id, title, content):
    if not webhook_url:
        print("⚠️ webhook_url 없음")
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
        print("✅ 디스코드 전송 성공" if res.status_code == 204 else f"❌ 전송 실패 ({res.status_code})")
    except Exception as e:
        print(f"❌ 디스코드 오류: {e}")


# ===================================================================
def execute_dual_tactical_trader():
    mode, now_ny = get_market_mode()
    MODE_EMOJI = {"장전": "🌙", "장중": "☀️", "장후": "🌆"}

    print("======================================================================")
    print(f"📡 SOXL_VIX_SIGMA.py  {MODE_EMOJI[mode]} {mode} 모드")
    print(f"🕒 {now_ny.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print("======================================================================\n")

    cfg = load_config()
    pos_cfg = cfg["POSITIONS"]["SOXL"]
    vix_cfg = cfg["VIX_CONFIG"]["LONG"]

    webhook_url = os.environ.get("DISCORD_WEBHOOK") or cfg.get("DISCORD_WEBHOOK", "")
    user_id = os.environ.get("DISCORD_USER_ID") or cfg.get("DISCORD_USER_ID", "")

    SIGMA = vix_cfg["FIXED_SIGMA"]
    MULT_NORMAL = vix_cfg["MULT_NORMAL"]
    MULT_FEAR = vix_cfg["MULT_FEAR"]
    MULT_EXTREME = vix_cfg["MULT_EXTREME"]
    TAKE_PROFIT_RATIO = vix_cfg["TAKE_PROFIT_RATIO"]

    current_vix, prev_close, current_open, current_price = get_realtime_data(mode)
    if current_vix is None:
        print("❌ 데이터 조회 실패")
        return

    price_label = "현재가 (실시간)" if mode == "장중" else "전일 종가"

    # LOC 매수가 계산
    gap_ratio = (current_open - prev_close) / prev_close if prev_close != 0 else 0
    base_mult = (MULT_EXTREME if current_vix >= cfg["VIX_CONFIG"].get("LEVEL_HIGH", 30) else
                 MULT_FEAR if current_vix >= cfg["VIX_CONFIG"].get("LEVEL_LOW", 20) else MULT_NORMAL)
    
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

    print(f"📌 {price_label}: ${current_price:.2f}")
    print(f"📌 당일시가 : ${current_open:.2f}")
    print(f"📌 전일종가 : ${prev_close:.2f}")
    print(f"📌 VIX       : {current_vix:.2f}\n")

    discord_report = []
    any_triggered = False

    # ====================== LONG ======================
    shares_long = pos_cfg.get("TOTAL_SHARES_LONG", 0)
    avg_long = pos_cfg.get("MY_AVG_PRICE_LONG", 0)

    long_msg = "**🟢 [LONG] 매수 계좌 현황**\n"
    if shares_long > 0 and avg_long > 0:
        ret_long = (current_price - avg_long) / avg_long
        long_msg += f"• 수량: {shares_long}주 / 평단: ${avg_long:.4f}\n"
        long_msg += f"• 수익률: {ret_long*100:+.2f}%\n"

        if ret_long >= TAKE_PROFIT_RATIO:
            sell_shares = int(shares_long * 0.5)
            long_msg += f"🚨 **[+30% 부분 익절] → {sell_shares}주 매도 권장 (50%)**\n"
            any_triggered = True
    else:
        long_msg += "• 보유 물량 없음\n"

    long_msg += f"• LOC 매수 예정가: ${target_price_today:.2f}\n"   # ← 항상 표시
    discord_report.append(long_msg)

    # ====================== SHORT ======================
    shares_short = pos_cfg.get("TOTAL_SHARES_SHORT", 0)
    avg_short = pos_cfg.get("MY_AVG_PRICE_SHORT", 0)

    short_msg = "**🔵 [SHORT] 매도 계좌 현황**\n"
    if shares_short > 0 and avg_short > 0:
        ret_short = (current_price - avg_short) / avg_short
        tp_price = avg_short * (1 + TAKE_PROFIT_RATIO)
        short_msg += f"• 수량: {shares_short}주 / 평단: ${avg_short:.4f}\n"
        short_msg += f"• 수익률: {ret_short*100:+.2f}%\n"

        if ret_short >= TAKE_PROFIT_RATIO:
            sell_shares_s = int(shares_short * 0.5)
            short_msg += f"🚨 **[+30% 부분 익절] → {sell_shares_s}주 매도 권장 (50%)**\n"
            any_triggered = True

        short_msg += f"• 익절 지정가: ${tp_price:.2f}\n"   # ← 항상 표시
    else:
        short_msg += "• 보유 물량 없음\n"
        tp_price = 0  # dummy
        short_msg += f"• 익절 지정가: ${avg_short * (1 + TAKE_PROFIT_RATIO):.2f} (참고용)\n" if avg_short > 0 else "• 익절 지정가: - (보유중 아님)\n"

    discord_report.append(short_msg)

    # 디스코드 전송
    full_content = "\n----------------------------------------\n".join(discord_report)
    full_content += f"\n\n• **VIX**: {current_vix:.2f}"

    if any_triggered:
        title = "🚨 [부분 익절 발동] +30% 도달 - 50% 매도 권장"
    else:
        title = f"{MODE_EMOJI[mode]} [장중 브리핑]"

    send_discord_message(webhook_url, user_id, title, full_content)


if __name__ == "__main__":
    execute_dual_tactical_trader()