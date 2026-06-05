"""
sigma_position_manager.py
─────────────────────────────────────────────────────────────
SOXL VIX Sigma DCA 자동화 — LOC 예정가 디스코드 브리핑
─────────────────────────────────────────────────────────────
실행 흐름:
  1. config.json 로드
  2. 종목별 주기(LOOKBACK_DAYS)에 따라 시그마 자동 갱신
  3. 월초(1일) 운영 핑 발송
  4. VIX 조회 → 집중 매수 모드 판단
  5. 종목별 전일 종가 · LOC 예정가 계산
  6. 디스코드 브리핑 전송
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
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

warnings.filterwarnings("ignore", category=FutureWarning)

TARGET_TICKERS         = ["BOTZ", "SOXX", "SOXL"]
CONFIG_PATH            = "config.json"
_PERIOD_MAP            = {90: "3mo", 180: "6mo", 365: "1y"}
_DISCORD_TITLE_LIMIT   = 256
_DISCORD_CONTENT_LIMIT = 4096


# ═══════════════════════════════════════════════════════════
# I/O
# ═══════════════════════════════════════════════════════════

def load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"⚠️ {CONFIG_PATH} 없음 — 기본값으로 초기화합니다.")
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
        print(f"⚠️ {CONFIG_PATH} 저장 실패: {e}")


# ═══════════════════════════════════════════════════════════
# 시그마 자동 갱신 — 종목별 LOOKBACK_DAYS 주기
# ═══════════════════════════════════════════════════════════

def refresh_sigma_if_stale(cfg: dict) -> list[str]:
    messages = []
    today    = datetime.now()

    for ticker in TARGET_TICKERS:
        pos           = cfg["POSITIONS"].get(ticker, {})
        lookback_days = int(pos.get("LOOKBACK_DAYS", 365))
        period        = _PERIOD_MAP.get(lookback_days, "1y")

        last_str = pos.get("LAST_SIGMA_UPDATE", "2000-01-01")
        try:
            last_dt = datetime.strptime(last_str, "%Y-%m-%d")
        except ValueError:
            last_dt = datetime(2000, 1, 1)

        if (today - last_dt).days < lookback_days:
            continue

        try:
            hist = yf.Ticker(ticker).history(period=period, auto_adjust=True)
            if hist.empty or len(hist) < 10:
                messages.append(f"⚠️ {ticker} 시그마 갱신 실패: 데이터 부족")
                continue
            new_sigma = round(float(hist["Close"].pct_change().dropna().std()), 6)
            pos["DAILY_SIGMA"]       = new_sigma
            pos["LAST_SIGMA_UPDATE"] = today.strftime("%Y-%m-%d")
            messages.append(f"📊 {ticker} 시그마 갱신 ({period} 기준): {new_sigma:.6f}")
        except Exception as e:
            messages.append(f"⚠️ {ticker} 시그마 갱신 실패: {e}")

    return messages


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
    msg = f"🔔 **월초 핑** | {now.strftime('%Y년 %m월')}\n운용 시스템이 정상 가동 중입니다."
    _send_discord(webhook, user_id, "🗓️ 월간 운영 핑", msg)
    cfg["LAST_MONTHLY_PING"] = today_ym


# ═══════════════════════════════════════════════════════════
# 가격 조회
# ═══════════════════════════════════════════════════════════

def _safe_float(val) -> float | None:
    try:
        if val is None:
            return None
        f = float(val)
        return None if np.isnan(f) else f
    except (TypeError, ValueError):
        return None


def get_prev_close(ticker: str) -> float | None:
    """
    전일 확정 종가를 반환한다.

    history(period="5d", auto_adjust=True) 기준:
    - auto_adjust=True : 배당락 조정 반영 → 증권앱 표시 가격과 일치
    - 뉴욕 현재 날짜와 마지막 봉 날짜를 비교해
      오늘 미완성 봉이 포함된 경우 iloc[-2], 아니면 iloc[-1] 사용
    """
    try:
        t    = yf.Ticker(ticker)
        hist = t.history(period="5d", auto_adjust=True)

        if hist.empty or len(hist) < 1:
            print(f"⚠️ {ticker}: 가격 데이터 없음")
            return None

        close_valid = hist["Close"].dropna()
        if close_valid.empty:
            print(f"⚠️ {ticker}: 유효 Close 없음")
            return None

        # 오늘 봉(미완성) 포함 여부 확인
        last_date = hist.index[-1].date()
        today_ny  = datetime.now(ZoneInfo("America/New_York")).date()

        if last_date == today_ny and len(close_valid) >= 2:
            # 오늘 봉은 미완성 → 전일 확정 봉 사용
            prev_close = _safe_float(close_valid.iloc[-2])
        else:
            prev_close = _safe_float(close_valid.iloc[-1])

        if prev_close is None:
            print(f"⚠️ {ticker}: prev_close NaN")
            return None

        return prev_close

    except Exception as e:
        print(f"❌ {ticker} 가격 조회 실패: {e}")
        return None


def get_vix() -> float | None:
    try:
        hist = yf.Ticker("^VIX").history(period="1d")
        return _safe_float(hist["Close"].iloc[-1]) if not hist.empty else None
    except Exception as e:
        print(f"⚠️ VIX 조회 실패: {e}")
        return None


# ═══════════════════════════════════════════════════════════
# 디스코드
# ═══════════════════════════════════════════════════════════

def _send_discord(webhook_url: str, user_id: str, title: str, content: str) -> None:
    if not webhook_url:
        print("⚠️ DISCORD_WEBHOOK 미설정 — 전송 생략")
        return
    if not webhook_url.startswith("https://discord.com/api/webhooks/"):
        print(f"⚠️ DISCORD_WEBHOOK 형식 오류: {webhook_url[:40]}...")
        return

    if len(title)   > _DISCORD_TITLE_LIMIT:
        title   = title[:_DISCORD_TITLE_LIMIT - 3]   + "..."
    if len(content) > _DISCORD_CONTENT_LIMIT:
        content = content[:_DISCORD_CONTENT_LIMIT - 3] + "..."
        print(f"⚠️ Discord content {_DISCORD_CONTENT_LIMIT}자 초과 — 잘림 처리")

    payload = {
        "content": f"<@{user_id}>" if user_id else "",
        "embeds": [{"title": title, "description": content, "color": 3447003,
                    "timestamp": datetime.now(timezone.utc).isoformat()}],
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


# ═══════════════════════════════════════════════════════════
# 메인
# ═══════════════════════════════════════════════════════════

def execute_dual_tactical_trader() -> None:
    now_ny  = datetime.now(ZoneInfo("America/New_York"))
    cfg     = load_config()
    webhook = os.environ.get("DISCORD_WEBHOOK") or cfg.get("DISCORD_WEBHOOK", "")
    user_id = os.environ.get("DISCORD_USER_ID")  or cfg.get("DISCORD_USER_ID",  "")
    vix_threshold = cfg.get("STRATEGY", {}).get("VIX_THRESHOLD", 25.0)

    # ── 시스템 루틴 ───────────────────────────────────────
    sigma_messages = refresh_sigma_if_stale(cfg)
    send_monthly_ping_if_due(cfg, webhook, user_id)
    save_config(cfg)

    # ── VIX ───────────────────────────────────────────────
    vix_price    = get_vix()
    is_intensive = vix_price is not None and vix_price >= vix_threshold

    if vix_price is not None:
        vix_suffix = " 🚀 [집중 매수 모드]" if is_intensive else ""
        vix_line   = f"• VIX: {vix_price:.2f}{vix_suffix}"
    else:
        vix_line   = "• VIX: 조회 실패 ⚠️"

    # ── 브리핑 ────────────────────────────────────────────
    lines = [
        f"🌙 {now_ny.strftime('%Y-%m-%d %H:%M %Z')}",
        vix_line,
    ]

    for ticker in TARGET_TICKERS:
        pos_cfg    = cfg["POSITIONS"].get(ticker, {})
        prev_close = get_prev_close(ticker)

        if prev_close is None:
            lines.append(f"\n🔹 **{ticker}** — 가격 조회 실패 ⚠️")
            continue

        multiplier = pos_cfg.get("ENTRY_MULTIPLIER", 1.5)
        sigma      = pos_cfg.get("DAILY_SIGMA", 0.05)
        loc_price  = prev_close * np.exp(-multiplier * sigma)

        lines.append(f"\n🔹 **{ticker}**")
        lines.append(f"• 전일 종가: ${prev_close:.2f}  |  LOC: ${loc_price:.2f}")
        if is_intensive:
            lines.append("• 💡 **[집중 매수] 평소 2배 물량**")

    if sigma_messages:
        lines.append("\n" + "\n".join(sigma_messages))

    _send_discord(webhook, user_id, "📋 LOC 브리핑", "\n".join(lines))


if __name__ == "__main__":
    execute_dual_tactical_trader()