import os
import sys
import json
import numpy as np
import warnings
import requests
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

# 인코딩 설정 (GitHub Actions 호환)
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


def load_ledger():
    try:
        with open("ledger.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {
            "SOXL_LONG_BUY": [], "SOXL_LONG_SELL": [],
            "SOXL_SHORT_BUY": [], "SOXL_SHORT_SELL": []
        }


def save_config(cfg):
    try:
        with open("config.json", "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"⚠️ config.json 저장 실패: {e}")


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
        buys = ledger.get(buy_key, [])
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
# 시장 모드 판별 (장후 모드 제거)
# ===================================================================
def get_market_mode():
    now_ny = datetime.now(ZoneInfo("America/New_York"))
    hour = now_ny.hour + now_ny.minute / 60.0
    is_dst = now_ny.dst() != timedelta(0)

    print(f"🕒 뉴욕 현재 시각: {now_ny.strftime('%Y-%m-%d %H:%M:%S')} {'(EDT)' if is_dst else '(EST)'}")

    if 9.5 <= hour < 16.0:
        return "장중", now_ny
    else:
        return "장전", now_ny


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
# 자금 소진 속도에 따른 LOC 자동 조정
# ===================================================================
def adjust_loc_by_burn_rate(base_loc_price, cfg, mode):
    if mode != "장중":
        return base_loc_price

    pos = cfg["POSITIONS"]["SOXL"]
    total_cap = pos.get("TOTAL_CAPITAL_LONG", 0)
    if total_cap <= 0:
        return base_loc_price

    ledger = load_ledger()
    executed = sum(item.get("total_amount", 0) for item in ledger.get("SOXL_LONG_BUY", []))

    burn_rate = executed / total_cap
    today = datetime.now()
    month_progress = today.day / 30.5

    if burn_rate > month_progress * 1.25:           # 너무 빠름 → 보수적으로
        adj = 0.985
        print(f"⚠️ 자금 소진 빠름 → LOC를 {adj:.1%} 낮춤")
    elif burn_rate < month_progress * 0.70:         # 너무 느림 → 공격적으로
        adj = 1.015
        print(f"📈 자금 소진 느림 → LOC를 {adj:.1%} 높임")
    else:
        adj = 1.0
        print("📊 자금 소진 속도 정상")

    return round(base_loc_price * adj, 2)


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
    except Exception as e:
        print(f"❌ 디스코드 전송 실패: {e}")


# ===================================================================
# 메인 실행
# ===================================================================
def execute_dual_tactical_trader():
    mode, now_ny = get_market_mode()

    print("======================================================================")
    print(f"📡 sigma_position_manager.py  {MODE_EMOJI[mode]} {mode} 모드")
    print("======================================================================\n")

    cfg = load_config()
    update_casts_from_ledger(cfg)
    cfg = load_config()   # 갱신된 config 재로드

    pos_cfg = cfg["POSITIONS"]["SOXL"]
    vix_cfg = cfg["VIX_CONFIG"]["LONG"]

    webhook_url = os.environ.get("DISCORD_WEBHOOK") or cfg.get("DISCORD_WEBHOOK", "")
    user_id = os.environ.get("DISCORD_USER_ID") or cfg.get("DISCORD_USER_ID", "")

    SIGMA = vix_cfg.get("FIXED_SIGMA", 0.0460)
    TAKE_PROFIT_RATIO = vix_cfg.get("TAKE_PROFIT_RATIO", 0.30)

    current_vix, prev_close, current_open, current_price = get_market_data(mode)
    if current_vix is None:
        print("❌ 데이터를 가져올 수 없습니다.")
        return

    # LOC 기본 계산
    gap_ratio = (current_open - prev_close) / prev_close if prev_close != 0 else 0
    base_mult = (vix_cfg.get("MULT_EXTREME", 2.8) if current_vix >= vix_cfg.get("LEVEL_HIGH", 30) else
                 vix_cfg.get("MULT_FEAR", 2.65) if current_vix >= vix_cfg.get("LEVEL_LOW", 20) else
                 vix_cfg.get("MULT_NORMAL", 1.4))

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

    base_loc = current_open * np.exp(-SIGMA * adj_mult)

    # 자금 소진 속도에 따른 LOC 조정
    final_loc = adjust_loc_by_burn_rate(base_loc, cfg, mode)

    price_label = "현재가" if mode == "장중" else "전일 종가"

    print(f"📌 {price_label}     : ${current_price:.2f}")
    print(f"📌 당일 시가     : ${current_open:.2f}")
    print(f"📌 VIX           : {current_vix:.2f}")
    print(f"📌 LOC 예정가    : ${final_loc:.2f}\n")

    # ====================== Discord ======================
    discord_lines = []
    any_triggered = False

    # 헤더
    if mode == "장중":
        header = f"**현재가: ${current_price:.2f}**\n📍 당일 시가: **${current_open:.2f}** | VIX: {current_vix:.2f}\n{now_ny.strftime('%Y-%m-%d %H:%M %Z')}\n"
    else:
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
            long_msg += f"🚨 **[+30% 부분 익절] → {int(shares_long*0.5)}주 매도 권장 (50%)**\n"
            any_triggered = True
    else:
        long_msg += "• 보유 물량 없음\n"

    if mode == "장중":
        long_msg += f"• LOC 매수 예정가: **${final_loc:.2f}**"

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
            short_msg += f"🚨 **[+30% 부분 익절] → {int(shares_short*0.5)}주 매도 권장 (50%)**\n"
            any_triggered = True
        if mode == "장중":
            short_msg += f"• 익절 지정가: **${tp_price:.2f}**"
    else:
        short_msg += "• 보유 물량 없음"

    discord_lines.append(short_msg)

    full_content = "\n\n".join(discord_lines)
    title = (f"🚨 [부분 익절 발동] {MODE_EMOJI[mode]} {mode} 모드 - 50% 매도 권장"
             if any_triggered else
             f"{MODE_EMOJI[mode]} {mode} 모드 브리핑")

    send_discord(webhook_url, user_id, title, full_content)


if __name__ == "__main__":
    execute_dual_tactical_trader()