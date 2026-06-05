import os
import sys
import json
import shutil
import tempfile
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

# Discord 임베드 필드 제한
_DISCORD_TITLE_LIMIT   = 256
_DISCORD_CONTENT_LIMIT = 4096

# ───────────────────────────────────────────────
# 설정 및 파일 로드
# ───────────────────────────────────────────────
def load_config():
    try:
        with open("config.json", 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        print("⚠️ config.json 없음 — 기본값으로 초기화합니다.")
        return {"POSITIONS": {}, "LAST_MONTHLY_PING": "", "STRATEGY": {"VIX_THRESHOLD": 25.0}}
    except json.JSONDecodeError as e:
        raise RuntimeError(f"config.json 파싱 오류: {e}") from e

def save_config(cfg):
    """임시 파일 → rename 방식으로 원자적 저장 (race condition 방지)"""
    try:
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json", encoding="utf-8") as tmp:
            json.dump(cfg, tmp, indent=4, ensure_ascii=False)
            tmp_path = tmp.name
        shutil.move(tmp_path, "config.json")
    except Exception as e:
        print(f"⚠️ config.json 저장 실패: {e}")

def load_ledger():
    default = {
        "SOXL_LONG":  {"qty": 0, "avg_price": 0.0},
        "SOXL_SHORT": {"qty": 0, "avg_price": 0.0},
        "BOTZ_LONG":  {"qty": 0, "avg_price": 0.0},
        "SOXX_LONG":  {"qty": 0, "avg_price": 0.0},
    }
    try:
        with open("ledger.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print("⚠️ ledger.json 없음 — 빈 포지션으로 처리합니다.")
        return default
    except json.JSONDecodeError as e:
        print(f"⚠️ ledger.json 파싱 오류: {e}")
        return default

def update_positions_from_ledger(cfg):
    ledger = load_ledger()
    for ticker in TARGET_TICKERS:
        pos = cfg.setdefault("POSITIONS", {}).setdefault(ticker, {})
        targets = ["LONG", "SHORT"] if ticker == "SOXL" else ["LONG"]
        for key in targets:
            entry = ledger.get(f"{ticker}_{key}", {})
            pos[f"TOTAL_SHARES_{key}"] = entry.get("qty", 0)
            pos[f"MY_AVG_PRICE_{key}"]  = entry.get("avg_price", 0.0)

# ───────────────────────────────────────────────
# 시그마 갱신 (연 1회)
# ───────────────────────────────────────────────
def check_and_update_sigma(config):
    updated  = False
    messages = []
    today    = datetime.now()

    for ticker in TARGET_TICKERS:
        pos = config["POSITIONS"].get(ticker, {})
        last_update_str = pos.get("LAST_SIGMA_UPDATE", "2000-01-01")
        try:
            last_update = datetime.strptime(last_update_str, "%Y-%m-%d")
        except ValueError:
            last_update = datetime(2000, 1, 1)

        if (today - last_update).days < 365:
            continue

        try:
            hist = yf.Ticker(ticker).history(period="1y", auto_adjust=True)
            if hist.empty or len(hist) < 10:
                messages.append(f"⚠️ {ticker} 시그마 갱신 실패: 데이터 부족")
                continue
            new_sigma = round(float(hist['Close'].pct_change().dropna().std()), 6)
            pos["DAILY_SIGMA"]        = new_sigma
            pos["LAST_SIGMA_UPDATE"]  = today.strftime("%Y-%m-%d")
            updated = True
            messages.append(f"📊 {ticker} 시그마 갱신 (1년 기준): {new_sigma:.6f}")
        except Exception as e:
            messages.append(f"⚠️ {ticker} 시그마 갱신 실패: {e}")

    return updated, "\n".join(messages)

# ───────────────────────────────────────────────
# 월초 핑
# ───────────────────────────────────────────────
def check_monthly_ping(cfg):
    now = datetime.now()
    if now.day != 1:
        return False
    today_str = now.strftime("%Y-%m")
    if cfg.get("LAST_MONTHLY_PING") == today_str:
        return False
    msg = f"🔔 **월초 핑**: {now.strftime('%Y년 %m월')} 운용 시스템이 정상 가동 중입니다."
    send_discord(
        os.environ.get("DISCORD_WEBHOOK") or cfg.get("DISCORD_WEBHOOK", ""),
        os.environ.get("DISCORD_USER_ID")  or cfg.get("DISCORD_USER_ID",  ""),
        "🗓️ 월간 운영 핑", msg
    )
    cfg["LAST_MONTHLY_PING"] = today_str
    return True

# ───────────────────────────────────────────────
# 시장 모드 / 가격 조회
# ───────────────────────────────────────────────
def get_market_mode():
    now_ny = datetime.now(ZoneInfo("America/New_York"))
    hour   = now_ny.hour + now_ny.minute / 60.0
    mode   = "장중" if 9.5 <= hour < 16.0 else "장전"
    return mode, now_ny

def get_ticker_data(ticker, mode):
    """
    전일 확정 종가와 현재가를 반환한다.
    - 장전: hist[-1]이 전일 확정 종가
    - 장중: hist[-1]은 미완성 봉이므로 날짜 확인 후 [-2] 사용
    - float 변환 후 NaN 명시 검증 (yfinance가 빈 응답 시 NaN 반환 가능)
    - fast_info.last_price가 None이면 prev_close로 대체
    """
    try:
        t    = yf.Ticker(ticker)
        hist = t.history(period="10d", auto_adjust=False)
        if hist.empty or len(hist) < 2:
            print(f"⚠️ {ticker}: 가격 데이터 부족 ({len(hist)}봉)")
            return None, None

        # Close 컬럼 NaN 제거 후 유효 데이터 확인
        close_valid = hist['Close'].dropna()
        if len(close_valid) < 2:
            print(f"⚠️ {ticker}: 유효 Close 데이터 부족 (NaN 제외 {len(close_valid)}봉)")
            return None, None

        if mode == "장중":
            last_date = hist.index[-1].date()
            today_ny  = datetime.now(ZoneInfo("America/New_York")).date()
            prev_close = float(close_valid.iloc[-2]) if last_date == today_ny else float(close_valid.iloc[-1])
        else:
            prev_close = float(close_valid.iloc[-1])

        # NaN 최종 검증
        if np.isnan(prev_close):
            print(f"⚠️ {ticker}: prev_close NaN — 데이터 무효")
            return None, None

        # fast_info.last_price가 None이면 prev_close 사용
        raw_price     = t.fast_info.last_price
        current_price = float(raw_price) if (raw_price is not None and not np.isnan(float(raw_price))) else prev_close

        return prev_close, current_price

    except Exception as e:
        print(f"❌ {ticker} 데이터 에러: {e}")
        return None, None

# ───────────────────────────────────────────────
# 디스코드 전송
# ───────────────────────────────────────────────
def send_discord(webhook_url, user_id, title, content):
    if not webhook_url:
        print("⚠️ DISCORD_WEBHOOK 미설정 — 전송 생략")
        return
    if not webhook_url.startswith("https://discord.com/api/webhooks/"):
        print(f"⚠️ DISCORD_WEBHOOK 형식 오류: {webhook_url[:40]}...")
        return

    # 길이 초과 방지 (Discord API 제한)
    if len(title) > _DISCORD_TITLE_LIMIT:
        title = title[:_DISCORD_TITLE_LIMIT - 3] + "..."
    if len(content) > _DISCORD_CONTENT_LIMIT:
        content = content[:_DISCORD_CONTENT_LIMIT - 3] + "..."
        print(f"⚠️ Discord content {_DISCORD_CONTENT_LIMIT}자 초과 — 잘림 처리")

    payload = {
        "content": f"<@{user_id}>" if user_id else "",
        "embeds": [{"title": title, "description": content, "color": 3447003,
                    "timestamp": datetime.now(timezone.utc).isoformat()}]
    }
    try:
        resp = requests.post(webhook_url, json=payload, timeout=15)
        if not resp.ok:
            print(f"❌ 디스코드 전송 실패 — HTTP {resp.status_code}: {resp.text}")
        else:
            print(f"✅ 디스코드 전송 성공 — HTTP {resp.status_code}")
        resp.raise_for_status()
    except requests.exceptions.Timeout:
        print("❌ 디스코드 전송 실패 — 타임아웃 (15s)")
    except requests.exceptions.ConnectionError as e:
        print(f"❌ 디스코드 전송 실패 — 연결 오류: {e}")
    except Exception as e:
        print(f"❌ 디스코드 전송 실패: {e}")

# ───────────────────────────────────────────────
# 계산 유틸
# ───────────────────────────────────────────────
def calc_loc(prev_close, entry_multiplier, daily_sigma):
    return prev_close * np.exp(-entry_multiplier * daily_sigma)

def calc_profit_pct(current, avg, direction):
    """LONG: (현재-평균)/평균 / SHORT: (평균-현재)/평균"""
    if avg <= 0:
        return 0.0
    return (avg - current) / avg * 100 if direction == "SHORT" else (current - avg) / avg * 100

# ───────────────────────────────────────────────
# 메인 실행부
# ───────────────────────────────────────────────
def execute_dual_tactical_trader():
    mode, now_ny = get_market_mode()
    cfg = load_config()

    webhook = os.environ.get("DISCORD_WEBHOOK") or cfg.get("DISCORD_WEBHOOK", "")
    user_id = os.environ.get("DISCORD_USER_ID")  or cfg.get("DISCORD_USER_ID",  "")
    vix_threshold = cfg.get("STRATEGY", {}).get("VIX_THRESHOLD", 25.0)

    sigma_changed, sigma_msg = check_and_update_sigma(cfg)
    update_positions_from_ledger(cfg)
    check_monthly_ping(cfg)
    save_config(cfg)

    # VIX 조회
    try:
        vix_hist  = yf.Ticker("^VIX").history(period="1d")
        vix_price = float(vix_hist['Close'].iloc[-1]) if not vix_hist.empty else None
    except Exception as e:
        print(f"⚠️ VIX 조회 실패: {e}")
        vix_price = None

    is_intensive = (vix_price is not None) and (vix_price >= vix_threshold)
    vix_line = (
        f"• VIX 지수: {vix_price:.2f}" + (" 🚀 [집중 매수 모드 활성화]" if is_intensive else "")
        if vix_price is not None else "• VIX 지수: 조회 실패 ⚠️"
    )

    lines = [
        f"{MODE_EMOJI[mode]} {mode} 모드 | {now_ny.strftime('%Y-%m-%d %H:%M %Z')}",
        vix_line,
    ]

    for ticker in TARGET_TICKERS:
        pos_cfg = cfg["POSITIONS"].get(ticker, {})
        prev_close, current_price = get_ticker_data(ticker, mode)
        if prev_close is None:
            lines.append(f"\n🔹 **{ticker}** — 가격 데이터 조회 실패 ⚠️")
            continue

        loc_price = calc_loc(
            prev_close,
            pos_cfg.get("ENTRY_MULTIPLIER", 1.5),
            pos_cfg.get("DAILY_SIGMA", 0.05)
        )

        ticker_info = [
            f"\n🔹 **{ticker}**",
            f"• 전일 종가: ${prev_close:.2f} / LOC 예정가: ${loc_price:.2f}"
        ]
        if is_intensive:
            ticker_info.append("• 💡 **[집중 매수] LOC 도달 시 평소 2배 물량 투입**")
        if mode == "장중":
            ticker_info.append(f"• 현재가: ${current_price:.2f}")

        targets = ["LONG", "SHORT"] if ticker == "SOXL" else ["LONG"]
        for k in targets:
            qty = pos_cfg.get(f"TOTAL_SHARES_{k}", 0)
            avg = pos_cfg.get(f"MY_AVG_PRICE_{k}", 0.0)
            if qty > 0:
                line = f"• [{k}] 보유: {qty}주"
                if avg > 0:
                    profit = calc_profit_pct(current_price, avg, k)
                    line  += f" / 수익: {profit:+.2f}%"
                ticker_info.append(line)

        lines.extend(ticker_info)

    if sigma_changed:
        lines.append(f"\n{sigma_msg}")

    send_discord(webhook, user_id, f"{MODE_EMOJI[mode]} {mode} 브리핑", "\n".join(lines))

if __name__ == "__main__":
    execute_dual_tactical_trader()
