import os
import sys
import json
import numpy as np
import warnings
import requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import yfinance as yf

# 경로 설정
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
LEDGER_PATH = os.path.join(BASE_DIR, "ledger.json")

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
warnings.filterwarnings('ignore', category=FutureWarning)

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except: return default

def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4, ensure_ascii=False)
    except: pass

def update_positions(cfg):
    ledger = load_json(LEDGER_PATH, {"SOXL_LONG_BUY": [], "SOXL_LONG_SELL": [], "SOXL_SHORT_BUY": [], "SOXL_SHORT_SELL": []})
    pos = cfg.setdefault("POSITIONS", {}).setdefault("SOXL", {})
    for key, b_key, s_key in [("LONG", "SOXL_LONG_BUY", "SOXL_LONG_SELL"), ("SHORT", "SOXL_SHORT_BUY", "SOXL_SHORT_SELL")]:
        buys, sells = ledger.get(b_key, []), ledger.get(s_key, [])
        qty_buys = sum(i.get("qty", 0) for i in buys)
        qty_sells = sum(i.get("qty", 0) for i in sells)
        pos[f"CURRENT_CASTS_{key}"] = len(buys)
        pos[f"TOTAL_SHARES_{key}"] = max(qty_buys - qty_sells, 0)
        pos[f"MY_AVG_PRICE_{key}"] = round(sum(i.get("total_amount", 0) for i in buys) / qty_buys, 4) if qty_buys > 0 else 0.0
    save_config(cfg)

def get_market_data(mode):
    try:
        ticker = yf.Ticker("SOXL")
        hist = ticker.history(period="3d", auto_adjust=False)
        if len(hist) < 1: return None, None
        now_ny = datetime.now(ZoneInfo("America/New_York"))
        prev = float(hist['Close'].iloc[-2] if hist.index[-1].date() == now_ny.date() and len(hist) > 1 else hist['Close'].iloc[-1])
        curr = float(ticker.fast_info.last_price) if mode == "장중" else prev
        return prev, curr
    except: return None, None

def send_discord(title, content):
    cfg = load_json(CONFIG_PATH, {})
    url = os.environ.get("DISCORD_WEBHOOK") or cfg.get("DISCORD_WEBHOOK", "")
    uid = os.environ.get("DISCORD_USER_ID") or cfg.get("DISCORD_USER_ID", "")
    if not url: return
    payload = {"content": f"<@{uid}> " if uid else "", "embeds": [{"title": title, "description": content, "color": 15158332 if "🚨" in title else 3447003, "timestamp": datetime.now(timezone.utc).isoformat()}]}
    try: requests.post(url, json=payload, timeout=15)
    except: pass

def execute():
    # 1. 환경 준비
    cfg = load_json(CONFIG_PATH, {})
    pos = cfg.setdefault("POSITIONS", {}).setdefault("SOXL", {})
    
    # 2. 데이터 무결성 검증 (Self-Healing)
    if not isinstance(pos.get("FIXED_SIGMA"), (int, float)):
        pos["FIXED_SIGMA"] = 0.0832
        save_config(cfg)
    
    # 3. 로직 실행
    update_positions(cfg)
    mode = "장중" if 9.5 <= (datetime.now(ZoneInfo("America/New_York")).hour + datetime.now(ZoneInfo("America/New_York")).minute / 60.0) < 16.0 else "장전"
    prev, curr = get_market_data(mode)
    
    if prev:
        # LOC 계산: 0.0832 기본값 보장
        sigma = float(pos.get("FIXED_SIGMA", 0.0832))
        base_loc = prev * np.exp(-sigma)
        
        # 소진율 조정
        ex = sum(i.get("total_amount", 0) for i in load_json(LEDGER_PATH, {}).get("SOXL_LONG_BUY", []))
        rate = ex / pos.get("TOTAL_CAPITAL_LONG", 1)
        quota = pos.get("CURRENT_CASTS_LONG", 0) / pos.get("ANNUAL_QUOTA_LONG", 21)
        adj = 0.99 if rate > quota * 1.35 else (1.008 if rate < quota * 0.65 else 1.0)
        
        loc = round(base_loc * adj, 2)
        
        msg = f"💰 {('전일 종가' if mode == '장전' else '현재가')}: ${curr:.2f}\n🎯 LOC 예정가: ${loc:.2f}"
        print(msg)
        send_discord(f"[{mode} 모드 브리핑]", msg)

if __name__ == "__main__":
    execute()