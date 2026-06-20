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
import numpy as np
import requests
import yfinance as yf
from datetime import datetime, timezone, timedelta, date
from zoneinfo import ZoneInfo
from alpha_vantage.timeseries import TimeSeries

# .env 파일이 존재할 경우 로드 (로컬 환경 변수 매핑용, 라이브러리가 없어도 예외 처리)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ====================== 인코딩 설정 ======================
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

TARGET_TICKERS         = ["SOXL", "MVLL"]
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

    # GitHub Secrets에서 API 키 불러오기
    av_key = os.environ.get("ALPHA_VANTAGE_KEY")
    print(f"🔑 Alpha Vantage 키 로드 상태: {'설정됨' if av_key else '미설정'}")

    for ticker in TARGET_TICKERS:
        pos = positions_data.setdefault(ticker, {})
        lookback_days = int(pos.setdefault("LOOKBACK_DAYS", 90))
        
        # ENTRY_MULTIPLIER가 없으면 기본값인 1.41로 자동 입력
        if "ENTRY_MULTIPLIER" not in pos:
            pos["ENTRY_MULTIPLIER"] = 1.41

        last_str = pos.get("LAST_SIGMA_UPDATE", "2000-01-01")
        last_dt = datetime.strptime(last_str, "%Y-%m-%d").date() if last_str != "2000-01-01" else datetime(2000, 1, 1).date()

        # DAILY_SIGMA가 없거나, 마지막 갱신일로부터 lookback_days 이상 경과했으면 자동 갱신 실행
        is_missing = "DAILY_SIGMA" not in pos
        is_stale = (today - last_dt).days >= lookback_days

        if not (is_missing or is_stale):
            continue

        try:
            if av_key:
                print(f"📡 {ticker} Alpha Vantage로 시그마 계산 중...")
                ts = TimeSeries(key=av_key, output_format='pandas')
                data, _ = ts.get_daily(symbol=ticker, outputsize='compact')
                closes = data['4. close']
            else:
                print(f"⚠️ Alpha Vantage 키 없음 → {ticker} yfinance로 대체")
                stock = yf.Ticker(ticker)
                hist = stock.history(period=f"{lookback_days + 15}d", interval="1d", auto_adjust=False)
                closes = hist['Close']

            if closes.empty:
                messages.append(f"⚠️ {ticker} 시그마 갱신 실패: 데이터 없음")
                continue
                
            # 과거 데이터를 오름차순(시간 순)으로 정렬
            closes = closes.sort_index(ascending=True).dropna()

            if not closes.empty:
                last_idx = closes.index[-1]
                if hasattr(last_idx, "date"):
                    last_date = last_idx.date()
                elif hasattr(last_idx, "to_pydatetime"):
                    last_date = last_idx.to_pydatetime().date()
                elif isinstance(last_idx, str):
                    try:
                        last_date = datetime.strptime(last_idx[:10], "%Y-%m-%d").date()
                    except ValueError:
                        last_date = None
                else:
                    last_date = None
                
                if last_date == today and datetime.now(ZoneInfo("America/New_York")).hour < 16:
                    closes = closes.iloc[:-1]

            # 로그 수익률 계산 (국제 표준이자 월가 정석)
            log_returns = np.log(closes / closes.shift(1)).dropna()
            
            if len(log_returns) < lookback_days:
                messages.append(f"⚠️ {ticker} 시그마 갱신 실패: 데이터 부족 ({len(log_returns)}일, 필요: {lookback_days}일)")
                continue
                
            # ddof=1 표본표준편차 구하기
            new_sigma = round(float(np.std(log_returns.tail(lookback_days), ddof=1)), 6)
            
            pos["DAILY_SIGMA"] = new_sigma
            pos["LAST_SIGMA_UPDATE"] = today.strftime("%Y-%m-%d")
            
            log_sigma_update(ticker, new_sigma)
            messages.append(f"📊 {ticker} 시그마 자동 갱신 ({lookback_days}일 기준 로그수익률): {new_sigma:.6f}")
            
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
    SOXL 전일 종가 조회 함수 (최종 리팩토링 버전)
    우선순위: Alpha Vantage → yfinance history (재시도) → info fallback
    """
    print(f"🔍 {ticker} 가격 조회 시작...")
    av_key = os.environ.get("ALPHA_VANTAGE_KEY")
    
    now_ny = datetime.now(ZoneInfo("America/New_York"))
    today_ny = now_ny.date()

    # ==================== 1. Alpha Vantage 우선 시도 ====================
    if av_key:
        try:
            print(f"   → Alpha Vantage GLOBAL_QUOTE 시도...")
            url = f"https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol={ticker}&apikey={av_key}"
            resp = requests.get(url, timeout=15)
            
            if resp.ok:
                data = resp.json()
                quote = data.get("Global Quote")
                if quote:
                    price_val = _safe_float(quote.get("05. price"))
                    prev_close_val = _safe_float(quote.get("08. previous close"))
                    latest_day_str = quote.get("07. latest trading day", "")
                    
                    if price_val is not None and latest_day_str:
                        latest_date = datetime.strptime(latest_day_str, "%Y-%m-%d").date()
                        
                        if latest_date < today_ny:
                            # API의 마지막 거래일이 과거이면, 해당 거래일 종가(05. price)를 사용
                            prev_close = price_val
                            date_str = latest_date.strftime("%m-%d")
                        elif latest_date == today_ny:
                            # API의 마지막 거래일이 오늘인 경우
                            # 16:00 (장 마감) 전이면 오늘의 가격은 실시간이므로 직전 거래일 종가(08. previous close) 사용
                            if now_ny.hour < 16:
                                prev_close = prev_close_val
                                prev_date = get_prev_trading_date(latest_date)
                                date_str = prev_date.strftime("%m-%d")
                            else:
                                # 16:00 이후이면 오늘 종가가 마감되었으므로 오늘의 종가(05. price) 사용
                                prev_close = price_val
                                date_str = latest_date.strftime("%m-%d")
                        else:
                            prev_close = price_val
                            date_str = latest_date.strftime("%m-%d")
                            
                        if prev_close is not None:
                            print(f"✅ {ticker} Alpha Vantage 성공: ${prev_close:.2f} ({date_str})")
                            return prev_close, date_str
        except Exception as e:
            print(f"   ⚠️ Alpha Vantage 실패: {e}")

    # ==================== 2. yfinance History (재시도 강화) ====================
    for attempt in range(3):
        try:
            print(f"   → yfinance history 시도 ({attempt+1}/3)...")
            stock = yf.Ticker(ticker)
            hist = stock.history(
                period="15d", 
                interval="1d", 
                auto_adjust=False, 
                rounding=True
            )
            
            if not hist.empty:
                close_series = hist['Close'].dropna()
                if not close_series.empty:
                    last_idx = close_series.index[-1]
                    last_date = last_idx.date() if hasattr(last_idx, "date") else last_idx
                    
                    if last_date < today_ny:
                        # 마지막 데이터 날짜가 과거이면, 그 날의 종가를 사용
                        prev_close = float(close_series.iloc[-1])
                        date_str = last_date.strftime("%m-%d")
                    elif last_date == today_ny:
                        # 마지막 데이터 날짜가 오늘인 경우
                        # 16:00 전이면 오늘의 데이터는 실시간봉이므로 직전 거래일 봉(index -2)을 사용
                        if now_ny.hour < 16 and len(close_series) >= 2:
                            prev_close = float(close_series.iloc[-2])
                            prev_date = close_series.index[-2].date()
                            date_str = prev_date.strftime("%m-%d")
                        else:
                            # 16:00 이후이면 오늘 종가가 확정되었으므로 오늘의 종가를 사용
                            prev_close = float(close_series.iloc[-1])
                            date_str = last_date.strftime("%m-%d")
                    else:
                        prev_close = float(close_series.iloc[-1])
                        date_str = last_date.strftime("%m-%d")
                        
                    print(f"✅ {ticker} yfinance 성공: ${prev_close:.2f} ({date_str})")
                    return prev_close, date_str
                    
        except Exception as e:
            print(f"   ⚠️ yfinance 시도 {attempt+1} 실패: {e}")
            if attempt < 2:
                import time
                time.sleep(2.0)

    # ==================== 3. yfinance Info Fallback ====================
    try:
        print(f"   → yfinance info fallback 시도...")
        info = stock.info
        
        is_weekday = now_ny.weekday() < 5
        is_market_hours = is_weekday and (9 <= now_ny.hour < 16 or (now_ny.hour == 9 and now_ny.minute >= 30))
        
        # 장중일 때는 이전 영업일 종가인 previousClose를 우선 참조하고, 장외/휴일일 때는 최신 가격인 currentPrice/regularMarketPrice를 우선 참조
        if is_market_hours:
            keys_priority = ["previousClose", "regularMarketPreviousClose", "currentPrice", "regularMarketPrice"]
        else:
            keys_priority = ["currentPrice", "regularMarketPrice", "previousClose", "regularMarketPreviousClose", "navPrice"]
            
        for key in keys_priority:
            price = _safe_float(info.get(key))
            if price is not None and not np.isnan(price):
                print(f"✅ {ticker} info 성공: ${price:.2f} (key: {key})")
                return price, "N/A"
    except Exception as e:
        print(f"   ⚠️ info fallback 실패: {e}")

    # ==================== 최종 실패 ====================
    print(f"❌ {ticker} 모든 가격 조회 방법 실패")
    return None, "N/A"


def calculate_loc_price(ticker: str, prev_close: float, cfg: dict) -> float:
    """
    설정 파일(config.json)의 1시그마와 진입배수를 적용하여
    최종 오늘 밤 LOC 매수 지정가를 계산하는 함수
    """
    pos_cfg = cfg.setdefault("POSITIONS", {}).setdefault(ticker, {})
    multiplier = pos_cfg.get("ENTRY_MULTIPLIER", 1.41)
    sigma = pos_cfg.get("DAILY_SIGMA")
 
    if sigma is not None:
        target_drop_rate = sigma * multiplier
        loc_price = prev_close * (1 - target_drop_rate)
        print(f"   [설정 값 사용] 1시그마(로그변동성): {sigma*100:.6f}% (진입 배수: {multiplier}배)")
        print(f"   [설정 값 사용] 목표 진입 하락률       : {target_drop_rate*100:.3f}%")
        return round(loc_price, 2)
 
    # ==================== Fallback (설정값 없을 시 실시간 연산) ====================
    print(f"   ⚠️ {ticker}의 설정값이 없어 실시간으로 시그마를 계산합니다...")
    stock = yf.Ticker(ticker)
    hist = stock.history(period="92d", interval="1d", auto_adjust=False)
    
    if len(hist) < 90:
        raise ValueError(f"{ticker}의 90영업일 데이터가 충분하지 않습니다.")
        
    close_prices = hist['Close'].dropna().tail(91)
    log_returns = np.log(close_prices / close_prices.shift(1)).dropna()
    sigma_90 = np.std(log_returns.tail(90), ddof=1)
    target_drop_rate = sigma_90 * multiplier
    loc_price = prev_close * (1 - target_drop_rate)
    
    print(f"   [실시간 분석] 90일 1시그마(로그변동성): {sigma_90*100:.6f}% (진입 배수: {multiplier}배)")
    print(f"   [실시간 분석] 목표 진입 하락률       : {target_drop_rate*100:.3f}%")
    
    return round(loc_price, 2)


def run_integrated_system(ticker: str, cfg: dict):
    """수집 엔진과 연산 엔진을 유기적으로 제어하는 메인 실행 함수"""
    print("=" * 60)
    print(f"📡 {ticker} 통합 LOC 연산 시스템 가동")
    print("=" * 60)
    
    # Step 1: 전일 종가 자동 수집
    prev_close, trading_date = get_prev_close(ticker)
    
    if prev_close is None:
        print(f"🚨 {ticker} 가격 데이터를 수집하는 데 실패했습니다.")
        print("=" * 60)
        return
 
    print(f"✅ 전일 종가 수집 성공: ${prev_close:.2f} (거래일 기준: {trading_date})")
    print("-" * 60)
    
    # Step 2: 목표 LOC 가격 자동 연산
    try:
        final_loc_price = calculate_loc_price(ticker, prev_close, cfg)
        print("-" * 60)
        print(f"🎯 오늘 밤 {ticker} LOC 매수 지정가: ${final_loc_price:.2f}")
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
        loc_price = prev_close * (1 - multiplier * sigma)

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
    # 1. 설정 로드 및 시그마/진입배수 자동 갱신
    print("⚙️ 시스템 설정 및 변동성 자동 갱신 시작...")
    cfg = load_config()
    sigma_messages = refresh_sigma_if_stale(cfg)
    save_config(cfg)
    if sigma_messages:
        print("\n".join(sigma_messages))
        print()
    print("✅ 설정 로드 및 시그마 갱신 완료\n")

    # 2. 포트폴리오의 두 주인공인 SOXL과 MVLL를 차례대로 연산하여 출력
    for ticker in TARGET_TICKERS:
        run_integrated_system(ticker, cfg)
        print("\n")

    # 3. 디스코드 브리핑 전송 및 시스템 루틴 완료 (월초 핑 등 포함)
    print("📡 디스코드 브리핑 전송 시작...")
    execute_dual_tactical_trader()