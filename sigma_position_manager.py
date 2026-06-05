"""
sigma_position_manager.py
─────────────────────────────────────────────────────────────
SOXL VIX Sigma DCA 자동화 — 포지션 모니터링 & 디스코드 브리핑
─────────────────────────────────────────────────────────────
실행 흐름:
  1. config.json / ledger.json 로드
  2. 종목별 주기(LOOKBACK_DAYS)에 따라 시그마 자동 갱신
     - BOTZ : 90일 주기 / 3개월 데이터
     - SOXX : 365일 주기 / 1년 데이터
     - SOXL : 365일 주기 / 1년 데이터
  3. ledger → config POSITIONS 동기화
  4. 월초(1일) 운영 핑 발송
  5. VIX 조회 → 집중 매수 모드 판단
  6. 종목별 LOC 예정가 · 수익률 계산
  7. 디스코드 브리핑 전송
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

# ── 인코딩 설정 ──────────────────────────────────────────────
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

warnings.filterwarnings("ignore", category=FutureWarning)

# ── 상수 ─────────────────────────────────────────────────────
MODE_EMOJI             = {"장전": "🌙", "장중": "☀️"}
TARGET_TICKERS         = ["BOTZ", "SOXX", "SOXL"]
CONFIG_PATH            = "config.json"
LEDGER_PATH            = "ledger.json"
_DISCORD_TITLE_LIMIT   = 256
_DISCORD_CONTENT_LIMIT = 4096

# LOOKBACK_DAYS → yfinance period 매핑
_PERIOD_MAP = {90: "3mo", 180: "6mo", 365: "1y"}


# ═══════════════════════════════════════════════════════════
# I/O 헬퍼
# ═══════════════════════════════════════════════════════════

def load_config() -> dict:
    """config.json 로드. 파일이 없으면 최소 구조를 반환한다."""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"⚠️ {CONFIG_PATH} 없음 — 기본값으로 초기화합니다.")
        return {
            "POSITIONS": {},
            "LAST_MONTHLY_PING": "",
            "STRATEGY": {"VIX_THRESHOLD": 25.0},
        }
    except json.JSONDecodeError as e:
        raise RuntimeError(f"config.json 파싱 오류: {e}") from e


def save_config(cfg: dict) -> None:
    """tempfile → rename 방식으로 원자적 저장 (race condition 방지)."""
    try:
        with tempfile.NamedTemporaryFile(
            "w", delete=False, suffix=".json", encoding="utf-8"
        ) as tmp:
            json.dump(cfg, tmp, indent=4, ensure_ascii=False)
            tmp_path = tmp.name
        shutil.move(tmp_path, CONFIG_PATH)
    except Exception as e:
        print(f"⚠️ {CONFIG_PATH} 저장 실패: {e}")


def load_ledger() -> dict:
    """ledger.json 로드. 파일이 없으면 빈 포지션 구조를 반환한다."""
    default = {
        "SOXL_LONG":  {"qty": 0, "avg_price": 0.0},
        "SOXL_SHORT": {"qty": 0, "avg_price": 0.0},
        "BOTZ_LONG":  {"qty": 0, "avg_price": 0.0},
        "SOXX_LONG":  {"qty": 0, "avg_price": 0.0},
    }
    try:
        with open(LEDGER_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"⚠️ {LEDGER_PATH} 없음 — 빈 포지션으로 처리합니다.")
        return default
    except json.JSONDecodeError as e:
        print(f"⚠️ ledger.json 파싱 오류: {e} — 빈 포지션으로 처리합니다.")
        return default


# ═══════════════════════════════════════════════════════════
# 포지션 동기화
# ═══════════════════════════════════════════════════════════

def sync_positions_from_ledger(cfg: dict) -> None:
    """ledger.json의 수량·평균단가를 config POSITIONS에 반영한다."""
    ledger = load_ledger()
    for ticker in TARGET_TICKERS:
        pos = cfg.setdefault("POSITIONS", {}).setdefault(ticker, {})
        directions = ["LONG", "SHORT"] if ticker == "SOXL" else ["LONG"]
        for d in directions:
            entry = ledger.get(f"{ticker}_{d}", {})
            pos[f"TOTAL_SHARES_{d}"] = entry.get("qty", 0)
            pos[f"MY_AVG_PRICE_{d}"] = entry.get("avg_price", 0.0)


# ═══════════════════════════════════════════════════════════
# 시그마 자동 갱신 — 종목별 LOOKBACK_DAYS 주기 적용
# ═══════════════════════════════════════════════════════════

def refresh_sigma_if_stale(cfg: dict) -> list[str]:
    """
    각 종목의 LOOKBACK_DAYS 주기마다 DAILY_SIGMA를 자동 재산출한다.

    config.json 기준:
      BOTZ : LOOKBACK_DAYS=90  → 90일 경과 시 3개월 데이터로 갱신
      SOXX : LOOKBACK_DAYS=365 → 365일 경과 시 1년 데이터로 갱신
      SOXL : LOOKBACK_DAYS=365 → 365일 경과 시 1년 데이터로 갱신
    """
    messages = []
    today    = datetime.now()

    for ticker in TARGET_TICKERS:
        pos = cfg["POSITIONS"].get(ticker, {})

        # 갱신 주기: 종목별 LOOKBACK_DAYS, 없으면 365일 기본값
        lookback_days = int(pos.get("LOOKBACK_DAYS", 365))
        period        = _PERIOD_MAP.get(lookback_days, "1y")

        # 마지막 갱신일 파싱
        last_str = pos.get("LAST_SIGMA_UPDATE", "2000-01-01")
        try:
            last_dt = datetime.strptime(last_str, "%Y-%m-%d")
        except ValueError:
            last_dt = datetime(2000, 1, 1)

        if (today - last_dt).days < lookback_days:
            continue  # 아직 갱신 주기 미도달

        try:
            hist = yf.Ticker(ticker).history(period=period, auto_adjust=True)
            if hist.empty or len(hist) < 10:
                messages.append(f"⚠️ {ticker} 시그마 갱신 실패: 데이터 부족")
                continue

            new_sigma = round(float(hist["Close"].pct_change().dropna().std()), 6)
            pos["DAILY_SIGMA"]       = new_sigma
            pos["LAST_SIGMA_UPDATE"] = today.strftime("%Y-%m-%d")
            messages.append(
                f"📊 {ticker} 시그마 갱신 ({period} 기준): {new_sigma:.6f}"
            )
        except Exception as e:
            messages.append(f"⚠️ {ticker} 시그마 갱신 실패: {e}")

    return messages


# ═══════════════════════════════════════════════════════════
# 월초 운영 핑
# ═══════════════════════════════════════════════════════════

def send_monthly_ping_if_due(cfg: dict, webhook: str, user_id: str) -> None:
    """매월 1일에 운영 핑을 1회 전송한다."""
    now = datetime.now()
    if now.day != 1:
        return
    today_ym = now.strftime("%Y-%m")
    if cfg.get("LAST_MONTHLY_PING") == today_ym:
        return
    msg = (
        f"🔔 **월초 핑** | {now.strftime('%Y년 %m월')}\n"
        "운용 시스템이 정상 가동 중입니다."
    )
    _send_discord(webhook, user_id, "🗓️ 월간 운영 핑", msg)
    cfg["LAST_MONTHLY_PING"] = today_ym


# ═══════════════════════════════════════════════════════════
# 시장 상태 / 가격 조회
# ═══════════════════════════════════════════════════════════

def get_market_mode() -> tuple[str, datetime]:
    """뉴욕 시간 기준 장전/장중을 반환한다."""
    now_ny = datetime.now(ZoneInfo("America/New_York"))
    hour   = now_ny.hour + now_ny.minute / 60.0
    mode   = "장중" if 9.5 <= hour < 16.0 else "장전"
    return mode, now_ny


def get_ticker_data(ticker: str, mode: str) -> tuple[float | None, float | None]:
    """
    전일 확정 종가와 현재가를 반환한다. (BOTZ $40.08 문제 해결 버전)
    """
    try:
        t = yf.Ticker(ticker)
        
        # 1. fast_info에서 Previous Close 우선 사용 (가장 신뢰할 수 있는 방법)
        prev_close = t.fast_info.get('previousClose')
        
        # 2. fast_info가 실패하면 history로 폴백
        if prev_close is None or np.isnan(float(prev_close)):
            hist = t.history(period="5d", auto_adjust=False, prepost=False)
            if not hist.empty:
                prev_close = float(hist["Close"].dropna().iloc[-1])
            else:
                prev_close = None

        if prev_close is None or np.isnan(float(prev_close)):
            print(f"⚠️ {ticker}: prev_close 조회 실패")
            return None, None

        prev_close = float(prev_close)

        # 현재가
        current_price = t.fast_info.last_price
        if current_price is None or np.isnan(float(current_price)):
            current_price = prev_close

        current_price = float(current_price)

        return prev_close, current_price

    except Exception as e:
        print(f"❌ {ticker} 데이터 에러: {e}")
        return None, None
        
def get_vix() -> float | None:
    """VIX 지수를 반환한다. 실패 시 None."""
    try:
        hist = yf.Ticker("^VIX").history(period="1d")
        if hist.empty:
            return None
        val = float(hist["Close"].iloc[-1])
        return None if np.isnan(val) else val
    except Exception as e:
        print(f"⚠️ VIX 조회 실패: {e}")
        return None


# ═══════════════════════════════════════════════════════════
# 디스코드 전송
# ═══════════════════════════════════════════════════════════

def _send_discord(webhook_url: str, user_id: str, title: str, content: str) -> None:
    """디스코드 웹훅으로 임베드 메시지를 전송한다."""
    if not webhook_url:
        print("⚠️ DISCORD_WEBHOOK 미설정 — 전송 생략")
        return
    if not webhook_url.startswith("https://discord.com/api/webhooks/"):
        print(f"⚠️ DISCORD_WEBHOOK 형식 오류: {webhook_url[:40]}...")
        return

    if len(title) > _DISCORD_TITLE_LIMIT:
        title = title[:_DISCORD_TITLE_LIMIT - 3] + "..."
    if len(content) > _DISCORD_CONTENT_LIMIT:
        content = content[:_DISCORD_CONTENT_LIMIT - 3] + "..."
        print(f"⚠️ Discord content {_DISCORD_CONTENT_LIMIT}자 초과 — 잘림 처리")

    payload = {
        "content": f"<@{user_id}>" if user_id else "",
        "embeds": [{
            "title": title,
            "description": content,
            "color": 3447003,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }],
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
# 계산 유틸
# ═══════════════════════════════════════════════════════════

def calc_loc(prev_close: float, multiplier: float, sigma: float) -> float:
    """LOC 예정가 = 전일 종가 × exp(−multiplier × sigma)"""
    return prev_close * np.exp(-multiplier * sigma)


def calc_profit_pct(current: float, avg: float, direction: str) -> float:
    """
    수익률(%) 계산.
    LONG:  (현재가 − 평균단가) / 평균단가 × 100
    SHORT: (평균단가 − 현재가) / 평균단가 × 100
    """
    if avg <= 0:
        return 0.0
    if direction == "SHORT":
        return (avg - current) / avg * 100
    return (current - avg) / avg * 100


# ═══════════════════════════════════════════════════════════
# 메인
# ═══════════════════════════════════════════════════════════

def execute_dual_tactical_trader() -> None:
    # ── 1. 초기화 ─────────────────────────────────────────
    mode, now_ny = get_market_mode()
    cfg          = load_config()

    webhook       = os.environ.get("DISCORD_WEBHOOK") or cfg.get("DISCORD_WEBHOOK", "")
    user_id       = os.environ.get("DISCORD_USER_ID")  or cfg.get("DISCORD_USER_ID",  "")
    vix_threshold = cfg.get("STRATEGY", {}).get("VIX_THRESHOLD", 25.0)

    # ── 2. 시스템 루틴 ────────────────────────────────────
    sigma_messages = refresh_sigma_if_stale(cfg)   # 종목별 주기 시그마 갱신
    sync_positions_from_ledger(cfg)                # ledger → config 동기화
    send_monthly_ping_if_due(cfg, webhook, user_id)  # 월초 핑
    save_config(cfg)                               # 원자적 저장

    # ── 3. VIX 조회 ──────────────────────────────────────
    vix_price    = get_vix()
    is_intensive = vix_price is not None and vix_price >= vix_threshold

    if vix_price is not None:
        vix_suffix = " 🚀 [집중 매수 모드 활성화]" if is_intensive else ""
        vix_line   = f"• VIX 지수: {vix_price:.2f}{vix_suffix}"
    else:
        vix_line   = "• VIX 지수: 조회 실패 ⚠️"

    # ── 4. 브리핑 헤더 ───────────────────────────────────
    lines = [
        f"{MODE_EMOJI[mode]} {mode} 모드 | {now_ny.strftime('%Y-%m-%d %H:%M %Z')}",
        vix_line,
    ]

    # ── 5. 종목별 섹션 ───────────────────────────────────
    for ticker in TARGET_TICKERS:
        pos_cfg = cfg["POSITIONS"].get(ticker, {})

        prev_close, current_price = get_ticker_data(ticker, mode)
        if prev_close is None:
            lines.append(f"\n🔹 **{ticker}** — 가격 데이터 조회 실패 ⚠️")
            continue

        loc_price = calc_loc(
            prev_close,
            pos_cfg.get("ENTRY_MULTIPLIER", 1.5),
            pos_cfg.get("DAILY_SIGMA", 0.05),
        )

        ticker_info = [
            f"\n🔹 **{ticker}**",
            f"• 전일 종가: ${prev_close:.2f}  |  LOC 예정가: ${loc_price:.2f}",
        ]

        if is_intensive:
            ticker_info.append("• 💡 **[집중 매수] LOC 도달 시 평소 2배 물량 투입**")
        if mode == "장중":
            ticker_info.append(f"• 현재가: ${current_price:.2f}")

        directions = ["LONG", "SHORT"] if ticker == "SOXL" else ["LONG"]
        for d in directions:
            qty = pos_cfg.get(f"TOTAL_SHARES_{d}", 0)
            avg = pos_cfg.get(f"MY_AVG_PRICE_{d}", 0.0)
            if qty <= 0:
                continue
            line = f"• [{d}] 보유: {qty}주"
            if avg > 0:
                profit = calc_profit_pct(current_price, avg, d)
                line  += f"  |  수익: {profit:+.2f}%"
            ticker_info.append(line)

        lines.extend(ticker_info)

    # ── 6. 시그마 갱신 결과 ──────────────────────────────
    if sigma_messages:
        lines.append("\n" + "\n".join(sigma_messages))

    # ── 7. 디스코드 전송 ─────────────────────────────────
    _send_discord(
        webhook, user_id,
        f"{MODE_EMOJI[mode]} {mode} 브리핑",
        "\n".join(lines),
    )


if __name__ == "__main__":
    execute_dual_tactical_trader()
