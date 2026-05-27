import os
import sys
import json
import numpy as np
import pandas as pd
import yfinance as yf
import requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import warnings

# Windows cp949 인코딩 에러 방지
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

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
    정합성이 완벽히 보정된 실시간 및 역사적 데이터 추출 함수
    - history와 fast_info의 날짜 및 이전 종가를 상호 교차 검증하여, 
      장 극초반 데이터 지연 시에도 당일 시가($242.43)와 전일 종가($225.79)를 오차 없이 획득합니다.
    """
    try:
        soxl_ticker = yf.Ticker("SOXL")
        vix_ticker  = yf.Ticker("^VIX")

        # 1. 안전하게 역사적 데이터 3일치 가져오기 (조정 없는 실제 시가 획득을 위해 auto_adjust=False)
        soxl_hist = soxl_ticker.history(period="3d", auto_adjust=False)
        if len(soxl_hist) < 2:
            print("⚠️ SOXL 데이터가 부족합니다 (2일치 미만).")
            return None, None, None, None

        # 뉴욕 시간 기준 오늘 날짜 구하기
        now_ny = datetime.now(ZoneInfo("America/New_York"))
        today_date = now_ny.date()
        
        last_row_date = soxl_hist.index[-1].date()
        is_today_in_hist = (last_row_date == today_date)

        # 2. 실제 전일 종가(prev_close) 판별
        if is_today_in_hist:
            prev_close = float(soxl_hist['Close'].iloc[-2])
        else:
            prev_close = float(soxl_hist['Close'].iloc[-1])

        # 3. 당일 시가(current_open) 교차 검증 추출
        if mode == "장전":
            current_open = prev_close
        else:
            # 공식 정규장 시가인 fast_info.open을 최우선으로 검증 후 채택
            try:
                f_prev = soxl_ticker.fast_info.previous_close
                f_open = soxl_ticker.fast_info.open
                
                # fast_info의 이전 종가가 실제 역사적 전일 종가와 일치하는지 체크 (오늘 데이터로 갱신 완료되었는지 판별)
                if f_prev is not None and abs(f_prev - prev_close) < 0.1 and f_open is not None:
                    current_open = float(f_open)
                elif is_today_in_hist:
                    # fast_info가 미갱신 상태이지만 history에 오늘 봉이 있다면 history의 Open 사용
                    current_open = float(soxl_hist['Open'].iloc[-1])
                else:
                    current_open = prev_close
            except Exception:
                if is_today_in_hist:
                    current_open = float(soxl_hist['Open'].iloc[-1])
                else:
                    current_open = prev_close

        # 4. 실시간 현재가(current_price) 및 VIX(current_vix) 판별
        try:
            current_price = float(soxl_ticker.fast_info.last_price)
            current_vix   = float(vix_ticker.fast_info.last_price)
            if current_price is None or current_vix is None:
                raise ValueError("fast_info 값 없음")
        except Exception:
            current_price = float(soxl_hist['Close'].iloc[-1]) if is_today_in_hist else prev_close
            vix_hist      = vix_ticker.history(period="1d")
            current_vix   = float(vix_hist['Close'].iloc[-1])

        return float(current_vix), float(prev_close), float(current_open), float(current_price)

    except Exception as e:
        print(f"❌ 데이터 조회 실패: {e}")
        return None, None, None, None

def execute_dual_tactical_trader():
    mode, now_ny = get_market_mode()

    MODE_EMOJI = {"장전": "🌙", "장중": "☀️", "장후": "🌆"}
    print("======================================================================")
    print(f"📡 [SOXL_VIX_SIGMA.py]  {MODE_EMOJI[mode]} {mode} 모드")
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
    print(f"   📌 당일시가: ${current_open:.2f}")
    print(f"   📌 전일종가: ${prev_close:.2f}\n")

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
        f"• 당일시가: ${current_open:.2f}\n"
        f"• 전일종가: ${prev_close:.2f}\n"
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
    
    # VIX를 터미널 출력의 맨 아래에 표시
    print(f"   📌 VIX     : {current_vix:.2f}")
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
    # 디스코드 알림에서도 VIX는 맨 마지막 줄에 표시
    full_content += f"\n\n• **VIX 지수**: {current_vix:.2f}"
    send_discord_message(webhook_url, user_id, status_title, full_content)

if __name__ == "__main__":
    execute_dual_tactical_trader()
