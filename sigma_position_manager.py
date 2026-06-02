import os
import sys
import json
import numpy as np
import warnings
import requests
import yfinance as yf
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

# ── 인코딩 설정
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

warnings.filterwarnings('ignore', category=FutureWarning)

MODE_EMOJI = {"장전": "🌙", "장중": "☀️"}

# ───────────────────────────────────────────────
# 설정 / 장부 로드 
# ───────────────────────────────────────────────

webhook = os.environ.get("DISCORD_WEBHOOK")
user_id = os.environ.get("DISCORD_USER_ID")

print(f"DEBUG: 환경변수 Webhook: {webhook}")
print(f"DEBUG: 환경변수 UserID: {user_id}")

INITIAL_SIGMA_SOXL = 0.0818
INITIAL_SIGMA_JPM = 0.01995

def load_config():
    config_path = "config.json" 
    try:
        with open(config_path, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        default_cfg = {
            "POSITIONS": {
                "SOXL": {
                    "ENTRY_MULTIPLIER": 1.5,
                    "DAILY_SIGMA": INITIAL_SIGMA_SOXL,
                    "LAST_SIGMA_UPDATE": "2026-05-29"
                },
                "JPM": {
                    "ENTRY_MULTIPLIER": 1.5,
                    "DAILY_SIGMA": INITIAL_SIGMA_JPM,
                    "LAST_SIGMA_UPDATE": datetime.now().strftime("%Y-%m-%d")
                }
            },
            "LAST_MONTHLY_PING": ""
        }
        return default_cfg
              
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
            "SOXL_LONG":  {"qty": 0, "avg_price": 0.0},
            "SOXL_SHORT": {"qty": 0, "avg_price": 0.0},
            "JPM_LONG":   {"qty": 0, "avg_price": 0.0},
        }

def update_positions_from_ledger(cfg):
    ledger = load_ledger()
    for ticker in ["SOXL", "JPM"]:
        pos = cfg.setdefault("POSITIONS", {}).setdefault(ticker, {})
        targets = ["LONG", "SHORT"] if ticker == "SOXL" else ["LONG"]
        
        for key in targets:
            ledger_key = f"{ticker}_{key}"
            entry     = ledger.get(ledger_key, {})
            qty       = entry.get("qty", 0)
            avg_price = entry.get("avg_price", 0.0)

            pos[f"TOTAL_SHARES_{key}"] = qty
            pos[f"MY_AVG_PRICE_{key}"] = avg_price
            print(f"   📒 [{ticker} {key}] 보유 {qty}주 / 평균가격 ${avg_price:.4f}")

# ───────────────────────────────────────────────
# 시그마 및 월말 핑 로직
# ───────────────────────────────────────────────

def check_and_update_sigma(config):
    updated = False
    messages = []
    for ticker in ["SOXL", "JPM"]:
        pos = config["POSITIONS"].get(ticker, {})
        last_update_str = pos.get("LAST_SIGMA_UPDATE", "2026-05-29")
        last_update = datetime.strptime(last_update_str, "%Y-%m-%d")
        today = datetime.now()
        
        if (today - last_update).days >= 365:
            try:
                hist = yf.Ticker(ticker).history(period="365d", auto_adjust=True)
                new_sigma = round(float(hist['Close'].pct_change().dropna().std()), 6)
                pos["DAILY_SIGMA"] = new_sigma
                pos["LAST_SIGMA_UPDATE"] = today.strftime("%Y-%m-%d")
                updated = True
                messages.append(f"📊 {ticker} 시그마 자동 갱신: {new_sigma}")
            except Exception as e:
                messages.append(f"⚠️ {ticker} 시그마 갱신 실패: {e}")
    return updated, "\n".join(messages)

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
# 시세 및 알림 로직
# ───────────────────────────────────────────────

def get_market_mode():
    now_ny = datetime.now(ZoneInfo("America/New_York"))
    hour    = now_ny.hour + now_ny.minute / 60.0
    is_dst  = now_ny.dst() != timedelta(0)
    tz_label = "EDT (서머타임)" if is_dst else "EST"
    print(f"🕒 뉴욕 현재 시각: {now_ny.strftime('%Y-%m-%d %H:%M:%S')} ({tz_label})")
    mode = "장중" if 9.5 <= hour < 16.0 else "장전"
    return mode, now_ny

def get_ticker_data(ticker):
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period="5d", auto_adjust=False)
        if len(hist) < 2: return None, None
        prev_close = float(hist['Close'].iloc[-1])
        current_price = float(t.fast_info.last_price)
        return prev_close, current_price
    except:
        return None, None
    
def send_discord(webhook_url, user_id, title, content):
    if not webhook_url: return
    payload = {
        "content": f"<@{user_id}> " if user_id else "",
        "embeds": [{
            "title":   title,
            "description": content,
            "color":      3447003,
            "timestamp":  datetime.now(timezone.utc).isoformat()
        }]
    }
    try:
        res = requests.post(webhook_url, json=payload, timeout=15)
        if res.status_code == 204: print("✅ 디스코드 알림 전송 성공")
        else: print(f"❌ 디스코드 응답 에러 (코드: {res.status_code})")
    except Exception as e:
        print(f"❌ 디스코드 전송 실패: {e}")

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
    
    # VIX 데이터 조회
    try:
        vix = yf.Ticker("^VIX").history(period="1d")
        vix_price = float(vix['Close'].iloc[-1]) if not vix.empty else 0.0
    except:
        vix_price = 0.0
    
    lines = [f"{MODE_EMOJI[mode]} {mode} 모드 | {now_ny.strftime('%Y-%m-%d %H:%M %Z')}", f"• VIX 지수: {vix_price:.2f}"]
    
    for ticker in ["SOXL", "JPM"]:
        pos_cfg = cfg["POSITIONS"][ticker]
        prev_close, current_price = get_ticker_data(ticker)
        
        if prev_close is None:
            lines.append(f"\n⚠️ {ticker} 데이터 수신 실패")
            continue
            
        loc_price = calc_loc(prev_close, pos_cfg.get("ENTRY_MULTIPLIER", 1.5), pos_cfg.get("DAILY_SIGMA", 0.0818))
        
        ticker_info = [f"\n🔹 **{ticker}**", f"• 전일 종가: ${prev_close:.2f} / LOC 예정가: ${loc_price:.2f}"]
        if mode == "장중":
            ticker_info.append(f"• 현재가: ${current_price:.2f}")

        targets = ["LONG", "SHORT"] if ticker == "SOXL" else ["LONG"]
        for k in targets:
            qty = pos_cfg.get(f"TOTAL_SHARES_{k}", 0)
            avg = pos_cfg.get(f"MY_AVG_PRICE_{k}", 0)
            msg = f"• [{k}] 보유: {qty}주"
            if qty > 0 and avg > 0:
                msg += f" / 평균: ${avg:.4f} / 수익: {(current_price - avg)/avg*100:+.2f}%"
            ticker_info.append(msg)
        lines.extend(ticker_info)
    
    if sigma_changed:
        lines.append(f"\n{sigma_msg}")

    send_discord(os.environ.get("DISCORD_WEBHOOK") or cfg.get("DISCORD_WEBHOOK", ""), 
                 os.environ.get("DISCORD_USER_ID") or cfg.get("DISCORD_USER_ID", ""), 
                 f"{MODE_EMOJI[mode]} {mode} 브리핑", "\n".join(lines))
            
if __name__ == "__main__":
    execute_dual_tactical_trader()