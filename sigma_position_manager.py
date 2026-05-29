import os
import sys
import json
import numpy as np
import warnings
import requests
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import yfinance as yf

# 경로 설정
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
LEDGER_PATH = os.path.join(BASE_DIR, "ledger.json")

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
warnings.filterwarnings('ignore', category=FutureWarning)

def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except: return {}

def load_ledger():
    try:
        with open(LEDGER_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except: return {"SOXL_LONG_BUY": [], "SOXL_LONG_SELL": [], "SOXL_SHORT_BUY": [], "SOXL_SHORT_SELL": []}

def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4, ensure_ascii=False)
    except: pass

def update_positions_from_ledger(cfg):
    ledger = load_ledger()
    pos = cfg.setdefault("POSITIONS", {}).setdefault("SOXL", {})
    for key, b_key, s_key in [("LONG", "SOXL_LONG_BUY", "SOXL_LONG_SELL"), ("SHORT", "SOXL_SHORT_BUY", "SOXL_SHORT_SELL")]:
        buys, sells = ledger.get(b_key, []), ledger.get(s_key, [])
        hold_qty = max(sum(i.get("qty", 0) for i in buys) - sum(i.get("qty", 0) for i in sells), 0)
        total_buy_amt = sum(i.get("total_amount", 0) for i in buys)
        pos[f"CURRENT_CASTS_{key}"] = len(buys)
        pos[f"TOTAL_SHARES_{key}"] = hold_qty
        pos[f"MY_AVG_PRICE_{key}"] = round(total_buy_amt / sum(i.get("qty", 0) for i in buys), 4) if sum(i.get("qty", 0) for i in buys) > 0 else 0.0
    save_config(cfg)

def auto_update_annual_quota(cfg):
    now = datetime.now(ZoneInfo("America/New_York"))
    if now.month == 1 and now.day <= 10:
        quota_cfg = cfg["POSITIONS"]["SOXL"]
        if str(now.year) == quota_cfg.get("LAST_QUOTA_UPDATE", "1970"): return
        try:
            df = yf.Ticker("SOXL").history(period="6y", auto_adjust=False)
            hits = []
            for y in range(now.year - 5, now.year):
                df_y = df[df.index.year == y]
                if len(df_y) < 200: continue
                ret = np.log(df_y['Close'] / df_y['Close'].shift(1)).dropna()
                thr = -(ret.mean() - 1.5 * ret.std())
                hits.append(sum(1 for i in range(1, len(df_y)) if (df_y['Low'].iloc[i] / df_y['Close'].iloc[i-1] - 1) <= -thr))
            quota_cfg["ANNUAL_QUOTA_LONG"] = max(12, min(30, int(round(np.mean(hits)))))
            quota_cfg["LAST_QUOTA_UPDATE"] = str(now.year)
            save_config(cfg)
        except: pass

def auto_update_rolling_sigma(cfg):
    soxl_cfg = cfg.setdefault("POSITIONS", {}).setdefault("SOXL", {})
    last = datetime.strptime(soxl_cfg.get("LAST_SIGMA_UPDATE", "1970-01-01"), "%Y-%m-%d").date()
    today = datetime.now(ZoneInfo("America/New_York")).date()
    if (today - last).days >= 180:
        try:
            df = yf.Ticker("SOXL").history(period="18m", auto_adjust=False).tail(253)
            ret = np.log(df['Close'] / df['Close'].shift(1)).dropna()
            soxl_cfg["FIXED_SIGMA"] = round(float(-(ret.mean() - 1.5 * ret.std())), 4)
            soxl_cfg["LAST_SIGMA_UPDATE"] = today.strftime("%Y-%m-%d")
            save_config(cfg)
        except: pass

def get_market_mode():
    now_ny = datetime.now(ZoneInfo("America/New_York"))
    h = now_ny.hour + now_ny.minute / 60.0
    return ("장중" if 9.5 <= h < 16.0 else "장전"), now_ny

def get_market_data(mode):
    try:
        soxl = yf.Ticker("SOXL")
        hist = soxl.history(period="3d", auto_adjust=False)
        if len(hist) < 1: return None, None, None
        now_ny = datetime.now(ZoneInfo("America/New_York"))
        prev = float(hist['Close'].iloc[-2] if hist.index[-1].date() == now_ny.date() and len(hist) > 1 else hist['Close'].iloc[-1])
        curr = float(soxl.fast_info.last_price) if mode == "장중" else prev
        return prev, curr
    except: return None, None

def check_burn_rate_and_adjust_loc(base, cfg):
    pos = cfg["POSITIONS"]["SOXL"]
    ex = sum(i.get("total_amount", 0) for i in load_ledger().get("SOXL_LONG_BUY", []))
    rate = ex / pos.get("TOTAL_CAPITAL_LONG", 1)
    quota = pos.get("CURRENT_CASTS_LONG", 0) / pos.get("ANNUAL_QUOTA_LONG", 21)
    adj = 0.99 if rate > quota * 1.35 else (1.008 if rate < quota * 0.65 else 1.0)
    return round(base * adj, 2)

def send_discord(title, content):
    cfg = load_config()
    url = os.environ.get("DISCORD_WEBHOOK") or cfg.get("DISCORD_WEBHOOK", "")
    uid = os.environ.get("DISCORD_USER_ID") or cfg.get("DISCORD_USER_ID", "")
    if not url: return
    payload = {"content": f"<@{uid}> " if uid else "", "embeds": [{"title": title, "description": content, "color": 15158332 if "🚨" in title or "[반기 결산]" in title else 3447003, "timestamp": datetime.now(timezone.utc).isoformat()}]}
    try: requests.post(url, json=payload, timeout=15)
    except: pass

def execute_dual_tactical_trader():
    mode, now_ny = get_market_mode()
    cfg = load_config()
    auto_update_annual_quota(cfg)
    auto_update_rolling_sigma(cfg)
    update_positions_from_ledger(cfg)
    cfg = load_config()
    pos = cfg["POSITIONS"]["SOXL"]
    
    prev, curr = get_market_data(mode)
    if prev:
        loc = check_burn_rate_and_adjust_loc(prev * np.exp(-pos.get("FIXED_SIGMA", 0.0832)), cfg)
        msg = (f"💰 {('전일 종가' if mode == '장전' else '현재가')}: ${curr:.2f}\n"
               f"🎯 LOC 예정가: ${loc:.2f}")
        print(msg)
        send_discord(f"[{mode} 모드 브리핑]", msg)

if __name__ == "__main__":
    execute_dual_tactical_trader()