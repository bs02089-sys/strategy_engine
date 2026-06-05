import os
import sys
import json
import numpy as np
import warnings
import requests
import yfinance as yf
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# ── 인코딩 및 환경 설정
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

warnings.filterwarnings('ignore', category=FutureWarning)

MODE_EMOJI = {"장전": "🌙", "장중": "☀️"}
TARGET_TICKERS = ["BOTZ", "SOXX", "SOXL"]

# ───────────────────────────────────────────────
# 설정 및 파일 로드
# ───────────────────────────────────────────────
def load_config():
    config_path = "config.json" 
    try:
        with open(config_path, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        return {"POSITIONS": {}, "LAST_MONTHLY_PING": ""}

def save_config(cfg):
    try:
        with open("config.json", "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"⚠️ config.json 저장 실패: {e}")

def load_ledger():
    try:
        with open("ledger.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {
            "SOXL_LONG": {"qty": 0, "avg_price": 0.0},
            "SOXL_SHORT": {"qty": 0, "avg_price": 0.0},
            "BOTZ_LONG": {"qty": 0, "avg_price": 0.0},
            "SOXX_LONG": {"qty": 0, "avg_price": 0.0},
        }

def update_positions_from_ledger(cfg):
    ledger = load_ledger()
    for ticker in TARGET_TICKERS:
        pos = cfg.setdefault("POSITIONS", {}).setdefault(ticker, {})
        targets = ["LONG", "SHORT"] if ticker == "SOXL" else ["LONG"]
        for key in targets:
            ledger_key = f"{ticker}_{key}"
            entry = ledger.get(ledger_key, {})
            pos[f"TOTAL_SHARES_{key}"] = entry.get("qty", 0)
            pos[f"MY_AVG_PRICE_{key}"] = entry.get("avg_price", 0.0)

# ───────────────────────────────────────────────
# 시그마 및 핑 로직
# ───────────────────────────────────────────────
def check_and_update_sigma(config):
    updated = False
    messages = []
    today = datetime.now()
    
    for ticker in TARGET_TICKERS:
        pos = config["POSITIONS"].get(ticker, {})
        last_update_str = pos.get("LAST_SIGMA_UPDATE", "2000-01-01")
        last_update = datetime.strptime(last_update_str, "%Y-%m-%d")
        
        # [수정] 시그마 갱신은 365일(1년) 주기로 고정
        if (today - last_update).days >= 365:
            try:
                # 데이터 수집은 1년치로 고정
                hist = yf.Ticker(ticker).history(period="1y", auto_adjust=True)
                if hist.empty: continue
                
                new_sigma = round(float(hist['Close'].pct_change().dropna().std()), 6)
                pos["DAILY_SIGMA"] = new_sigma
                pos["LAST_SIGMA_UPDATE"] = today.strftime("%Y-%m-%d")
                updated = True
                messages.append(f"📊 {ticker} 시그마 갱신 (1년 기준): {new_sigma}")
            except Exception as e:
                messages.append(f"⚠️ {ticker} 시그마 갱신 실패: {e}")
    return updated, "\n".join(messages)

def get_ticker_data(ticker, mode):
    try:
        t = yf.Ticker(ticker)
        # 데이터가 비어있을 확률을 줄이기 위해 기간 확대
        hist = t.history(period="5d", auto_adjust=False)
        if len(hist) < 2: return None, None
        
        prev_close = float(hist['Close'].iloc[-2]) if mode == "장중" else float(hist['Close'].iloc[-1])
        # last_price가 없을 경우 prev_close를 대체하여 $nan 방지
        current_price = float(t.fast_info.get('last_price', prev_close))
        return prev_close, current_price
    except:
        return None, None
    
def check_monthly_ping(cfg):
    now = datetime.now()
    if now.day == 1:
        last_ping = cfg.get("LAST_MONTHLY_PING", "")
        today_str = now.strftime("%Y-%m")
        if last_ping != today_str:
            msg = f"🔔 **월말 핑**: {now.strftime('%Y년 %m월')} 운용 시스템이 정상 가동 중입니다."
            send_discord(os.environ.get("DISCORD_WEBHOOK") or cfg.get("DISCORD_WEBHOOK", ""), 
                         os.environ.get("DISCORD_USER_ID") or cfg.get("DISCORD_USER_ID", ""), 
                         "🗓️ 월간 리포트 핑", msg)
            cfg["LAST_MONTHLY_PING"] = today_str
            return True
    return False

# ───────────────────────────────────────────────
# 기타 유틸리티 함수
# ───────────────────────────────────────────────
def get_market_mode():
    now_ny = datetime.now(ZoneInfo("America/New_York"))
    hour = now_ny.hour + now_ny.minute / 60.0
    mode = "장중" if 9.5 <= hour < 16.0 else "장전"
    return mode, now_ny

def get_ticker_data(ticker, mode):
    try:
        t = yf.Ticker(ticker)
        # 데이터를 10일치로 넉넉하게 받아옵니다.
        hist = t.history(period="10d", auto_adjust=False)
        if hist.empty or len(hist) < 2: return None, None
        
        # 전일 종가: 장중(iloc[-2]), 장전(iloc[-1])
        prev_close = float(hist['Close'].iloc[-2]) if mode == "장중" else float(hist['Close'].iloc[-1])
        
        # 현재가: last_price가 없으면 prev_close를 사용하여 계산 오류 차단
        current_price = float(t.fast_info.get('last_price')) if t.fast_info.get('last_price') else prev_close
        
        return prev_close, current_price
    except Exception as e:
        print(f"❌ {ticker} 데이터 에러: {e}")
        return None, None
        
def send_discord(webhook_url, user_id, title, content):
    if not webhook_url: return
    payload = {
        "content": f"<@{user_id}> " if user_id else "",
        "embeds": [{"title": title, "description": content, "color": 3447003, "timestamp": datetime.now(timezone.utc).isoformat()}]
    }
    try: requests.post(webhook_url, json=payload, timeout=15)
    except Exception as e: print(f"❌ 전송 실패: {e}")

def calc_loc(prev_close, ENTRY_MULTIPLIER, DAILY_SIGMA):
    return prev_close * np.exp(-ENTRY_MULTIPLIER * DAILY_SIGMA)

# ───────────────────────────────────────────────
# 메인 실행부
# ───────────────────────────────────────────────
def execute_dual_tactical_trader():
    mode, now_ny = get_market_mode()
    cfg = load_config()

    sigma_changed, sigma_msg = check_and_update_sigma(cfg)
    update_positions_from_ledger(cfg)
    check_monthly_ping(cfg)
    save_config(cfg)
    
    lines = [f"{MODE_EMOJI[mode]} {mode} 모드 | {now_ny.strftime('%Y-%m-%d %H:%M %Z')}"]
    
    for ticker in TARGET_TICKERS:
        pos_cfg = cfg["POSITIONS"].get(ticker, {})
        prev_close, current_price = get_ticker_data(ticker, mode)
        if prev_close is None: continue
        
        loc_price = calc_loc(prev_close, pos_cfg.get("ENTRY_MULTIPLIER", 1.5), pos_cfg.get("DAILY_SIGMA", 0.05))
        
        ticker_info = [f"\n🔹 **{ticker}**", f"• 전일 종가: ${prev_close:.2f} / LOC 예정가: ${loc_price:.2f}"]
        if mode == "장중": ticker_info.append(f"• 현재가: ${current_price:.2f}")

        targets = ["LONG", "SHORT"] if ticker == "SOXL" else ["LONG"]
        for k in targets:
            qty = pos_cfg.get(f"TOTAL_SHARES_{k}", 0)
            avg = pos_cfg.get(f"MY_AVG_PRICE_{k}", 0)
            if qty > 0:
                msg = f"• [{k}] 보유: {qty}주"
                if avg > 0: msg += f" / 수익: {(current_price - avg)/avg*100:+.2f}%"
                ticker_info.append(msg)
        lines.extend(ticker_info)
    
    if sigma_changed: lines.append(f"\n{sigma_msg}")
    send_discord(os.environ.get("DISCORD_WEBHOOK") or cfg.get("DISCORD_WEBHOOK", ""), 
                 os.environ.get("DISCORD_USER_ID") or cfg.get("DISCORD_USER_ID", ""), 
                 f"{MODE_EMOJI[mode]} {mode} 브리핑", "\n".join(lines))
                
if __name__ == "__main__":
    execute_dual_tactical_trader()