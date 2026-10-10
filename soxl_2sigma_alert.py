import os
import yfinance as yf
import os
import yfinance as yf
import pandas as pd
import numpy as np
import requests
import json 
from datetime import datetime, timedelta
from pathlib import Path

# ==================== 설정 불러오기 ====================
def load_config():
    config_path = Path(__file__).parent / "soxl_config.json"
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

config = load_config()

# 환경변수에서 웹훅 가져오기 (필수)
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

if not DISCORD_WEBHOOK_URL:
    raise ValueError("환경변수 DISCORD_WEBHOOK_URL이 설정되지 않았습니다.")

# soxl_config.json에서 티커 읽어오기 (기본값 문자열 제거 및 예외 처리 추가)
TICKER = config.get("ticker")
if not TICKER:
    raise ValueError("설정 파일(soxl_config.json)에 'ticker' 항목이 설정되지 않았습니다.")

LOOKBACK_DAYS = config.get("lookback_days", 60)
ROLLING_WINDOW = config.get("rolling_window", 20)
SIGMA_MULTIPLIER = config.get("sigma_multiplier", 2.0)
# ======================================================

def send_discord_message(content: str):
    """디스코드 웹훅으로 메시지 전송"""
    data = {"content": content}
    try:
        response = requests.post(DISCORD_WEBHOOK_URL, json=data, timeout=10)
        if response.status_code == 204:
            print("✅ 디스코드 알림 전송 성공")
        else:
            print(f"❌ 디스코드 전송 실패: {response.status_code} - {response.text}")
    except Exception as e:
        print(f"❌ 디스코드 전송 중 오류: {e}")

def extract_scalar(val):
    """Pandas Series나 복합 구조에서 안전하게 스칼라(숫자) 값 추출"""
    if hasattr(val, "iloc"):
        return float(val.iloc[0])
    if hasattr(val, "item"):
        return float(val.item())
    return float(val)

def check_signal():
    end_date = datetime.now()
    start_date = end_date - timedelta(days=LOOKBACK_DAYS)

    print(f"📡 {TICKER} 데이터 불러오는 중...")
    df = yf.download(
        TICKER,
        start=start_date.strftime("%Y-%m-%d"),
        end=end_date.strftime("%Y-%m-%d"),
        progress=False
    )

    if df.empty:
        print("❌ 데이터를 가져오지 못했습니다.")
        return

    df['Return'] = df['Close'].pct_change()
    df['Vol_20'] = df['Return'].rolling(window=ROLLING_WINDOW).std()
    df['Sigma2'] = df['Vol_20'] * SIGMA_MULTIPLIER

    clean_df = df.dropna()
    if clean_df.empty:
        print("❌ 유효한 계산 데이터가 부족합니다.")
        return

    latest = clean_df.iloc[-1]
    
    # 전일 종가 안전 추출
    prev_close_raw = df['Close'].iloc[-2]
    prev_close = extract_scalar(prev_close_raw)

    # 당일 지표 안전 추출
    latest_close = extract_scalar(latest['Close'])
    latest_return = extract_scalar(latest['Return'])
    latest_sigma2 = extract_scalar(latest['Sigma2'])

    # 당일 종가 기준 2시그마 하락 LOC 매수 목표가 계산
    target_loc_price = latest_close * (1.0 - latest_sigma2)
    is_buy_signal = latest_return < -latest_sigma2

    # 2σ 자체의 퍼센트 수치
    sigma_pct = latest_sigma2 * 100

    # 콘솔 출력
    print("=" * 55)
    print(f"체크 시간      : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"종목           : {TICKER}")
    print(f"전일 종가      : ${prev_close:.2f}")
    print(f"당일 종가      : ${latest_close:.2f}")
    print(f"당일 수익률    : {latest_return*100:.2f}%")
    print(f"20일 롤링 2σ   : {sigma_pct:.2f}% (배율: {SIGMA_MULTIPLIER})")
    print(f"🎯 2σ LOC 목표가: ${target_loc_price:.2f}")
    print(f"매수 신호      : {'✅ 발생!' if is_buy_signal else '❌ 없음'}")
    print("=" * 55)

    # 디스코드 메시지 구성 (2σ 기준값 및 당일 종가 기준 목표가 포함)
    date_str = latest.name.strftime('%Y-%m-%d') if hasattr(latest.name, 'strftime') else datetime.now().strftime('%Y-%m-%d')
    
    if is_buy_signal:
        message = (
            f"[SOXL 2σ 매수 신호 발생]\n"
            f"날짜: {date_str}\n"
            f"당일 종가: ${latest_close:.2f} ({latest_return*100:.2f}%)\n"
            f"2σ 기준값: {sigma_pct:.2f}% (20일 롤링 {SIGMA_MULTIPLIER}배)\n"
            f"🎯 2σ LOC 목표가: ${target_loc_price:.2f}\n"
            f"상태: 매수 조건 충족!"
        )
    else:
        message = (
            f"[SOXL 장마감 및 LOC 목표가 안내]\n"
            f"날짜: {date_str}\n"
            f"당일 종가: ${latest_close:.2f} ({latest_return*100:.2f}%)\n"
            f"2σ 기준값: {sigma_pct:.2f}% (20일 롤링 {SIGMA_MULTIPLIER}배)\n"
            f"🎯 2σ LOC 목표가: ${target_loc_price:.2f}\n"
            f"상태: 일반 장세 (참고용)"
        )

    send_discord_message(message)

if __name__ == "__main__":
    check_signal()