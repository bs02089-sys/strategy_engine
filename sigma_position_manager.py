import os
import json
import numpy as np
import pandas as pd
import yfinance as yf
import requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import warnings

warnings.filterwarnings('ignore', category=FutureWarning)

# ──────────────────────────────────────────────
# 뉴욕 시간 기준 실행 모드 판별
# ──────────────────────────────────────────────
def get_market_mode():
    now_ny = datetime.now(ZoneInfo("America/New_York"))
    hour   = now_ny.hour + now_ny.minute / 60
    if hour < 9.5:
        return "장전", now_ny
    elif hour < 16.0:
        return "장중", now_ny
    else:
        return "장후", now_ny

def load_config():
    with open("config.json", "r", encoding="utf-8") as f:
        return json.load(f)

def send_discord_message(webhook_url, user_id, title, content):
    if not webhook_url:
        print("⚠️ [디스코드 경고] 웹훅 URL을 시스템 환경변수(또는 컨피그)에서 찾을 수 없습니다.")
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
        res = requests.post(webhook_url, json=payload, timeout=10)
        if res.status_code == 204:
            print("🚀 [디스코드] 알림 전송 완료!")
        else:
            print(f"❌ 디스코드 응답 에러 (코드: {res.status_code})")
    except Exception:
        print("❌ 디스코드 네트워크 전송 실패")

def get_realtime_data(mode):
    """
    mode == '장중' : current_price / current_vix → fast_info.last_price (실시간)
    mode != '장중' : current_price / current_vix → history 종가 (전일 기준)
    """
    try:
        soxl_ticker = yf.Ticker("SOXL")
        vix_ticker  = yf.Ticker("^VIX")

        soxl_hist = soxl_ticker.history(period="2d")
        if len(soxl_hist) < 2:
            print("⚠️ SOXL 데이터가 부족합니다 (2일치 미만).")
            return None, None, None, None

        prev_close   = soxl_hist['Close'].iloc[-2]
        current_open = soxl_hist['Open'].iloc[-1]

        if mode == "장중":
            # ── 실시간 현재가 (fast_info 우선, 실패 시 history 폴백)
            try:
                current_price = soxl_ticker.fast_info.last_price
                current_vix   = vix_ticker.fast_info.last_price
                if current_price is None or current_vix is None:
                    raise ValueError("fast_info 값 없음")
                print("   📡 [데이터 소스] fast_info 실시간 현재가")
            except Exception:
                print("   ⚠️ fast_info 실패 → history 종가로 폴백")
                current_price = soxl_hist['Close'].iloc[-1]
                vix_hist      = vix_ticker.history(period="1d")
                current_vix   = vix_hist['Close'].iloc[-1]
        else:
            # ── 장전 / 장후 : history 전일 종가 사용
            current_price = soxl_hist['Close'].iloc[-1]
            vix_hist      = vix_ticker.history(period="1d")
            current_vix   = vix_hist['Close'].iloc[-1]
            print("   📋 [데이터 소스] history 전일 종가")

        return current_vix, prev_close, current_open, current_price

    except Exception as e:
        print(f"❌ 데이터 조회 실패: {e}")
        return None, None, None, None

def execute_dual_tactical_trader():

    mode, now_ny = get_market_mode()

    MODE_EMOJI = {"장전": "🌙", "장중": "☀️", "장후": "🌆"}
    print("======================================================================")
    print(f"📡 [SOXL_VIX_2YEAR_DUAL_TRADER.py]  {MODE_EMOJI[mode]} {mode} 모드")
    print(f"🕐 뉴욕 현지 시각: {now_ny.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print("🛡️ [시스템 연동] 파워셸 환경변수 기반 매수/매도 통합 관제탑")
    print("======================================================================\n")

    cfg     = load_config()
    pos_cfg = cfg["POSITIONS"]["SOXL"]
    vix_cfg = cfg["VIX_CONFIG"]["LONG"]

    webhook_url = os.environ.get("DISCORD_WEBHOOK") or cfg.get("DISCORD_WEBHOOK", "")
    user_id     = os.environ.get("DISCORD_USER_ID")  or cfg.get("DISCORD_USER_ID", "")

    SIGMA             = vix_cfg["FIXED_SIGMA"]
    MULT_NORMAL       = vix_cfg["MULT_NORMAL"]
    MULT_FEAR         = vix_cfg["MULT_FEAR"]
    MULT_EXTREME      = vix_cfg["MULT_EXTREME"]
    TAKE_PROFIT_RATIO = vix_cfg["TAKE_PROFIT_RATIO"]

    current_vix, prev_close, current_open, current_price = get_realtime_data(mode)
    if current_vix is None:
        return

    price_label = "현재가 (실시간)" if mode == "장중" else "전일 종가"
    print(f"   📌 {price_label}: ${current_price:.2f}")
    print(f"   📌 VIX     : {current_vix:.2f}")
    print(f"   📌 전일종가: ${prev_close:.2f}  |  당일시가: ${current_open:.2f}\n")

    gap_ratio = (current_open - prev_close) / prev_close if prev_close != 0 else 0

    base_mult = (
        MULT_EXTREME if current_vix >= cfg["VIX_CONFIG"]["LEVEL_HIGH"]
        else (MULT_FEAR if current_vix >= cfg["VIX_CONFIG"]["LEVEL_LOW"] else MULT_NORMAL)
    )

    if gap_ratio >= -0.03:   adj_mult = base_mult
    elif gap_ratio >= -0.05: adj_mult = 0.45
    elif gap_ratio >= -0.07: adj_mult = 0.25
    elif gap_ratio >= -0.10: adj_mult = 0.10
    else:                    adj_mult = 0.0

    target_price_today = current_open * np.exp(-SIGMA * adj_mult)

    discord_report = []
    any_triggered  = False

    # 디스코드 공통 헤더
    header_msg = (
        f"**{MODE_EMOJI[mode]} {mode} 모드** | {now_ny.strftime('%Y-%m-%d %H:%M %Z')}\n"
        f"• {price_label}: ${current_price:.2f}\n"
        f"• VIX: {current_vix:.2f}\n"
        f"• 전일종가: ${prev_close:.2f} | 당일시가: ${current_open:.2f}\n"
    )
    discord_report.append(header_msg)

    # ─────────────────────────────────────────────────────────────────────
    # 🟢 [PART 1] 매수 계좌 (LONG) — LOC 매수 전략
    # ─────────────────────────────────────────────────────────────────────
    print("🟢 [SECTION A] 자산 포지션 (LONG)")
    shares_long = pos_cfg["TOTAL_SHARES_LONG"]
    avg_long    = pos_cfg["MY_AVG_PRICE_LONG"]

    long_msg = "**🟢 [LONG] 매수 계좌 현황**\n"
    if shares_long > 0 and avg_long > 0:
        return_long = (current_price - avg_long) / avg_long
        print(f"   • 수량 : {shares_long} 주 / 평단가: ${avg_long:.4f}")
        print(f"   • 수익률 : {return_long*100:+.2f}%")
        long_msg += f"• 수량: {shares_long}주 / 평단가: ${avg_long:.4f}\n"
        long_msg += f"• 수익률: {return_long*100:+.2f}%\n"

        if return_long >= TAKE_PROFIT_RATIO:
            print("   🚨 [익절 발동] 목표 돌파! 전량 매도 청산하세요!")
            long_msg += "🚨 **[익절 발동] 전량 매도 청산 권장!**\n"
            any_triggered = True
    else:
        print("   • 보유 물량 없음 (매수 대기)")
        long_msg += "• 보유 물량 없음 (매수 대기)\n"

    print(f"   🎯 SOXL 매수 예정가 (LOC): ${target_price_today:.2f}")
    long_msg += f"• LOC: ${target_price_today:.2f}\n"
    discord_report.append(long_msg)
    print("----------------------------------------------------------------------")

    # ─────────────────────────────────────────────────────────────────────
    # 🔵 [PART 2] 매도 계좌 (SHORT) — 지정가 청산 전략
    # ─────────────────────────────────────────────────────────────────────
    print("🔵 [SECTION B] 처형 자산 포지션 (SHORT)")
    shares_short = pos_cfg["TOTAL_SHARES_SHORT"]
    avg_short    = pos_cfg["MY_AVG_PRICE_SHORT"]

    short_msg = "**🔵 [SHORT] 매도 계좌 현황**\n"
    if shares_short > 0 and avg_short > 0:
        return_short      = (current_price - avg_short) / avg_short
        take_profit_price = avg_short * (1 + TAKE_PROFIT_RATIO)

        print(f"   • 수량 : {shares_short} 주 / 평단가: ${avg_short:.4f}")
        print(f"   • 수익률 : {return_short*100:+.2f}%")
        print(f"   🎯 익절 지정가: ${take_profit_price:.2f}")
        short_msg += f"• 수량: {shares_short}주 / 평단가: ${avg_short:.4f}\n"
        short_msg += f"• 수익률: {return_short*100:+.2f}%\n"
        short_msg += f"• 익절 지정가: ${take_profit_price:.2f}\n"

        if return_short >= TAKE_PROFIT_RATIO:
            print("   🚨 [익절 발동] 목표 돌파! 전량 매도 청산하세요!")
            short_msg += "🚨 **[익절 발동] 전량 매도 청산 권장!**\n"
            any_triggered = True
    else:
        print("   • 보유 물량 없음")
        short_msg += "• 보유 물량 없음\n"

    discord_report.append(short_msg)
    print("======================================================================\n")

    # 디스코드 전송
    if any_triggered:
        status_title = "🚨 [관제탑 긴급 청산 명령] 목표 수익률 돌파!!"
    elif mode == "장중":
        status_title = "☀️ [관제탑 장중 브리핑]"
    elif mode == "장전":
        status_title = "🌙 [관제탑 장전 대기 브리핑]"
    else:
        status_title = "🌆 [관제탑 장후 브리핑]"

    full_content = "\n----------------------------------------\n".join(discord_report)
    send_discord_message(webhook_url, user_id, status_title, full_content)

if __name__ == "__main__":
    execute_dual_tactical_trader()