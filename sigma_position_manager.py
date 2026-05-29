import os
import sys
import json
import numpy as np
import warnings
import requests
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import yfinance as yf

# 인코딩 설정
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

warnings.filterwarnings('ignore', category=FutureWarning)

# ===================================================================
# 설정 및 장부 로드/저장
# ===================================================================
def load_config():
    try:
        with open("config.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"❌ config.json 로드 실패: {e}")
        sys.exit(1)

def load_ledger():
    try:
        with open("ledger.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"SOXL_LONG_BUY": [], "SOXL_LONG_SELL": [], "SOXL_SHORT_BUY": [], "SOXL_SHORT_SELL": []}

def save_config(cfg):
    try:
        with open("config.json", "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"⚠️ config.json 저장 실패: {e}")

def update_positions_from_ledger(cfg):
    ledger = load_ledger()
    pos = cfg.setdefault("POSITIONS", {}).setdefault("SOXL", {})
    for key, buy_key, sell_key in [("LONG", "SOXL_LONG_BUY", "SOXL_LONG_SELL"), ("SHORT", "SOXL_SHORT_BUY", "SOXL_SHORT_SELL")]:
        buys = ledger.get(buy_key, [])
        sells = ledger.get(sell_key, [])
        total_buy_qty = sum(item.get("qty", 0) for item in buys)
        total_sell_qty = sum(item.get("qty", 0) for item in sells)
        hold_qty = max(total_buy_qty - total_sell_qty, 0)
        total_buy_amt = sum(item.get("total_amount", 0) for item in buys)
        avg_price = round(total_buy_amt / total_buy_qty, 4) if total_buy_qty > 0 else 0.0
        pos[f"CURRENT_CASTS_{key}"] = len(buys)
        pos[f"TOTAL_SHARES_{key}"] = hold_qty
        pos[f"MY_AVG_PRICE_{key}"] = avg_price
    save_config(cfg)

# ===================================================================
# 자동화 로직
# ===================================================================
def auto_update_annual_quota(cfg):
    now = datetime.now(ZoneInfo("America/New_York"))
    if now.month == 1 and now.day <= 10:
        quota_cfg = cfg["POSITIONS"]["SOXL"]
        if str(now.year) == quota_cfg.get("LAST_QUOTA_UPDATE", "1970"): return
        try:
            soxl = yf.Ticker("SOXL")
            df = soxl.history(period="6y", auto_adjust=False)
            yearly_hits = []
            for year in range(now.year - 5, now.year):
                df_year = df[df.index.year == year]
                if len(df_year) < 200: continue
                log_ret = np.log(df_year['Close'] / df_year['Close'].shift(1)).dropna()
                threshold = -(log_ret.mean() - 1.5 * log_ret.std())
                hits = sum(1 for i in range(1, len(df_year)) if (df_year['Low'].iloc[i] / df_year['Close'].iloc[i-1] - 1) <= -threshold)
                yearly_hits.append(hits)
            if yearly_hits:
                quota_cfg["ANNUAL_QUOTA_LONG"] = max(12, min(30, int(round(np.mean(yearly_hits)))))
                quota_cfg["LAST_QUOTA_UPDATE"] = str(now.year)
                save_config(cfg)
        except Exception as e: print(f"❌ 쿼터 최적화 실패: {e}")

def auto_update_rolling_sigma(cfg):
    soxl_cfg = cfg.setdefault("POSITIONS", {}).setdefault("SOXL", {})
    last_update = datetime.strptime(soxl_cfg.get("LAST_SIGMA_UPDATE", "1970-01-01"), "%Y-%m-%d").date()
    today = datetime.now(ZoneInfo("America/New_York")).date()
    if (today - last_update).days >= 180:
        try:
            df = yf.Ticker("SOXL").history(period="18m", auto_adjust=False).tail(253)
            log_ret = np.log(df['Close'] / df['Close'].shift(1)).dropna()
            soxl_cfg["FIXED_SIGMA"] = round(float(-(log_ret.mean() - 1.5 * log_ret.std())), 4)
            soxl_cfg["LAST_SIGMA_UPDATE"] = today.strftime("%Y-%m-%d")
            save_config(cfg)
        except Exception as e: print(f"❌ 시그마 자동 연산 실패: {e}")

# ===================================================================
# 나머지 로직
# ===================================================================
def get_market_mode():
    now_ny = datetime.now(ZoneInfo("America/New_York"))
    hour = now_ny.hour + now_ny.minute / 60.0
    return ("장중" if 9.5 <= hour < 16.0 else "장전"), now_ny

def get_market_data(mode):
    try:
        soxl = yf.Ticker("SOXL")
        hist = soxl.history(period="3d", auto_adjust=False)
        if len(hist) < 1: return None, None, None
        now_ny = datetime.now(ZoneInfo("America/New_York"))
        prev_close = float(hist['Close'].iloc[-2] if hist.index[-1].date() == now_ny.date() and len(hist) > 1 else hist['Close'].iloc[-1])
        current_open = prev_close if mode == "장전" else float(getattr(soxl.fast_info, 'open', hist['Open'].iloc[-1]))
        return prev_close, current_open, float(soxl.fast_info.last_price)
    except: return None, None, None

def check_burn_rate_and_adjust_loc(base_loc_price, cfg, mode):
    if mode != "장중": return base_loc_price
    pos = cfg["POSITIONS"]["SOXL"]
    executed = sum(item.get("total_amount", 0) for item in load_ledger().get("SOXL_LONG_BUY", []))
    burn_rate = executed / pos.get("TOTAL_CAPITAL_LONG", 1)
    adj = 0.99 if burn_rate > (pos.get("CURRENT_CASTS_LONG", 0) / pos.get("ANNUAL_QUOTA_LONG", 21)) * 1.35 else (1.008 if burn_rate < (pos.get("CURRENT_CASTS_LONG", 0) / pos.get("ANNUAL_QUOTA_LONG", 21)) * 0.65 else 1.0)
    return round(base_loc_price * adj, 2)

def send_discord(webhook_url, user_id, title, content):
    if not webhook_url: return
    payload = {"content": f"<@{user_id}> " if user_id else "", "embeds": [{"title": title, "description": content, "color": 15158332 if "🚨" in title or "[반기 결산]" in title else 3447003, "timestamp": datetime.now(timezone.utc).isoformat()}]}
    requests.post(webhook_url, json=payload, timeout=15)

def execute_dual_tactical_trader():
    mode, now_ny = get_market_mode()
    cfg = load_config()
    auto_update_annual_quota(cfg)
    auto_update_rolling_sigma(cfg)
    update_positions_from_ledger(cfg)
    cfg = load_config()
    
    pos = cfg["POSITIONS"]["SOXL"]
    webhook_url = os.environ.get("DISCORD_WEBHOOK") or cfg.get("DISCORD_WEBHOOK", "")
    user_id = os.environ.get("DISCORD_USER_ID") or cfg.get("DISCORD_USER_ID", "")
    
    if now_ny.month in [6, 12] and now_ny.hour == 16:
        if now_ny.date() == yf.Ticker("SOXL").history(period="1mo").index[-1].date():
            send_discord(webhook_url, user_id, "[반기 결산] 시그마 최적화 보고", "시그마(σ) 최신화 완료.")

    prev_close, _, current_price = get_market_data(mode)
    if prev_close:
        final_loc = check_burn_rate_and_adjust_loc(prev_close * np.exp(-pos.get("FIXED_SIGMA", 0.0832)), cfg, mode)
        print(f"📌 {mode} 모드 | 현재가: ${current_price:.2f} | LOC 예정가: ${final_loc:.2f}")

if __name__ == "__main__":
    execute_dual_tactical_trader()