import os
import sys
import json
import numpy as np
import warnings
import requests
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

# Windows / GitHub Actions 인코딩 문제 방지
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

warnings.filterwarnings('ignore', category=FutureWarning)

MODE_EMOJI = {"장전": "🌙", "장중": "☀️"}


# ===================================================================
# 설정 및 장부 로드
# ===================================================================
def load_config():
    try:
        with open("config.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"❌ config.json 로드 실패: {e}")
        sys.exit(1)


def save_config(cfg):
    try:
        with open("config.json", "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"⚠️ config.json 저장 실패: {e}")


def load_ledger():
    try:
        with open("ledger.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {
            "SOXL_LONG_BUY": [], "SOXL_LONG_SELL": [],
            "SOXL_SHORT_BUY": [], "SOXL_SHORT_SELL": []
        }


# ===================================================================
# ledger → config.json 자동 동기화
# ===================================================================
def update_positions_from_ledger(cfg):
    ledger = load_ledger()
    pos = cfg.setdefault("POSITIONS", {}).setdefault("SOXL", {})

    for key, buy_key, sell_key in [
        ("LONG",  "SOXL_LONG_BUY",  "SOXL_LONG_SELL"),
        ("SHORT", "SOXL_SHORT_BUY", "SOXL_SHORT_SELL"),
    ]:
        buys  = ledger.get(buy_key, [])
        sells = ledger.get(sell_key, [])

        total_buy_qty = sum(item.get("qty", 0) for item in buys)
        total_sell_qty = sum(item.get("qty", 0) for item in sells)
        hold_qty = max(total_buy_qty - total_sell_qty, 0)

        total_buy_amt = sum(item.get("total_amount", 0) for item in buys)
        avg_price = round(total_buy_amt / total_buy_qty, 4) if total_buy_qty > 0 else 0.0

        pos[f"CURRENT_CASTS_{key}"] = len(buys)
        pos[f"TOTAL_SHARES_{key}"] = hold_qty
        pos[f"MY_AVG_PRICE_{key}"] = avg_price

        print(f"   📒 [{key}] {len(buys)}회 매수 | 보유 {hold_qty}주 | 평단 ${avg_price:.4f}")

    save_config(cfg)


# ===================================================================
# 시장 모드 판별 (서머타임 자동 적용)
# ===================================================================
def get_market_mode():
    now_ny = datetime.now(ZoneInfo("America/New_York"))
    hour = now_ny.hour + now_ny.minute / 60.0
    is_dst = now_ny.dst() != timedelta(0)
    tz_label = "EDT (서머타임)" if is_dst else "EST"

    print(f"🕒 뉴욕 현재 시각: {now_ny.strftime('%Y-%m-%d %H:%M:%S')} ({tz_label})")

    # 정규장: 09:30 ~ 16:00
    if 9.5 <= hour < 16.0:
        return "장중", now_ny
    else:
        return "장전", now_ny   # 애프터마켓 + 밤 + 프리마켓 모두 장전으로 통합


# ===================================================================
# 시세 데이터 조회
# ===================================================================
def get_market_data(mode):
    try:
        import yfinance as yf
        soxl = yf.Ticker("SOXL")
        vix = yf.Ticker("^VIX")
        hist = soxl.history(period="3d", auto_adjust=False)

        if len(hist) < 1:
            print("⚠️ SOXL 데이터 부족")
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
        print(f"❌ 시세 조회 실패: {e}")
        return None, None, None, None


# ===================================================================
# 디스코드 전송
# ===================================================================
def send_discord(webhook_url, user_id, title, content):
    if not webhook_url:
        print("⚠️ DISCORD_WEBHOOK이 설정되지 않았습니다.")
        return

    payload = {
        "content": f"<@{user_id}> " if user_id else "",
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
            print(f"❌ 디스코드 응답: {res.status_code}")
    except Exception as e:
        print(f"❌ 디스코드 전송 실패: {e}")


# ===================================================================
# LOC 매수 타점 계산
# ===================================================================
def calc_loc(current_open, prev_close, current_vix, vix_cfg, SIGMA):
    gap_ratio = (current_open - prev_close) / prev_close if prev_close != 0 else 0

    base_mult = (
        vix_cfg.get("MULT_EXTREME", 2.80) if current_vix >= vix_cfg.get("LEVEL_HIGH", 30) else
        vix_cfg.get("MULT_FEAR",    2.65) if current_vix >= vix_cfg.get("LEVEL_LOW",  20) else
        vix_cfg.get("MULT_NORMAL",  1.40)
    )

    if   gap_ratio >= -0.03: adj_mult = base_mult
    elif gap_ratio >= -0.05: adj_mult = 0.45
    elif gap_ratio >= -0.07: adj_mult = 0.25
    elif gap_ratio >= -0.10: adj_mult = 0.10
    else:                    adj_mult = 0.0

    loc_price = current_open * np.exp(-SIGMA * adj_mult)
    return loc_price, gap_ratio, base_mult, adj_mult


# ===================================================================
# 메인 실행
# ===================================================================
def execute_dual_tactical_trader():
    mode, now_ny = get_market_mode()

    print("======================================================================")
    print(f"📡 sigma_position_manager.py  {MODE_EMOJI[mode]} {mode} 모드")
    print("======================================================================\n")

    cfg = load_config()
    print("📒 ledger.json → config.json 자동 갱신 중...")
    update_positions_from_ledger(cfg)
    cfg = load_config()                    # 갱신된 config 재로드

    pos_cfg = cfg["POSITIONS"]["SOXL"]
    vix_cfg = cfg["VIX_CONFIG"]["LONG"]

    webhook_url = os.environ.get("DISCORD_WEBHOOK") or cfg.get("DISCORD_WEBHOOK", "")
    user_id     = os.environ.get("DISCORD_USER_ID") or cfg.get("DISCORD_USER_ID", "")

    SIGMA = vix_cfg.get("FIXED_SIGMA", 0.0460)
    TAKE_PROFIT_RATIO = vix_cfg.get("TAKE_PROFIT_RATIO", 0.30)

    current_vix, prev_close, current_open, current_price = get_market_data(mode)
    if current_vix is None:
        print("❌ 시세 데이터를 가져올 수 없습니다.")
        return

    loc_price, gap_ratio, base_mult, adj_mult = calc_loc(
        current_open, prev_close, current_vix, vix_cfg, SIGMA
    )

    price_label = "현재가" if mode == "장중" else "전일 종가"

    print(f"📌 {price_label}: ${current_price:.2f}")
    print(f"📌 당일 시가 : ${current_open:.2f}")
    print(f"📌 전일 종가 : ${prev_close:.2f}")
    print(f"📌 VIX       : {current_vix:.2f}\n")

    # ====================== Discord 메시지 구성 ======================
    discord_lines = []
    any_triggered = False

    header = f"**{price_label}: ${current_price:.2f}**\n{now_ny.strftime('%Y-%m-%d %H:%M %Z')}\n"
    discord_lines.append(header)

    # LONG
    shares_long = pos_cfg.get("TOTAL_SHARES_LONG", 0)
    avg_long = pos_cfg.get("MY_AVG_PRICE_LONG", 0)

    long_msg = "**🟢 [LONG] 매수 계좌**\n"
    if shares_long > 0 and avg_long > 0:
        ret = (current_price - avg_long) / avg_long
        long_msg += f"• {shares_long}주 / 평단 ${avg_long:.4f} / 수익률 {ret*100:+.2f}%\n"
        if ret >= TAKE_PROFIT_RATIO:
            sell_qty = int(shares_long * 0.5)
            long_msg += f"🚨 **[+30% 부분 익절] → {sell_qty}주 매도 권장 (50%)**\n"
            any_triggered = True
    else:
        long_msg += "• 보유 물량 없음\n"

    if mode == "장중":
        long_msg += f"• LOC 매수 예정가: **${loc_price:.2f}**"

    discord_lines.append(long_msg)

    # SHORT
    shares_short = pos_cfg.get("TOTAL_SHARES_SHORT", 0)
    avg_short = pos_cfg.get("MY_AVG_PRICE_SHORT", 0)

    short_msg = "**🔵 [SHORT] 매도 계좌**\n"
    if shares_short > 0 and avg_short > 0:
        ret = (current_price - avg_short) / avg_short
        tp_price = avg_short * (1 + TAKE_PROFIT_RATIO)
        short_msg += f"• {shares_short}주 / 평단 ${avg_short:.4f} / 수익률 {ret*100:+.2f}%\n"
        if ret >= TAKE_PROFIT_RATIO:
            sell_qty = int(shares_short * 0.5)
            short_msg += f"🚨 **[+30% 부분 익절] → {sell_qty}주 매도 권장 (50%)**\n"
            any_triggered = True
        if mode == "장중":
            short_msg += f"• 익절 지정가: **${tp_price:.2f}**"
    else:
        short_msg += "• 보유 물량 없음"

    discord_lines.append(short_msg)

    # 전송
    full_content = "\n\n".join(discord_lines)
    title = (f"🚨 [부분 익절 발동] {MODE_EMOJI[mode]} {mode} 모드 - 50% 매도 권장"
             if any_triggered else
             f"{MODE_EMOJI[mode]} {mode} 모드 브리핑")

    send_discord(webhook_url, user_id, title, full_content)


if __name__ == "__main__":
    execute_dual_tactical_trader()