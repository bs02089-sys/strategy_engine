"""SOXL/TQQQ 롤링 σ LOC 알림 — 마지막 마감 세션 판정 + 다음 세션 LOC 목표가를 디스코드로 전송.

설정·σ 정의·다운로드는 전부 `soxl_tqqq_2sigma_count.py` 공용 (이 파일엔 판정 결과의 포맷·전송만 있다).
- '신호'는 체결 기준(전일까지의 σ)으로 판정한다 — 어제 걸어둔 LOC 가 오늘 실제로 체결됐는가.
- 다음 세션 목표가는 기준 세션까지의 σ 로 계산하고, 매수 지정가라 센트 단위로 **내림**한다.
- 종목당 이력(STATS_DAYS)을 한 번만 받아 판정과 발동 빈도 통계가 같이 쓴다.
"""

import argparse
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from soxl_tqqq_2sigma_count import (
    STATS_DAYS,
    download_daily,
    expected_session,
    frequency_stats,
    iso,
    load_settings,
    signal_frame,
)

KST = ZoneInfo("Asia/Seoul")

# ==================== 설정 ====================
_settings = load_settings()
TICKERS = _settings.tickers
WINDOW = _settings.window
MULT = _settings.multiplier
SIG = f"{MULT:g}σ"  # 메시지·로그 라벨 — 배수를 바꿔도 문구가 어긋나지 않는다

# 환경변수에서 웹훅 가져오기 (필수) — 시크릿 복사 때 섞인 공백·개행은 제거
DISCORD_WEBHOOK_URL = (os.getenv("DISCORD_WEBHOOK_URL") or "").strip()
if not DISCORD_WEBHOOK_URL:
    raise ValueError("환경변수 DISCORD_WEBHOOK_URL이 설정되지 않았습니다.")

RESEND_NOTE = "\n🔁 전송 재시도 (이전 전송 실패분 — 중복 수신 시 주문 중복 주의)"
RATE_LIMIT_MAX_WAIT = 30.0  # 디스코드 429 의 retry_after 상한(초)
# ==============================================


# ==================== 전송 ====================
def _retry_after(response: requests.Response) -> float:
    try:
        return min(float(response.json().get("retry_after", 1.0)), RATE_LIMIT_MAX_WAIT)
    except Exception:
        return 1.0


def send_discord_message(content: str) -> bool:
    """디스코드 웹훅으로 메시지 전송 — 성공 여부를 돌려준다(실패는 삼키지 않고 호출부가 처리).

    429(레이트리밋)는 응답의 retry_after 만큼 기다려 1회만 즉시 재시도한다(429 는 미전달이 확실해
    중복 위험이 없다). 예외 로그엔 타입만 남긴다 — requests 예외 메시지엔 웹훅 경로(토큰)가 섞인다.
    """
    for attempt in range(2):
        try:
            response = requests.post(DISCORD_WEBHOOK_URL, json={"content": content}, timeout=10)
        except Exception as e:
            print(f"❌ 디스코드 전송 중 오류: {type(e).__name__}")
            return False
        if response.status_code == 204:
            print("✅ 디스코드 알림 전송 성공")
            return True
        if response.status_code == 429 and attempt == 0:
            wait = _retry_after(response)
            print(f"⏳ 디스코드 레이트리밋 — {wait:g}초 뒤 1회 재시도")
            time.sleep(wait)
            continue
        print(f"❌ 디스코드 전송 실패: {response.status_code} - {response.text[:200]}")
        return False
    return False


def send_ping() -> None:
    """월간 수신 경로 점검 — 매월 1일 웹훅이 살아있는지 확인용 메시지 1건.

    용도: 디스코드를 **수신 전용**으로 쓰다 보면 웹훅이 무효화돼도 알림이 오지 않는 것으로만
    알 수 있다(알림이 없는 게 정상인 날도 많아서). 그래서 달력 기준 매월 1일 09:00 KST 에
    PING 메시지 1건을 보내고, 보내기 실패는 종료코드 1 로 올려 워크플로우 실패(GitHub 알림 메일)가
    수신 경로의 2차 신호가 된다 — 판정·LOC 목표가와는 무관한 메시지다.
    """
    kst = datetime.now(KST)
    ok = send_discord_message("\n".join([
        f"[{SIG} 알림 웹훅 PING] {kst:%Y-%m-%d} (KST {kst:%H:%M})",
        "수신 경로 점검용 월간 핑입니다 — 이 메시지가 보이면 알림 웹훅이 살아 있습니다.",
        f"이 핑이 안 오면 웹훅이 무효화된 것이고, {SIG}·약세 알림도 함께 오지 않습니다.",
    ]))
    if not ok:
        # 웹훅 무효·장애를 '조용한 성공' 으로 넘기지 않는다 (워크플로우 실패 = GitHub 알림 메일)
        raise SystemExit(1)


def send_missing_notice(ticker: str, reason: str, known: str, retries: int = 0) -> bool:
    """판정 불가 안내 — 사유와 마지막 확정 세션을 남겨 직전 알림 목표가 재사용을 막는다."""
    tail = f"재시도 {retries}회 후에도 미해결" if retries else "재시도 없음"
    return send_discord_message("\n".join([
        f"[{ticker} {SIG} 알림 생략 — 판정 불가]",
        f"사유: {reason}",
        f"마지막 확정 세션: {known}",
        f"🎯 다음 세션 LOC 목표가: 계산 보류 — 직전 목표가 재사용 금지 ({tail})",
    ]))


# ==================== 판정 ====================
def floor_cent(x: float) -> float:
    """매수 지정가는 내림 — 반올림으로 올라간 1센트만큼 체결 범위가 넓어지는 것을 막는다."""
    return float(Decimal(repr(x)).quantize(Decimal("0.01"), rounding=ROUND_DOWN))


def frequency_note(ticker: str, close: pd.Series) -> str:
    """최근 1년 발동 실적 + 장기 중앙값 한 줄 (연간 예산을 몇 회로 나눌지의 분모).

    이미 받은 이력(`close`)을 재사용하고, 계산은 공용 `frequency_stats` 에서만 한다.
    통계는 부가 기능이라 실패해도 알림은 그대로 나가야 한다.
    """
    try:
        s = frequency_stats(ticker, WINDOW, MULT, close=close)
    except Exception as e:
        print(f"⚠️ [{ticker}] 발동 빈도 계산 실패(알림은 계속): {type(e).__name__}: {e}")
        return ""
    return (f"📊 예상 발동: 연 {s['median']:g}회(장기 중앙값) · 최근 1년 실제 {s['recent']}회 "
            f"→ 연간 예산 ÷ {s['median']:g}")


def evaluate(ticker: str) -> tuple[str | None, str, str]:
    """종목 1회 판정 → (알림 메시지, 판정 불가 사유, 마지막 확정 세션).

    정상이면 (메시지, "", ""), 판정 불가면 (None, 사유, 마지막 확정 세션). **전송은 호출부가 한다** —
    결측(데이터)과 전송 실패를 같은 재시도 루프에서 다루려면 메시지를 만들어 두고 재전송할 수 있어야 한다.
    """
    print(f"\n📡 [{ticker}] 데이터 불러오는 중...")
    try:
        df = download_daily(ticker, datetime.now() - timedelta(days=STATS_DAYS))
    except RuntimeError as e:
        print(f"❌ {e}")
        return (None, str(e), "없음")

    raw_close = df["Close"]
    close = raw_close.dropna()  # 결측 봉을 건너뛴 확정 종가 (σ·신호 계산은 이것만 쓴다)
    session = iso(df.index[-1])
    known = f"${float(close.iloc[-1]):.2f} (세션 {iso(close.index[-1])})" if len(close) else "없음"
    frame = signal_frame(close, WINDOW, MULT)
    expected = expected_session()  # 캘린더가 없으면 None → 봉 미수신 검사만 생략

    # ⚠️ 판정 불가 가드 — 마지막 마감 세션 봉이 확정되지 않았으면 직전 세션 값을 다시 알리지 않는다.
    # ① 봉은 있는데 종가가 NaN (2026-10-17 실측: 야후가 10-16 일봉을 Open/High/Low/Volume 만 주고
    #    Close 는 NaN 으로 반환 → 알림이 어제와 같은 세션·같은 목표가를 재발송할 뻔했다).
    # ② 봉 자체가 아직 없음 — NaN 검사로는 안 잡히므로 '있어야 할 세션'과 최신 봉 날짜를 비교한다
    #    (휴장일은 캘린더가 직전 세션으로 되돌려 주므로 오탐이 없다).
    # ③ σ 계산 불가 — 기준 세션과 직전 세션 σ 가 모두 필요하다 (σ[t] = 목표가, σ[t−1] = 신호 판정).
    if pd.isna(raw_close.iloc[-1]):
        reason = f"{session} 세션 종가 미제공 (yfinance 결측)"
    elif expected and session < expected:
        reason = f"{expected} 세션 봉 미수신 (최신 봉 {session})"
    elif len(frame) < WINDOW + 2 or frame["sigma"].iloc[-2:].isna().any():
        reason = f"{session} 세션 σ 계산 불가 (최근 {WINDOW + 1}거래일 연속 종가 필요)"
    else:
        reason = None
    if reason:
        print(f"⚠️ [{ticker}] 판정 불가 — {reason}")
        return (None, reason, known)

    last = frame.iloc[-1]
    prev_close = float(frame["close"].iloc[-2])  # 콘솔 출력용
    latest_close = float(last["close"])
    latest_return = float(last["ret"])
    fill_threshold = float(frame["sigma"].iloc[-2]) * MULT  # 신호 판정: 전일까지 σ × 배수
    next_threshold = float(last["sigma"]) * MULT            # 목표가: 기준 세션까지 σ × 배수
    is_buy_signal = bool(last["fill_hit"])
    target_loc_price = floor_cent(latest_close * (1.0 - next_threshold))

    # 콘솔 출력
    print("=" * 55)
    print(f"체크 시간      : {datetime.now(KST):%Y-%m-%d %H:%M:%S} KST")
    print(f"종목           : {ticker}")
    print(f"직전 세션 종가 : ${prev_close:.2f}")
    print(f"기준 세션 종가 : ${latest_close:.2f}")
    print(f"기준 세션 수익률: {latest_return * 100:.2f}%")
    print(f"신호 임계({SIG}, 전일까지 σ): −{fill_threshold * 100:.2f}%")
    print(f"다음 세션 {SIG}  : {next_threshold * 100:.2f}% ({WINDOW}일 롤링, 배율 {MULT:g})")
    print(f"🎯 다음 세션 LOC 목표가: ${target_loc_price:.2f} (센트 내림)")
    print(f"매수 신호      : {'✅ 발생!' if is_buy_signal else '❌ 없음'}")
    print("=" * 55)

    # 용어는 '주문 기준' 프레임으로 쓴다 — 여기서 '전일 종가' 는 알림 직전에 마감된 세션(기준
    # 세션)의 종가이고, 목표가는 그 다음 세션에 걸 주문 가격이다. 신호 여부에 따라 헤더·상태만
    # 다르다 (빈 줄은 filter(None, ...) 로 제거)
    message = "\n".join(filter(None, [
        f"[{ticker} {SIG} 매수 신호 발생]" if is_buy_signal else f"[{ticker} 장마감 및 LOC 목표가 안내]",
        f"기준 세션: {session} 마감",
        f"전일 종가: ${latest_close:.2f} ({latest_return * 100:+.2f}%)",
        f"{SIG} 신호 임계: −{fill_threshold * 100:.2f}% (전일까지 {WINDOW}일 롤링 σ × {MULT:g})",
        f"{SIG} 기준값: {next_threshold * 100:.2f}% (기준 세션까지 σ → 다음 세션 목표가에 사용)",
        f"🎯 다음 세션 LOC 목표가: ${target_loc_price:.2f}",
        frequency_note(ticker, close),
        "상태: 매수 조건 충족!" if is_buy_signal else "상태: 일반 장세 (참고용)",
    ]))
    return (message, "", "")


# ==================== 실행 (재시도 큐) ====================
@dataclass
class Job:
    ticker: str
    message: str | None = None  # 만들어 둔 알림 — 있으면 '재전송' 대상 (재다운로드·재판정 없음)
    reason: str = ""            # 판정 불가 사유
    known: str = ""             # 마지막 확정 세션


def check_signals(retry_wait: float = 0.0, retry_attempts: int = 0) -> None:
    """모든 종목을 판정·전송하고, **결측이거나 전송이 실패한** 종목은 retry_wait 초 뒤 다시 시도한다.

    재시도는 **같은 프로세스에서 다시 받아온다** — yfinance 의 가격 히스토리 경로
    (`YfData.get_raw_json`)는 응답 캐시를 쓰지 않아 매번 새 요청이 나간다(2026-10-17 확인).
    - 결측 재시도: 그 종목을 다시 받아 재판정한다.
    - 전송 실패 재시도: **이미 만든 메시지를 그대로 다시 보낸다** — 재다운로드·재판정이 없어 같은
      세션이 두 번 알림되지 않는다(전송만 실패한 경우 값이 바뀌지 않는다). 이때만 재전송 표시를
      붙인다 — 결측이었다가 복구돼 처음 보내는 메시지에는 붙이지 않는다.
    끝까지 실패한 종목만 판정 불가 안내를 한 번 받고, 알림을 끝내 전달하지 못했으면 종료코드 1 로
    올려 워크플로우 실패(= GitHub 알림 메일)가 수신 경로의 2차 신호가 된다.
    """
    wait_label = f"{retry_wait / 60:.0f}분" if retry_wait >= 60 else f"{retry_wait:g}초"
    queue = [Job(t) for t in TICKERS]
    undelivered: list[str] = []  # 재시도를 다 쓰고도 전달되지 않은 종목

    for attempt in range(retry_attempts + 1):
        is_last = attempt == retry_attempts
        retry_queue: list[Job] = []
        for job in queue:
            # 지역 변수에 담아 쓴다 — 속성(job.message)은 str | None 으로 남아 편집기가 None 가능성을 지적한다
            message = job.message
            is_resend = message is not None  # 루프 진입 시점에 메시지가 있으면 이전 전송이 실패한 것
            if message is None:  # 첫 실행·결측 재시도분 — 판정부터
                message, job.reason, job.known = evaluate(job.ticker)
                job.message = message
                if message is None:  # 결측 — 재판정 대상
                    if not is_last:
                        print(f"⏳ [{job.ticker}] {job.reason} — {wait_label} 뒤 재시도 "
                              f"({attempt + 1}/{retry_attempts})")
                        retry_queue.append(job)
                    elif not send_missing_notice(job.ticker, job.reason, job.known, attempt):
                        undelivered.append(job.ticker)
                    continue
            # 여기부터 message 는 항상 str. 재전송에는 표시를 붙인다 — 타임아웃 뒤 실제로는 도착했을 수 있어 중복 주문을 막는다
            if send_discord_message(message + RESEND_NOTE if is_resend else message):
                continue
            if not is_last:
                print(f"⏳ [{job.ticker}] 전송 실패 — {wait_label} 뒤 재전송 ({attempt + 1}/{retry_attempts})")
                retry_queue.append(job)
            else:
                print(f"❌ [{job.ticker}] 알림 전송 {attempt + 1}회 실패 — 전달되지 않았다")
                undelivered.append(job.ticker)
        queue = retry_queue
        if not queue:
            break
        time.sleep(retry_wait)

    if undelivered:
        # 수신 경로가 죽었는데 조용한 성공으로 끝내지 않는다 — 워크플로우 실패가 2차 신호다
        raise SystemExit(f"❌ 전송 실패: {', '.join(undelivered)} — 웹훅이 무효화됐을 수 있습니다")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=f"SOXL/TQQQ 롤링 {SIG} LOC 알림")
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