#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dollar_alerter.py — 달러(USD/KRW) '매직 스플릿' 환테크 알리미
====================================================================

박성현 『매직 스플릿』의 달러 매매 아이디어를 swing_alerter.py 와 같은
알림 앱 구조로 재구현한 버전. 데이터는 yfinance `USDKRW=X` (일봉/실시간)를
사용하며, 판정 로직은 dollar_split_backtest.py 로 검증된 규칙을 따른다.

전략 (백테스트 검증 — 세븐 스플릿 정통, 2004~2026 CAGR +5.2% vs 바이앤홀드
+0.8% · MDD -17.2% vs -36.4% · 스프레드 왕복 0% = 나무 멤버스 100% 환전 우대
반영 — 95% 우대(왕복 0.1%) 기준은 CAGR +4.1%):
  - 매수 신호: 전일 종가 대비 -BUY_DROP_PCT%(기본 0.3) 이하 하락
    (밴드 BUY_BAND_PCT%(기본 0.5)보다 깊은 급락은 신호 제외 — 설정으로 해제 가능)
  - 익절 신호: 계좌별 매수가 대비 +SELL_TARGET_PCT%(기본 0.5) 도달
  - 임박 신호: 트리거/목표까지 IMMINENT_GAP_PCT%p(기본 0.1) 이내

판정 기준:
  - --monitor(장중)는 **실시간 가격**(yfinance, 15분 지연) 기준 — 나무증권
    달러 환전(주간 09:00~16:00 + 야간 16:00~익일 02:00 KST, 점검 23:50~24:30
    제외) 가능 시간대에만 신호 판정 (2026-08-17 조사 반영).
  - 브리핑/대시보드는 확정 종가(전일 종가) 기준 트리거 가격을 안내.

파일 구조 (swing_alerter.py 와 동일한 분리 원칙):
  - dollar_config.json    — 공용 설정 (사용자 소유 — 전략 파라미터/푸시 설정)
  - dollar_state.json     — 봇 전용 상태 (신호 발송 플래그 — 봇만 커밋)
  - dollar_personal.json  — 🔒 개인 매수 포지션 (사용자 소유, 봇은 읽기만)

사용법:
  python3 dollar_alerter.py                    # 상태 출력 + 대시보드 생성
  python3 dollar_alerter.py --discord          # + Discord 일일 브리핑 (09:00 KST)
  python3 dollar_alerter.py --monitor          # 장중 실시간 신호 감지 + 푸시 (cron-job.org)
  python3 dollar_alerter.py --serve            # 스마트폰 대시보드 서버 (기본 포트 8080)
  python3 dollar_alerter.py --reset USDKRW=X   # 신호 상태 수동 초기화
  python3 dollar_alerter.py --test-push        # OneSignal 테스트 푸시
"""
import argparse
import json
import os
import socket
import time
from datetime import datetime, time as dtime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

import yfinance as yf

from LOC_DCA_strategy import _send_discord, get_prev_close, resolve_discord_config
from swing_alerter import _resolve_onesignal, get_prior_close, send_onesignal_push

CONFIG_PATH = "dollar_config.json"
STATE_PATH = "dollar_state.json"           # 봇 전용 상태 (신호 발송 플래그) — 설정과 분리
PERSONAL_CONFIG_PATH = "dollar_personal.json"  # 🔒 개인 매수 포지션 — 봇은 읽기만
DASHBOARD_PATH = "dollar_dashboard.html"
PORTFOLIO_CONFIG_PATH = "portfolio_config.json"
KST_TZ = ZoneInfo("Asia/Seoul")
NY_TZ = ZoneInfo("America/New_York")

# dollar_state.json 에 보관하는 봇 전용 상태 키 (POSITIONS 내부) —
# dollar_config.json(사용자 설정)과 분리해 봇/사용자 커밋 충돌로 상태가 유실되지 않게 한다.
_STATE_KEYS = ("BUY_SIGNAL_SENT", "BUY_SIGNAL_DATE", "BUY_IMMINENT_SENT", "BUY_IMMINENT_DATE",
               "SELL_SIGNAL_SENT", "SELL_IMMINENT_SENT", "CYCLE_RESET_DONE")
_STATE_DEFAULTS = {
    # 매수 신호/임박은 '당일 한정' — 발송일(BUY_*_DATE)이 바뀌면 자동 리셋되어
    # 새 전일 종가 기준의 새 신호를 받을 수 있다 (2026-08-17)
    "BUY_SIGNAL_SENT": False,
    "BUY_SIGNAL_DATE": None,
    "BUY_IMMINENT_SENT": False,
    "BUY_IMMINENT_DATE": None,
    # 익절 신호/임박은 계좌별 사이클당 1회 — {계좌번호: true}
    # 전 계좌 익절 완료(사이클 완료) 시 auto_cycle_reset 이 리셋 (수동 --reset 도 가능)
    "SELL_SIGNAL_SENT": {},
    "SELL_IMMINENT_SENT": {},
    "CYCLE_RESET_DONE": False,
}

# ── 기본 설정 (dollar_config.json 에서 덮어쓸 수 있음) ──────────────
DEFAULT_CFG = {
    "ENABLED": True,
    "BUY_DROP_PCT": 0.3,             # 매수 트리거: 전일 종가 대비 하락 % (백테스트 검증값)
    "BUY_BAND_PCT": 0.5,             # 매수 밴드 상한 % (0 = 무제한 — 급락도 신호)
    "SELL_TARGET_PCT": 0.5,          # 익절 목표: 매수가 대비 상승 % (백테스트 검증값)
    "IMMINENT_GAP_PCT": 0.1,         # 임박 알림 %p (트리거/목표까지)
    "PAGES_URL": "",                 # GitHub Pages 주소 — 설정 시 대시보드에 라이브 링크 표시
    "ONESIGNAL_APP_ID": "",          # OneSignal 웹 푸시 앱 ID (공개값)
    "POSITIONS": {},
}

# ═══════════════════════════════════════════════════════════
# 설정 로드/저장
# ═══════════════════════════════════════════════════════════

def _write_json(path: str, data: dict) -> None:
    """원자적 저장 — 임시 파일 후 rename (깨진 파일 방지)."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
    os.replace(tmp, path)


def _load_state() -> dict:
    """봇 전용 상태(dollar_state.json) 로드 — 없거나 깨졌으면 빈 상태."""
    if not os.path.isfile(STATE_PATH):
        return {"POSITIONS": {}}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {"POSITIONS": {}}


def _load_personal_positions() -> dict:
    """🔒 개인 매수 포지션(dollar_personal.json) 로드 — 없거나 깨졌으면 빈 dict.

    BUY_PRICE(매수 환율)/SHARES(보유 달러 수량)는 사용자 개인 정보라 공용 설정에
    두지 않고 별도 파일에서 관리한다. 봇은 이 파일을 절대 쓰지 않는다.
    """
    if not os.path.isfile(PERSONAL_CONFIG_PATH):
        return {}
    try:
        with open(PERSONAL_CONFIG_PATH, "r", encoding="utf-8") as f:
            return (json.load(f) or {}).get("POSITIONS", {}) or {}
    except Exception:  # noqa: BLE001
        return {}


def _normalize_lots(pp: dict) -> list[dict]:
    """개인 파일의 LOTS 또는 구형 단일 BUY_PRICE 를 정규화된 로트 리스트로 변환.

    반환: [{account, buy_price, shares}, ...]  (입력된 계좌만)
    """
    lots = pp.get("LOTS")
    if isinstance(lots, list) and lots:
        out = []
        for i, lot in enumerate(lots, 1):
            bp = lot.get("BUY_PRICE")
            sh = lot.get("SHARES")
            if bp is None and sh is None:
                continue  # 미입력 계좌 — 건너뜀
            out.append({
                "account": int(lot.get("ACCOUNT") or i),
                "buy_price": float(bp) if bp is not None else None,
                "shares": float(sh) if sh is not None else 0.0,
            })
        return out
    if pp.get("BUY_PRICE") is not None:
        # 구형 단일 키 → 1번 계좌 로트로 승격 (하위 호환)
        return [{"account": 1, "buy_price": float(pp["BUY_PRICE"]),
                 "shares": float(pp.get("SHARES") or 0)}]
    return []


def _ensure_state(pos: dict) -> dict:
    """포지션의 봇 전용 상태(_STATE) 기본값 보장."""
    st = pos.setdefault("_STATE", {})
    for k, v in _STATE_DEFAULTS.items():
        if k in ("SELL_SIGNAL_SENT", "SELL_IMMINENT_SENT"):
            st.setdefault(k, {})
        else:
            st.setdefault(k, v)
    return st


def load_config() -> dict:
    """dollar_config.json(설정) + dollar_state.json(상태) + dollar_personal.json(개인) 병합.

    설정은 사용자, 상태는 봇이 각자 소유하는 두 파일 구조 — 봇이 설정 파일을
    쓰지 않으므로 git 충돌로 신호 상태가 유실되지 않는다.
    """
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:  # noqa: BLE001
        cfg = {}
    merged = {**DEFAULT_CFG, **cfg}
    merged["POSITIONS"] = {**DEFAULT_CFG["POSITIONS"], **cfg.get("POSITIONS", {})}
    state = _load_state()
    personal = _load_personal_positions()
    for ticker, pos in merged["POSITIONS"].items():
        _ensure_state(pos)
        # 봇 상태 주입 (dollar_state.json) — _STATE 키로 격리해 설정과 섞이지 않게
        saved = state.get("POSITIONS", {}).get(ticker, {})
        pos["_STATE"].update({k: v for k, v in saved.items() if k in _STATE_KEYS})
        # 개인 LOTS 주입 (dollar_personal.json) — 계산용 참조 (봇은 저장 안 함)
        lots = _normalize_lots(personal.get(ticker, {}) or {})
        if lots:
            pos["LOTS"] = lots
    return merged


def save_config(cfg: dict) -> None:
    """봇 전용 상태(_STATE)만 dollar_state.json 에 영속화.

    설정(사용자 소유)과 개인 포지션(사용자 소유)은 절대 쓰지 않는다.
    """
    state = {"POSITIONS": {}}
    for ticker, pos in cfg.get("POSITIONS", {}).items():
        st = pos.get("_STATE") or {}
        state["POSITIONS"][ticker] = {k: st.get(k) for k in _STATE_KEYS}
    _write_json(STATE_PATH, state)


def _resolve_discord(cfg: dict) -> tuple[str, str]:
    """Discord 웹훅/유저 ID — env 우선, dollar_config → portfolio_config 폴백."""
    try:
        with open(PORTFOLIO_CONFIG_PATH, "r", encoding="utf-8") as f:
            portfolio = json.load(f)
    except Exception:  # noqa: BLE001
        portfolio = {}
    merged = {**portfolio, **cfg}
    return resolve_discord_config(merged)


# ═══════════════════════════════════════════════════════════
# 가격 조회 / 판정
# ═══════════════════════════════════════════════════════════

def _bank_hours_open(now: datetime | None = None) -> bool:
    """나무증권 달러 환전 가능 시간 여부 — 신호 판정 게이트 (2026-08-17 조사 반영).

    - 주간환전 09:00~16:00 + 야간환전 16:00~익일 02:00 (2024-07 외환시장
      마감 연장 반영) → 평일 09:00 ~ 익일 02:00 KST
    - 일일 점검 23:50~24:30(익일 00:30) 제외
    - 금요일 야간 세션은 토요일 02:00까지, 토 02:00 이후·일요일은 휴무
    공휴일은 별도 판정하지 않는다 (swing_alerter 와 동일한 단순화).
    """
    now = now or datetime.now(KST_TZ)
    wd = now.weekday()  # 0=월 .. 6=일
    m = now.hour * 60 + now.minute
    if wd == 6:                    # 일요일 — 휴무
        return False
    if wd == 0 and m < 540:        # 월요일 새벽(00:00~08:59) — 일요일 무세션
        return False
    if wd == 5 and m >= 120:       # 토요일 02:00 이후 — 휴무
        return False
    # 주간 09:00(540)~23:50(1430) / 전일 야간 꼬리 00:30(30)~02:00(120)
    if m >= 540:
        return m < 1430
    return 30 <= m < 120


def get_live_price(ticker: str) -> float | None:
    """실시간 가격 (yfinance, 15분 지연) — fast_info 우선, 1분봉 폴백.

    환테크는 장중에만 체결하므로 매수/익절 신호 판정은 이 값을 사용한다.
    """
    try:
        p = yf.Ticker(ticker).fast_info.last_price
        if p is not None and float(p) > 0:
            return float(p)
    except Exception:  # noqa: BLE001
        pass
    try:
        h = yf.Ticker(ticker).history(period="1d", interval="1m")
        if not h.empty:
            return float(h["Close"].iloc[-1])
    except Exception:  # noqa: BLE001
        pass
    return None


def compute_ticker(ticker: str, pos: dict, cfg: dict, live: bool = False) -> dict:
    """티커 1개의 현재 상태 계산 (종가/매수 트리거/계좌별 익절 목표).

    live=True 면 은행 영업시간 중 실시간 가격으로 신호 판정 기준을 교체한다.
    브리핑/대시보드는 종가 기준 트리거 가격 안내 + (장중이면) 실시간 표시.
    """
    st: dict = {"ticker": ticker, "error": None, "live": False, "close_price": None}

    price, as_of = get_prev_close(ticker)   # 마지막 완료 세션 종가 = 전일 종가 기준
    if price is None:
        st["error"] = f"{ticker} 가격 조회 실패"
        return st
    st["close_price"] = price

    # 전전 세션 종가 — 전일 대비 등락률 표시용 (swing 과 동일 패턴)
    prior_close, _ = get_prior_close(ticker, as_of)
    day_change_pct = None
    if prior_close and prior_close > 0:
        day_change_pct = (price - prior_close) / prior_close * 100.0

    buy_drop = float(cfg.get("BUY_DROP_PCT", 0.3)) / 100.0
    buy_band = float(cfg.get("BUY_BAND_PCT", 0.5)) / 100.0
    sell_target_pct = float(cfg.get("SELL_TARGET_PCT", 0.5)) / 100.0
    gap_pct = float(cfg.get("IMMINENT_GAP_PCT", 0.1))

    buy_trigger = price * (1 - buy_drop)
    buy_band_lo = price * (1 - buy_band) if buy_band > 0 else None

    live_price = None
    if live and _bank_hours_open():
        live_price = get_live_price(ticker)
        if live_price is not None and live_price > 0:
            st["live"] = True

    # 신호 판정 기준가 — 장중=실시간, 장외=확정 종가 (장외면 '어제 기준'으로 hit 판정됨)
    # st["live"]는 live_price가 유효할 때만 True이므로 is not None 가드로 타입을 좁힌다
    ref = live_price if (st["live"] and live_price is not None) else price

    hit_buy = bool(ref <= buy_trigger + 1e-9
                   and (buy_band_lo is None or ref >= buy_band_lo - 1e-9))
    buy_gap = None if hit_buy else max((ref - buy_trigger) / price * 100.0, 0.0)
    buy_imminent = bool(not hit_buy and buy_gap is not None and buy_gap <= gap_pct)

    # 계좌별 익절 목표 — 개인 포지션 LOTS 기준 (매수가 × (1 + SELL_TARGET_PCT))
    lot_stats = []
    for lot in (pos.get("LOTS") or []):
        bp = lot.get("buy_price")
        sh = float(lot.get("shares") or 0)
        ls = {"account": lot.get("account"), "buy_price": None, "shares": sh,
              "sell_target": None, "sell_ready": False, "sell_gap_pct": None,
              "exp_profit_krw": None}
        if bp:
            bp = float(bp)
            s_target = bp * (1 + sell_target_pct)
            s_ready = bool(ref >= s_target - 1e-9)
            s_gap = None if s_ready else max((s_target - ref) / s_target * 100.0, 0.0)
            ls.update(buy_price=bp, sell_target=s_target, sell_ready=s_ready,
                      sell_gap_pct=s_gap,
                      exp_profit_krw=(s_target - bp) * sh if sh > 0 else None)
        lot_stats.append(ls)

    st.update({
        "label": pos.get("LABEL") or ticker,
        "price": price,
        "as_of": as_of,
        "prior_close": prior_close,
        "day_change_pct": day_change_pct,
        "buy_trigger": buy_trigger,
        "buy_band_lo": buy_band_lo,
        "hit_buy": hit_buy,
        "buy_imminent": buy_imminent,
        "buy_gap_pct": buy_gap,
        "live_price": live_price,
        "sell_target_pct": sell_target_pct * 100.0,
        "lots": lot_stats,
        "personal": bool(pos.get("_PERSONAL")),
    })
    return st


def _compute_all(cfg: dict, force: bool = False, live: bool = False) -> list[dict]:
    """전 티커 상태 계산 (60초 캐시 — 서버 폴링 부하 방지)."""
    if not force and _LAST_COMPUTE["statuses"] is not None \
            and _LAST_COMPUTE["live"] == live and time.time() - _LAST_COMPUTE["ts"] < 60:
        return _LAST_COMPUTE["statuses"]
    statuses = []
    for ticker, pos in cfg.get("POSITIONS", {}).items():
        if not pos.get("ENABLED", True):
            continue
        statuses.append(compute_ticker(ticker, pos, cfg, live=live))
    _LAST_COMPUTE.update(ts=time.time(), statuses=statuses, live=live)
    return statuses


# ═══════════════════════════════════════════════════════════
# 알림 감지 (--monitor) — 상태를 변경하며 1회성 알림 메시지 생성
# ═══════════════════════════════════════════════════════════

def detect_alerts(st: dict, pos: dict, cfg: dict) -> tuple[list[str], list[str], list[str]]:
    """신규 매수/임박/익절 알림 감지 — 상태 갱신, 재폴링 시 중복 없음.

    반환: (전체 메시지, 매수 푸시 전용, 익절 푸시 전용)
    - 매수/임박: 전일 종가(공개 정보) 기준 → 앱 구독자 전원(= 내 기기) 푸시 가능
    - 익절/임박: 개인 매수가 기반이지만 단독 사용 전제(전체 구독자 = 내 기기)로 발송
    """
    msgs: list[str] = []
    buy_msgs: list[str] = []
    sell_msgs: list[str] = []
    if st.get("error"):
        return msgs, buy_msgs, sell_msgs
    state = _ensure_state(pos)
    today = datetime.now(KST_TZ).strftime("%Y-%m-%d")
    ticker = st["ticker"]

    # 당일 한정 리셋 — 날짜가 바뀌면 매수 신호/임박 재무장 (전일 종가 기준이 새로 계산됨)
    if state.get("BUY_SIGNAL_DATE") != today:
        state["BUY_SIGNAL_SENT"] = False
        state["BUY_SIGNAL_DATE"] = today
    if state.get("BUY_IMMINENT_DATE") != today:
        state["BUY_IMMINENT_SENT"] = False
        state["BUY_IMMINENT_DATE"] = today

    gap = float(cfg.get("IMMINENT_GAP_PCT", 0.1))
    cur = st.get("live_price") or st["price"]
    drop = float(cfg.get("BUY_DROP_PCT", 0.3))
    tgt = st["sell_target_pct"]

    # 1) 매수 신호 — 트리거 이하 하락 (밴드 적용 시 급락 제외)
    if st["hit_buy"] and not state["BUY_SIGNAL_SENT"]:
        state["BUY_SIGNAL_SENT"] = True
        band_txt = f" (밴드 -{float(cfg.get('BUY_BAND_PCT', 0.5)):g}% 초과 급락 제외)" if st["buy_band_lo"] else ""
        msg = (f"🔻 **{ticker} 매수 신호**\n"
               f"현재가 {cur:,.2f}원 ≤ 트리거 {st['buy_trigger']:,.2f}원 "
               f"(전일 종가 {st['price']:,.2f}원 대비 -{drop:g}%){band_txt}\n"
               f"▶ 환전 → "
               f"매수가 대비 +{tgt:g}% 에서 익절")
        msgs.append(msg)
        buy_msgs.append(msg)

    # 2) 매수 임박 — 트리거까지 IMMINENT_GAP_PCT%p 이내
    if st["buy_imminent"] and not state["BUY_IMMINENT_SENT"]:
        state["BUY_IMMINENT_SENT"] = True
        msg = (f"📡 **{ticker} 매수 트리거 임박**\n"
               f"현재가 {cur:,.2f}원 | 트리거 {st['buy_trigger']:,.2f}원 "
               f"(남은 {st['buy_gap_pct']:.2f}%p)")
        msgs.append(msg)
        buy_msgs.append(msg)

    # 3) 익절 임박/신호 — 계좌별 사이클당 1회
    sell_sent = state["SELL_SIGNAL_SENT"]
    im_sent = state["SELL_IMMINENT_SENT"]
    for lot in st["lots"]:
        if lot["sell_target"] is None:
            continue
        acc = str(lot["account"])
        if lot["sell_ready"] and not sell_sent.get(acc):
            sell_sent[acc] = True
            msg = (f"🚨 **{ticker} 익절 신호 — {lot['account']}번 계좌**\n"
                   f"현재가 {cur:,.2f}원 ≥ 목표 {lot['sell_target']:,.2f}원 "
                   f"(매수가 {lot['buy_price']:,.2f}원, +{tgt:g}%)\n"
                   f"▶ 매도 후 dollar_personal.json 의 LOTS 정리")
            msgs.append(msg)
            sell_msgs.append(msg)
        elif (not lot["sell_ready"] and lot["sell_gap_pct"] is not None
              and lot["sell_gap_pct"] <= gap and not im_sent.get(acc)):
            im_sent[acc] = True
            msg = (f"🚀 **{ticker} 익절 목표 임박 — {lot['account']}번 계좌**\n"
                   f"현재가 {cur:,.2f}원 | 목표 {lot['sell_target']:,.2f}원 "
                   f"(남은 {lot['sell_gap_pct']:.2f}%p)")
            msgs.append(msg)
            sell_msgs.append(msg)
    return msgs, buy_msgs, sell_msgs


def auto_cycle_reset(st: dict, pos: dict) -> tuple[str | None, bool]:
    """전 계좌 익절 목표 도달(사이클 완료) → 익절 알림 상태 자동 리셋.

    swing_alerter 의 auto_cycle_reset 과 동일 패턴 — CYCLE_RESET_DONE 플래그로
    중복 방지, 익절 미도달 상태가 되면 자동 재무장. 수동 --reset 도 가능.
    """
    if st.get("error"):
        return None, False
    state = _ensure_state(pos)
    lots = [l for l in st["lots"] if l["sell_target"] is not None]
    if not lots:
        return None, False
    all_sold = all(l["sell_ready"] for l in lots)
    if all_sold:
        if not state.get("CYCLE_RESET_DONE"):
            state["SELL_SIGNAL_SENT"] = {}
            state["SELL_IMMINENT_SENT"] = {}
            state["CYCLE_RESET_DONE"] = True
            return (f"🔄 **{st['ticker']} 사이클 완료 — 전 계좌 익절 목표 도달**\n"
                    f"익절 알림 상태 자동 리셋 완료. 다음 매수 신호부터 새 사이클 시작."), True
    else:
        if state.get("CYCLE_RESET_DONE"):
            state["CYCLE_RESET_DONE"] = False
            return f"🔄 **{st['ticker']} 새 사이클 재무장** — 익절 미도달 상태로 복귀", True
    return None, False


# ═══════════════════════════════════════════════════════════
# 메시지 빌더 (콘솔 / Discord)
# ═══════════════════════════════════════════════════════════

def _sell_chip(lot: dict, gap_pct: float = 0.1) -> str:
    """계좌 1개의 익절 상태 칩."""
    if lot.get("sell_target") is None:
        return "매도 미설정"
    if lot["sell_ready"]:
        return "🚨 익절 도달"
    if lot.get("sell_gap_pct") is not None and lot["sell_gap_pct"] <= gap_pct:
        return "🚀 임박"
    return "⏳ 대기"


def build_briefing_text(statuses: list[dict], cfg: dict) -> str:
    """일일 종합 브리핑 (Discord 설명란용) — 실행 액션(▶) 라인 포함."""
    lines = []
    for st in statuses:
        if st.get("error"):
            lines.append(f"**{st['ticker']}** ❌ {st['error']}")
            continue
        drop = float(cfg.get("BUY_DROP_PCT", 0.3))
        tgt = st["sell_target_pct"]
        band_txt = f"~-{float(cfg.get('BUY_BAND_PCT', 0.5)):g}%" if st["buy_band_lo"] else "이하"
        lines.append(f"**{st['label']}** · 전일 종가 {st['price']:,.2f}원"
                     + (f" (전일 대비 {st['day_change_pct']:+.2f}%)" if st["day_change_pct"] is not None else ""))
        lines.append(f"- 매수 트리거: **{st['buy_trigger']:,.2f}원** 이하 (전일 종가 대비 -{drop:g}%{band_txt})")
        if st.get("live"):
            live = st["live_price"]
            status_txt = "🔻 매수 신호" if st["hit_buy"] else ("📡 임박" if st["buy_imminent"] else "대기")
            lines.append(f"- 현재가(실시간): {live:,.2f}원 → {status_txt}")
        open_lots = [l for l in st["lots"] if l["sell_target"] is not None]
        if open_lots:
            lines.append(f"- 보유 계좌 {len(open_lots)}개:")
            for l in open_lots:
                lines.append(f"  · {l['account']}번 — 매수 {l['buy_price']:,.2f}원 → "
                             f"익절 목표 {l['sell_target']:,.2f}원 (+{tgt:g}%) {_sell_chip(l)}")
        lines.append(f"- ▶ 실행: 오늘 {st['buy_trigger']:,.2f}원 이하로 내려가면 환전 → "
                     f"매수가 대비 +{tgt:g}% 에서 익절")
        lines.append("")
    return "\n".join(lines).strip()


def print_console(statuses: list[dict], cfg: dict) -> None:
    """터미널 상태 출력 (기본 실행)."""
    for st in statuses:
        if st.get("error"):
            print(f"❌ {st['ticker']}: {st['error']}")
            continue
        cur = st.get("live_price") or st["price"]
        src = "실시간" if st.get("live") else "종가"
        print(f"\n{st['label']} — 전일 종가 {st['price']:,.2f}원 ({src} {cur:,.2f}원)")
        print(f"  매수 트리거: {st['buy_trigger']:,.2f}원 이하 (-{float(cfg.get('BUY_DROP_PCT', 0.3)):g}%)"
              + (f" | 밴드 -{float(cfg.get('BUY_BAND_PCT', 0.5)):g}% 초과 제외" if st["buy_band_lo"] else "")
              + ("  🔻 신호!" if st["hit_buy"] else ("  📡 임박" if st["buy_imminent"] else "  ⏳ 대기")))
        for l in st["lots"]:
            if l["sell_target"] is None:
                continue
            print(f"  {l['account']}번 계좌: 매수 {l['buy_price']:,.2f}원 → 익절 {l['sell_target']:,.2f}원 "
                  f"(+{st['sell_target_pct']:g}%) {_sell_chip(l)}"
                  + (f" · 예상수익 {l['exp_profit_krw']:,.0f}원" if l.get("exp_profit_krw") else ""))
    print()


# ═══════════════════════════════════════════════════════════
# 모바일 대시보드 HTML (JS 없음 — meta refresh 자동 갱신)
# ═══════════════════════════════════════════════════════════

_CSS = """
:root{--bg:#0b0e14;--card:#141a26;--border:#20293b;--text:#e6edf3;--muted:#8b949e;
--green:#3fb950;--green-dim:#12281c;--red:#f85149;--red-dim:#2d1517;
--amber:#d29922;--amber-dim:#2a2112;--blue:#58a6ff;--blue-dim:#12253d}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
html,body{background:var(--bg);color:var(--text);
font-family:-apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Noto Sans KR","Malgun Gothic",sans-serif}
body{max-width:640px;margin:0 auto;padding:0 14px 40px;font-size:16px}
header{position:sticky;top:0;z-index:5;background:rgba(11,14,20,.92);backdrop-filter:blur(8px);
padding:16px 0 12px;border-bottom:1px solid var(--border);margin-bottom:14px}
header h1{font-size:21px;letter-spacing:-.3px}
header .sub{color:var(--muted);font-size:13px;margin-top:4px}
.card{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:16px;margin-bottom:16px}
.row{display:flex;align-items:center;justify-content:space-between;gap:8px}
.tick{font-size:19px;font-weight:800}
.chip{font-size:12px;padding:5px 10px;border-radius:999px;font-weight:700}
.chip.gray{background:#1c2533;color:var(--muted)}
.chip.green{background:var(--green-dim);color:var(--green)}
.chip.red{background:var(--red-dim);color:var(--red)}
.chip.amber{background:var(--amber-dim);color:var(--amber)}
.price{font-size:38px;font-weight:800;margin:10px 0 2px}
.price .unit{font-size:15px;color:var(--muted);font-weight:400;margin-left:4px}
.meta{color:var(--muted);font-size:13px}
.info{background:#0f1420;border:1px solid var(--border);border-radius:12px;
padding:10px 12px;margin-top:12px;font-size:15px;line-height:1.7}
.info b{color:var(--text)}
.lots-title{margin-top:14px;margin-bottom:4px;font-size:16px;font-weight:700}
.lot{display:flex;align-items:center;justify-content:space-between;gap:8px;
padding:8px 0;border-bottom:1px solid #1a2231;font-size:14px}
.lot:last-child{border-bottom:none}
.lot .acc{font-weight:700;color:var(--muted)}
.lot .val{color:var(--muted);font-size:12px}
footer{color:var(--muted);font-size:12px;text-align:center;margin-top:8px}
"""


def _buy_chip_html(st: dict) -> str:
    if st["hit_buy"]:
        return "<span class='chip green'>🔻 매수 신호</span>"
    if st["buy_imminent"]:
        return "<span class='chip amber'>📡 임박</span>"
    return "<span class='chip gray'>⏳ 대기</span>"


def render_dashboard(statuses: list[dict], cfg: dict, updated_at: str) -> str:
    """모바일 대시보드 HTML — 달러 카드 + 보유 계좌 익절 목표."""
    cards = []
    for st in statuses:
        if st.get("error"):
            cards.append(f"<div class='card'><div class='tick'>{st['ticker']}</div>"
                         f"<div class='info'>❌ {st['error']}</div></div>")
            continue
        cur = st.get("live_price") or st["price"]
        src = f"실시간 {st['as_of']}" if st.get("live") else f"종가 {st['as_of']}"
        band_txt = f"~-{float(cfg.get('BUY_BAND_PCT', 0.5)):g}%" if st["buy_band_lo"] else "이하"
        lot_rows = []
        for l in st["lots"]:
            if l["sell_target"] is None:
                continue
            chip_cls = {"🚨 익절 도달": "red", "🚀 임박": "amber", "⏳ 대기": "gray"}.get(_sell_chip(l), "gray")
            lot_rows.append(
                f"<div class='lot'><span class='acc'>{l['account']}번</span>"
                f"<span class='val'>매수 {l['buy_price']:,.2f}원 → 익절 {l['sell_target']:,.2f}원 "
                f"(+{st['sell_target_pct']:g}%)</span>"
                f"<span class='chip {chip_cls}'>{_sell_chip(l)}</span></div>")
        lots_block = ""
        if lot_rows:
            lots_block = (f"<div class='lots-title'>보유 계좌 ({len(lot_rows)}개)</div>"
                          + "".join(lot_rows))
        cards.append(f"""
        <div class="card">
          <div class="row">
            <span class="tick">{st['label']}</span>
            {_buy_chip_html(st)}
          </div>
          <div class="price">{cur:,.2f}<span class="unit">원</span></div>
          <div class="meta">{src} · 전일 종가 {st['price']:,.2f}원
            {f'· 전일 대비 {st['day_change_pct']:+.2f}%' if st['day_change_pct'] is not None else ''}</div>
          <div class="info">매수 트리거 <b>{st['buy_trigger']:,.2f}원</b> 이하
            (전일 종가 대비 -{float(cfg.get('BUY_DROP_PCT', 0.3)):g}%{band_txt})<br>
            익절 목표 <b>매수가 대비 +{st['sell_target_pct']:g}%</b></div>
          {lots_block}
        </div>""")
    pages = (cfg.get("PAGES_URL") or "").strip()
    pages_link = f" · <a href='{pages}' style='color:var(--blue)'>GitHub Pages</a>" if pages else ""
    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<meta http-equiv="refresh" content="300">
<title>달러 매직 스플릿</title>
<style>{_CSS}</style>
</head>
<body>
<header>
  <h1>💵 달러 매직 스플릿</h1>
  <div class="sub">USD/KRW 환테크 알리미 · {updated_at}{pages_link}</div>
</header>
{''.join(cards)}
<footer>신호는 알림일 뿐 — 실제 체결은 은행 앱에서 수동으로 하세요.</footer>
</body>
</html>
"""


def write_dashboard(statuses: list[dict], cfg: dict, path: str) -> None:
    """대시보드 HTML 파일 저장 (gh-pages 배포용)."""
    html = render_dashboard(statuses, cfg, datetime.now(KST_TZ).strftime("%Y-%m-%d %H:%M"))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html)
    os.replace(tmp, path)


class _DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 — 요청 로그 최소화
        pass

    def _send(self, body: bytes, ctype: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        cfg = load_config()
        statuses = _compute_all(cfg, live=True)
        if self.path.startswith("/api/status"):
            self._send(json.dumps(
                {"updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 "positions": statuses}, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8")
            return
        self._send(render_dashboard(
            statuses, cfg,
            updated_at=datetime.now(KST_TZ).strftime("%Y-%m-%d %H:%M"),
        ).encode("utf-8"), "text/html; charset=utf-8")


def run_serve(port: int) -> None:
    server = ThreadingHTTPServer(("0.0.0.0", port), _DashboardHandler)
    print(f"🖥  달러 알리미 대시보드 서버 시작 (포트 {port})")
    print("   스마트폰에서 같은 Wi-Fi로 접속:")
    try:
        ips: set[str] = set()
        try:
            hostname = socket.gethostname()
            for ip in socket.gethostbyname_ex(hostname)[2]:
                ips.add(ip)
        except Exception:  # noqa: BLE001
            pass
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ips.add(s.getsockname()[0])
            s.close()
        except Exception:  # noqa: BLE001
            pass
        for ip in sorted(ips):
            print(f"   http://{ip}:{port}")
    except Exception:  # noqa: BLE001
        print(f"   http://localhost:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n🛑 서버 종료")


# ═══════════════════════════════════════════════════════════
# 리셋 / 푸시
# ═══════════════════════════════════════════════════════════

def reset_position(ticker: str) -> None:
    """티커 신호 상태 수동 초기화 (전 계좌 익절/매수 신호 재무장)."""
    state = _load_state()
    state.setdefault("POSITIONS", {}).pop(ticker, None)
    _write_json(STATE_PATH, state)
    print(f"✅ {ticker} 신호 상태 초기화 완료 (매수/익절 알림 재무장)")


def _push(cfg: dict, title: str, body: str) -> None:
    """OneSignal 푸시 — 전체 구독자(= 내 기기) 대상 (단독 사용 전제, swing 과 동일 정책)."""
    app_id, api_key = _resolve_onesignal(cfg)
    if not app_id or not api_key:
        print("⚠️ ONESIGNAL_APP_ID / ONESIGNAL_REST_API_KEY 미설정 — 푸시 생략")
        return
    code, resp = send_onesignal_push(app_id, api_key, title, body,
                                     url=(cfg.get("PAGES_URL") or "").strip() or None)
    print(f"   OneSignal: HTTP {code} — {resp[:120]}" if not str(code).startswith("2")
          else "   ✅ 푸시 발송 완료")


_LAST_COMPUTE = {"ts": 0.0, "statuses": None, "live": False}


def main() -> None:
    parser = argparse.ArgumentParser(description="달러 매직 스플릿 환테크 알리미 (매수/익절 신호 + 모바일 대시보드)")
    parser.add_argument("--discord", action="store_true", help="상태 출력 + Discord 일일 브리핑 발송")
    parser.add_argument("--monitor", action="store_true", help="실시간 모니터 — 신규 매수/익절/임박 신호만 푸시 발송")
    parser.add_argument("--serve", nargs="?", const=8080, type=int, metavar="PORT",
                        help="스마트폰용 대시보드 HTTP 서버 실행 (기본 포트 8080)")
    parser.add_argument("--reset", metavar="TICKER", help="티커 신호 상태 초기화 (기본 USDKRW=X)")
    parser.add_argument("--dashboard", default=DASHBOARD_PATH, help=f"대시보드 저장 경로 (기본 {DASHBOARD_PATH})")
    parser.add_argument("--test-push", action="store_true", help="OneSignal 테스트 푸시 발송")
    args = parser.parse_args()

    if args.reset:
        reset_position(args.reset)
        return
    if args.serve is not None:
        run_serve(args.serve)
        return
    if args.test_push:
        cfg = load_config()
        _push(cfg, "💵 달러 매직 스플릿 테스트", "푸시가 정상적으로 수신되고 있습니다.")
        return

    cfg = load_config()
    if not cfg.get("ENABLED", True):
        raise SystemExit("ℹ️  dollar_config.json 의 ENABLED=false — 종료합니다.")
    if not cfg.get("POSITIONS"):
        raise SystemExit("ℹ️  POSITIONS 이 비어 있습니다. dollar_config.json 에 티커를 추가하세요.")

    statuses = _compute_all(cfg, force=True, live=True)
    webhook, user_id = _resolve_discord(cfg)

    if args.monitor:
        # ── 실시간 모니터: 신규 신호만 감지 → 푸시 + Discord ──
        alerts: list[str] = []
        changed = False
        cycle_changed = False
        for st in statuses:
            pos = cfg["POSITIONS"][st["ticker"]]
            msgs, buy_msgs, sell_msgs = detect_alerts(st, pos, cfg)
            # 사이클 자동 리셋 (전 계좌 익절 완료 시 재무장)
            cycle_msg, cycle_changed = auto_cycle_reset(st, pos)
            if cycle_msg:
                alerts.append(f"**{st['ticker']}**\n{cycle_msg}")
            if msgs:
                changed = True
                alerts.extend([f"**{st['ticker']}**"] + msgs)
            # 푸시 — 매수(공개 정보)와 익절(개인 정보, 단독 사용 전제) 모두 전체 구독자
            for m in buy_msgs:
                _push(cfg, "💵 달러 매직 스플릿", m)
            for m in sell_msgs:
                _push(cfg, "💵 달러 매직 스플릿", m)
        if changed or cycle_changed:
            save_config(cfg)  # 신호 플래그 영속화 (중복 방지)
        # 장중 실시간 대시보드 갱신 — 디스패치마다 신선한 HTML 저장 (gh-pages 재배포용)
        write_dashboard(statuses, cfg, args.dashboard)
        content = "\n\n".join(alerts)
        if content:
            print(content)
            if webhook:
                _send_discord(webhook, user_id, "💵 달러 매직 스플릿 신호", content)
                print("✅ Discord 알림 발송 완료")
            else:
                print("⚠️ DISCORD_WEBHOOK 미설정 — 콘솔 출력만 표시")
        else:
            print("✅ 신규 신호 없음 (매수/익절/임박 변화 없음)")
        return

    # ── 기본 실행 / 일일 브리핑 ──
    cycle_msgs: list[str] = []
    state_changed = False
    for st in statuses:
        msg, changed = auto_cycle_reset(st, cfg["POSITIONS"][st["ticker"]])
        if msg:
            cycle_msgs.append(msg)
        state_changed = state_changed or changed
    if state_changed:
        save_config(cfg)

    if cycle_msgs:
        print("\n" + "\n\n".join(cycle_msgs) + "\n")
    print_console(statuses, cfg)
    write_dashboard(statuses, cfg, args.dashboard)

    if args.discord:
        title = f"💵 달러 매직 스플릿 브리핑 — {datetime.now(KST_TZ).strftime('%m-%d')}"
        content = build_briefing_text(statuses, cfg)
        if cycle_msgs:
            content = "\n\n".join(cycle_msgs) + "\n\n" + content
        print(f"\n📨 Discord 브리핑 ({len(statuses)}개 티커)")
        if webhook:
            _send_discord(webhook, user_id, title, content)
            print("✅ Discord 발송 완료")
        else:
            print("⚠️ DISCORD_WEBHOOK 미설정 — 발송 생략")


if __name__ == "__main__":
    main()
