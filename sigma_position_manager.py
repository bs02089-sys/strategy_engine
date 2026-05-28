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
    try:
        with open("config.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"❌ config.json 로드 실패: {e}")
        sys.exit(1)


def load_ledger():
    try:
        with open("ledger.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {"SOXL_LONG_BUY": [], "SOXL_SHORT_BUY": []}


def update_casts_from_ledger(cfg):
    ledger = load_ledger()
    long_casts = len(ledger.get("SOXL_LONG_BUY", []))
    short_casts = len(ledger.get("SOXL_SHORT_BUY", []))

    pos = cfg.setdefault("POSITIONS", {}).setdefault("SOXL", {})
    pos["CURRENT_CASTS_LONG"] = long_casts
    pos["CURRENT_CASTS_SHORT"] = short_casts

    try:
        with open("config.json", "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4, ensure_ascii=False)
        print(f"✅ CAST 업데이트 → LONG: {long_casts}회 | SHORT: {short_casts}회")
    except:
        pass


def get_market_mode():
    now_ny = datetime.now(ZoneInfo("America/New_York"))
    hour = now_ny.hour + now_ny.minute / 60.0
    if hour < 9.5:
        return "장전", now_ny
    else:
        return "장중", now_ny   # 장후 모드 제거


def get_realtime_data(mode):
    try:
        import yfinance as yf
        soxl = yf.Ticker("SOXL")
        vix = yf.Ticker("^VIX")
        hist = soxl.history(period="3d", auto_adjust=False)

        if len(hist) < 1:
            return None, None, None, None

        now_ny = datetime.now(ZoneInfo("America/New_York"))
        is_today = (hist.index[-1].date() == now_ny.date())

        prev_close = float(hist['Close'].iloc[-2] if is_today and len(hist) > 1 else hist['Close'].iloc[-1])

        if mode == "장전":
            current_open = prev_close
        else:
            current_open = float(getattr(soxl.fast_info, 'open', None) or 
                               (hist['Open'].iloc[-1] if is_today else prev_close))

        current_price = float(soxl.fast_info.last_price)
        current_vix = float(vix.fast_info.last_price)

        return float(current_vix), float(prev_close), float(current_open), float(current_price)
    except Exception as e:
        print(f"❌ 데이터 조회 실패: {e}")
        return None, None, None, None


def send_discord_message(webhook_url, user_id, title, content):
    if not webhook_url:
        print("⚠️ DISCORD_WEBHOOK이 설정되지 않았습니다.")
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
        print(f"📡 Discord 응답: {res.status_code}")
    except Exception as e:
        print(f"❌ Discord 전송 실패: {e}")


# ===================================================================
def execute_dual_tactical_trader():
    mode, now_ny = get_market_mode()
    MODE_EMOJI = {"장전": "🌙", "장중": "☀️"}

    print("======================================================================")
    print(f"📡 SOXL_VIX_SIGMA.py  {MODE_EMOJI[mode]} {mode} 모드")
    print(f"🕒 {now_ny.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print("======================================================================\n")

    cfg = load_config()
    update_casts_from_ledger(cfg)

    pos_cfg = cfg["POSITIONS"]["SOXL"]
    vix_cfg = cfg["VIX_CONFIG"]["LONG"]

    webhook_url = os.environ.get("DISCORD_WEBHOOK") or cfg.get("DISCORD_WEBHOOK", "")
    user_id = os.environ.get("DISCORD_USER_ID") or cfg.get("DISCORD_USER_ID", "")

    SIGMA = vix_cfg.get("FIXED_SIGMA", 0.046)
    MULT_NORMAL = vix_cfg.get("MULT_NORMAL", 1.4)
    MULT_FEAR = vix_cfg.get("MULT_FEAR", 2.7)
    MULT_EXTREME = vix_cfg.get("MULT_EXTREME", 2.8)
    TAKE_PROFIT_RATIO = vix_cfg.get("TAKE_PROFIT_RATIO", 0.30)

    current_vix, prev_close, current_open, current_price = get_realtime_data(mode)
    if current_vix is None:
        print("❌ 데이터를 가져올 수 없습니다.")
        return

    gap_ratio = (current_open - prev_close) / prev_close if prev_close != 0 else 0
    base_mult = (MULT_EXTREME if current_vix >= vix_cfg.get("LEVEL_HIGH", 30) else
                 MULT_FEAR if current_vix >= vix_cfg.get("LEVEL_LOW", 20) else MULT_NORMAL)

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
    price_label = "현재가" if mode == "장중" else "전일 종가"

    print(f"📌 {price_label}: ${current_price:.2f}")
    print(f"📌 당일시가 : ${current_open:.2f}")
    print(f"📌 전일종가 : ${prev_close:.2f}")
    print(f"📌 VIX       : {current_vix:.2f}\n")

    discord_report = []
    any_triggered = False

    header = f"**{price_label}: ${current_price:.2f}**\n{now_ny.strftime('%Y-%m-%d %H:%M %Z')}\n"
    discord_report.append(header)

    # LONG
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

    if mode == "장중":
        long_msg += f"• LOC 매수 예정가: ${target_price_today:.2f}\n"

    discord_report.append(long_msg)

    # SHORT
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

        if mode == "장중":
            short_msg += f"• 익절 지정가: ${tp_price:.2f}\n"
    else:
        short_msg += "• 보유 물량 없음\n"

    discord_report.append(short_msg)

    full_content = "\n----------------------------------------\n".join(discord_report)
    full_content += f"\n\n• **VIX**: {current_vix:.2f}"

    if any_triggered:
        title = f"🚨 [부분 익절 발동] {MODE_EMOJI[mode]} {mode} 모드 - 50% 매도 권장"
    else:
        title = f"{MODE_EMOJI[mode]} {mode} 모드 브리핑"

    send_discord_message(webhook_url, user_id, title, full_content)


if __name__ == "__main__":
    execute_dual_tactical_trader()