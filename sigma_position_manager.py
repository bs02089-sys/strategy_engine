import os
import sys
import json
import numpy as np
import warnings
import requests
import yfinance as yf
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

# ── 인코딩 설정 (GitHub Actions 한글 깨짐 방지)
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

warnings.filterwarnings('ignore', category=FutureWarning)

MODE_EMOJI = {"장전": "🌙", "장중": "☀️"}

# ───────────────────────────────────────────────
# 설정 / 장부 로드 (원본 유지)
# ───────────────────────────────────────────────

def load_config():
    try:
        with open("config.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"❌ config.json 로드 실패: {e}")
        sys.exit(1)

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

# ───────────────────────────────────────────────
# 장부 → config.json 자동 갱신 (원본 유지)
# ───────────────────────────────────────────────

def update_positions_from_ledger(cfg):
    ledger = load_ledger()
    pos    = cfg.setdefault("POSITIONS", {}).setdefault("SOXL", {})

    for key, ledger_key in [("LONG", "SOXL_LONG"), ("SHORT", "SOXL_SHORT")]:
        entry     = ledger.get(ledger_key, {})
        qty       = entry.get("qty",       0)
        avg_price = entry.get("avg_price", 0.0)

        pos[f"TOTAL_SHARES_{key}"] = qty
        pos[f"MY_AVG_PRICE_{key}"] = avg_price

        print(f"   📒 [{key}] 보유 {qty}주 / 평단 ${avg_price:.4f}")

    save_config(cfg)

# ───────────────────────────────────────────────
# 시그마 자동 갱신 로직 (추가된 부분)
# ───────────────────────────────────────────────

def check_and_update_sigma(cfg):
    pos = cfg["POSITIONS"]["SOXL"]
    last_update_str = pos.get("LAST_SIGMA_UPDATE", "2000-01-01")
    last_update = datetime.strptime(last_update_str, "%Y-%m-%d")
    
    if (datetime.now() - last_update).days > 180:
        print("🔄 6개월 경과, 시그마 재계산 중...")
        data = yf.Ticker("SOXL").history(period="250d")
        new_sigma = float(data['Close'].pct_change().dropna().std())
        
        pos["DAILY_SIGMA"] = round(new_sigma, 4)
        pos["LAST_SIGMA_UPDATE"] = datetime.now().strftime("%Y-%m-%d")
        save_config(cfg)
        print(f"✅ 시그마 갱신 완료: {pos['DAILY_SIGMA']}")
    return pos.get("DAILY_SIGMA", 0.04)

# ───────────────────────────────────────────────
# 장 시간 판별 (원본 유지)
# ───────────────────────────────────────────────

def get_market_mode():
    now_ny   = datetime.now(ZoneInfo("America/New_York"))
    hour     = now_ny.hour + now_ny.minute / 60.0
    is_dst   = now_ny.dst() != timedelta(0)
    tz_label = "EDT (서머타임)" if is_dst else "EST"

    print(f"🕒 뉴욕 현재 시각: {now_ny.strftime('%Y-%m-%d %H:%M:%S')} ({tz_label})")

    mode = "장중" if 9.5 <= hour < 16.0 else "장전"
    return mode, now_ny

# ───────────────────────────────────────────────
# 시세 데이터 조회 (원본 유지)
# ───────────────────────────────────────────────

def get_market_data(mode):
    try:
        soxl = yf.Ticker("SOXL")
        hist = soxl.history(period="3d", auto_adjust=False)

        if len(hist) < 2:
            print("⚠️ SOXL 데이터 부족 (3d 기준 2일치 미만)")
            return None, None, None

        now_ny   = datetime.now(ZoneInfo("America/New_York"))
        is_today = (hist.index[-1].date() == now_ny.date())

        prev_close = float(
            hist['Close'].iloc[-2] if is_today else hist['Close'].iloc[-1]
        )

        if mode == "장중":
            current_open = float(hist['Open'].iloc[-1] if is_today else prev_close)
        else:
            current_open = prev_close

        current_price = float(soxl.fast_info.last_price)

        return prev_close, current_open, current_price

    except Exception as e:
        print(f"❌ 시세 조회 실패: {e}")
        return None, None, None

# ───────────────────────────────────────────────
# 디스코드 알림 전송 (원본 유지)
# ───────────────────────────────────────────────

def send_discord(webhook_url, user_id, title, content):
    if not webhook_url:
        print("⚠️ DISCORD_WEBHOOK이 설정되지 않았습니다.")
        return

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
        if res.status_code == 204:
            print("✅ 디스코드 알림 전송 성공")
        else:
            print(f"❌ 디스코드 응답 에러 (코드: {res.status_code})")
    except Exception as e:
        print(f"❌ 디스코드 전송 실패: {e}")

# ───────────────────────────────────────────────
# LOC 매수 타점 계산 (원본 유지)
# ───────────────────────────────────────────────

def calc_loc(current_open, prev_close, FIXED_SIGMA, DAILY_SIGMA):
    loc_price = prev_close * np.exp(-FIXED_SIGMA * DAILY_SIGMA)
    return loc_price

# ───────────────────────────────────────────────
# 메인 실행
# ───────────────────────────────────────────────

# ───────────────────────────────────────────────
# 메인 실행부 (조건부 출력 로직 적용)
# ───────────────────────────────────────────────

def execute_dual_tactical_trader():
    mode, now_ny = get_market_mode()
    cfg = get_config()
    DAILY_SIGMA = check_and_update_sigma(cfg)
    update_positions_from_ledger(cfg)
    cfg = get_config()
    pos_cfg = cfg["POSITIONS"]["SOXL"]
    
    prev_close, curr_open, curr_price = get_market_data(mode)
    if prev_close is None: return
    
    # LOC 계산
    loc_price = calc_loc(curr_open, prev_close, pos_cfg.get("FIXED_SIGMA", 1.5), DAILY_SIGMA)

    # 1. 메시지 구성 (공통 헤더)
    lines = [f"**{MODE_EMOJI[mode]} {mode} 모드** | {now_ny.strftime('%Y-%m-%d %H:%M %Z')}\n"]
    
    # 2. 모드별 정보 출력
    if mode == "장전":
        # 장전: 전일 종가, LOC 예정가
        lines.append(f"• 전일 종가: **${prev_close:.2f}**")
        lines.append(f"• LOC 예정가: **${loc_price:.2f}**")
    else:
        # 장중: 전일 종가, 현재가, LOC 예정가
        lines.append(f"• 전일 종가: ${prev_close:.2f}")
        lines.append(f"• 현재가: **${curr_price:.2f}**")
        lines.append(f"• LOC 예정가: **${loc_price:.2f}**")
    
    # 3. 계좌 정보 (수량, 평단, 수익률 - 공통)
    for k in ["LONG", "SHORT"]:
        qty = pos_cfg.get(f"TOTAL_SHARES_{k}", 0)
        avg = pos_cfg.get(f"MY_AVG_PRICE_{k}", 0)
        msg = f"\n**{ '🟢' if k == 'LONG' else '🔵' } [{k}] {'매수' if k == 'LONG' else '매도'} 계좌**\n"
        if qty > 0 and avg > 0:
            msg += f"• {qty}주 / 평단 ${avg:.4f} / 수익률 {(curr_price - avg)/avg*100:+.2f}%"
        else: 
            msg += "• 보유 물량 없음"
        lines.append(msg)
    
    # 4. 디스코드 전송
    full_content = "\n".join(lines)
    send_discord(os.environ.get("DISCORD_WEBHOOK") or cfg.get("DISCORD_WEBHOOK"), 
                 os.environ.get("DISCORD_USER_ID") or cfg.get("DISCORD_USER_ID"), 
                 f"{MODE_EMOJI[mode]} {mode} 브리핑", full_content)
    

if __name__ == "__main__":
    execute_dual_tactical_trader()