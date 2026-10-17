import os
import time
import argparse
import yfinance as yf
import pandas as pd
import requests
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from soxl_tqqq_2sigma_count import download_end  # 다운로드 창 끝(마지막 마감 세션) 공용 정의

# ==================== 설정 불러오기 ====================
def load_config():
    config_path = Path(__file__).parent / "soxl_tqqq_config.json"
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

config = load_config()

# 환경변수에서 웹훅 가져오기 (필수)
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL") or ""  # 타입도 str 로 고정(빈 값은 아래에서 거른다)

if not DISCORD_WEBHOOK_URL:
    raise ValueError("환경변수 DISCORD_WEBHOOK_URL이 설정되지 않았습니다.")

# soxl_tqqq_config.json에서 종목 설정 불러오기 (단일/복수 모두 호환)
TICKERS = config.get("tickers")
if not TICKERS:
    single_ticker = config.get("ticker")
    if single_ticker:
        TICKERS = [single_ticker]
    else:
        raise ValueError("설정 파일(soxl_config.json)에 'ticker' 또는 'tickers'가 설정되지 않았습니다.")

LOOKBACK_DAYS = config.get("lookback_days", 60)
ROLLING_WINDOW = config.get("rolling_window", 20)
SIGMA_MULTIPLIER = config.get("sigma_multiplier", 2.0)
# ======================================================

def send_discord_message(content: str) -> bool:
    """디스코드 웹훅으로 메시지 전송 — 성공 여부를 돌려준다(실패는 삼키지 않고 로그·호출부가 처리)."""
    data = {"content": content}
    try:
        response = requests.post(DISCORD_WEBHOOK_URL, json=data, timeout=10)
        if response.status_code == 204:
            print("✅ 디스코드 알림 전송 성공")
            return True
        print(f"❌ 디스코드 전송 실패: {response.status_code} - {response.text}")
    except Exception as e:
        print(f"❌ 디스코드 전송 중 오류: {e}")
    return False

def send_ping() -> None:
    """월간 수신 경로 점검 — 매월 1일 웹훅이 살아있는지 확인용 메시지 1건.

    용도: 디스코드를 **수신 전용**으로 쓰다 보면 웹훅이 무효화돼도 알림이 오지 않는 것으로만
    알 수 있다(알림이 없는 게 정상인 날도 많아서). 그래서 달력 기준 매월 1일 09:00 KST 에
    PING 메시지 1건을 보내고, 보내기 실패는 종료코드 1 로 올려 워크플로우 실패(GitHub 알림 메일)가
    수신 경로의 2차 신호가 된다 — 판정·LOC 목표가와는 무관한 메시지다.
    """
    kst = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Seoul"))
    ok = send_discord_message("\n".join([
        f"[2σ 알림 웹훅 PING] {kst:%Y-%m-%d} (KST {kst:%H:%M})",
        "수신 경로 점검용 월간 핑입니다 — 이 메시지가 보이면 알림 웹훅이 살아 있습니다.",
        "이 핑이 안 오면 웹훅이 무효화된 것이고, 2σ·약세 알림도 함께 오지 않습니다.",
    ]))
    if not ok:
        # 웹훅 무효·장애를 '조용한 성공' 으로 넘기지 않는다 (워크플로우 실패 = GitHub 알림 메일)
        raise SystemExit(1)

def frequency_note(ticker: str) -> str:
    """최근 1년 발동 실적 + 장기 중앙값 한 줄 (연간 예산을 몇 회로 나눌지의 분모).

    계산은 `soxl_tqqq_2sigma_count.frequency_stats` 공용 함수에서만 한다 (σ·판정 정의 단일 소스).
    통계는 부가 기능이라 실패해도 2σ 알림은 그대로 나가야 한다.
    """
    try:
        from soxl_tqqq_2sigma_count import frequency_stats
        s = frequency_stats(ticker, ROLLING_WINDOW, SIGMA_MULTIPLIER)
    except Exception as e:
        print(f"⚠️ [{ticker}] 발동 빈도 계산 실패(알림은 계속): {e}")
        return ""
    return (f"📊 예상 발동: 연 {s['median']:g}회(장기 중앙값) · 최근 1년 실제 {s['recent']}회 "
            f"→ 연간 예산 ÷ {s['median']:g}")


def extract_scalar(val):
    """Pandas Series나 복합 구조에서 안전하게 스칼라(숫자) 값 추출"""
    if hasattr(val, "iloc"):
        return float(val.iloc[0])
    if hasattr(val, "item"):
        return float(val.item())
    return float(val)

def send_missing_notice(ticker: str, reason: str, known: str, retries: int = 0) -> bool:
    """판정 불가 안내 — 사유와 마지막 확정 세션을 남겨 직전 알림 목표가 재사용을 막는다."""
    tail = f"재시도 {retries}회 후에도 미해결" if retries else "재시도 없음"
    return send_discord_message("\n".join([
        f"[{ticker} 2σ 알림 생략 — 판정 불가]",
        f"사유: {reason}",
        f"마지막 확정 세션: {known}",
        f"🎯 다음 세션 LOC 목표가: 계산 보류 — 직전 목표가 재사용 금지 ({tail})",
    ]))

def evaluate(ticker: str, start_date: datetime) -> tuple[str | None, str, str]:
    """종목 1회 판정 → (알림 메시지, 판정 불가 사유, 마지막 확정 세션).

    정상이면 (메시지, "", ""), 판정 불가면 (None, 사유, 마지막 확정 세션). **전송은 호출부가 한다** —
    결측(데이터)과 전송 실패를 같은 재시도 루프에서 다루려면 메시지를 만들어 두고 재전송할 수 있어야 한다.
    """
    print(f"\n📡 [{ticker}] 데이터 불러오는 중...")
    df = yf.download(
        ticker,
        start=start_date.strftime("%Y-%m-%d"),
        end=download_end(),  # 시계(UTC/KST) 대신 ET 마지막 마감 세션 기준 — CI·로컬 동일
        progress=False
    )

    if df is None or df.empty:  # yfinance 는 실패 시 None 을 돌려줄 수 있다
        print(f"❌ [{ticker}] 데이터를 가져오지 못했습니다.")
        return (None, f"{ticker} 데이터 수신 실패 (yfinance)", "없음")

    df['Return'] = df['Close'].pct_change()
    df['Vol_20'] = df['Return'].rolling(window=ROLLING_WINDOW).std()
    df['Sigma2'] = df['Vol_20'] * SIGMA_MULTIPLIER

    # ⚠️ 판정 불가 가드 — 마지막 마감 세션 봉이 확정되지 않았으면 직전 세션 값을 다시 알리지 않는다.
    # `clean_df = df.dropna()` 만 쓰면 종가가 결측인 마지막 봉이 조용히 사라지고, 알림이 어제와
    # 같은 세션·같은 목표가를 재발송한다 (2026-10-17 실측: 야후가 10-16 일봉을 Open/High/Low/
    # Volume 만 주고 Close 는 NaN 으로 반환 → 10-15 세션이 '기준 세션'으로 두 번 발송되고, 새 LOC
    # 목표가도 하루 낡은 값이었다). 휴장일은 그 날 봉 자체가 없어 이 가드에 걸리지 않는다 —
    # 그때는 직전 봉이 곧 마지막 마감 세션이고, 결측은 '있는데 값이 없는' 경우에만 발생한다.
    session_str = str(df.index[-1])[:10]
    session_close = extract_scalar(df['Close'].iloc[-1])
    clean_df = df.dropna()
    if pd.isna(session_close):
        reason = f"{session_str} 세션 종가 미제공 (yfinance 결측)"
    elif clean_df.empty or clean_df.index[-1] != df.index[-1]:
        reason = f"{session_str} 세션 σ 계산 이력 부족 (최근 {ROLLING_WINDOW}거래일 필요)"
    else:
        reason = None
    if reason:
        known = df['Close'].dropna()  # 결측 봉을 건너뛴 마지막 확정 종가
        known_str = (f"${extract_scalar(known.iloc[-1]):.2f} (세션 {str(known.index[-1])[:10]})"
                     if len(known) else "없음")
        print(f"⚠️ [{ticker}] 판정 불가 — {reason}")
        return (None, reason, known_str)

    latest = clean_df.iloc[-1]

    # 기준 세션의 하루 전(직전 세션) 종가 — 결측 봉을 건너뛴 유효 종가에서 뽑는다
    prev_close = extract_scalar(df['Close'].dropna().iloc[-2])

    # 기준 세션(마지막으로 마감된 세션) 지표 안전 추출
    latest_close = extract_scalar(latest['Close'])
    latest_return = extract_scalar(latest['Return'])
    latest_sigma2 = extract_scalar(latest['Sigma2'])

    # 기준 세션 종가 × (1 − 2σ) → 다음 세션에 걸 LOC 매수 목표가
    target_loc_price = latest_close * (1.0 - latest_sigma2)
    is_buy_signal = latest_return < -latest_sigma2

    # 2σ 자체의 퍼센트 수치
    sigma_pct = latest_sigma2 * 100

    # 콘솔 출력
    print("=" * 55)
    print(f"체크 시간      : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"종목           : {ticker}")
    print(f"직전 세션 종가 : ${prev_close:.2f}")
    print(f"기준 세션 종가 : ${latest_close:.2f}")
    print(f"기준 세션 수익률: {latest_return*100:.2f}%")
    print(f"20일 롤링 2σ   : {sigma_pct:.2f}% (배율: {SIGMA_MULTIPLIER})")
    print(f"🎯 다음 세션 LOC 목표가: ${target_loc_price:.2f}")
    print(f"매수 신호      : {'✅ 발생!' if is_buy_signal else '❌ 없음'}")
    print("=" * 55)

    # 디스코드 메시지 구성
    date_str = session_str  # 가드가 확정한 마지막 마감 세션 — 스텁 이슈 없는 문자열 라벨 재사용

    # 용어는 '주문 기준' 프레임으로 쓴다 — 여기서 '전일 종가' 는 알림 직전에 마감된 세션(기준
    # 세션)의 종가이고, 목표가는 그 다음 세션에 걸 주문 가격이다(σ·수익률도 같은 기준 세션 값).
    # 신호 여부에 따라 헤더·상태만 다르다 (빈 줄은 filter(None, ...) 로 제거)
    message = "\n".join(filter(None, [
        f"[{ticker} 2σ 매수 신호 발생]" if is_buy_signal else f"[{ticker} 장마감 및 LOC 목표가 안내]",
        f"기준 세션: {date_str} 마감",
        f"전일 종가: ${latest_close:.2f} ({latest_return*100:.2f}%)",
        f"2σ 기준값: {sigma_pct:.2f}% (20일 롤링 {SIGMA_MULTIPLIER}배)",
        f"🎯 다음 세션 LOC 목표가: ${target_loc_price:.2f}",
        frequency_note(ticker),
        "상태: 매수 조건 충족!" if is_buy_signal else "상태: 일반 장세 (참고용)",
    ]))

    return (message, "", "")

def check_signals(retry_wait: float = 0.0, retry_attempts: int = 0) -> None:
    """모든 종목을 판정·전송하고, **결측이거나 전송이 실패한** 종목은 retry_wait 초 뒤 다시 시도한다.

    재시도는 **같은 프로세스에서 다시 받아온다** — yfinance 의 가격 히스토리 경로
    (`YfData.get_raw_json`)는 응답 캐시를 쓰지 않아 매번 새 요청이 나간다(2026-10-17 확인).
    - 결측 재시도: 그 종목을 다시 받아 재판정한다.
    - 전송 실패 재시도: **이미 만든 메시지를 그대로 다시 보낸다** — 재다운로드·재판정이 없어 같은
      세션이 두 번 알림되지 않는다(전송만 실패한 경우 값이 바뀌지 않는다).
    끝까지 실패한 종목만 판정 불가 안내를 한 번 받고, 알림을 끝내 전달하지 못했으면 종료코드 1 로
    올려 워크플로우 실패(= GitHub 알림 메일)가 수신 경로의 2차 신호가 된다.
    """
    start_date = datetime.now() - timedelta(days=LOOKBACK_DAYS)
    wait_label = f"{retry_wait / 60:.0f}분" if retry_wait >= 60 else f"{retry_wait:g}초"
    # 작업 큐: (종목, 보낼 메시지, 판정 불가 사유, 마지막 확정 세션) — 전송 실패 시 메시지를 재사용한다
    queue: list[tuple[str, str | None, str, str]] = [(str(t), None, "", "") for t in TICKERS]
    undelivered: list[str] = []  # 재시도를 다 쓰고도 전달되지 않은 종목

    for attempt in range(retry_attempts + 1):
        retry_queue: list[tuple[str, str | None, str, str]] = []
        for ticker, message, reason, known in queue:
            if message is None:  # 첫 실행·결측 재시도분 — 판정부터
                message, reason, known = evaluate(ticker, start_date)
                if message is None:  # 결측 — 재판정 대상
                    if attempt < retry_attempts:
                        print(f"⏳ [{ticker}] {reason} — {wait_label} 뒤 재시도 ({attempt + 1}/{retry_attempts})")
                        retry_queue.append((ticker, None, reason, known))
                    elif not send_missing_notice(ticker, reason, known, attempt):
                        undelivered.append(ticker)
                    continue
            # 전송 재시도에는 표시를 붙인다 — 타임아웃 뒤 실제로는 도착했을 수 있어 중복 주문을 막는다
            text = message if attempt == 0 else message + "\n🔁 전송 재시도 (이전 전송 실패분 — 중복 수신 시 주문 중복 주의)"
            if send_discord_message(text):
                continue
            if attempt < retry_attempts:
                print(f"⏳ [{ticker}] 전송 실패 — {wait_label} 뒤 재전송 ({attempt + 1}/{retry_attempts})")
                retry_queue.append((ticker, message, reason, known))
            else:
                print(f"❌ [{ticker}] 알림 전송 {attempt + 1}회 실패 — 전달되지 않았다")
                undelivered.append(ticker)
        queue = retry_queue
        if not queue:
            break
        time.sleep(retry_wait)

    if undelivered:
        # 수신 경로가 죽었는데 조용한 성공으로 끝내지 않는다 — 워크플로우 실패가 2차 신호다
        raise SystemExit(f"❌ 전송 실패: {', '.join(undelivered)} — 웹훅이 무효화됐을 수 있습니다")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SOXL/TQQQ 롤링 2σ LOC 알림")
    parser.add_argument("--retry-wait", type=float, default=0.0,
                        help="결측·전송 실패 시 재시도 간격(초). 기본 0 = 대기 없음")
    parser.add_argument("--retry-attempts", type=int, default=0,
                        help="재시도 횟수(결측 재판정 · 전송 재전송). 기본 0 = 재시도 없음 (CI 는 1시간 간격 2회)")
    parser.add_argument("--ping", action="store_true",
                        help="수신 경로 점검용 메시지 1건만 전송 (매월 1일 CI. 시세 조회 없음)")
    args = parser.parse_args()
    if args.ping:
        send_ping()
    else:
        check_signals(args.retry_wait, args.retry_attempts)
