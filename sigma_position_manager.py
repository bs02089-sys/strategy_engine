import os
import json
import numpy as np
import pandas as pd
import yfinance as yf
from datetime import datetime
import warnings

warnings.filterwarnings('ignore', category=FutureWarning)

def load_config():
    with open("config.json", "r", encoding="utf-8") as f:
        return json.load(f)

def get_realtime_data():
    """실전 매수 타깃가 계산을 위한 야후 파이낸스 실시간 데이터 스캔"""
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

def execute_real_tactical_trader():
    print("======================================================================")
    print("📡 [SOXL_VIX_2YEAR_REAL_TRADER.py]")
    print("🛡️ [실전 운용] 2년 주기 + 30% 익절 프로토콜 탑재형 메인 관제탑")
    print("======================================================================\n")

    # 1. 설정값 및 현재 내 계좌 상태 로드
    cfg = load_config()
    vix_cfg = cfg["VIX_CONFIG"]["LONG"]
    pos_cfg = cfg["POSITIONS"]["SOXL"]
    strat_cfg = cfg["STRATEGY"]

    # 황금 스펙 동기화
    SIGMA = vix_cfg["FIXED_SIGMA"]
    MULT_NORMAL = vix_cfg["MULT_NORMAL"]
    MULT_FEAR = vix_cfg["MULT_FEAR"]
    MULT_EXTREME = vix_cfg["MULT_EXTREME"]
    TAKE_PROFIT_RATIO = vix_cfg["TAKE_PROFIT_RATIO"]

    print("🔎 [현재 가동 중인 마스터 스펙 스캔]")
    print(f"   • 고정 시그마: {SIGMA*100:.2f}% | 목표 익절선: +{TAKE_PROFIT_RATIO*100:.0f}%")
    print(f"   • 배수 세팅  : 평시 {MULT_NORMAL:.2f}x / 공포 {MULT_FEAR:.2f}x / 극단 {MULT_EXTREME:.2f}x\n")

    # 2. 실시간 시장 상황 스캔
    print("📡 현재 미국 시장 실시간 변동성 및 주가 스캔 중...")
    current_vix, prev_close, current_open = get_realtime_data()
    
    if current_vix is None:
        print("❌ 시장 데이터를 불러올 수 없어 관제탑 가동을 일시 중단합니다."); return

    gap_ratio = (current_open - prev_close) / prev_close

    # 3. 갭 보정 적용된 최종 배수 산출
    base_mult = MULT_EXTREME if current_vix >= cfg["VIX_CONFIG"]["LEVEL_HIGH"] else (MULT_FEAR if current_vix >= cfg["VIX_CONFIG"]["LEVEL_LOW"] else MULT_NORMAL)
    
    # 시가 갭락 패널티 제어 (VectorBT와 100% 일치화)
    if gap_ratio >= -0.03:    adj_mult = base_mult
    elif gap_ratio >= -0.05:  adj_mult = 0.45
    elif gap_ratio >= -0.07:  adj_mult = 0.25
    elif gap_ratio >= -0.10:  adj_mult = 0.10
    else:                     adj_mult = 0.0

    # 🎯 오늘 밤 최종 LOC 매수 타깃가 계산
    target_price = current_open * np.exp(-SIGMA * adj_mult)

    # 4. 내 계좌 현재 수익률 진단 및 [30% 익절 프로토콜] 가동
    total_shares = pos_cfg["TOTAL_SHARES_LONG"]
    my_avg_price = pos_cfg["MY_AVG_PRICE_LONG"]
    current_casts = pos_cfg["CURRENT_CASTS_LONG"]

    print("----------------------------------------------------------------------")
    print("📊 [현 시간부 내 자산 포지션 및 전략 상태 점검]")
    print(f"   • 현재 보유 수량 : {total_shares} 주")
    print(f"   • 내 평균 단가   : ${my_avg_price:.4f}")
    print(f"   • 현재 매수 횟수 : {current_casts}회 진행 중 (2년 주기 내부)")
    
    # 임시로 야후 종가를 현재가로 대입하여 계좌 수익률 계산
    try:
        current_price = yf.Ticker("SOXL").history(period="1d")['Close'].iloc[-1]
    except:
        current_price = current_open
        
    if total_shares > 0 and my_avg_price > 0:
        my_return = (current_price - my_avg_price) / my_avg_price
        print(f"   • 현재 계좌 수익률 : {my_return*100:+.2f}%")
        
        # 🚨 [익절 스위치 발동]
        if my_return >= TAKE_PROFIT_RATIO:
            print("\n🚨🚨🚨 [EMERGENCY: 익절 프로토콜 발동] 🚨🚨🚨")
            print(f"🎯 목표 수익률 +{TAKE_PROFIT_RATIO*100:.0f}% 돌파 완료!")
            print("💰 [관제탑 명령] 오늘 밤 프리마켓/본장에서 보유 중인 SOXL 전량 청산(시장가 매도)을 집행하세요.")
            print("🛡️ 수익을 현금 금고로 안전하게 대피시키고, 다음 사이클 리셋을 준비합니다.")
            print("----------------------------------------------------------------------")
            return
    else:
        print("   • 현재 계좌 수익률 : 보유 물량 없음 (매수 대기 상태)")

    # 5. [매수 1년 주기 통제] 및 오늘 밤 주문 전략 수립
    # 실전 환경에서는 진행된 매수 횟수(CURRENT_CASTS)나 날짜 데이터로 1년 경과를 체크합니다.
    # 여기서는 안전하게 연간 배정 쿼터 내에 있을 때만 주문을 생성합니다.
    print("----------------------------------------------------------------------")
    print("📡 [오늘 밤 관제탑 최종 작전 명령]")
    print(f"   • 현재 VIX 지수 : {current_vix:.2f}")
    
    if current_casts >= pos_cfg["ANNUAL_QUOTA_LONG"]:
        print("🛑 [매수 중단] 올해 설정된 매수 쿼터를 모두 소진했거나 홀딩 기간입니다.")
        print("   ➔ 오늘 밤은 추가 주문 없이 편안하게 기존 물량을 홀딩(관망)하세요.")
    else:
        if adj_mult == 0.0:
            print("🛑 [매수 금지] 시가가 전일 대비 -10% 이상 폭락하는 비정상 괴질이 발생했습니다.")
            print("   ➔ 안전을 위해 오늘 밤 거미줄 매수는 전면 취소하고 관망합니다.")
        else:
            print(f"   🟢 [매수 가동] 오늘 밤 SOXL LOC 매수 추천 타깃가: ${target_price:.2f}")
            print(f"   ➔ 장중에 주가가 ${target_price:.2f} 이하로 내려오면 거미줄이 체결됩니다.")
    print("======================================================================\n")

if __name__ == "__main__":
    execute_real_tactical_trader();