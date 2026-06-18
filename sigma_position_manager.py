"""
sigma_position_manager.py
─────────────────────────────────────────────────────────────
Sigma DCA 자동화 — LOC 예정가 디스코드 브리핑
─────────────────────────────────────────────────────────────
실행 흐름:
  1. config.json 로드
  2. 종목별 주기(LOOKBACK_DAYS)에 따라 시그마 자동 갱신
  3. 월초(1일) 운영 핑 발송
  4. 종목별 전일 종가 · LOC 예정가 계산
  5. 디스코드 브리핑 전송
"""
import csv
import os
import sys
import json
import shutil
import tempfile
import numpy as np
import requests
import yfinance as yf
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from alpha_vantage.timeseries import TimeSeries

# ====================== 인코딩 설정 ======================
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

TARGET_TICKERS         = ["TQQQ", "SOXQ", "SOXL"]
CONFIG_PATH            = "config.json"
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
        return {"POSITIONS": {}, "LAST_MONTHLY_PING": ""}
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

# CSV 로그 기록 함수
def log_sigma_update(ticker: str, sigma: float):
    file_path = "sigma_history.csv"
    file_exists = os.path.isfile(file_path)
    
    with open(file_path, 'a', newline='') as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(['Date', 'Ticker', 'Sigma'])
        writer.writerow([datetime.now().strftime("%Y-%m-%d"), ticker, sigma])

def refresh_sigma_if_stale(cfg: dict) -> list[str]:
    messages = []
    today = datetime.now(ZoneInfo("America/New_York")).date()
    positions_data = cfg.setdefault("POSITIONS", {})

    # 함수 내부에서 환경 변수를 다시 호출하여 안전하게 가져옵니다
    current_api_key = os.environ.get("ALPHA_VANTAGE_KEY")
    if not current_api_key:
        return ["⚠️ API 키가 설정되지 않아 시그마 갱신 불가"]

    ts = TimeSeries(key=current_api_key, output_format='pandas')

    for ticker in TARGET_TICKERS:
        pos = positions_data.setdefault(ticker, {})
        lookback_days = int(pos.get("LOOKBACK_DAYS", 90))
        
        last_str = pos.get("LAST_SIGMA_UPDATE", "2000-01-01")
        last_dt = datetime.strptime(last_str, "%Y-%m-%d").date() if last_str != "2000-01-01" else datetime(2000, 1, 1).date()

        if (today - last_dt).days < lookback_days:
            continue

        try:
            data, _ = ts.get_daily(symbol=ticker, outputsize='compact')
            
            if data.empty:
                messages.append(f"⚠️ {ticker} 시그마 갱신 실패: 데이터 없음")
                continue
                
            closes = data['4. close']
            returns = closes.pct_change().dropna()
            
            if len(returns) < lookback_days:
                messages.append(f"⚠️ {ticker} 시그마 갱신 실패: 데이터 부족 (확보: {len(returns)}일)")
                continue
                
            new_sigma = round(float(returns.tail(lookback_days).std()), 6)
            
            pos["DAILY_SIGMA"] = new_sigma
            pos["LAST_SIGMA_UPDATE"] = today.strftime("%Y-%m-%d")
            
            log_sigma_update(ticker, new_sigma)
            messages.append(f"📊 {ticker} 시그마 갱신 ({lookback_days}일 기준): {new_sigma:.6f}")
            
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
        return None if (np.isnan(f) or np.isinf(f)) else f
    except (TypeError, ValueError):
        return None
    
def get_prev_close(ticker: str) -> tuple[float | None, str]:
    """실무용 안정 버전 - GitHub Actions에서도 최대한 잘 돌아가도록"""
    print(f"🔍 {ticker} 가격 조회 시작...")
    
    # 최대 3회 재시도 (GitHub 환경 고려)
    for attempt in range(3):
        try:
            stock = yf.Ticker(ticker)
            hist = stock.history(period="10d", auto_adjust=False, rounding=True)
            
            if not hist.empty:
                close_series = hist['Close'].dropna()
                if not close_series.empty:
                    prev_close = float(close_series.iloc[-1])
                    last_date = close_series.index[-1].date()
                    date_str = last_date.strftime("%m-%d")
                    
                    print(f"✅ {ticker} 성공: ${prev_close:.2f} ({date_str})")
                    return prev_close, date_str
                    
        except Exception as e:
            print(f"   ⚠️ 시도 {attempt+1}/3 실패: {e}")
            if attempt < 2:
                import time
                time.sleep(1.5)   # GitHub 환경에서 잠시 대기
    
    # info fallback
    try:
        info = stock.info
        for key in ["previousClose", "regularMarketPreviousClose", "currentPrice"]:
            price = _safe_float(info.get(key))
            if price is not None and not np.isnan(price):
                print(f"✅ {ticker} info 성공: ${price:.2f}")
                return price, "N/A"
    except Exception as e:
        print(f"   ⚠️ info fallback 실패: {e}")

    print(f"❌ {ticker} 최종 실패")
    return None, "N/A"
                                            

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
            "timestamp": datetime.now(timezone.utc).isoformat()
        }],
    }
    try:
        resp = requests.post(webhook_url, json=payload, timeout=15)
        if resp.ok:
            print(f"✅ 디스코드 전송 성공 — HTTP {resp.status_code}")
        else:
            print(f"❌ 디스코드 전송 실패 — HTTP {resp.status_code}: {resp.text}")
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
    """Sigma DCA LOC 브리핑 실행 메인 함수"""
    now_ny = datetime.now(ZoneInfo("America/New_York"))
    
    # 1. 설정 로드 및 초기화
    cfg = load_config()
    webhook = os.environ.get("DISCORD_WEBHOOK") or cfg.get("DISCORD_WEBHOOK", "")
    user_id = os.environ.get("DISCORD_USER_ID") or cfg.get("DISCORD_USER_ID", "")

    # 2. 시스템 루틴 실행 (시그마 갱신은 여기서 한 번만 수행)
    sigma_messages = refresh_sigma_if_stale(cfg)
    send_monthly_ping_if_due(cfg, webhook, user_id)
    save_config(cfg)

    # 3. 브리핑 메시지 생성
    briefing_lines = _build_briefing_lines(now_ny, cfg, sigma_messages)

    # 4. 디스코드 전송
    _send_discord(
        webhook_url=webhook,
        user_id=user_id,
        title="📋 LOC 브리핑",
        content="\n".join(briefing_lines)
    )

def _build_briefing_lines(
    now_ny: datetime, 
    cfg: dict, 
    sigma_messages: list[str]
) -> list[str]:
    """브리핑 본문 라인들을 생성하는 헬퍼 함수"""
    lines = [f"🌙 {now_ny.strftime('%Y-%m-%d %H:%M %Z')}"]

    positions = cfg.get("POSITIONS", {})

    for ticker in TARGET_TICKERS:
        pos_cfg = positions.get(ticker, {})
        
        prev_close, last_date_str = get_prev_close(ticker)

        if prev_close is None:
            lines.append(f"\n🔹 **{ticker}** — 가격 조회 실패 ⚠️")
            continue

        multiplier = pos_cfg.get("ENTRY_MULTIPLIER", 1.41)
        sigma = pos_cfg.get("DAILY_SIGMA", 0.05)
        loc_price = prev_close * np.exp(-multiplier * sigma)

        lines.append(f"\n🔹 **{ticker}**")
        lines.append(
            f"• 전일 종가: ${prev_close:.2f} ({last_date_str}) | "
            f"LOC: ${loc_price:.2f}"
        )

    # 시그마 갱신 메시지 추가 (있을 경우에만)
    if sigma_messages:
        lines.append("\n" + "\n".join(sigma_messages))

    return lines

if __name__ == "__main__":
    execute_dual_tactical_trader()