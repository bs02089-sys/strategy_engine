# =======================================================================
# sigma_position_manager.py
# VIX 시그마 전략 기반 SOXL 듀얼 계좌 관제탑
# - 장전: 전일 종가 기준 브리핑
# - 장중: 실시간 현재가 + LOC 매수가 + 익절 지정가 브리핑
# - ledger.json 기반 수량/평단가 자동 갱신
# =======================================================================

import os
import sys
import json
import numpy as np
import warnings
import requests
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

# ── 인코딩 설정 (GitHub Actions 한글 깨짐 방지)
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

warnings.filterwarnings('ignore', category=FutureWarning)

MODE_EMOJI = {"장전": "🌙", "장중": "☀️"}

# ───────────────────────────────────────────────
# 설정 / 장부 로드
# ───────────────────────────────────────────────

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
            "SOXL_LONG":  {"qty": 0, "avg_price": 0.0},
            "SOXL_SHORT": {"qty": 0, "avg_price": 0.0},
        }

# ───────────────────────────────────────────────
# 장부 → config.json 자동 갱신
# ───────────────────────────────────────────────

def update_positions_from_ledger(cfg):
    """
    ledger.json 기반으로 config.json 포지션 자동 갱신
      - TOTAL_SHARES : ledger의 qty 값 직접 반영
      - MY_AVG_PRICE : ledger의 avg_price 값 직접 반영
    ledger.json 구조:
    {
        "SOXL_LONG":  {"qty": 17, "avg_price": 165.1124},
        "SOXL_SHORT": {"qty": 4,  "avg_price": 164.3043}
    }
    """
    ledger = load_ledger()
    pos    = cfg.setdefault("POSITIONS", {}).setdefault("SOXL", {})

    for key, ledger_key in [("LONG", "SOXL_LONG"), ("SHORT", "SOXL_SHORT")]:
        entry     = ledger.get(ledger_key, {})
        qty       = entry.get("qty",       0)
        avg_price = entry.get("avg_price", 0.0)

        pos[f"TOTAL_SHARES_{key}"] = qty
        pos[f"MY_AVG_PRICE_{key}"] = avg_price

        print(f"   📒 [{key}] 보유 {qty}주 / 평단 ${avg_price:.4f}")

    save_config(cfg)

# ───────────────────────────────────────────────
# 장 시간 판별 (뉴욕 기준)
# ───────────────────────────────────────────────

def get_market_mode():
    now_ny   = datetime.now(ZoneInfo("America/New_York"))
    hour     = now_ny.hour + now_ny.minute / 60.0
    is_dst   = now_ny.dst() != timedelta(0)
    tz_label = "EDT (서머타임)" if is_dst else "EST"

    print(f"🕒 뉴욕 현재 시각: {now_ny.strftime('%Y-%m-%d %H:%M:%S')} ({tz_label})")

    mode = "장중" if 9.5 <= hour < 16.0 else "장전"
    return mode, now_ny

# ───────────────────────────────────────────────
# 시세 데이터 조회
# ───────────────────────────────────────────────

def get_market_data(mode):
    """
    장중 : current_price / current_vix → fast_info 실시간
           current_open               → history 시가 (fast_info.open 오류 방지)
    장전 : current_price / current_vix → fast_info (전일 종가 수준)
           current_open               → prev_close 동일 적용
    반환 : (current_vix, prev_close, current_open, current_price)
    """
    try:
        import yfinance as yf

        soxl = yf.Ticker("SOXL")
        vix  = yf.Ticker("^VIX")
        hist = soxl.history(period="3d", auto_adjust=False)

        if len(hist) < 2:
            print("⚠️ SOXL 데이터 부족 (3d 기준 2일치 미만)")
            return None, None, None, None

        now_ny   = datetime.now(ZoneInfo("America/New_York"))
        is_today = (hist.index[-1].date() == now_ny.date())

        # 전일 종가
        prev_close = float(
            hist['Close'].iloc[-2] if is_today else hist['Close'].iloc[-1]
        )

        # 당일 시가: history 우선 (fast_info.open은 간헐적으로 오류값 반환)
        if mode == "장중":
            current_open = float(hist['Open'].iloc[-1] if is_today else prev_close)
        else:
            current_open = prev_close

        # 실시간 현재가 / VIX
        current_price = float(soxl.fast_info.last_price)
        current_vix   = float(vix.fast_info.last_price)

        return current_vix, prev_close, current_open, current_price

    except Exception as e:
        print(f"❌ 시세 조회 실패: {e}")
        return None, None, None, None

# ───────────────────────────────────────────────
# 디스코드 알림 전송
# ───────────────────────────────────────────────

def send_discord(webhook_url, user_id, title, content):
    if not webhook_url:
        print("⚠️ DISCORD_WEBHOOK이 설정되지 않았습니다.")
        return

    payload = {
        "content": f"<@{user_id}> " if user_id else "",
        "embeds": [{
            "title":       title,
            "description": content,
            "color":       15158332 if "🚨" in title else 3447003,
            "timestamp":   datetime.now(timezone.utc).isoformat()
        }]
    }
    try:
        res = requests.post(webhook_url, json=payload, timeout=15)
        if res.status_code == 204:
            print("✅ 디스코드 알림 전송 성공")
        else:
            print(f"❌ 디스코드 응답 에러 (코드: {res.status_code})")
    except Exception as e:
        print(f"❌ 디스코드 전송 실패: {e}")

# ───────────────────────────────────────────────
# LOC 매수 타점 계산
# ───────────────────────────────────────────────

def calc_loc(current_open, prev_close, current_vix, SIGMA):
    """
    타점 = 기준가 × exp(-SIGMA × 조정배수)
    조정배수 = VIX 구간 배수 + 갭 하락 보정
    """
    gap_ratio = (current_open - prev_close) / prev_close if prev_close != 0 else 0

    if   current_vix >= 30: base_mult = 2.80  # 극단 공포
    elif current_vix >= 20: base_mult = 2.65  # 공포
    else:                   base_mult = 1.40  # 평시

    if   gap_ratio >= -0.03: adj_mult = base_mult
    elif gap_ratio >= -0.05: adj_mult = 0.45
    elif gap_ratio >= -0.07: adj_mult = 0.25
    elif gap_ratio >= -0.10: adj_mult = 0.10
    else:                    adj_mult = 0.0

    return current_open * np.exp(-SIGMA * adj_mult), gap_ratio, base_mult, adj_mult

# ───────────────────────────────────────────────
# 메인 실행
# ───────────────────────────────────────────────

def execute_dual_tactical_trader():
    mode, now_ny = get_market_mode()

    print("======================================================================")
    print(f"📡 sigma_position_manager.py  {MODE_EMOJI[mode]} {mode} 모드")
    print("======================================================================\n")

    # ── 설정 로드 및 장부 갱신
    cfg = load_config()
    print("📒 장부 → config.json 갱신 중...")
    update_positions_from_ledger(cfg)
    cfg = load_config()  # 갱신된 값 재로드

    pos_cfg = cfg["POSITIONS"]["SOXL"]

    webhook_url       = os.environ.get("DISCORD_WEBHOOK") or cfg.get("DISCORD_WEBHOOK", "")
    user_id           = os.environ.get("DISCORD_USER_ID")  or cfg.get("DISCORD_USER_ID", "")
    SIGMA             = pos_cfg.get("FIXED_SIGMA",        0.0460)
    TAKE_PROFIT_RATIO = pos_cfg.get("TAKE_PROFIT_RATIO",  0.30)

    # ── 시세 조회
    current_vix, prev_close, current_open, current_price = get_market_data(mode)
    if current_vix is None:
        print("❌ 시세 데이터를 가져올 수 없습니다. 종료합니다.")
        return

    # ── LOC 계산
    loc_price, gap_ratio, base_mult, adj_mult = calc_loc(
        current_open, prev_close, current_vix, SIGMA
    )

    price_label = "현재가" if mode == "장중" else "전일 종가"

    print(f"\n📌 {price_label}  : ${current_price:.2f}")
    print(f"📌 당일 시가    : ${current_open:.2f}")
    print(f"📌 전일 종가    : ${prev_close:.2f}")
    print(f"📌 VIX          : {current_vix:.2f}")
    print(f"📌 갭 비율      : {gap_ratio*100:+.2f}%")
    print(f"📌 VIX 배수     : {base_mult:.2f}  →  조정 배수: {adj_mult:.2f}")
    print(f"📌 LOC 매수가   : ${loc_price:.2f}\n")

    discord_lines = []
    any_triggered = False

    # ── 디스코드 헤더
    discord_lines.append(
        f"**{MODE_EMOJI[mode]} {mode} 모드** | {now_ny.strftime('%Y-%m-%d %H:%M %Z')}\n"
        f"• {price_label}: **${current_price:.2f}**\n"
        f"• VIX: {current_vix:.2f} | 시가: ${current_open:.2f} | 전일종가: ${prev_close:.2f}"
    )

    # ── 🟢 LONG 계좌
    shares_long = pos_cfg.get("TOTAL_SHARES_LONG", 0)
    avg_long    = pos_cfg.get("MY_AVG_PRICE_LONG",  0)

    long_msg = "**🟢 [LONG] 매수 계좌**\n"
    if shares_long > 0 and avg_long > 0:
        ret_long = (current_price - avg_long) / avg_long
        long_msg += f"• {shares_long}주 / 평단 ${avg_long:.4f} / 수익률 {ret_long*100:+.2f}%\n"
        if ret_long >= TAKE_PROFIT_RATIO:
            long_msg += f"🚨 **[+30% 익절 발동] → {int(shares_long * 0.5)}주 매도 권장 (50%)**\n"
            any_triggered = True
    else:
        long_msg += "• 보유 물량 없음\n"

    if mode == "장중":
        long_msg += f"• LOC 매수 예정가: **${loc_price:.2f}**"

    discord_lines.append(long_msg)

    # ── 🔵 SHORT (매도) 계좌
    shares_short = pos_cfg.get("TOTAL_SHARES_SHORT", 0)
    avg_short    = pos_cfg.get("MY_AVG_PRICE_SHORT",  0)

    short_msg = "**🔵 [SHORT] 매도 계좌**\n"
    if shares_short > 0 and avg_short > 0:
        ret_short = (current_price - avg_short) / avg_short
        tp_price  = avg_short * (1 + TAKE_PROFIT_RATIO)
        short_msg += f"• {shares_short}주 / 평단 ${avg_short:.4f} / 수익률 {ret_short*100:+.2f}%\n"
        if ret_short >= TAKE_PROFIT_RATIO:
            short_msg += f"🚨 **[+30% 익절 발동] → {int(shares_short * 0.5)}주 매도 권장 (50%)**\n"
            any_triggered = True
        if mode == "장중":
            short_msg += f"• 익절 지정가: **${tp_price:.2f}**"
    else:
        short_msg += "• 보유 물량 없음"

    discord_lines.append(short_msg)

    # ── 디스코드 전송
    full_content = "\n\n".join(discord_lines)
    title = (
        f"🚨 [익절 발동] {MODE_EMOJI[mode]} {mode} - 50% 매도 권장"
        if any_triggered else
        f"{MODE_EMOJI[mode]} {mode} 브리핑"
    )
    send_discord(webhook_url, user_id, title, full_content)


if __name__ == "__main__":
    execute_dual_tactical_trader()
