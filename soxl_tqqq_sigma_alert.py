"""soxl_tqqq_sigma_alert.py

SOXL/TQQQ 롤링 시그마 기반 매수·매도 목표가 알림
- 매수: 롤링 WINDOW일 MULT σ LOC (센트 내림)
- 매도: 롤링 WINDOW일 2σ 지정가 (반올림)
- 마지막 확정 세션을 기준으로 다음 세션 목표가를 계산하여 디스코드로 전송

설정·σ 계산·데이터 다운로드는 soxl_tqqq_2sigma_count.py를 공용으로 사용한다.
이 파일은 판정 결과의 포맷팅과 전송만 담당한다.

주요 로직:
- 신호 판정은 전일까지의 σ를 기준으로 한다 (어제 걸어둔 LOC가 오늘 체결되었는지).
- 다음 세션 목표가는 기준 세션까지의 σ로 계산한다.
- 종목당 이력을 한 번만 받아 판정과 발동 빈도 통계에 함께 사용한다.
"""

import argparse
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from soxl_tqqq_2sigma_count import (
    STATS_DAYS,
    download_daily,
    expected_session,
    frequency_stats,
    is_trading_day_today,
    iso,
    load_settings,
    signal_frame,
)

KST = ZoneInfo("Asia/Seoul")

# ==================== 설정 ====================
_settings = load_settings()
TICKERS = _settings.tickers
WINDOW = _settings.window
MULT = _settings.multiplier          # 매수 배수 (기본 2.0)
SELL_MULT = 2.0                      # 매도 배수
SIG = f"{MULT:g}σ"                   # 매수 라벨
SELL_SIG = f"{SELL_MULT:g}σ"         # 매도 라벨

# 디스코드 웹훅 (필수)
DISCORD_WEBHOOK_URL = (os.getenv("DISCORD_WEBHOOK_URL") or "").strip()
if not DISCORD_WEBHOOK_URL:
    raise ValueError("환경변수 DISCORD_WEBHOOK_URL이 설정되지 않았습니다.")

RESEND_NOTE = "\n🔁 전송 재시도 (이전 전송 실패분 — 중복 수신 시 주문 중복 주의)"
RATE_LIMIT_MAX_WAIT = 30.0
# ==============================================


# ==================== 전송 ====================
def _retry_after(response: requests.Response) -> float:
    try:
        return min(float(response.json().get("retry_after", 1.0)), RATE_LIMIT_MAX_WAIT)
    except Exception:
        return 1.0


def send_discord_message(content: str) -> bool:
    """디스코드 웹훅으로 메시지 전송. 성공 여부를 반환한다."""
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
    """월간 수신 경로 점검용 핑 메시지."""
    kst = datetime.now(KST)
    ok = send_discord_message("\n".join([
        f"[{SIG}/{SELL_SIG} 알림 웹훅 PING] {kst:%Y-%m-%d} (KST {kst:%H:%M})",
        "수신 경로 점검용 월간 핑입니다 — 이 메시지가 보이면 알림 웹훅이 살아 있습니다.",
        f"이 핑이 안 오면 웹훅이 무효화된 것이고, {SIG} 알림도 함께 오지 않습니다.",
    ]))
    if not ok:
        raise SystemExit(1)


def send_missing_notice(ticker: str, reason: str, known: str, retries: int = 0) -> bool:
    """판정 불가 시 안내 메시지."""
    tail = f"재시도 {retries}회 후에도 미해결" if retries else "재시도 없음"
    return send_discord_message("\n".join([
        f"[{ticker} {SIG}/{SELL_SIG} 알림 생략 — 판정 불가]",
        f"사유: {reason}",
        f"마지막 확정 세션: {known}",
        f"🎯 다음 세션 목표가: 계산 보류 — 직전 목표가 재사용 금지 ({tail})",
    ]))


# ==================== 판정 ====================
def floor_cent(x: float) -> float:
    """매수 LOC 가격 — 센트 단위 내림."""
    return float(Decimal(repr(x)).quantize(Decimal("0.01"), rounding=ROUND_DOWN))


def round_cent(x: float) -> float:
    """매도 지정가 — 센트 단위 반올림."""
    return float(Decimal(repr(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def frequency_note(ticker: str, close: pd.Series) -> str:
    """매수 발동 빈도 통계 한 줄."""
    try:
        s = frequency_stats(ticker, WINDOW, MULT, close=close)
    except Exception as e:
        print(f"⚠️ [{ticker}] 발동 빈도 계산 실패(알림은 계속): {type(e).__name__}: {e}")
        return ""
    return (f"📊 예상 발동(매수): 연 {s['median']:g}회(장기 중앙값) · 최근 1년 실제 {s['recent']}회 "
            f"→ 연간 예산 ÷ {s['median']:g}")


def evaluate(ticker: str) -> tuple[str | None, str, str]:
    """종목 1회 판정 → (알림 메시지, 판정 불가 사유, 마지막 확정 세션)."""
    print(f"\n📡 [{ticker}] 데이터 불러오는 중...")
    try:
        df = download_daily(ticker, datetime.now() - timedelta(days=STATS_DAYS))
    except RuntimeError as e:
        print(f"❌ {e}")
        return (None, str(e), "없음")

    raw_close = df["Close"]
    close = raw_close.dropna()
    session = iso(df.index[-1])
    known = f"${float(close.iloc[-1]):.2f} (세션 {iso(close.index[-1])})" if len(close) else "없음"
    frame = signal_frame(close, WINDOW, MULT)
    expected = expected_session()

    # 판정 불가 가드
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
    prev_close = float(frame["close"].iloc[-2])
    latest_close = float(last["close"])
    latest_return = float(last["ret"])

    # 매수 (MULT σ + LOC)
    fill_threshold = float(frame["sigma"].iloc[-2]) * MULT
    next_threshold_buy = float(last["sigma"]) * MULT
    is_buy_signal = bool(last["fill_hit"])
    target_buy = floor_cent(latest_close * (1.0 - next_threshold_buy))

    # 매도 (2σ + Limit)
    next_threshold_sell = float(last["sigma"]) * SELL_MULT
    target_sell = round_cent(latest_close * (1.0 + next_threshold_sell))

    # 콘솔 출력
    print("=" * 55)
    print(f"체크 시간      : {datetime.now(KST):%Y-%m-%d %H:%M:%S} KST")
    print(f"종목           : {ticker}")
    print(f"직전 세션 종가 : ${prev_close:.2f}")
    print(f"기준 세션 종가 : ${latest_close:.2f}")
    print(f"기준 세션 수익률: {latest_return * 100:.2f}%")
    print(f"매수 신호 임계({SIG}): −{fill_threshold * 100:.2f}%")
    print(f"다음 매수 {SIG}  : {next_threshold_buy * 100:.2f}% → LOC ${target_buy:.2f}")
    print(f"다음 매도 {SELL_SIG}: {next_threshold_sell * 100:.2f}% → Limit ${target_sell:.2f}")
    print(f"매수 신호      : {'✅ 발생!' if is_buy_signal else '❌ 없음'}")
    print("=" * 55)

    # 디스코드 메시지
    message = "\n".join(filter(None, [
        f"[{ticker} {SIG} 매수 신호 발생]" if is_buy_signal else f"[{ticker} 장마감 및 목표가 안내]",
        f"기준 세션: {session} 마감",
        f"전일 종가: ${latest_close:.2f} ({latest_return * 100:+.2f}%)",
        f"매수 {SIG} 임계: −{fill_threshold * 100:.2f}% (전일까지)",
        f"🎯 다음 세션 매수 LOC ({SIG}): ${target_buy:.2f}",
        f"🎯 다음 세션 매도 Limit ({SELL_SIG}): ${target_sell:.2f}",
        frequency_note(ticker, close),
        "상태: 매수 조건 충족!" if is_buy_signal else "상태: 일반 장세 (참고용)",
    ]))
    return (message, "", "")


# ==================== 실행 ====================
@dataclass
class Job:
    ticker: str
    message: str | None = None
    reason: str = ""
    known: str = ""


def check_signals(retry_wait: float = 0.0, retry_attempts: int = 0) -> None:
    """모든 종목을 판정·전송하고, 결측/전송 실패 시 재시도한다."""
    wait_label = f"{retry_wait / 60:.0f}분" if retry_wait >= 60 else f"{retry_wait:g}초"
    queue = [Job(t) for t in TICKERS]
    undelivered: list[str] = []

    for attempt in range(retry_attempts + 1):
        is_last = attempt == retry_attempts
        retry_queue: list[Job] = []
        for job in queue:
            message = job.message
            is_resend = message is not None
            if message is None:
                message, job.reason, job.known = evaluate(job.ticker)
                job.message = message
                if message is None:
                    if not is_last:
                        print(f"⏳ [{job.ticker}] {job.reason} — {wait_label} 뒤 재시도 "
                              f"({attempt + 1}/{retry_attempts})")
                        retry_queue.append(job)
                    elif not send_missing_notice(job.ticker, job.reason, job.known, attempt):
                        undelivered.append(job.ticker)
                    continue
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
        raise SystemExit(f"❌ 전송 실패: {', '.join(undelivered)} — 웹훅이 무효화됐을 수 있습니다")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=f"SOXL/TQQQ 롤링 {SIG}/{SELL_SIG} 목표가 알림")
    parser.add_argument("--retry-wait", type=float, default=0.0,
                        help="결측·전송 실패 시 재시도 간격(초)")
    parser.add_argument("--retry-attempts", type=int, default=0,
                        help="재시도 횟수")
    parser.add_argument("--ping", action="store_true",
                        help="수신 경로 점검용 핑 메시지만 전송")
    parser.add_argument("--skip-non-trading-day", action="store_true",
                        help="미국 증시 휴장일이면 알림 없이 종료")
    args = parser.parse_args()

    if args.ping:
        send_ping()
    elif args.skip_non_trading_day and is_trading_day_today() is False:
        print("ℹ️ 오늘은 미국 증시 휴장일 — 알림 없이 종료")
    else:
        check_signals(args.retry_wait, args.retry_attempts)