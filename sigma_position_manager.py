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
    if not webhook_url: return
    mention = f"<@{user_id}> " if user_id else ""
    payload = {
        "content": mention,
        "embeds": [{
            "title": title,
            "description": content,
            "color": 15158332 if "🚨" in title else 3447003,
            "timestamp": datetime.utcnow().isoformat()
        }]
    }
    try: requests.post(webhook_url, json=payload, timeout=10)
    except: print("❌ 디스코드 전송 실패")

def get_realtime_data():
    try:
        soxl = yf.Ticker("SOXL").history(period="2d")
        vix = yf.Ticker("^VIX").history(period="1d")
        current_vix = vix['Close'].iloc[-1]
        prev_close = soxl['Close'].iloc[-2]
        current_open = soxl['Open'].iloc[-1]
        return current_vix, prev_close, current_open
    except: return None, None, None

def execute_dual_tactical_trader():
    print("======================================================================")
    print("📡 [SOXL_VIX_2YEAR_DUAL_TRADER.py] (오류 수정 완본)")
    print("🛡️ [개념 교정] 매수(LONG) & 매도(SHORT) 계좌 현황 브리핑")
    print("======================================================================\n")

    cfg = load_config()
    webhook_url = cfg.get("DISCORD_WEBHOOK", "")
    user_id = cfg.get("DISCORD_USER_ID", "")
    pos_cfg = cfg["POSITIONS"]["SOXL"]
    vix_cfg = cfg["VIX_CONFIG"]["LONG"]

    SIGMA = vix_cfg["FIXED_SIGMA"]
    TAKE_PROFIT_RATIO = vix_cfg["TAKE_PROFIT_RATIO"]

    current_vix, prev_close, current_open = get_realtime_data()
    if current_vix is None: return

    try: current_price = yf.Ticker("SOXL").history(period="1d")['Close'].iloc[-1]
    except: current_price = current_open

    discord_report = []
    any_triggered = False

    # 🟢 [PART 1] 베프님 계좌 (LONG 방 스캔)
    print("🟢 [SECTION A] 마이 베프 자산 포지션 (LONG 방)")
    shares_long = pos_cfg["TOTAL_SHARES_LONG"]
    avg_long = pos_cfg["MY_AVG_PRICE_LONG"]
    
    if shares_long > 0 and avg_long > 0:
        return_long = (current_price - avg_long) / avg_long
        print(f"   • 현재 보유 수량 : {shares_long} 주 / 평단가: ${avg_long:.4f}")
        print(f"   • 현재 계좌 수익률 : {return_long*100:+.2f}%")
        long_msg = f"**🟢 [LONG] 베프님 계좌 현황**\n• 수량: {shares_long}주 / 평단: ${avg_long:.4f}\n• 수익률: {return_long*100:+.2f}%"
        
        if return_long >= TAKE_PROFIT_RATIO:
            print("   🚨 [익절 발동] 목표 돌파! 오늘 밤 전량 매도 청산하세요!")
            long_msg += "\n🚨 **[익절 발동] 오늘 밤 전량 매도 청산!**"
            any_triggered = True
    else:
        print("   • 보유 물량 없음")
        long_msg = "**🟢 [LONG] 베프님 계좌 현황**\n• 보유 물량 없음"
    discord_report.append(long_msg)
    print("----------------------------------------------------------------------")

    # 🔵 [PART 2] 처형님 계좌 (SHORT 방 스캔 -> 매칭된 롱 포지션 정상 계산)
    print("🔵 [SECTION B] 처형님 자산 포지션 (SHORT 방)")
    shares_short = pos_cfg["TOTAL_SHARES_SHORT"]
    avg_short = pos_cfg["MY_AVG_PRICE_SHORT"]
    
    if shares_short > 0 and avg_short > 0:
        # 공매도가 아니라 일반 매수 잔량이므로 정상 롱 공식으로 복원
        return_short = (current_price - avg_short) / avg_short
        print(f"   • 현재 보유 수량 : {shares_short} 주 / 평단가: ${avg_short:.4f}")
        print(f"   • 현재 계좌 수익률 : {return_short*100:+.2f}%")
        short_msg = f"**🔵 [SHORT 방] 처형님 계좌 현황**\n• 수량: {shares_short}주 / 평단: ${avg_short:.4f}\n• 수익률: {return_short*100:+.2f}%"
        
        if return_short >= TAKE_PROFIT_RATIO:
            print("   🚨 [익절 발동] 목표 돌파! 오늘 밤 전량 매도 청산하세요!")
            short_msg += "\n🚨 **[익절 발동] 오늘 밤 전량 매도 청산!**"
            any_triggered = True
    else:
        print("   • 보유 물량 없음")
        short_msg = "**🔵 [SHORT 방] 처형님 계좌 현황**\n• 보유 물량 없음"
    discord_report.append(short_msg)
    print("======================================================================\n")

    # 디스코드 전송
    status_title = "🚨 [관제탑 긴급 청산 명령] 목표 수익률 돌파!!" if any_triggered else "📡 [관제탑 듀얼 모드 작전 브리핑]"
    full_content = f"🗓️ 기준 일시: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
    full_content += "\n----------------------------------------\n".join(discord_report)
    send_discord_message(webhook_url, user_id, status_title, full_content)

if __name__ == "__main__":
    execute_dual_tactical_trader()