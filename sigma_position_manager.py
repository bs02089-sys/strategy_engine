import os
import json
import numpy as np
import pandas as pd
import yfinance as yf
import requests
from datetime import datetime
import warnings

warnings.filterwarnings('ignore', category=FutureWarning)

def load_config():
    with open("config.json", "r", encoding="utf-8") as f:
        return json.load(f)

def send_discord_message(webhook_url, user_id, title, content):
    """디스코드 채널로 실전 작전 명령 및 멘션 알림 전송"""
    if not webhook_url:
        return  # 웹훅 주소가 비어있으면 전송 스킵
        
    mention = f"<@{user_id}> " if user_id else ""
    payload = {
        "content": mention,
        "embeds": [{
            "title": title,
            "description": content,
            "color": 15158332 if "🚨" in title else 3447003, # 경보는 빨간색, 평시는 파란색
            "timestamp": datetime.utcnow().isoformat()
        }]
    }
    try:
        requests.post(webhook_url, json=payload, timeout=10)
    except Exception as e:
        print(f"❌ 디스코드 알림 전송 실패: {e}")

def get_realtime_data():
    try:
        soxl = yf.Ticker("SOXL").history(period="2d")
        vix = yf.Ticker("^VIX").history(period="1d")
        
        if soxl.empty or vix.empty:
            return None, None, None
            
        current_vix = vix['Close'].iloc[-1]
        prev_close = soxl['Close'].iloc[-2]
        current_open = soxl['Open'].iloc[-1]
        
        return current_vix, prev_close, current_open
    except Exception as e:
        print(f"❌ 실시간 데이터 수집 실패: {e}")
        return None, None, None

def execute_dual_tactical_trader():
    print("======================================================================")
    print("📡 [SOXL_VIX_2YEAR_DUAL_TRADER.py]")
    print("🛡️ [디스코드 복원] 내 계좌(LONG) & 처형 계좌(SHORT) 통합 관제탑")
    print("======================================================================\n")

    cfg = load_config()
    webhook_url = cfg.get("DISCORD_WEBHOOK", "")
    user_id = cfg.get("DISCORD_USER_ID", "")
    
    vix_cfg = cfg["VIX_CONFIG"]["LONG"]
    pos_cfg = cfg["POSITIONS"]["SOXL"]

    SIGMA = vix_cfg["FIXED_SIGMA"]
    MULT_NORMAL = vix_cfg["MULT_NORMAL"]
    MULT_FEAR = vix_cfg["MULT_FEAR"]
    MULT_EXTREME = vix_cfg["MULT_EXTREME"]
    TAKE_PROFIT_RATIO = vix_cfg["TAKE_PROFIT_RATIO"]

    current_vix, prev_close, current_open = get_realtime_data()
    if current_vix is None:
        print("❌ 시장 데이터를 불러올 수 없어 관제탑 가동을 일시 중단합니다."); return

    gap_ratio = (current_open - prev_close) / prev_close
    base_mult = MULT_EXTREME if current_vix >= cfg["VIX_CONFIG"]["LEVEL_HIGH"] else (MULT_FEAR if current_vix >= cfg["VIX_CONFIG"]["LEVEL_LOW"] else MULT_NORMAL)
    
    if gap_ratio >= -0.03:    adj_mult = base_mult
    elif gap_ratio >= -0.05:  adj_mult = 0.45
    elif gap_ratio >= -0.07:  adj_mult = 0.25
    elif gap_ratio >= -0.10:  adj_mult = 0.10
    else:                     adj_mult = 0.0

    target_price_long = current_open * np.exp(-SIGMA * adj_mult)

    try:
        current_price = yf.Ticker("SOXL").history(period="1d")['Close'].iloc[-1]
    except:
        current_price = current_open

    discord_report = []

    # ======================================================================
    # 🔴 [PART 1] 베프님 계좌 (LONG)
    # ======================================================================
    print("🟢 [SECTION A] 마이 베프 자산 포지션 (LONG)")
    shares_long = pos_cfg["TOTAL_SHARES_LONG"]
    avg_long = pos_cfg["MY_AVG_PRICE_LONG"]
    casts_long = pos_cfg["CURRENT_CASTS_LONG"]
    long_triggered = False

    long_msg = f"**🟢 [LONG] 베프님 계좌 현황**\n• 보유 수량: {shares_long}주 / 평단가: ${avg_long:.4f}\n"
    if shares_long > 0 and avg_long > 0:
        return_long = (current_price - avg_long) / avg_long
        long_msg += f"• 현재 수익률: {return_long*100:+.2f}% (목표: +{TAKE_PROFIT_RATIO*100:.0f}%)\n"
        print(f"   • 현재 보유 수량 : {shares_long} 주 / 평단가: ${avg_long:.4f}")
        print(f"   • 현재 계좌 수익률 : {return_long*100:+.2f}%")
        
        if return_long >= TAKE_PROFIT_RATIO:
            alert_text = "🚨 **[LONG 익절 발동]** 목표 돌파! 오늘 밤 전량 매도 청산하세요!"
            print(f"   {alert_text}")
            long_msg += f"{alert_text}\n"
            long_triggered = True
    else:
        print("   • 현재 계좌 수익률 : 보유 물량 없음")
        long_msg += "• 보유 물량 없음 (매수 대기)\n"

    if not long_triggered:
        if casts_long >= pos_cfg["ANNUAL_QUOTA_LONG"]:
            print("   🛑 금년 매수 쿼터 소진 또는 홀딩 구간입니다.")
            long_msg += "• 🛑 금년 매수 쿼터 소진 또는 홀딩 구간 (관망)\n"
        else:
            print(f"   🎯 오늘 밤 SOXL 롱 LOC 매수 추천가: ${target_price_long:.2f}")
            long_msg += f"• 🎯 오늘 밤 롱 LOC 매수 추천가: **${target_price_long:.2f}**\n"
    
    discord_report.append(long_msg)
    print("----------------------------------------------------------------------")

    # ======================================================================
    # 🔵 [PART 2] 처형 계좌 (SHORT)
    # ======================================================================
    print("🔵 [SECTION B] 처형님 자산 포지션 (SHORT)")
    shares_short = pos_cfg["TOTAL_SHARES_SHORT"]
    avg_short = pos_cfg["MY_AVG_PRICE_SHORT"]
    casts_short = pos_cfg["CURRENT_CASTS_SHORT"]
    short_triggered = False

    short_msg = f"**🔵 [SHORT] 처형님 계좌 현황**\n• 보유 수량: {shares_short}주 / 평단가: ${avg_short:.4f}\n"
    if shares_short > 0 and avg_short > 0:
        return_short = (avg_short - current_price) / avg_short
        short_msg += f"• 현재 수익률: {return_short*100:+.2f}% (목표: +{TAKE_PROFIT_RATIO*100:.0f}%)\n"
        print(f"   • 현재 보유 수량 : {shares_short} 주 / 평단가: ${avg_short:.4f}")
        print(f"   • 현재 계좌 수익률 : {return_short*100:+.2f}%")
        
        if return_short >= TAKE_PROFIT_RATIO:
            alert_text = "🚨 **[SHORT 익절 발동]** 목표 돌파! 오늘 밤 전량 숏 청산하세요!"
            print(f"   {alert_text}")
            short_msg += f"{alert_text}\n"
            short_triggered = True
    else:
        print("   • 현재 계좌 수익률 : 보유 물량 없음")
        short_msg += "• 보유 물량 없음\n"

    if not short_triggered:
        if casts_short >= pos_cfg["ANNUAL_QUOTA_SHORT"]:
            print("   🛑 금년 숏 쿼터 소진 또는 홀딩 구간입니다.")
            short_msg += "• 🛑 금년 숏 쿼터 소진 또는 홀딩 구간\n"
        else:
            target_price_short = current_open * np.exp(SIGMA * adj_mult)
            print(f"   🎯 오늘 밤 SOXL 숏 LOC 추천가: ${target_price_short:.2f}")
            short_msg += f"• 🎯 오늘 밤 숏 LOC 진입 추천가: **${target_price_short:.2f}**\n"
            
    discord_report.append(short_msg)
    print("======================================================================\n")

    # ======================================================================
    # 🚀 디스코드 전송 집행
    # ======================================================================
    status_title = "📡 [관제탑 듀얼 모드 작전 브리핑]"
    if long_triggered or short_triggered:
        status_title = "🚨 [관제탑 긴급 청산 명령] 목표 수익률 돌파!!"

    full_content = f"🗓️ 기준 일시: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
    full_content += f"📈 현재 VIX 지수: {current_vix:.2f}\n\n"
    full_content += "\n----------------------------------------\n".join(discord_report)

    send_discord_message(webhook_url, user_id, status_title, full_content)

if __name__ == "__main__":
    execute_dual_tactical_trader()