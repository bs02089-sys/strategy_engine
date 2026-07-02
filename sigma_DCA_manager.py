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
    
TARGET_TICKERS         = ["SOXX", "AIPO", "QNDX", "NVDX", "SOXL"]
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
        pos["LOOKBACK_DAYS"] = lookback_days
        pos.setdefault("ENTRY_MULTIPLIER", 1.41)
        
        last_str = pos.get("LAST_SIGMA_UPDATE", "2000-01-01")
        last_dt = datetime.strptime(last_str, "%Y-%m-%d").date()
        
        # 63 영업일 주기 체크 (달력상 약 90일 경과 시 갱신)
        days_passed = (today - last_dt).days
        if "DAILY_SIGMA" in pos and days_passed < 90:
            continue

        try:
            stock = yf.Ticker(ticker)
            hist = stock.history(period=f"{lookback_days + 30}d", interval="1d", auto_adjust=False)
            closes = hist['Close'].dropna()
            
            # [수정] 타입 검사기의 ndarray 오판 우회를 위해 .dropna()와 .tail()을 
            # 각각 Numpy 불리언 인덱싱과 슬라이싱 기법으로 변경하여 에러 밑줄을 제거합니다.
            log_returns = np.log(closes / closes.shift(1))
            log_returns_clean = log_returns[~np.isnan(log_returns)]
            recent_returns = log_returns_clean[-lookback_days:]
            
            new_sigma = round(float(recent_returns.std(ddof=1)), 4)
            
            pos["DAILY_SIGMA"] = new_sigma
            pos["LAST_SIGMA_UPDATE"] = today.strftime("%Y-%m-%d")
            log_sigma_update(ticker, new_sigma)
            messages.append(f"📊 {ticker} 자동 갱신 [{lookback_days}일]: {new_sigma:.4f}")
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


def get_realtime_sigma(ticker: str, lookback_days: int) -> float:
    """
    yfinance를 활용해 실시간으로 종목의 로그 수익률 표준편차(Sigma)를 계산하는 함수.
    네트워크 오류 및 API 제한에 대응하기 위해 3회 재시도(Retry) 루프를 포함합니다.
    """
    print(f"📊 {ticker} 실시간 시그마(Sigma) 계산 시작 (룩백: {lookback_days}일)...")
    
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
                
            # 로그 수익률 계산 및 지정된 룩백 영업일 데이터 추출
            # 정적 타입 검사기가 np.log() 반환값을 ndarray로 인식해 .dropna()와 .tail()에서
            # 붉은 밑줄을 긋는 현상을 방지하기 위해 표준 슬라이싱 및 Numpy 인덱싱을 적용합니다.
            log_returns = np.log(closes / closes.shift(1))
            log_returns_clean = log_returns[~np.isnan(log_returns)]
            recent_returns = log_returns_clean[-lookback_days:]
            
            # 표준편차(Sigma) 산출
            sigma = np.std(recent_returns, ddof=1)
            new_sigma = float(sigma)
            
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

    # 설정값이 있으면 즉시 사용
    if sigma is not None:
        target_drop_rate = sigma * multiplier
        loc_price = prev_close * (1 - target_drop_rate)
        return round(loc_price, 2)

    # 없으면 위에서 정의한 get_realtime_sigma를 호출
    print(f"  ⚠️ {ticker} 설정값 없음 → 실시간 계산 실행")
    sigma = get_realtime_sigma(ticker, pos_cfg.get("LOOKBACK_DAYS", 252))
    
    target_drop_rate = sigma * multiplier
    loc_price = prev_close * (1 - target_drop_rate)
    return round(loc_price, 2)


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


def run_integrated_system(ticker: str, cfg: dict):
    """수집 엔진과 연산 엔진을 유기적으로 제어하는 메인 실행 함수"""
    print("=" * 60)
    print(f"📡 {ticker} 통합 LOC 연산 시스템 가동")
    
    # 설정값 로드
    pos_cfg = cfg.get("POSITIONS", {}).get(ticker, {})
    lookback = pos_cfg.get("LOOKBACK_DAYS", 252)
    print(f"⚙️ 전략: {lookback}일 룩백 기준 (배수: {pos_cfg.get('ENTRY_MULTIPLIER', 1.41)})")
    print("-" * 60)
    
    # Step 1: 전일 종가 자동 수집
    prev_close, trading_date = get_prev_close(ticker)
    if prev_close is None or prev_close <= 0:
        print(f"🚨 {ticker} 가격 데이터 수집 실패: ${prev_close}")
        print("=" * 60)
        return

    print(f"✅ 전일 종가 수집 성공: ${prev_close:.2f} (거래일: {trading_date})")
    
    # Step 2: 시장 위험 점수 확인
    market_score = get_market_score()
    print(f"📊 현재 시장 위험 점수: {market_score}/14")
    
    # Step 3: 목표 LOC 가격 자동 연산 및 위험 점수 보정
    try:
        base_loc = calculate_loc_price(ticker, prev_close, cfg)
        final_loc = calculate_final_loc(base_loc) # 시장 상황 반영 보정
        
        print("-" * 60)
        if base_loc != final_loc:
            print(f"⚠️ 시장 위험 반영: 기본값 ${base_loc:.2f} → 최종가 ${final_loc:.2f}")
        print(f"🎯 오늘 밤 {ticker} LOC 매수 지정가: ${final_loc:.2f}")
        print("=" * 60)
        
    except Exception as e:
        print(f"🚨 통계 연산 중 오류 발생: {e}")
        print("=" * 60)


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
# 월초 운영 핑 (save_config 호출 추가)
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
    
    # 설정값 업데이트 및 즉시 저장
    cfg["LAST_MONTHLY_PING"] = today_ym
    save_config(cfg) # <--- 여기서 핑 전송 후 확실하게 저장!
        
    
# ═══════════════════════════════════════════════════════════
# 메인
# ═══════════════════════════════════════════════════════════

def execute_dual_tactical_trader() -> None:
    """통합 메인 실행 함수"""
    now_ny = datetime.now(ZoneInfo("America/New_York"))
    
    # 1. 설정 로드
    cfg = load_config()
    webhook = os.environ.get("DISCORD_WEBHOOK") or cfg.get("DISCORD_WEBHOOK", "")
    user_id = os.environ.get("DISCORD_USER_ID") or cfg.get("DISCORD_USER_ID", "")

    # 2. 시스템 루틴: 시그마 갱신
    sigma_messages = refresh_sigma_if_stale(cfg)
    save_config(cfg) # 시그마 갱신 내용 저장
    
    # 3. 브리핑 데이터 구성 및 디스코드 전송
    briefing_lines = _build_briefing_lines(now_ny, cfg, sigma_messages)
    _send_discord(webhook_url=webhook, user_id=user_id, title="📋 LOC 브리핑", content="\n".join(briefing_lines))
    
    # 4. 생존 신호 (내부에서 save_config가 작동함)
    try:
        send_monthly_ping_if_due(cfg, webhook, user_id)
    except Exception as e:
        print(f"⚠️ 월초 핑 전송 중 오류 발생: {e}")
            

def _build_briefing_lines(now_ny: datetime, cfg: dict, sigma_messages: list[str]) -> list[str]:
    lines = [f"🌙 {now_ny.strftime('%Y-%m-%d %H:%M %Z')}"]
    positions = cfg.get("POSITIONS", {})

    # 모든 포지션 순회
    for ticker in positions.keys():
        prev_close, last_date_str = get_prev_close(ticker)

        if prev_close is None:
            lines.append(f"\n🔹 **{ticker}** — 가격 조회 실패 ⚠️")
            continue

        # calculate_loc_price 함수를 재활용하여 일관성 유지
        loc_price = calculate_loc_price(ticker, prev_close, cfg)

        lines.append(f"\n🔹 **{ticker}**")
        lines.append(f"• 전일 종가: ${prev_close:.2f} ({last_date_str}) | LOC: ${loc_price:.2f}")

    if sigma_messages:
        lines.append("\n" + "\n".join(sigma_messages))
    return lines

if __name__ == "__main__":
    # 시스템 실행은 딱 한 번만!
    execute_dual_tactical_trader()