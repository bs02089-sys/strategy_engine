#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
달러 매수 알림 (2시그마 기반, 루트 .env 적용)
- 신호: 환율 < (20일 이동평균 - 2 * 표준편차)
- 데이터: USD/KRW (yfinance: USDKRW=X)
- 알림: Discord webhook
"""

import os
import json
import pandas as pd
import yfinance as yf
import requests
from dotenv import load_dotenv

# 루트 디렉토리의 .env 파일 로드
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(ROOT_DIR, ".env")
load_dotenv(dotenv_path=ENV_PATH)

# 환경변수 불러오기
TICKER = os.getenv("TICKER", "USDKRW=X")
PERIOD = os.getenv("PERIOD", "2y")
INTERVAL = os.getenv("INTERVAL", "1d")
WINDOW = int(os.getenv("WINDOW", "20"))
SIGMA = float(os.getenv("SIGMA", "2.0"))
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL", "")

# 데이터 가져오기
def fetch_data():
    df = yf.download(TICKER, period=PERIOD, interval=INTERVAL,
                     progress=False, auto_adjust=False)
    df = df[['Close']].rename(columns={'Close': 'rate'}).dropna()
    return df

# 밴드 계산
def compute_bands(df):
    df['MA'] = df['rate'].rolling(WINDOW).mean()
    df['STD'] = df['rate'].rolling(WINDOW).std()
    df['BB_lower'] = df['MA'] - SIGMA * df['STD']
    df['BB_upper'] = df['MA'] + SIGMA * df['STD']
    return df

# 신호 판별
def classify_signal(df):
    r = df['rate'].iloc[-1].item()
    lower = df['BB_lower'].iloc[-1].item()
    upper = df['BB_upper'].iloc[-1].item()

    if pd.isna(lower) or pd.isna(upper):
        return "neutral", "지표 계산이 충분하지 않습니다."
    if r < lower:
        return "buy", "2σ 하단 밴드 하향 돌파 → 매수 신호"
    return "neutral", "밴드 내 움직임 → 정기 적립식 유지"

# 메시지 포맷
def format_message(df):
    latest = df.iloc[-1]
    sig, desc = classify_signal(df)

    embed = {
        "title": f"달러 매수 알림 (2σ) — {TICKER}",
        "description": f"{desc}\n기준일: {latest.name.strftime('%Y-%m-%d')}",
        "color": 5814783 if sig == "buy" else 8355711,
        "fields": [
            {"name": "현재 환율", "value": f"{df['rate'].iloc[-1].item():,.2f} KRW", "inline": True},
            {"name": "20일선", "value": f"{df['MA'].iloc[-1].item():,.2f}", "inline": True},
            {"name": "하단 밴드", "value": f"{df['BB_lower'].iloc[-1].item():,.2f}", "inline": True},
            {"name": "상단 밴드", "value": f"{df['BB_upper'].iloc[-1].item():,.2f}", "inline": True},
            {"name": "신호", "value": sig, "inline": True},
        ],
        "footer": {"text": "달러_바이_얼러트.py — 2σ 기반"}
    }
    return {"embeds": [embed]}

# 디스코드 전송
def post_to_discord(payload):
    if not DISCORD_WEBHOOK:
        print("No webhook configured.")
        return
    resp = requests.post(DISCORD_WEBHOOK, json=payload)
    if resp.status_code in (200, 204):
        print("Discord post: OK")
    else:
        print(f"Discord post: FAILED ({resp.status_code})")

def main():
    df = fetch_data()
    df = compute_bands(df)
    payload = format_message(df)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    post_to_discord(payload)

if __name__ == "__main__":
    main()
