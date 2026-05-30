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

# 환경 변수와 config 파일에서 값을 불러오는지 확인
webhook = os.environ.get("DISCORD_WEBHOOK")
user_id = os.environ.get("DISCORD_USER_ID")

print(f"DEBUG: 환경변수 Webhook: {webhook}")
print(f"DEBUG: 환경변수 UserID: {user_id}")

# 시스템의 논리적 근거가 되는 검증된 초기값
INITIAL_SIGMA = 0.0818  # 한셀로 직접 검증한 최초의 숫자(2026-05-29)

def load_config():
    config_path = "config.json" 
    
    # 파일이 있으면 읽고, 없으면 default_cfg를 사용하게 로직 추가
    try:
        with open(config_path, 'r') as f:
            config = json.load(f)
            return config
    except FileNotFoundError:
        # 파일이 없을 때 default_cfg를 시스템에 로드
        default_cfg = {
            "POSITIONS": {
                "SOXL": {
                    "ENTRY_MULTIPLIER": 1.5,
                    "DAILY_SIGMA": INITIAL_SIGMA,
                    "LAST_SIGMA_UPDATE": "2026-05-29"
                }
            }
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
        }

def update_positions_from_ledger(cfg):
    ledger = load_ledger()
    pos    = cfg.setdefault("POSITIONS", {}).setdefault("SOXL", {})

    for key, ledger_key in [("LONG", "SOXL_LONG"), ("SHORT", "SOXL_SHORT")]:
        entry     = ledger.get(ledger_key, {})
        qty       = entry.get("qty",       0)
        avg_price = entry.get("avg_price", 0.0)

        pos[f"TOTAL_SHARES_{key}"] = qty
        pos[f"MY_AVG_PRICE_{key}"] = avg_price
        print(f"   📒 [{key}] 보유 {qty}주 / 평균가격 ${avg_price:.4f}")

# ───────────────────────────────────────────────
# 시그마 자동 갱신 로직
# ───────────────────────────────────────────────

def check_and_update_sigma(config):
    last_update = datetime.strptime(config["POSITIONS"]["SOXL"]["LAST_SIGMA_UPDATE"], "%Y-%m-%d")    
    today = datetime.now()
    
    # 180일이 지났는지 확인
    if (today - last_update).days >= 180:
        print("6개월 경과, 시그마 값 재산출 시작...")
        # 1. 새로운 데이터로 시그마 계산
        # 2. config["POSITIONS"]["SOXL"]["DAILY_SIGMA"] = new_sigma_value
        # 3. config["POSITIONS"]["SOXL"]["LAST_SIGMA_UPDATE"] = today.strftime("%Y-%m-%d")
        # 4. json.dump()를 통해 config.json 파일 저장
        print("시그마 값 갱신 완료!")
        
# ───────────────────────────────────────────────
# 장 시간 판별 / 시세 / 디스코드 / 타점 계산 
# ───────────────────────────────────────────────

def get_market_mode():
    now_ny = datetime.now(ZoneInfo("America/New_York"))
    hour     = now_ny.hour + now_ny.minute / 60.0
    is_dst   = now_ny.dst() != timedelta(0)
    tz_label = "EDT (서머타임)" if is_dst else "EST"
    print(f"🕒 뉴욕 현재 시각: {now_ny.strftime('%Y-%m-%d %H:%M:%S')} ({tz_label})")
    mode = "장중" if 9.5 <= hour < 16.0 else "장전"
    return mode, now_ny

def get_market_data(mode):
    try:
        soxl = yf.Ticker("SOXL")
        hist = soxl.history(period="3d", auto_adjust=False)
        if len(hist) < 2: return None, None, None
        now_ny   = datetime.now(ZoneInfo("America/New_York"))
        is_today = (hist.index[-1].date() == now_ny.date())
        prev_close = float(hist['Close'].iloc[-2] if is_today else hist['Close'].iloc[-1])
        current_price = float(soxl.fast_info.last_price)
        return prev_close, current_price
    except Exception as e:
        print(f"❌ 시세 조회 실패: {e}")
        return None, None, None

def send_discord(webhook_url, user_id, title, content):
    if not webhook_url: return
    payload = {
        "content": f"<@{user_id}> " if user_id else "",
        "embeds": [{
            "title":    title,
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

def calc_loc(prev_close, FIXED_SIGMA, DAILY_SIGMA):
    return prev_close * np.exp(-FIXED_SIGMA * DAILY_SIGMA)

# ───────────────────────────────────────────────
# 메인 실행부
# ───────────────────────────────────────────────

def execute_dual_tactical_trader():
    mode, now_ny = get_market_mode()
    cfg = load_config()

    # 1. 시그마 갱신 및 데이터 업데이트
    sigma_changed, sigma_msg = check_and_update_sigma(cfg)
    update_positions_from_ledger(cfg)
    
    # 변경사항 있을 시에만 저장
    if sigma_changed:
        save_config(cfg)
    
    pos_cfg = cfg["POSITIONS"]["SOXL"]
    prev_close, current_price = get_market_data(mode)
    
    # 2. 데이터 없을 시 예외 처리 (함수 종료 없이 메시지 구성 후 진행)
    if prev_close is None:
        print("⚠️ 시장 데이터를 가져오지 못했습니다.")
        lines = [f"{MODE_EMOJI[mode]} {mode} 브리핑", "⚠️ 데이터 수신 실패로 상세 내역을 가져올 수 없습니다."]
    else:
        loc_price = calc_loc(prev_close, pos_cfg.get("FIXED_SIGMA", 1.5), pos_cfg.get("DAILY_SIGMA", 0.04))
        
        # 메시지 구성
        lines = [f"{MODE_EMOJI[mode]} {mode} 모드 | {now_ny.strftime('%Y-%m-%d %H:%M %Z')}"]
        
        if mode == "장전":
            lines.append(f"• 전일 종가: ${prev_close:.2f}\n• LOC 예정가: ${loc_price:.2f}")
        else:
            lines.append(f"• 전일 종가: ${prev_close:.2f}\n• 현재가: ${current_price:.2f}\n• LOC 예정가: ${loc_price:.2f}")

        # 계좌 정보
        for k in ["LONG", "SHORT"]:
            qty = pos_cfg.get(f"TOTAL_SHARES_{k}", 0)
            avg = pos_cfg.get(f"MY_AVG_PRICE_{k}", 0)
            msg = f"[{k}] {'매수' if k == 'LONG' else '매도'} 계좌"
            if qty > 0 and avg > 0:
                msg += f"\n• {qty}주 / 평균가격 ${avg:.4f} / 수익률 {(current_price - avg)/avg*100:+.2f}%"
            else:
                msg += "\n• 보유 물량 없음"
            lines.append(msg)
    
    # 3. 시스템 알림 (항상 전송되도록 로직 통합)
    if sigma_changed:
        lines.append(sigma_msg)

    send_discord(os.environ.get("DISCORD_WEBHOOK") or cfg.get("DISCORD_WEBHOOK", ""), 
                 os.environ.get("DISCORD_USER_ID") or cfg.get("DISCORD_USER_ID", ""), 
                 f"{MODE_EMOJI[mode]} {mode} 브리핑", "\n\n".join(lines))
            
if __name__ == "__main__":
    execute_dual_tactical_trader()