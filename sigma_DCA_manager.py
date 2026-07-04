"""
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
import time
import numpy as np
import requests
import yfinance as yf
from datetime import datetime, timezone, timedelta, date
from zoneinfo import ZoneInfo


# ====================== 인코딩 설정 ======================
if hasattr(sys.stdout, "reconfigure"):
    getattr(sys.stdout, "reconfigure")(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    getattr(sys.stderr, "reconfigure")(encoding="utf-8")
    
TARGET_TICKERS         = ["SOXX", "AIPO", "QNDX", "NVDX", "SOXL", "IONQ"]
CONFIG_PATH            = "config.json"
_DISCORD_TITLE_LIMIT   = 256
_DISCORD_CONTENT_LIMIT = 4096


# ════════════════════════════════════════════
# I/O
# ════════════════════════════════════════════

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

    for ticker, pos in positions_data.items():
        lookback_days = int(pos.get("LOOKBACK_DAYS", 252))
        vol_method = str(pos.get("VOL_METHOD", "EWMA")).upper()
        ewma_lambda = float(pos.get("EWMA_LAMBDA", 0.94))
        pos["LOOKBACK_DAYS"] = lookback_days
        pos.setdefault("ENTRY_MULTIPLIER", 1.41)
        pos["VOL_METHOD"] = vol_method
        pos["EWMA_LAMBDA"] = ewma_lambda
        
        last_str = pos.get("LAST_SIGMA_UPDATE", "2000-01-01")
        last_dt = datetime.strptime(last_str, "%Y-%m-%d").date()
        
        # 63 영업일 주기 체크 (달력상 약 90일 경과 시 갱신)
        days_passed = (today - last_dt).days
        method_changed = pos.get("LAST_SIGMA_METHOD") != vol_method
        lambda_changed = vol_method == "EWMA" and pos.get("LAST_EWMA_LAMBDA") != ewma_lambda
        if "DAILY_SIGMA" in pos and days_passed < 90 and not method_changed and not lambda_changed:
            continue

        try:
            stock = yf.Ticker(ticker)
            hist = stock.history(period=f"{lookback_days + 30}d", interval="1d", auto_adjust=False)
            closes = hist['Close'].dropna()
            
            new_sigma = round(_calculate_volatility_from_closes(closes, lookback_days, vol_method, ewma_lambda), 4)
            
            pos["DAILY_SIGMA"] = new_sigma
            pos["LAST_SIGMA_UPDATE"] = today.strftime("%Y-%m-%d")
            pos["LAST_SIGMA_METHOD"] = vol_method
            pos["LAST_EWMA_LAMBDA"] = ewma_lambda if vol_method == "EWMA" else None
            log_sigma_update(ticker, new_sigma)
            messages.append(f"📊 {ticker} 자동 갱신 [{lookback_days}일/{vol_method}]: {new_sigma:.4f}")
        except Exception as e:
            messages.append(f"⚠️ {ticker} 갱신 오류: {e}")
    return messages


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


def _get_recent_log_returns(closes, lookback_days: int):
    log_returns = np.log(closes / closes.shift(1))
    log_returns_clean = log_returns[~np.isnan(log_returns)]
    return log_returns_clean[-lookback_days:]


def _calculate_sigma_from_closes(closes, lookback_days: int) -> float:
    recent_returns = _get_recent_log_returns(closes, lookback_days)
    return float(recent_returns.std(ddof=1))


def _calculate_ewma_sigma_from_closes(closes, lookback_days: int, ewma_lambda: float) -> float:
    if not 0 < ewma_lambda < 1:
        raise ValueError(f"EWMA_LAMBDA는 0과 1 사이여야 합니다: {ewma_lambda}")

    recent_returns = _get_recent_log_returns(closes, lookback_days)
    if len(recent_returns) < 2:
        raise ValueError("EWMA 계산에 필요한 로그 수익률 데이터가 부족합니다.")

    variance = float(recent_returns.var(ddof=1))
    for r in recent_returns:
        variance = ewma_lambda * variance + (1 - ewma_lambda) * float(r) ** 2
    return float(np.sqrt(variance))


def _calculate_volatility_from_closes(closes, lookback_days: int, vol_method: str, ewma_lambda: float) -> float:
    method = vol_method.upper()
    if method == "EWMA":
        return _calculate_ewma_sigma_from_closes(closes, lookback_days, ewma_lambda)
    if method in {"STD", "HISTORICAL", "SIMPLE"}:
        return _calculate_sigma_from_closes(closes, lookback_days)
    raise ValueError(f"지원하지 않는 VOL_METHOD입니다: {vol_method}")


def _calculate_loc_from_sigma(prev_close: float, sigma: float, multiplier: float) -> float:
    target_drop_rate = sigma * multiplier
    return round(prev_close * (1 - target_drop_rate), 2)
    
def get_prev_trading_date(d: date) -> date:
    """주말을 제외하고 직전 거래일 날짜를 계산하는 헬퍼 함수"""
    wd = d.weekday()  # 0=Mon, ..., 6=Sun
    if wd == 0:    # 월요일 -> 금요일 (3일 전)
        return d - timedelta(days=3)
    elif wd == 6:  # 일요일 -> 금요일 (2일 전)
        return d - timedelta(days=2)
    elif wd == 5:  # 토요일 -> 금요일 (1일 전)
        return d - timedelta(days=1)
    else:          # 화~금 -> 1일 전
        return d - timedelta(days=1)

def get_prev_close(ticker: str) -> tuple[float | None, str]:
    """
    yfinance를 활용한 안정적인 전일 종가 조회 함수 (3회 재시도 적용)
    """
    print(f"🔍 {ticker} 가격 조회 시작...")
    now_ny = datetime.now(ZoneInfo("America/New_York"))
    today_ny = now_ny.date()
    
    # 3회 재시도 루프
    for attempt in range(1, 4):
        try:
            print(f"   → yfinance history 시도 ({attempt}/3)...")
            stock = yf.Ticker(ticker)
            hist = stock.history(period="15d", interval="1d", auto_adjust=False, rounding=True)
            
            if not hist.empty:
                close_series = hist['Close'].dropna()
                if not close_series.empty:
                    last_idx = close_series.index[-1]
                    last_date = last_idx.date() if hasattr(last_idx, "date") else last_idx
                    
                    # 로직: 장중이면 전일 종가(index -2), 장 마감 후면 당일 종가(index -1)
                    if last_date == today_ny and now_ny.hour < 16 and len(close_series) >= 2:
                        prev_close = float(close_series.iloc[-2])
                        prev_date = close_series.index[-2].date()
                        date_str = prev_date.strftime("%m-%d")
                    else:
                        prev_close = float(close_series.iloc[-1])
                        date_str = last_date.strftime("%m-%d")
                        
                    print(f"✅ {ticker} yfinance 성공: ${prev_close:.2f} ({date_str})")
                    return prev_close, date_str
        
        except Exception as e:
            print(f"   ⚠️ yfinance 시도 {attempt} 실패: {e}")
            if attempt < 3:
                time.sleep(2.0) # 2초 대기 후 재시도

    # 최후의 수단: info fallback
    try:
        print(f"   → yfinance info fallback 시도...")
        info = yf.Ticker(ticker).info
        for key in ["previousClose", "regularMarketPreviousClose", "currentPrice", "regularMarketPrice"]:
            price = _safe_float(info.get(key))
            if price is not None and not np.isnan(price):
                print(f"✅ {ticker} info 성공: ${price:.2f} (key: {key})")
                return price, "N/A"
    except Exception as e:
        print(f"   ⚠️ info fallback 실패: {e}")

    print(f"❌ {ticker} 모든 가격 조회 방법 실패")
    return None, "N/A"


def get_realtime_sigma(ticker: str, lookback_days: int, vol_method: str = "EWMA", ewma_lambda: float = 0.94) -> float:
    """
    yfinance를 활용해 실시간으로 종목의 로그 수익률 표준편차(Sigma)를 계산하는 함수.
    네트워크 오류 및 API 제한에 대응하기 위해 3회 재시도(Retry) 루프를 포함합니다.
    """
    vol_method = vol_method.upper()
    print(f"📊 {ticker} 실시간 시그마(Sigma) 계산 시작 (룩백: {lookback_days}일/{vol_method})...")
    
    for attempt in range(1, 4):
        try:
            print(f"   → yfinance history 데이터 수집 시도 ({attempt}/3)...")
            stock = yf.Ticker(ticker)
            # 로그 수익률 계산을 위해 요구하는 룩백 일수보다 넉넉하게 (+30일) 수집합니다.
            hist = stock.history(period=f"{lookback_days + 30}d", interval="1d", auto_adjust=False)
            
            if hist.empty:
                raise ValueError("수집된 데이터가 비어 있습니다.")
                
            closes = hist['Close'].dropna()
            if len(closes) < lookback_days:
                raise ValueError(f"유효 종가 데이터 일수({len(closes)}일)가 요구되는 룩백 일수({lookback_days}일)보다 부족합니다.")
                
            # 표준편차(Sigma) 산출
            new_sigma = _calculate_volatility_from_closes(closes, lookback_days, vol_method, ewma_lambda)
            
            print(f"✅ {ticker} 실시간 시그마 산출 성공: {new_sigma:.4f}")
            return new_sigma
            
        except Exception as e:
            print(f"   ⚠️ 시도 {attempt} 실패: {e}")
            if attempt < 3:
                time.sleep(2.0)  # 2초 대기 후 재시도
            else:
                # 3회 모두 실패 시, 에러를 상위 호출부로 전파하여 시스템이 인지하도록 합니다.
                raise RuntimeError(f"❌ {ticker} 실시간 시그마 계산 실패 (3회 초과 재시도)") from e

    # 루프 바깥 영역에도 예외 처리를 명시해 '-> float'의 None 반환 오판 문제를 차단합니다.
    raise RuntimeError(f"❌ {ticker} 실시간 시그마 계산 실패 (알 수 없는 오류)")
            

def calculate_loc_price(ticker: str, prev_close: float, cfg: dict) -> float:
    """
    2. 정의된 함수를 이용하여 실제 매수가를 산출합니다 (실행 과정)
    """
    pos_cfg = cfg.setdefault("POSITIONS", {}).setdefault(ticker, {})
    multiplier = pos_cfg.get("ENTRY_MULTIPLIER", 1.41)
    sigma = pos_cfg.get("DAILY_SIGMA")
    lookback_days = int(pos_cfg.get("LOOKBACK_DAYS", 252))
    vol_method = str(pos_cfg.get("VOL_METHOD", "EWMA")).upper()
    ewma_lambda = float(pos_cfg.get("EWMA_LAMBDA", 0.94))

    # 설정값이 있으면 즉시 사용
    if sigma is not None:
        return _calculate_loc_from_sigma(prev_close, sigma, multiplier)

    # 없으면 위에서 정의한 get_realtime_sigma를 호출
    print(f"  ⚠️ {ticker} 설정값 없음 → 실시간 계산 실행")
    sigma = get_realtime_sigma(ticker, lookback_days, vol_method, ewma_lambda)
    
    return _calculate_loc_from_sigma(prev_close, sigma, multiplier)


def get_market_score(filepath="signal_report.json"):
    """경보 시스템의 결과물을 읽어와 점수를 반환"""
    if not os.path.exists(filepath):
        return 0 
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("total_score", 0)
    except:
        return 0


def calculate_final_loc(base_price: float) -> float:
    """기본 계산된 LOC에 위험 점수 반영하여 보정"""
    score = get_market_score()
    if score >= 10: discount = 0.95
    elif score >= 6: discount = 0.98
    else: discount = 1.0
    return base_price * discount


# ==============================================================================
# 빅테크 CAPEX 및 기술 지표 참/거짓(True/False) 연산 엔진
# ==============================================================================

def get_current_vix() -> float | None:
    try:
        vix_hist = yf.Ticker("^VIX").history(period="5d", interval="1d")
        if vix_hist.empty:
            return None
        vix_closes = vix_hist['Close'].dropna()
        if vix_closes.empty:
            return None
        return float(vix_closes.iloc[-1])
    except Exception:
        return None


def check_macro_and_technical_signals(ticker: str, pos_cfg: dict, current_vix: float | None) -> tuple[bool, bool, str]:
    """
    설정된 투자 유형, 주가 추세, VIX 지수로 매수/매도 참(True)/거짓(False)을 판정합니다.
    """
    if current_vix is None:
        return False, False, "VIX 데이터 수집 지연 (관망)"

    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period="120d", interval="1d", auto_adjust=False)
        
        if hist.empty:
            return False, False, "주가 데이터 수집 지연 (관망)"

        closes = hist['Close'].dropna()
        if len(closes) < 60:
            return False, False, f"60일 추세 데이터 부족 ({len(closes)}일)"
            
        current_price = float(closes.iloc[-1])
        ma20 = float(closes.rolling(window=20).mean().iloc[-1])
        ma60 = float(closes.rolling(window=60).mean().iloc[-1])
    except Exception as e:
        return False, False, f"API 지연 ({e})"

    invest_type = str(pos_cfg.get("INVEST_TYPE", "")).upper()

    # 1. 단기 모멘텀/기한형 자산 -> 정배열일 때만 안전하게 LOC 오픈
    if invest_type in {"ROTATION_3M", "END_DEC"}:
        buy_signal = bool(current_price > ma20 and current_price > ma60 and current_vix < 20)
        sell_signal = bool(current_price < ma60 or current_vix > 25)
        reason = f"정배열 추세 구간 (VIX: {current_vix:.1f})" if buy_signal else "추세 혼조 또는 리스크 관리 구간"
    
    # 2. 인프라 및 적립식 자산 (SOXX, AIPO, QNDX) -> 매크로 폭락장(VIX 대발작)만 아니면 진입
    else:
        buy_signal = bool(current_vix < 23)
        sell_signal = bool(current_vix > 28)
        reason = "AI 인프라 사이클 유효" if buy_signal else "글로벌 매크로 위험 경계"

    return buy_signal, sell_signal, reason


def format_position_meta(pos_cfg: dict, today: date) -> str:
    parts = []
    invest_type = pos_cfg.get("INVEST_TYPE")
    if invest_type:
        parts.append(str(invest_type))

    start_str = pos_cfg.get("START_DATE")
    if start_str:
        try:
            start_date = datetime.strptime(start_str, "%Y-%m-%d").date()
            parts.append(f"D+{max((today - start_date).days, 0)}")
        except ValueError:
            parts.append(f"START_DATE 오류: {start_str}")

    return f" | {' / '.join(parts)}" if parts else ""


# ═══════════════════════════════════════════════════════════
# 디스코드 전송 엔진 
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
            print(f"✅ 디스코드 브리핑 전송 성공")
        resp.raise_for_status()
    except Exception as e:
        print(f"❌ 디스코드 전송 실패: {e}")


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
    save_config(cfg)


# ═══════════════════════════════════════════════════════════
# 통합 시그널 & LOC 반영 브리핑 빌더
# ═══════════════════════════════════════════════════════════
def _build_briefing_lines(now_ny: datetime, cfg: dict, sigma_messages: list[str]) -> list[str]:
    lines = [f"🌙 **미국 증시 LOC 포트폴리오 브리핑** ({now_ny.strftime('%Y-%m-%d %H:%M %Z')})"]
    today_ny = now_ny.date()
    
    # 위험 스코어 연동 (있으면 표기, 없으면 패스)
    market_score = get_market_score()
    lines.append(f"📊 **시장 리스크 스코어:** {market_score} / 14")
    lines.append("─" * 40)

    positions = cfg.get("POSITIONS", {})
    current_vix = get_current_vix()

    for ticker, pos_cfg in positions.items():
        # 복잡한 날짜 배제하고 순수 시그널만 조회
        buy_sig, sell_sig, reason = check_macro_and_technical_signals(ticker, pos_cfg, current_vix)
        
        prev_close, last_date_str = get_prev_close(ticker)
        if prev_close is None:
            lines.append(f"\n🔹 **{ticker}** — 주가 조회 실패 ⚠️")
            continue

        position_meta = format_position_meta(pos_cfg, today_ny)
        lines.append(f"\n🔹 **{ticker}** (종가: ${prev_close:.2f} | {last_date_str}{position_meta})")
        lines.append(f"• **시그널:** 매수[{buy_sig}] / 매도[{sell_sig}] | {reason}")

        # 조건 만족 시 기계적으로 걸어둘 LOC 가격 산출
        if sell_sig is True:
            lines.append(f"• 🚨 **[액션] 위험 매도!** 종가 대피 권장 (LOC 매도가: ${prev_close:.2f})")
        elif buy_sig is True:
            base_loc = calculate_loc_price(ticker, prev_close, cfg)
            final_loc = calculate_final_loc(base_loc) # 리스크 점수 보정 적용
            
            if base_loc != final_loc:
                lines.append(f"• 🎯 **[액션] LOC 매수 걸어두기:** ~~${base_loc:.2f}~~ ➡️ **${final_loc:.2f}** (위험 할인가)")
            else:
                lines.append(f"• 🎯 **[액션] LOC 매수 걸어두기:** **${final_loc:.2f}**")
        else:
            lines.append("• 🟡 **[액션] 시그널 거짓(False) — 오늘 밤은 주문 없이 패스**")

    if sigma_messages:
        lines.append("\n📝 **[시스템 로그]**")
        for msg in sigma_messages:
            lines.append(f"• {msg}")
            
    return lines


# ═══════════════════════════════════════════════════════════
# 메인 제어 루프 실행 
# ═══════════════════════════════════════════════════════════
def execute_dual_tactical_trader() -> None:
    """통합 매크로 시그널 & LOC 자동화 시스템 가동"""
    now_ny = datetime.now(ZoneInfo("America/New_York"))
    
    # 1. 설정 로드
    cfg = load_config()
    webhook = os.environ.get("DISCORD_WEBHOOK") or cfg.get("DISCORD_WEBHOOK", "")
    user_id = os.environ.get("DISCORD_USER_ID") or cfg.get("DISCORD_USER_ID", "")

    # 2. 시스템 루틴: 시그마 갱신 및 저장
    sigma_messages = refresh_sigma_if_stale(cfg)
    save_config(cfg) 
    
    # 3. [업데이트] 참/거짓 시그널과 결합된 최종 브리핑 생성
    briefing_lines = _build_briefing_lines(now_ny, cfg, sigma_messages)
    
    # 4. 디스코드 전송
    _send_discord(
        webhook_url=webhook, 
        user_id=user_id, 
        title="📋 AI & 반도체 포트폴리오 LOC 운용 브리핑", 
        content="\n".join(briefing_lines)
    )
    
    # 5. 월초 핑 관리
    try:
        send_monthly_ping_if_due(cfg, webhook, user_id)
    except Exception as e:
        print(f"⚠️ 월초 핑 전송 중 오류 발생: {e}")


if __name__ == "__main__":
    execute_dual_tactical_trader()
