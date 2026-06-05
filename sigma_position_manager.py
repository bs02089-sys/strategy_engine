"""
sigma_position_manager.py
SOXL VIX Sigma DCA 자동화 — 심플 브리핑 + 월초 핑
"""

import os
import sys
import json
import shutil
import tempfile
import numpy as np
import warnings
import requests
import yfinance as yf
from datetime import datetime
from zoneinfo import ZoneInfo

# ── 인코딩 설정 ──────────────────────────────────────────────
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

warnings.filterwarnings("ignore", category=FutureWarning)

# ── 상수 ─────────────────────────────────────────────────────
TARGET_TICKERS     = ["BOTZ", "SOXX", "SOXL"]
CONFIG_PATH        = "config.json"
LEDGER_PATH        = "ledger.json"

# LOOKBACK_DAYS → yfinance period 매핑 (원래 코드 복원)
_PERIOD_MAP = {
    90: "3mo",
    180: "6mo",
    365: "1y"
}

# ═══════════════════════════════════════════════════════════
# I/O 헬퍼
# ═══════════════════════════════════════════════════════════

def load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"⚠️ {CONFIG_PATH} 없음 — 기본값 생성")
        return {"POSITIONS": {}, "LAST_MONTHLY_PING": "", "STRATEGY": {"VIX_THRESHOLD": 25.0}}
    except json.JSONDecodeError as e:
        raise RuntimeError(f"config.json 파싱 오류: {e}") from e


def save_config(cfg: dict) -> None:
    try:
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json", encoding="utf-8") as tmp:
            json.dump(cfg, tmp, indent=4, ensure_ascii=False)
            tmp_path = tmp.name
        shutil.move(tmp_path, CONFIG_PATH)
    except Exception as e:
        print(f"⚠️ config 저장 실패: {e}")


def load_ledger() -> dict:
    default = {
        "SOXL_LONG": {"qty": 0, "avg_price": 0.0},
        "SOXL_SHORT": {"qty": 0, "avg_price": 0.0},
        "BOTZ_LONG": {"qty": 0, "avg_price": 0.0},
        "SOXX_LONG": {"qty": 0, "avg_price": 0.0},
    }
    try:
        with open(LEDGER_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


# ═══════════════════════════════════════════════════════════
# 포지션 동기화
# ═══════════════════════════════════════════════════════════

def sync_positions_from_ledger(cfg: dict) -> None:
    ledger = load_ledger()
    positions = cfg.setdefault("POSITIONS", {})
    
    for ticker in TARGET_TICKERS:
        pos = positions.setdefault(ticker, {})
        directions = ["LONG", "SHORT"] if ticker == "SOXL" else ["LONG"]
        for d in directions:
            entry = ledger.get(f"{ticker}_{d}", {})
            pos[f"TOTAL_SHARES_{d}"] = entry.get("qty", 0)
            pos[f"MY_AVG_PRICE_{d}"] = entry.get("avg_price", 0.0)


# ═══════════════════════════════════════════════════════════
# 월초 운영 핑
# ═══════════════════════════════════════════════════════════

def send_monthly_ping_if_due(cfg: dict, webhook: str, user_id: str) -> None:
    now = datetime.now()
    if now.day != 1:
        return
    today_ym = now.strftime("%Y-%m")
    if cfg.get("LAST_MONTHLY_PING") == today_ym:
        return

    msg = f"🔔 **월초 운영 핑** | {now.strftime('%Y년 %m월')}\n운용 시스템이 정상 가동 중입니다."
    send_discord(webhook, user_id, "🗓️ 월간 운영 핑", msg)
    cfg["LAST_MONTHLY_PING"] = today_ym


# ═══════════════════════════════════════════════════════════
# 시그마 자동 갱신
# ═══════════════════════════════════════════════════════════

def refresh_sigma_if_stale(cfg: dict) -> list[str]:
    messages = []
    today = datetime.now().date()

    for ticker in TARGET_TICKERS:
        pos = cfg["POSITIONS"].get(ticker, {})
        lookback_days = int(pos.get("LOOKBACK_DAYS", 365))
        
        # _PERIOD_MAP 사용 (원래 로직 복원)
        period = _PERIOD_MAP.get(lookback_days, "1y")

        last_str = pos.get("LAST_SIGMA_UPDATE", "2000-01-01")
        try:
            last_dt = datetime.strptime(last_str, "%Y-%m-%d").date()
        except ValueError:
            last_dt = datetime(2000, 1, 1).date()

        if (today - last_dt).days < lookback_days:
            continue

        try:
            hist = yf.Ticker(ticker).history(period=period, auto_adjust=True)
            if hist.empty or len(hist) < 10:
                messages.append(f"⚠️ {ticker} 데이터 부족")
                continue

            new_sigma = round(float(hist["Close"].pct_change().dropna().std()), 6)
            pos["DAILY_SIGMA"] = new_sigma
            pos["LAST_SIGMA_UPDATE"] = today.strftime("%Y-%m-%d")
            messages.append(f"📊 {ticker} 시그마 갱신 ({period} 기준): {new_sigma:.6f}")
        except Exception as e:
            messages.append(f"⚠️ {ticker} 시그마 갱신 실패: {e}")

    return messages


# ═══════════════════════════════════════════════════════════
# 가격 조회
# ═══════════════════════════════════════════════════════════

def get_prev_close(ticker: str) -> float | None:
    """Twelve Data API를 사용한 전일 종가 조회"""
    try:
        # 환경변수에서만 API Key 가져오기 (PowerShell 등록 우선)
        api_key = os.environ.get("TWELVEDATA_API_KEY")
        
        if not api_key:
            print(f"⚠️ {ticker}: TWELVEDATA_API_KEY 환경변수가 설정되지 않았습니다.")
            return None

        from twelvedata import TDClient
        td = TDClient(apikey=api_key)
        
        ts = td.time_series(
            symbol=ticker,
            interval="1day",
            outputsize=10
        ).as_json()
        
        if ts and "values" in ts and len(ts["values"]) > 0:
            prev_close = float(ts["values"][0]["close"])
            print(f"📌 {ticker} → Twelve Data 전일 종가: ${prev_close:.2f}")
            return prev_close
        else:
            print(f"⚠️ {ticker}: Twelve Data 응답 데이터 없음")
            return None
            
    except ImportError:
        print("⚠️ twelvedata 라이브러리가 설치되지 않았습니다. → pip install twelvedata")
        return None
    except Exception as e:
        print(f"❌ {ticker} Twelve Data 조회 실패: {e}")
        return None
                                
    
def get_vix() -> float | None:
    try:
        hist = yf.Ticker("^VIX").history(period="1d")
        if hist.empty:
            return None
        val = float(hist["Close"].iloc[-1])
        return val if not np.isnan(val) else None
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════
# 계산 & 디스코드
# ═══════════════════════════════════════════════════════════

def calc_loc(prev_close: float, multiplier: float, sigma: float) -> float:
    return prev_close * np.exp(-multiplier * sigma)


def send_discord(webhook_url: str, user_id: str, title: str, content: str) -> None:
    if not webhook_url:
        print("⚠️ DISCORD_WEBHOOK 미설정")
        return

    payload = {
        "content": f"<@{user_id}>" if user_id else "",
        "embeds": [{
            "title": title[:256],
            "description": content[:4096],
            "color": 3447003,
            "timestamp": datetime.now(ZoneInfo("UTC")).isoformat(),
        }],
    }
    try:
        resp = requests.post(webhook_url, json=payload, timeout=15)
        print(f"✅ 디스코드 전송 {'성공' if resp.ok else '실패'}")
    except Exception as e:
        print(f"❌ 디스코드 전송 오류: {e}")


# ═══════════════════════════════════════════════════════════
# 메인
# ═══════════════════════════════════════════════════════════

def execute_dual_tactical_trader() -> None:
    cfg = load_config()
    
    webhook = os.environ.get("DISCORD_WEBHOOK") or cfg.get("DISCORD_WEBHOOK", "")
    user_id = os.environ.get("DISCORD_USER_ID") or cfg.get("DISCORD_USER_ID", "")
    vix_threshold = cfg.get("STRATEGY", {}).get("VIX_THRESHOLD", 25.0)

    # 시스템 루틴
    sigma_messages = refresh_sigma_if_stale(cfg)
    sync_positions_from_ledger(cfg)
    send_monthly_ping_if_due(cfg, webhook, user_id)
    save_config(cfg)

    # VIX
    vix_price = get_vix()
    is_intensive = vix_price is not None and vix_price >= vix_threshold

    # 브리핑
    now_ny = datetime.now(ZoneInfo("America/New_York"))
    lines = [
        f"📊 SIGMA DCA 브리핑 | {now_ny.strftime('%Y-%m-%d %H:%M')}",
        f"• VIX: {vix_price:.2f} {'🚀 집중 매수 모드' if is_intensive else ''}" if vix_price else "• VIX: 조회 실패 ⚠️",
    ]

    for ticker in TARGET_TICKERS:
        pos_cfg = cfg["POSITIONS"].get(ticker, {})
        prev_close = get_prev_close(ticker)
        
        if prev_close is None:
            lines.append(f"\n🔹 **{ticker}** — 가격 조회 실패 ⚠️")
            continue

        multiplier = pos_cfg.get("ENTRY_MULTIPLIER", 1.5)
        sigma = pos_cfg.get("DAILY_SIGMA", 0.05)
        loc_price = calc_loc(prev_close, multiplier, sigma)

        ticker_info = [
            f"\n🔹 **{ticker}**",
            f"• 전일 종가 : ${prev_close:.2f}",
            f"• LOC 예정가: ${loc_price:.2f}",
        ]
        
        if is_intensive:
            ticker_info.append("• 💡 집중 매수 모드")

        lines.extend(ticker_info)

    if sigma_messages:
        lines.append("\n" + "\n".join(sigma_messages))

    send_discord(webhook, user_id, "📊 SIGMA DCA 브리핑", "\n".join(lines))


if __name__ == "__main__":
    execute_dual_tactical_trader()