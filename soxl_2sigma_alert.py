import os
import yfinance as yf
import pandas as pd
import numpy as np
import requests
import json
from datetime import datetime, timedelta
from pathlib import Path

from soxl_tqqq_2sigma_count import download_end  # 다운로드 창 끝(마지막 마감 세션) 공용 정의

# ==================== 설정 불러오기 ====================
def load_config():
    config_path = Path(__file__).parent / "soxl_tqqq_config.json"
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

config = load_config()

# 환경변수에서 웹훅 가져오기 (필수)
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

if not DISCORD_WEBHOOK_URL:
    raise ValueError("환경변수 DISCORD_WEBHOOK_URL이 설정되지 않았습니다.")

# soxl_tqqq_config.json에서 종목 설정 불러오기 (단일/복수 모두 호환)
TICKERS = config.get("tickers")
if not TICKERS:
    single_ticker = config.get("ticker")
    if single_ticker:
        TICKERS = [single_ticker]
    else:
        raise ValueError("설정 파일(soxl_config.json)에 'ticker' 또는 'tickers'가 설정되지 않았습니다.")

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

def frequency_note(ticker: str) -> str:
    """최근 1년 발동 실적 + 장기 중앙값 한 줄 (연간 예산을 몇 회로 나눌지의 분모).

    계산은 `soxl_tqqq_2sigma_count.frequency_stats` 공용 함수에서만 한다 (σ·판정 정의 단일 소스).
    통계는 부가 기능이라 실패해도 2σ 알림은 그대로 나가야 한다.
    """
    try:
        from soxl_tqqq_2sigma_count import frequency_stats
        s = frequency_stats(ticker, ROLLING_WINDOW, SIGMA_MULTIPLIER)
    except Exception as e:
        print(f"⚠️ [{ticker}] 발동 빈도 계산 실패(알림은 계속): {e}")
        return ""
    return (f"📊 예상 발동: 연 {s['median']:g}회(장기 중앙값) · 최근 1년 실제 {s['recent']}회 "
            f"→ 연간 예산 ÷ {s['median']:g}")


def extract_scalar(val):
    """Pandas Series나 복합 구조에서 안전하게 스칼라(숫자) 값 추출"""
    if hasattr(val, "iloc"):
        return float(val.iloc[0])
    if hasattr(val, "item"):
        return float(val.item())
    return float(val)

def check_signals():
    start_date = datetime.now() - timedelta(days=LOOKBACK_DAYS)

    # 설정된 모든 종목을 순회하며 체크
    for ticker in TICKERS: # type: ignore
        print(f"\n📡 [{ticker}] 데이터 불러오는 중...")
        df = yf.download(
            ticker,
            start=start_date.strftime("%Y-%m-%d"),
            end=download_end(),  # 시계(UTC/KST) 대신 ET 마지막 마감 세션 기준 — CI·로컬 동일
            progress=False
        )

        if df.empty:
            print(f"❌ [{ticker}] 데이터를 가져오지 못했습니다.")
            continue

        df['Return'] = df['Close'].pct_change()
        df['Vol_20'] = df['Return'].rolling(window=ROLLING_WINDOW).std()
        df['Sigma2'] = df['Vol_20'] * SIGMA_MULTIPLIER

        clean_df = df.dropna()
        if clean_df.empty:
            print(f"❌ [{ticker}] 유효한 계산 데이터가 부족합니다.")
            continue

        latest = clean_df.iloc[-1]
        
        # 기준 세션의 하루 전(직전 세션) 종가 안전 추출
        prev_close_raw = df['Close'].iloc[-2]
        prev_close = extract_scalar(prev_close_raw)

        # 기준 세션(마지막으로 마감된 세션) 지표 안전 추출
        latest_close = extract_scalar(latest['Close'])
        latest_return = extract_scalar(latest['Return'])
        latest_sigma2 = extract_scalar(latest['Sigma2'])

        # 기준 세션 종가 × (1 − 2σ) → 다음 세션에 걸 LOC 매수 목표가
        target_loc_price = latest_close * (1.0 - latest_sigma2)
        is_buy_signal = latest_return < -latest_sigma2

        # 2σ 자체의 퍼센트 수치
        sigma_pct = latest_sigma2 * 100

        # 콘솔 출력
        print("=" * 55)
        print(f"체크 시간      : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"종목           : {ticker}")
        print(f"직전 세션 종가 : ${prev_close:.2f}")
        print(f"기준 세션 종가 : ${latest_close:.2f}")
        print(f"기준 세션 수익률: {latest_return*100:.2f}%")
        print(f"20일 롤링 2σ   : {sigma_pct:.2f}% (배율: {SIGMA_MULTIPLIER})")
        print(f"🎯 다음 세션 LOC 목표가: ${target_loc_price:.2f}")
        print(f"매수 신호      : {'✅ 발생!' if is_buy_signal else '❌ 없음'}")
        print("=" * 55)

        # 디스코드 메시지 구성
        date_str = latest.name.strftime('%Y-%m-%d') if hasattr(latest.name, 'strftime') else datetime.now().strftime('%Y-%m-%d')
        
        # 용어는 '주문 기준' 프레임으로 쓴다 — 여기서 '전일 종가' 는 알림 직전에 마감된 세션(기준
        # 세션)의 종가이고, 목표가는 그 다음 세션에 걸 주문 가격이다(σ·수익률도 같은 기준 세션 값).
        # 신호 여부에 따라 헤더·상태만 다르다 (빈 줄은 filter(None, ...) 로 제거)
        message = "\n".join(filter(None, [
            f"[{ticker} 2σ 매수 신호 발생]" if is_buy_signal else f"[{ticker} 장마감 및 LOC 목표가 안내]",
            f"기준 세션: {date_str} 마감",
            f"전일 종가: ${latest_close:.2f} ({latest_return*100:.2f}%)",
            f"2σ 기준값: {sigma_pct:.2f}% (20일 롤링 {SIGMA_MULTIPLIER}배)",
            f"🎯 다음 세션 LOC 목표가: ${target_loc_price:.2f}",
            frequency_note(ticker),
            "상태: 매수 조건 충족!" if is_buy_signal else "상태: 일반 장세 (참고용)",
        ]))

        send_discord_message(message)

if __name__ == "__main__":
    check_signals()