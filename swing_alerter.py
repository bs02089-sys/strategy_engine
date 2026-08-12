#!/usr/bin/env python3
"""
─────────────────────────────────────────────────────────────
스윙 투자 알리미 (Swing Alerter) — TQQQ 스윙 전략 계산기 & 알리미
─────────────────────────────────────────────────────────────
유튜브 'TQQQ 스윙 투자 전략 / 스윙 투자 계산기&매수 매도 시점 알리미'
(구글 스프레드시트 버전)의 로직을 자체 엔진으로 재구현한 도구입니다.

전략 요약 (매수: 스프레드시트 기준, 매도: 예상 수익률 기준으로 변형):
  - 매수: 역대 최고가(ATH) 대비 MDD 5% 단위 구간(-5% ~ -95%)에
    현재가가 도달하면 해당 구간이 '매수' 상태가 됩니다.
  - 매도: 실제 매수가(BUY_PRICE) 대비 스윙 목표 수익률(SWING_TARGET_PCT,
    기본 +10%) 도달 시 매도 알람이 울립니다 (예: 매수가 $100 → 목표 $110).
  - 계산기: 매수가 × 보유수량 → 목표 매도 시 예상 수익금/수익률 자동 계산.

기능:
  - MDD 래더 상태 계산 (매수/대기) — yfinance 배당 조정 종가(Adj Close) 기준
  - 매수 구간 도달 / 임박 / 매도 알림을 Discord로 발송 (--monitor)
  - 일일 종합 브리핑 Discord 발송 (--discord)
  - 모바일 대시보드 HTML 생성 + 로컬 HTTP 서버 (--serve) — 스마트폰 확인용
  - 설정은 swing_config.json(사용자 소유), 상태는 swing_state.json(봇 전용) 두 파일로 분리
    (봇이 상태 파일만 커밋 → git 충돌로 알림 상태가 유실되지 않음)

기존 인프라 재사용:
  - DCA_MA_strategy.py 의 get_prev_close / _send_discord / resolve_discord_config
  - GitHub Actions + cron-job.org (repository_dispatch) 실시간 폴링 패턴

사용법:
  python3 swing_alerter.py                     # 상태 출력 + 대시보드 HTML 저장
  python3 swing_alerter.py --discord           # 상태 + Discord 일일 브리핑 발송
  python3 swing_alerter.py --monitor           # 실시간 모니터 (변경분 알림만)
  python3 swing_alerter.py --serve [PORT]      # 스마트폰용 대시보드 서버
  python3 swing_alerter.py --reset TICKER      # 티커 알림 플래그 초기화
  python3 swing_alerter.py --dashboard PATH    # 대시보드 저장 경로 변경
"""
import argparse
import copy
import json
import os
import requests
import sys
import time
from datetime import datetime, time as dtime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

import yfinance as yf

from DCA_MA_strategy import _send_discord, get_prev_close, resolve_discord_config

CONFIG_PATH = "swing_config.json"
STATE_PATH = "swing_state.json"        # 봇 전용 상태 파일 (ZONE_ALERTS/매도 플래그) — 설정과 분리
PERSONAL_CONFIG_PATH = "swing_personal.json"   # 🔒 개인 매수 포지션 (BUY_PRICE/SHARES) — 공용 설정과 분리
DASHBOARD_PATH = "swing_dashboard.html"
PORTFOLIO_CONFIG_PATH = "portfolio_config.json"
NY_TZ = ZoneInfo("America/New_York")

# swing_state.json 에 보관하는 봇 전용 상태 키 (POSITIONS 내부) —
# swing_config.json(사용자 설정)과 분리해 봇/사용자 커밋 충돌로 상태가 유실되지 않게 한다.
_STATE_KEYS = ("SELL_ALARM_SENT", "SELL_IMMINENT_SENT", "SELL_PUSH_LAST_AT", "ZONE_ALERTS", "ATH_CYCLE_BASE", "CYCLE_RESET_DONE", "ZONE_PUSH_PENDING")
_STATE_DEFAULTS = {
    "SELL_ALARM_SENT": False,
    "SELL_IMMINENT_SENT": False,
    "SELL_PUSH_LAST_AT": None,
    "ZONE_ALERTS": {"hit": [], "imminent": []},
    # ATH_CYCLE_BASE: None — '부재 = 첫 실행' 계약 (save_config 가 None 을 걸러내므로 파일엔 안 쓰임)
    "ATH_CYCLE_BASE": None,
    # CYCLE_RESET_DONE: 전 계좌 매도 목표 도달(사이클 완료) 시 자동 리셋의 중복 방지 플래그 (2026-08-11)
    "CYCLE_RESET_DONE": False,
    # ZONE_PUSH_PENDING: 매수 구간 푸시 실패 시 대기 큐 {msgs: [...], date: "YYYY-MM-DD"} —
    # 다음 폴링에서 재시도, 하루 지난 대기분은 폐기 (스테일 방지). None 기본값 → 파일 노이즈 없음 (2026-08-11)
    "ZONE_PUSH_PENDING": None,
}

# ── 기본 설정 (swing_config.json 에서 덮어쓸 수 있음) ──────────────
DEFAULT_CFG = {
    "ENABLED": True,
    "MDD_START_PCT": 5,             # 매수 구간 시작 (-5%)
    "MDD_END_PCT": 95,              # 매수 구간 종료 (-95%)
    "MDD_STEP_PCT": 5,              # 구간 간격
    "SWING_TARGET_PCT": 10,         # 스윙 목표 수익률(%) — 매도 예정가 = 매수 예정가 × (1 + 목표/100). 앱 대시보드의 기본 선택값도 이 값을 읽는다 (JS 하드코딩 없음, 2026-08-10)
    "IMMINENT_GAP_PCT": 5,          # 임박 알림 기준 (구간/매도 목표까지 %p)
    "PAGES_URL": "",               # GitHub Pages 주소 — 설정 시 대시보드에 라이브 링크 표시
    "ONESIGNAL_APP_ID": "",        # OneSignal 웹 푸시 앱 ID (대시보드 SDK 초기화용, 공개값)
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
    """봇 전용 상태(swing_state.json) 로드 — 없거나 깨졌으면 빈 상태."""
    if not os.path.isfile(STATE_PATH):
        return {"POSITIONS": {}}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"POSITIONS": {}}


def _load_personal_positions() -> dict:
    """🔒 개인 매수 포지션(swing_personal.json) 로드 — 없거나 깨졌으면 빈 dict.

    BUY_PRICE/SHARES/LOTS 는 사용자 개인 정보라 공용 설정(swing_config.json)에 두지 않고
    별도 파일에서 관리한다. 이 값이 Discord 브리핑/전역 푸시 등 공용 알림에
    노출되지 않도록 _PERSONAL 마커로 구분한다. 봇은 이 파일을 절대 쓰지 않는다.
    """
    if not os.path.isfile(PERSONAL_CONFIG_PATH):
        return {}
    try:
        with open(PERSONAL_CONFIG_PATH, "r", encoding="utf-8") as f:
            return (json.load(f) or {}).get("POSITIONS", {}) or {}
    except Exception:
        return {}


def _normalize_lots(pp: dict) -> list[dict]:
    """개인 파일의 LOTS(신형 — 세븐 스플릿 7계좌) 또는 BUY_PRICE/SHARES(구형 단일)를
    정규화된 로트 리스트로 변환. 미입력 계좌(BUY_PRICE/SHARES 모두 null)는 제외.

    주수(SHARES)는 정수로만 기록한다 — 나무증권 등 정수 주 단위 매수 대응.
    소수 입력 시 **내림(floor)** — 반올림하면 예산($500)을 넘는 주수가 기록될 수 있기 때문
    (예: $500 ÷ $73.97 = 6.76 → 6주만 실제 매수 가능).
    반환: [{account, buy_price, shares}, ...]  (입력된 계좌만, shares 는 정수)
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
                "shares": float(int(sh)) if sh is not None else 0.0,
            })
        return out
    if pp.get("BUY_PRICE") is not None or pp.get("SHARES") is not None:
        # 구형 단일 키 → 1번 계좌 로트로 승격 (하위 호환)
        return [{
            "account": 1,
            "buy_price": float(pp["BUY_PRICE"]) if pp.get("BUY_PRICE") is not None else None,
            "shares": float(int(pp["SHARES"])) if pp.get("SHARES") is not None else 0.0,
        }]
    return []


def load_config() -> dict:
    """swing_config.json(설정) + swing_state.json(상태) 병합 로드.

    설정은 사용자, 상태는 봇이 각자 소유하는 두 파일 구조 — 봇이 설정 파일을
    쓰지 않으므로 git 충돌로 상태(ZONE_ALERTS 등)가 유실되지 않는다.
    없으면 기본 템플릿 생성 후 안내.
    """
    if not os.path.isfile(CONFIG_PATH):
        _write_json(CONFIG_PATH, DEFAULT_CFG)
        print(f"ℹ️  {CONFIG_PATH} 이(가) 없어 기본 템플릿을 생성했습니다.")
        print("   POSITIONS 에 모니터링할 티커를 추가한 뒤 다시 실행하세요.")
        return json.loads(json.dumps(DEFAULT_CFG))
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    # 기본값 병합 (새 필드 추가 시 하위 호환)
    merged = {**DEFAULT_CFG, **cfg}
    merged["POSITIONS"] = dict(DEFAULT_CFG["POSITIONS"], **cfg.get("POSITIONS", {}))
    # 봇 상태 오버레이 (swing_state.json) — 기본값 → 상태 파일 값 순으로 덮어씀
    # ⚠️ 기본값은 deepcopy 로 티커마다 새 객체를 줘야 한다 — ZONE_ALERTS 같은 가변 dict 를
    # 공유하면 첫 실행(상태 파일 없음)에서 티커 간 알림 상태가 서로 덮어쓴다.
    state_positions = _load_state().get("POSITIONS", {})
    for ticker, pos in merged["POSITIONS"].items():
        for key, default in _STATE_DEFAULTS.items():
            if key not in pos:
                pos[key] = copy.deepcopy(default)
        st = state_positions.get(ticker) or {}
        for key in _STATE_KEYS:
            if key in st:
                pos[key] = st[key]
    # 🔒 개인 매수 포지션 오버레이 — BUY_PRICE/SHARES/LOTS 는 개인 파일(swing_personal.json)에서만
    # 가져오고, _PERSONAL 마커를 붙여 공용 알림(Discord 브리핑/전역 푸시/대시보드)에서 제외한다.
    # 세븐 스플릿: LOTS(계좌별 로트) 구조 — 계좌 7개 각자의 매수가/수량을 개별 추적한다.
    personal = _load_personal_positions()
    # 방어 계층: 공용 설정(swing_config.json) POSITIONS 에 BUY_PRICE/SHARES 가 직접 들어오면
    # (옛 습관·문서 미숙지) _PERSONAL 마커 없이 남아 Discord 브리핑에 노출되는 누출 구멍이 되므로
    # 개인 취급 + 이동 경고를 출력한다. 전역 푸시가 제거된 지금 공용 BUY_PRICE 는 쓰임새가 없다.
    for ticker, pos in merged["POSITIONS"].items():
        if pos.get("BUY_PRICE") is not None or pos.get("SHARES") is not None:
            print(f"   ⚠️ {ticker}: BUY_PRICE/SHARES 가 공용 설정({CONFIG_PATH})에 있습니다 — "
                  f"{PERSONAL_CONFIG_PATH} 로 옮기세요. (개인 취급으로 공용 알림 제외)")
            pos["_PERSONAL"] = True
    for ticker, pos in merged["POSITIONS"].items():
        pp = personal.get(ticker)
        if not pp:
            continue
        lots = _normalize_lots(pp)
        has_lots_key = isinstance(pp.get("LOTS"), list)
        if lots or has_lots_key:
            pos["LOTS"] = lots
            pos["_PERSONAL"] = True
    # 개인 파일에만 있는 티커 (공용 POSITIONS 와 불일치) — 오타 조기 발견용 경고
    for ticker in personal:
        if ticker not in merged["POSITIONS"]:
            print(f"   ⚠️ {PERSONAL_CONFIG_PATH} 의 '{ticker}' 가 공용 설정 POSITIONS 에 없어 무시됩니다 — 티커 오타 확인.")
    return merged


def save_config(cfg: dict) -> None:
    """봇 상태만 swing_state.json 에 저장.

    swing_config.json(사용자 설정)은 절대 쓰지 않는다 — 봇/사용자 파일이 분리되어
    git pull 충돌이 상태를 덮어쓰는 문제가 원천 차단된다.
    """
    state = {"POSITIONS": {}}
    for ticker, pos in cfg.get("POSITIONS", {}).items():
        # None 값(예: 아직 푸시를 안 보낸 SELL_PUSH_LAST_AT)은 파일에 쓰지 않는다
        st = {k: pos[k] for k in _STATE_KEYS if k in pos and pos[k] is not None}
        if st:
            state["POSITIONS"][ticker] = st
    _write_json(STATE_PATH, state)


def _resolve_discord(cfg: dict) -> tuple[str, str]:
    """Discord 웹훅/유저 ID — env 우선, swing_config → portfolio_config 폴백."""
    try:
        with open(PORTFOLIO_CONFIG_PATH, "r", encoding="utf-8") as f:
            portfolio = json.load(f)
    except Exception:
        portfolio = {}
    merged = {**portfolio, **cfg}   # swing_config 값이 portfolio 값보다 우선
    return resolve_discord_config(merged)


def _resolve_onesignal(cfg: dict) -> tuple[str, str]:
    """OneSignal APP_ID / REST API KEY — env 우선, swing_config 폴백.

    - APP_ID: 공개값 (대시보드 SDK에 포함) → swing_config.json 에 보관 가능
    - REST API KEY: 비밀값 → GitHub Actions 시크릿(env)으로만 주입 권장
    """
    app_id = os.environ.get("ONESIGNAL_APP_ID") or cfg.get("ONESIGNAL_APP_ID", "")
    api_key = os.environ.get("ONESIGNAL_REST_API_KEY") or ""
    return (app_id or "").strip(), (api_key or "").strip()


def send_onesignal_push(app_id: str, api_key: str, title: str, body: str,
                        url: str | None = None,
                        filters: list | None = None) -> tuple[int, str]:
    """OneSignal REST API로 웹 푸시 발송.

    filters 미지정: 구독자 전체(Subscribed Users) 대상
    filters 지정: 태그 조건을 만족하는 구독자만 대상 (사용자별 푸시)
    ※ OneSignal은 요청당 타겟팅 방식 1개만 허용 — filters 시 included_segments 금지

    반환: (HTTP 상태코드, 응답 본문)
    """
    if not app_id or not api_key:
        return 0, "ONESIGNAL_APP_ID / ONESIGNAL_REST_API_KEY 미설정"
    payload: dict = {
        "app_id": app_id,
        "target_channel": "push",
        "headings": {"en": title},
        "contents": {"en": body},
    }
    if filters:
        payload["filters"] = filters
    else:
        payload["included_segments"] = ["Subscribed Users"]
    if url:
        payload["url"] = url
    # OneSignal 인증: 신형 앱 키(os_v2_app_…)는 'Key', 구형 REST 키는 'Basic'
    # → 키 형식(prefix)으로 우선 순위 결정, 401/403(인증·권한 오류)이면 다른 형식으로 재시도.
    # (Basic을 신형 키로 보내면 401이 아닌 403이 나올 수 있어 폴백이 발동 안 하던 문제 — 2026-08-12)
    schemes = (("Key", "Basic") if api_key.startswith("os_v2_") else ("Basic", "Key"))
    for scheme in schemes:
        try:
            resp = requests.post(
                "https://api.onesignal.com/notifications",
                headers={
                    "Authorization": f"{scheme} {api_key}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                json=payload,
                timeout=15,
            )
            if resp.status_code not in (401, 403):
                return resp.status_code, resp.text[:500]
        except Exception as e:  # noqa: BLE001
            return 0, f"전송 예외: {e}"
    return 401, "인증 실패 (Basic/Key 모두 401/403) — API 키가 현재 앱의 키인지 확인 필요"


def send_user_sell_pushes(statuses: list[dict], cfg: dict) -> bool:
    """매도 푸시 — 단독 사용 전환(2026-08-12) 후 '전체 구독자(Subscribed Users) = 내 기기' 대상.

    사용자별 태그 필터(swing_sell_{TICKER}_{ACCOUNT})·Liquid 개인화 제거 — 앱이 태그를
    등록하지 않아도 푸시가 동작한다 (태그 누락으로 'All included players are not subscribed'
    0명 응답이 나던 문제 해결). 매도 예정가는 서버 LOTS(swing_personal.json)의 계좌별 목표
    (매수가 × SWING_TARGET_PCT)를 그대로 사용한다. 계좌별 1일 1회 중복 방지 유지.
    ⚠️ 지인이 새로 구독하면 본인 매도 정보가 노출될 수 있다 (단독 사용 전제 — AGENTS.md 참조).
    """
    app_id, api_key = _resolve_onesignal(cfg)
    if not app_id or not api_key:
        return False
    today = datetime.now(NY_TZ).strftime("%Y-%m-%d")
    pages = (cfg.get("PAGES_URL") or "").strip()
    changed = False
    for st in statuses:
        if st.get("error"):
            continue
        ticker = st["ticker"]
        pos = cfg["POSITIONS"][ticker]
        # 발송일 기록 — 계좌별: {계좌번호: 날짜} (구형 단일 날짜 문자열은 1번 계좌로 마이그레이션)
        sent = pos.get("SELL_PUSH_LAST_AT") or {}
        if isinstance(sent, str):
            sent = {"1": sent}
        # 발송 대상 계좌 = 서버 LOTS 중 **매도 목표에 실제 도달한(sell_ready)** 계좌만 (앱 태그 불필요)
        # ⚠️ sell_ready 조건 필수 (2026-08-12) — 목표 미도달인데 '매도 신호' 푸시가 매일 발송되던 버그 수정:
        #    기존 코드가 sell_target 만 있으면 무조건 발송해, TQQQ $73.06(목표 $88.76 미만) 상태에서도
        #    '매도 신호' 푸시가 발화됐다 (구독 후 매일 거짓 신호 수신 위험).
        targets = []
        for lot in st.get("lots") or []:
            if lot.get("account") and lot.get("sell_target") and lot.get("sell_ready"):
                targets.append((int(lot["account"]), float(lot["sell_target"])))
        if not targets:
            continue
        sent_any = False
        for n, target in sorted(targets):
            if sent.get(str(n)) == today:
                continue  # 해당 계좌는 오늘 이미 발송 (계좌별 1일 1회)
            if len(targets) > 1:
                title = f"📈 {ticker} {n}번 계좌 매도 신호"
                body = (f"{n}번 계좌 매도 예정가(${target:,.2f})에 도달했습니다 — "
                        "매도 검토가 필요해요." + (f"\n앱에서 확인: {pages}" if pages else ""))
            else:
                title = f"📈 {ticker} 매도 신호"
                body = (f"내 매도 예정가(${target:,.2f})에 도달했습니다 — 매도 검토가 필요해요."
                        + (f"\n앱에서 확인: {pages}" if pages else ""))
            # 필터 없이 전체 구독자 발송 (단독 사용 전제)
            code, resp = send_onesignal_push(
                app_id, api_key, title=title, body=body,
                url=pages or None,
            )
            # 성공(2xx)일 때만 발송일 기록 — 실패 시 당일 재시도 가능 (알림 누락 방지)
            if str(code).startswith("2"):
                sent[str(n)] = today
                sent_any = True
            print(f"   📣 {ticker} {n}번 계좌 매도 푸시: HTTP {code} — {resp[:80]}")
        if sent_any:
            pos["SELL_PUSH_LAST_AT"] = sent
            changed = True
    if changed:
        save_config(cfg)
    return changed


def send_zone_pushes(zone_msgs: dict[str, list[str]], cfg: dict) -> None:
    """매수 구간 도달(🔻)/임박(📡) 푸시 — 전체 구독자(Subscribed Users = 내 기기) 대상 (2026-08-12 단독 사용 전환).

    swing_zone_{TICKER} 태그 필터 제거 — 앱을 열지 않은 기기도 수신. 매수 구간은 ATH(공개 정보)
    기준이므로 개인 정보 노출이 없다.

    중복 방지/재시도: 새 이벤트는 detect_alerts 의 ZONE_ALERTS 상태가 1회만 생성하고,
    발송 실패(비 2xx) 시 메시지를 ZONE_PUSH_PENDING(봇 상태)에 보관해 다음 폴링에서 재시도한다.
    대기분은 당일(미국 날짜)까지만 재시도 — 하루가 지나면 폐기해 무기한 재시도·스테일 발송을 막는다
    (매도 푸시의 SELL_PUSH_LAST_AT 날짜 경계와 동일 취지 — 푸시가 주 채널인 지인 누락 방지).
    """
    app_id, api_key = _resolve_onesignal(cfg)
    if not app_id or not api_key:
        return
    pages = (cfg.get("PAGES_URL") or "").strip()
    today = datetime.now(NY_TZ).strftime("%Y-%m-%d")
    changed = False
    # 발송 대상 티커 = 신규 이벤트 + 실패 대기 중인 티커
    tickers = set(zone_msgs)
    for ticker, pos in cfg.get("POSITIONS", {}).items():
        if pos.get("ZONE_PUSH_PENDING"):
            tickers.add(ticker)
    for ticker in sorted(tickers):
        pos = cfg["POSITIONS"].get(ticker)
        pending = (pos or {}).get("ZONE_PUSH_PENDING")
        p_msgs: list[str] = []
        if pending:
            if pending.get("date") == today:
                p_msgs = pending.get("msgs") or []
            else:
                # 지난 날짜 대기분 폐기 — 가격 정보가 스테일해진 알림은 발송하지 않는다
                pos.pop("ZONE_PUSH_PENDING", None)
                changed = True
                print(f"   📭 {ticker} 만료 매수 구간 푸시 폐기 (일자 {pending.get('date')})")
        msgs = list(p_msgs) + list(zone_msgs.get(ticker) or [])
        if not msgs:
            continue
        # Discord 마크다운(**) 제거 — 푸시 알림 본문 정제. 타이틀은 도달 포함 여부로 아이콘 선택.
        body = "\n".join(msgs).replace("**", "")
        has_hit = any("매수 구간 도달" in m for m in msgs)
        code, resp = send_onesignal_push(
            app_id, api_key,
            title=f"{'🔻' if has_hit else '📡'} {ticker} 매수 구간 신호",
            body=body,
            url=pages or None,
        )
        if str(code).startswith("2"):
            # 성공 → 대기 큐 제거 (신규 메시지는 이미 전송됨 — 재발송 없음)
            if pos and p_msgs:
                pos.pop("ZONE_PUSH_PENDING", None)
                changed = True
        elif pos:
            # 실패 → 대기 큐 보관 (신규+기존, 당일 한정) — 다음 폴링에서 재시도
            pos["ZONE_PUSH_PENDING"] = {"msgs": msgs, "date": today}
            changed = True
        print(f"   📣 {ticker} 매수 구간 푸시: HTTP {code} — {resp[:80]}")
    if changed:
        save_config(cfg)

# ═══════════════════════════════════════════════════════════
# 데이터 조회
# ═══════════════════════════════════════════════════════════

def get_ath(ticker: str, max_retries: int = 3) -> tuple[float | None, str | None]:
    """역대 최고 종가(ATH) 조회 — 배당 조정 종가(Adj Close) 기준.

    yfinance Adj Close 는 액면분할과 현금 분배(배당)를 모두 반영한 연속 가격이라
    분할 전후가 한 기준으로 비교된다. 사용자 참고 차트(TradingView 등 기본값)가
    조정가 기준이므로 매수 구간(MDD)을 차트 전고가와 맞추려면 조정 종가를 써야
    한다 (예: TQQQ 2026-06-02 — 원시 종가 $87.22 ↔ 조정 종가 $87.02).
    조정 계수는 최신 행에서 1.0 으로 고정되므로 현재가/일간 등락률은 원시 종가
    그대로 표시하는 게 맞다 (기준 비대칭은 의도적 — '실거래 표시 + 차트 기준 전고가').
    """
    last_err: Exception | None = None
    for attempt in range(max_retries):
        try:
            stock = yf.Ticker(ticker)
            hist = stock.history(period="max", interval="1d", auto_adjust=False)
            if hist.empty:
                raise ValueError("Data empty.")
            closes = hist["Adj Close"].dropna()
            if closes.empty:
                # 폴백: 조정 데이터가 전무한 데이터 피드 대비 — 원시 종가로 계산 (비정상 케이스)
                closes = hist["Close"].dropna()
                if closes.empty:
                    raise ValueError("No close data.")
            peak_idx = closes.idxmax()
            ath = float(closes.loc[peak_idx])
            ath_date = peak_idx.date().strftime("%Y-%m-%d")
            return ath, ath_date
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < max_retries - 1:
                time.sleep(2.0)
    print(f"   ⚠️ {ticker} ATH 조회 실패 (3회 재시도): {last_err}")
    return None, None


def get_prior_close(ticker: str, as_of: str, max_retries: int = 3) -> tuple[float | None, str | None]:
    """마지막 확정 종가(as_of, 'MM-DD') 세션의 직전 거래일 종가와 그 날짜.

    전일 종가 대비 등락률 + 대시보드 '전일 종가' 줄 표시용 — get_prev_close()가 반환한
    최종 세션의 바로 앞 세션 종가/날짜를 같은 yfinance 1개월 데이터에서 찾는다.
    as_of에 해당하는 행이 없으면(데이터 변경 등) 마지막에서 두 번째 행을 사용한다.

    ⚠️ 폴백은 반드시 '한 세션 앞' 행(iloc[-2])으로 해야 한다 — 마지막 행(iloc[-1])은
    현재 표시 중인 종가와 같은 값이라 '전일 종가 = 현재가'로 잘못 표시된다. (2026-08-12)
    """
    try:
        as_month, as_day = int(as_of[:2]), int(as_of[3:5])
    except (ValueError, TypeError, IndexError):
        return None, None
    last_err: Exception | None = None
    for attempt in range(max_retries):
        try:
            hist = yf.Ticker(ticker).history(period="1mo", interval="1d", auto_adjust=False)
            closes = hist["Close"].dropna()
            if len(closes) < 2:
                raise ValueError("Need at least 2 sessions.")
            for i in range(len(closes) - 1, -1, -1):
                d = closes.index[i].date() if hasattr(closes.index[i], "date") else None
                if d is not None and d.month == as_month and d.day == as_day:
                    if i == 0:
                        return None, None   # as_of 가 데이터 첫 행 — 이전 세션 없음
                    pd = closes.index[i - 1].date() if hasattr(closes.index[i - 1], "date") else None
                    return float(closes.iloc[i - 1]), (pd.strftime("%m-%d") if pd is not None else None)
            # as_of 미발견(새 fetch가 한 세션 뒤처진 데이터 지연 등) → 마지막에서 두 번째 행을
            # 직전 종가로 사용 (마지막 행 = 현재 표시 종가이므로 절대 쓰지 않는다)
            pd = closes.index[-2].date() if hasattr(closes.index[-2], "date") else None
            return float(closes.iloc[-2]), (pd.strftime("%m-%d") if pd is not None else None)
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < max_retries - 1:
                time.sleep(2.0)
    print(f"   ⚠️ {ticker} 직전 종가 조회 실패: {last_err}")
    return None, None


def _ny_market_open(now_ny: datetime | None = None) -> bool:
    """미국 정규장(월~금 09:30~16:00 ET) 여부 — 장중에만 실시간 표시 가격 사용.

    공휴일은 별도 판정하지 않는다 — 휴장일엔 yfinance 가격이 갱신되지 않아 실시간
    값이 이전 종가와 같아져 화면상 영향이 없다 (오버레이해도 값이 같을 뿐).
    """
    now_ny = now_ny or datetime.now(NY_TZ)
    if now_ny.weekday() >= 5:
        return False
    t = now_ny.time()
    return dtime(9, 30) <= t <= dtime(16, 0)


def _get_live_price(ticker: str) -> float | None:
    """장중 실시간 가격 (yfinance, 15분 지연) — fast_info 우선, 1분봉 폴백.

    정규장이 아니면 None 을 반환해 표시 가격이 확정 종가로 유지되게 한다.
    알림(매수 구간/임박/매도) 판정은 이 값을 쓰지 않는다 — 항상 확정 종가 기준.
    """
    if not _ny_market_open():
        return None
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


# ═══════════════════════════════════════════════════════════
# 상태 계산
# ═══════════════════════════════════════════════════════════

def build_ladder(ath: float, cfg: dict) -> list[dict]:
    """MDD 구간 래더 생성: [{pct, price}, ...] — -5% ~ -95%, 5% 간격."""
    start = int(cfg.get("MDD_START_PCT", 5))
    end = int(cfg.get("MDD_END_PCT", 95))
    step = int(cfg.get("MDD_STEP_PCT", 5))
    ladder = []
    for p in range(start, end + 1, step):
        ladder.append({"pct": p, "price": ath * (1 - p / 100.0)})
    return ladder


def compute_ticker(ticker: str, pos: dict, cfg: dict, live: bool = False) -> dict:
    """티커 1개의 현재 상태 계산 (가격/ATH/래더/매도/손익).

    live=True 면 미국 정규장 중에 **표시 가격만** yfinance 실시간(15분 지연) 값으로
    오버레이한다 (2026-08-11). 알림 판정(래더 hit/매도 플래그/dd_pct)은 항상 확정
    종가 기준으로 유지 — 장중 변동성에 알림이 흔들리지 않는다. 장외/휴장이면
    실시간 조회를 건너뛰어 종가 표시로 동작한다.
    """
    st: dict = {"ticker": ticker, "error": None, "live": False, "close_price": None}

    price, as_of = get_prev_close(ticker)
    if price is None:
        st["error"] = f"{ticker} 가격 조회 실패"
        return st
    st["close_price"] = price

    ath, ath_date = get_ath(ticker)
    if ath is None or ath <= 0:
        st["error"] = f"{ticker} ATH 조회 실패"
        return st

    dd_pct = (price - ath) / ath * 100.0  # ATH 대비 하락율 (음수 = 하락)

    # 전일 종가 대비 등락률 — 직전 거래일 종가 대비 (대시보드 전일 종가 줄 표시용)
    prior_close, prior_close_date = get_prior_close(ticker, as_of)
    day_change_pct = None
    if prior_close and prior_close > 0:
        day_change_pct = (price - prior_close) / prior_close * 100.0

    ladder = build_ladder(ath, cfg)
    for lvl in ladder:
        lvl["hit"] = bool(price <= lvl["price"] + 1e-9)

    hit_levels = [l for l in ladder if l["hit"]]
    deepest_hit = hit_levels[-1]["pct"] if hit_levels else None
    next_zone = next((l for l in ladder if not l["hit"]), None)

    # 매도 목표 — 계좌별 로트(LOTS, 개인 포지션) 또는 단일 BUY_PRICE(하위 호환) 기준.
    # 세븐 스플릿: 계좌 1~7 각자의 매수가 × (1 + 스윙 목표 수익률) → 계좌별 매도 목표를 개별 계산.
    target_pct = float(cfg.get("SWING_TARGET_PCT", 10))
    lots = pos.get("LOTS") or []
    lot_stats = []
    for lot in lots:
        bp = lot.get("buy_price")
        sh = float(lot.get("shares") or 0)
        ls = {"account": lot.get("account"), "buy_price": None, "shares": sh,
              "sell_target": None, "sell_ready": False, "sell_gap_pct": None,
              "exp_profit": None, "exp_roi": None}
        if bp:
            bp = float(bp)
            s_target = bp * (1 + target_pct / 100.0)
            s_ready = bool(price >= s_target - 1e-9)
            s_gap = None if s_ready else max((s_target - price) / s_target * 100.0, 0.0)
            ls.update(buy_price=bp, sell_target=s_target, sell_ready=s_ready,
                      sell_gap_pct=s_gap,
                      exp_profit=(s_target - bp) * sh if sh > 0 else None,
                      exp_roi=(s_target / bp - 1.0) * 100.0 if sh > 0 else None)
        lot_stats.append(ls)

    # 상위(집계) 필드 — 단일 BUY_PRICE 경로(하위 호환) + LOTS 집계 (아무 계좌나 목표 도달 시 매도 준비)
    buy_price = pos.get("BUY_PRICE")
    sell_target = None
    sell_ready = False
    sell_gap_pct = None
    if buy_price:
        sell_target = float(buy_price) * (1 + target_pct / 100.0)
        sell_ready = bool(price >= sell_target - 1e-9)
        if not sell_ready:
            sell_gap_pct = max((sell_target - price) / sell_target * 100.0, 0.0)
    open_lots = [l for l in lot_stats if l["sell_target"]]
    if open_lots:
        if any(l["sell_ready"] for l in open_lots):
            sell_ready = True
        if sell_target is None:
            sell_target = max(l["sell_target"] for l in open_lots)
        if not sell_ready:
            gaps = [l["sell_gap_pct"] for l in open_lots if l["sell_gap_pct"] is not None]
            sell_gap_pct = min(gaps) if gaps else None

    # 계산기 — 목표 매도 시 예상 손익 (단일 경로 + LOTS 가중평균)
    shares = pos.get("SHARES") or 0   # null/미입력 시 0 처리 (모니터링 전용 포지션)
    exp_profit = exp_roi = None
    if buy_price and sell_target and shares > 0:
        exp_profit = (sell_target - float(buy_price)) * float(shares)
        exp_roi = (sell_target / float(buy_price) - 1.0) * 100.0
    elif open_lots:
        total_shares = sum(l["shares"] for l in open_lots)
        total_cost = sum(l["buy_price"] * l["shares"] for l in open_lots)
        if total_shares > 0 and total_cost > 0:
            buy_price = total_cost / total_shares   # 대표 매수가 (가중평균)
            shares = total_shares
            exp_profit = sum((l["sell_target"] - l["buy_price"]) * l["shares"]
                             for l in open_lots if l["shares"] > 0)
            exp_roi = exp_profit / total_cost * 100.0

    st.update({
        "label": pos.get("LABEL") or ticker,
        "price": price,
        "as_of": as_of,
        "ath": ath,
        "ath_date": ath_date,
        "dd_pct": dd_pct,
        "day_change_pct": day_change_pct,
        "prior_close": prior_close,
        "prior_close_date": prior_close_date,
        "deepest_hit": deepest_hit,
        "next_zone": next_zone,
        "ladder": ladder,
        "sell_target": sell_target,
        "sell_ready": sell_ready,
        "sell_gap_pct": sell_gap_pct,
        "buy_price": float(buy_price) if buy_price else None,
        "shares": float(shares),
        "exp_profit": exp_profit,
        "exp_roi": exp_roi,
        "personal": bool(pos.get("_PERSONAL")),
        "lots": lot_stats,
        "zone_alerts": pos.setdefault("ZONE_ALERTS", {"hit": [], "imminent": []}),
    })
    # 실시간 표시 오버레이 — 장중 라이브 가격을 표시 필드에만 반영 (알림 필드는 종가 기준 유지)
    if live:
        live_price = _get_live_price(ticker)
        if live_price is not None and live_price > 0:
            st["price"] = live_price
            st["live"] = True
            st["as_of"] = datetime.now(NY_TZ).strftime("%m-%d %H:%M")
            # 전고가 대비 하락률/전일 대비 등락률도 라이브 가격 기준으로 표시 (표시 전용)
            st["live_dd_pct"] = (live_price - ath) / ath * 100.0
            if prior_close and prior_close > 0:
                st["day_change_pct"] = (live_price - prior_close) / prior_close * 100.0
    return st


# ═══════════════════════════════════════════════════════════
# 알림 감지 (--monitor) — 상태를 변경하며 1회성 알림 메시지 생성
# ═══════════════════════════════════════════════════════════

def _handle_ath_cycle_reset(st: dict, pos: dict, zone_alerts: dict, msgs: list[str]) -> None:
    """신규 전고가 감지 → 매수 구간 알림 상태 자동 리셋 (DCA 엔진 사이클 리셋과 동일 패턴).

    ATH_CYCLE_BASE(봇 상태) 대비 +1% 이상 신규 전고가가 확인되면 기존에 기록된
    hit/imminent 를 비워 새 하락 사이클을 시작한다 — 이전 사이클의 기록이 남아
    새 사이클의 구간 도달/임박 알림이 삼켜지는 문제를 방지.

    동작 주의: 리셋은 detect_alerts 의 구간 감지 루프보다 앞서 실행되므로, 같은 실행에서
    현재 도달 중인 구간이 다시 기록되고 '구간 도달' 알림으로 재발송된다 (새 사이클 재무장).
    이를 '재기록만 하고 재발송 미루기'로 바꾸면 다음 폴링에서 중복 방지(hit 에 이미 있음)로
    재알림이 영원히 오지 않으므로, 재기록+재알림이 의도된 동작이다.

    마이그레이션: 기존 상태 파일(hit/imminent 채워짐, ATH_CYCLE_BASE 없음)에 배포된 첫 실행은
    base = 현재 ATH 를 설정만 하고 기존 기록은 유지한다 — 이미 +1% 넘게 갱신된 ATH 상태로
    배포되는 경우 한 사이클 동안 스테일 기록이 살아남을 수 있으나, 신규 전고가 발생 시점부터
    정상 리셋이 동작한다.
    """
    ath = st.get("ath")
    if not ath or ath <= 0:
        return
    base = pos.get("ATH_CYCLE_BASE")
    if base is None:
        pos["ATH_CYCLE_BASE"] = round(ath, 2)          # 최초 실행 — 기준만 설정
    elif ath > float(base) * 1.01:
        zone_alerts["hit"] = []
        zone_alerts["imminent"] = []
        pos.pop("ZONE_PUSH_PENDING", None)   # 죽은 사이클의 미전송 푸시 대기 큐 정리 (2026-08-11)
        pos["ATH_CYCLE_BASE"] = round(ath, 2)
        msgs.append(
            f"🆕 **{st['ticker']} 신규 전고가 갱신 ${ath:,.2f} ({st.get('ath_date', '')})**\n"
            "   매수 구간이 새 전고가 기준으로 초기화되었습니다 (새 하락 사이클)."
        )


def detect_alerts(st: dict, pos: dict, cfg: dict) -> tuple[list[str], list[str]]:
    """새로 도달한 매수 구간 / 임박 / 매도 알림을 감지해 메시지 목록 반환.

    반환: (전체 메시지, 매수 구간 푸시 전용 메시지)
    - 전체: Discord 발송용 — 구간 도달/임박/매도/신규 전고가
    - 매수 구간 푸시 전용: 🔻 도달/📡 임박만 — ATH(공개 정보) 기준이라 앱 구독자 전원에게
      사용자별 태그 푸시로 발송 가능 (매도·신규 전고가는 개인 정보/노이즈라 제외, 2026-08-11)
    pos(ZONE_ALERTS/SELL_*) 상태를 갱신하므로 재폴링 시 중복 알림이 없습니다.
    """
    msgs: list[str] = []
    zone_msgs: list[str] = []
    if st.get("error"):
        return msgs, zone_msgs
    zone_alerts = st["zone_alerts"]
    # 신규 전고가 확인 → 기록된 구간 상태 리셋 (알림 삼킴 방지)
    _handle_ath_cycle_reset(st, pos, zone_alerts, msgs)
    tick = st["ticker"]
    # 알림 메시지의 현재가는 확정 종가 기준 (알림 판정이 종가 기반이므로 — 실시간은 표시 전용)
    price = st.get("close_price") or st["price"]
    gap_p = float(cfg.get("IMMINENT_GAP_PCT", 5))
    dd_abs = abs(st["dd_pct"])

    # 1) 새로 도달한 매수 구간
    for lvl in st["ladder"]:
        p = lvl["pct"]
        if lvl["hit"] and p not in zone_alerts["hit"]:
            zone_alerts["hit"].append(p)
            zone_alerts["imminent"] = [x for x in zone_alerts["imminent"] if x != p]
            msg = (f"🔻 **{tick} -{p:.0f}% 매수 구간 도달**\n"
                   f"현재가 ${price:.2f} | 목표가 ${lvl['price']:.2f} (하락 {st['dd_pct']:.1f}%)")
            msgs.append(msg)
            zone_msgs.append(msg)

    # 2) 다음 구간 임박
    nxt = st["next_zone"]
    if nxt and nxt["pct"] not in zone_alerts["imminent"]:
        remain = nxt["pct"] - dd_abs          # 다음 구간까지 남은 %p
        if 0 <= remain <= gap_p:
            zone_alerts["imminent"].append(nxt["pct"])
            msg = (f"📡 **{tick} -{nxt['pct']:.0f}% 매수 구간 임박**\n"
                   f"현재 하락 {st['dd_pct']:.1f}% (남은 {remain:.1f}%p) | 목표가 ${nxt['price']:.2f}")
            msgs.append(msg)
            zone_msgs.append(msg)

    # 3) 매도 목표 임박 — 개인 포지션(_PERSONAL)은 공용 알림에서 제외
    #    (내 매도 목표가 Discord/전역 푸시로 지인에게 노출되는 것 방지 — 개인은 앱 태그 푸시로 수신)
    if not pos.get("_PERSONAL") and st["sell_target"] and st["sell_gap_pct"] is not None and not pos.get("SELL_IMMINENT_SENT"):
        if 0 < st["sell_gap_pct"] <= gap_p:
            pos["SELL_IMMINENT_SENT"] = True
            msgs.append(
                f"🚀 **{tick} 매도 목표 임박**\n"
                f"현재가 ${price:.2f} | 매도 목표 ${st['sell_target']:.2f} "
                f"(남은 {st['sell_gap_pct']:.1f}%)"
            )

    # 4) 매도 알람 — 개인 포지션(_PERSONAL)은 공용 알림에서 제외 (위와 동일한 이유)
    if not pos.get("_PERSONAL") and st["sell_ready"] and not pos.get("SELL_ALARM_SENT"):
        pos["SELL_ALARM_SENT"] = True
        msgs.append(
            f"🚨 **{tick} 매도 알람 — 목표 도달!**\n"
            f"현재가 ${price:.2f} ≥ 매도 목표 ${st['sell_target']:.2f}\n"
            f"매도 검토 필요 (스윙 목표 수익률 {cfg.get('SWING_TARGET_PCT', 10):+.0f}%)"
        )
    return msgs, zone_msgs


# ═══════════════════════════════════════════════════════════
# 메시지 빌더 (콘솔 / Discord)
# ═══════════════════════════════════════════════════════════

def _sell_chip(st: dict, gap_pct: float = 5.0) -> str:
    if st.get("error"):
        return "❌ 오류"
    if st["sell_ready"]:
        return "🚨 매도"
    if st.get("sell_target") is None:
        return "—"
    if st["sell_gap_pct"] is not None and st["sell_gap_pct"] <= gap_pct:
        return "🚀 임박"
    return "⏳ 대기"


def _lot_chip(lot: dict, gap_pct: float = 5.0) -> str:
    """계좌(로트) 1개의 매도 상태 칩 — 목표 도달/임박/대기."""
    if lot.get("sell_target") is None:
        return "—"
    if lot["sell_ready"]:
        return "🚨 매도"
    if lot.get("sell_gap_pct") is not None and lot["sell_gap_pct"] <= gap_pct:
        return "🚀 임박"
    return "⏳ 대기"


def _ladder_summary(st: dict) -> str:
    """래더 요약 — 도달 구간 + 다음 구간 (브리핑용)."""
    if st.get("error"):
        return "-"
    hit = [f"-{l['pct']:.0f}%" for l in st["ladder"] if l["hit"]]
    nxt = st["next_zone"]
    parts = []
    if hit:
        parts.append("🟢 " + " · ".join(hit))
    if nxt:
        remain = nxt["pct"] - abs(st["dd_pct"])
        parts.append(f"다음 ⏳ -{nxt['pct']:.0f}% (남은 {max(remain, 0):.1f}%p)")
    if not parts:
        parts.append("모든 구간 대기")
    return " | ".join(parts)


def build_briefing_text(statuses: list[dict], cfg: dict) -> str:
    """일일 종합 브리핑 (Discord 설명란용).
    앱 대시보드 카드의 막대(래더 -5%~-95%) 위쪽 내용을 그대로 개조식(불릿)으로 표현한다.
    (앱 카드: 티커·매도 상태 → 현재가 → 전일 종가(전일 대비 등락률) → 매수 구간 → 전고가(ATH 대비 하락률))
    """
    gap = float(cfg.get("IMMINENT_GAP_PCT", 5))
    lines = []
    for st in statuses:
        if st.get("error"):
            lines.append(f"**{st['ticker']}** ❌ {st['error']}")
            continue
        # 매도 상태 — 개인 포지션(BUY_PRICE/SHARES)은 공용 브리핑에 노출하지 않는다
        # (지인이 Discord 채널에서 내 매도 목표/수량을 볼 수 없도록 매도 미설정으로 표시)
        if st.get("personal"):
            sell_txt = "매도 미설정"
        elif st["sell_ready"]:
            sell_txt = "🎉 매도 도달"
        elif st["sell_target"] is not None:
            if st["sell_gap_pct"] is not None and st["sell_gap_pct"] <= gap:
                sell_txt = "🚀 매도 임박"
            else:
                sell_txt = "⏳ 매도 대기"
        else:
            sell_txt = "매도 미설정"
        # 매수 구간 상태 (앱 카드 chip과 동일)
        hit_cnt = len([l for l in st["ladder"] if l["hit"]])
        buy_txt = f"🟢 매수 구간 {hit_cnt}개 경과" if hit_cnt else "매수 구간 대기"
        src_txt = f"실시간 {st['as_of']}" if st.get("live") else f"종가 기준 {st['as_of']}"
        lines.extend([
            f"**{st['ticker']}** · {sell_txt}",
            f"- 현재가 ${st['price']:.2f} ({src_txt})",
            f"- ATH ${st['ath']:.2f} ({st['ath_date']}) → 하락 **{st['dd_pct']:+.1f}%**",
            f"- {buy_txt}",
            f"- 전고가: ${st['ath']:,.2f} ({st['ath_date']})",
            "",
        ])
    return "\n".join(lines).strip()


# ═══════════════════════════════════════════════════════════
# 모바일 대시보드 HTML
# ═══════════════════════════════════════════════════════════

_CSS = """
:root{
  --bg:#0b0e14; --card:#141a26; --border:#20293b; --text:#e6edf3; --muted:#8b949e;
  --green:#3fb950; --green-dim:#12281c; --red:#f85149; --red-dim:#2d1517;
  --amber:#d29922; --amber-dim:#2a2112; --blue:#58a6ff; --blue-dim:#12253d;
}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
html,body{background:var(--bg);color:var(--text);
  font-family:-apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Noto Sans KR","Malgun Gothic",sans-serif}
body{max-width:640px;margin:0 auto;padding:0 14px 40px;font-size:16px}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
header{position:sticky;top:0;z-index:5;background:rgba(11,14,20,.92);
  backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);
  padding:16px 0 12px;border-bottom:1px solid var(--border);margin-bottom:14px}
header h1{font-size:21px;letter-spacing:-.3px}
header .sub{color:var(--muted);font-size:13px;margin-top:4px}
.chips{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap}
.chip{font-size:13px;padding:6px 11px;border-radius:999px;font-weight:600}
.chip.gray{background:#1c2533;color:var(--muted)}
.chip.green{background:var(--green-dim);color:var(--green)}
.chip.red{background:var(--red-dim);color:var(--red)}
.chip.amber{background:var(--amber-dim);color:var(--amber)}
.push-btn{font-size:14px;font-weight:700;padding:8px 15px;border-radius:999px;border:1px solid var(--blue);
  background:var(--blue-dim);color:var(--blue);cursor:pointer;-webkit-tap-highlight-color:transparent}
.push-btn:active{opacity:.7}
.push-btn:disabled{opacity:.4;cursor:default}
.push-btn.on{border-color:var(--green);background:var(--green-dim);color:var(--green)}
.push-err{font-size:11px;color:var(--amber);background:var(--amber-dim);border:1px solid var(--amber);
  border-radius:8px;padding:5px 9px;margin-top:6px;width:100%;word-break:break-all;line-height:1.5}
.card{background:var(--card);border:1px solid var(--border);border-radius:16px;
  padding:16px;margin-bottom:16px}
.row{display:flex;align-items:center;justify-content:space-between;gap:8px}
.tick{font-size:19px;font-weight:800}
.tag{font-size:12px;color:var(--muted);font-weight:400;margin-left:6px}
.dd{font-size:16px;font-weight:800}
.dd.down{color:var(--blue)} .dd.up{color:var(--red)} .dd.flat{color:var(--muted)}
.price{font-size:36px;font-weight:800;margin:10px 0 2px}
.meta{color:var(--muted);font-size:13px}
.meta .dd{font-size:13px}
.info{background:#0f1420;border:1px solid var(--border);border-radius:12px;
  padding:10px 12px;margin-top:12px;font-size:22px;line-height:1.7}
.info b{color:var(--text)}
.info .dd{font-size:22px}   /* 전고가 대비 하락률(▼) — 전고가 줄과 동일 크기 */
.ladder-title{margin-top:14px;margin-bottom:6px;font-size:22px;font-weight:700}
.ladder{margin-top:0}
.lvl{display:grid;grid-template-columns:52px 1fr 1.4fr 60px;gap:8px;
  align-items:center;padding:7px 0;border-bottom:1px solid #1a2231;font-size:14px}
.lvl:last-child{border-bottom:none}
.lvl .pct{font-weight:700;color:var(--muted);font-size:12px}   /* 한 단계 축소 — 긴 % 라벨이 달러 표시를 가리지 않도록 */
.lvl.hit .pct,.lvl.current .pct{color:var(--green)}
.lvl .val{color:var(--muted);font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px}
.lvl.hit{background:linear-gradient(90deg,rgba(63,185,80,.08),transparent 70%);border-radius:6px}
.lvl.current{background:linear-gradient(90deg,rgba(210,153,34,.10),transparent 70%);border-radius:6px}
.bar{height:6px;border-radius:3px;background:#1c2533;overflow:hidden}
.bar i{display:block;height:100%;border-radius:3px;background:var(--green)}
.lvl.current .bar i{background:var(--amber)}
.lvl .st{text-align:right;font-size:12px;font-weight:700}
.lvl.hit .st{color:var(--green)}   /* 경과 — 막대색(초록)과 동일 */
.lvl.current .st{color:var(--amber)}
.lvl.wait .st{color:#4b5563}
.lvl.wait .bar i{background:#2a3344}
footer{color:#4b5563;font-size:12px;text-align:center;margin-top:8px;line-height:1.8}
.legend{display:flex;gap:14px;justify-content:center;color:var(--muted);font-size:12px;margin-top:6px}
.pages a{color:var(--blue);text-decoration:none;font-weight:700;font-size:12px}
.pages a:active{opacity:.7}
.plan{margin-top:14px;background:#0f1420;border:1px solid var(--border);border-radius:12px;padding:12px}
.plan-row{display:flex;align-items:center;gap:8px;margin-bottom:8px}
.plan-row label{font-size:22px;color:var(--muted);width:168px;flex-shrink:0}
.plan-head{display:flex;align-items:baseline;gap:6px;flex-wrap:wrap;margin-bottom:8px}
.plan-head .pt{font-size:22px;font-weight:700;color:var(--text)}
.plan-head .ps{font-size:12px;color:#5b6572}
.plan-acc{display:flex;align-items:center;gap:8px;margin-bottom:6px}
.plan-hd{display:flex;align-items:center;gap:8px;margin-bottom:4px;font-size:11px;color:#5b6572}
.plan-hd .hd-no{width:26px;flex-shrink:0;text-align:center}
.plan-hd .lbl{flex:1;min-width:0}
.plan-hd .acc-sell{color:#5b6572;font-weight:400;width:94px}
.acc-no{width:26px;height:26px;flex-shrink:0;display:flex;align-items:center;justify-content:center;
  font-size:12px;font-weight:800;color:var(--muted);border:1px solid var(--border);
  border-radius:999px;background:#1c2533}
.plan-buy-input{flex:1;min-width:0;background:#1c2533;border:1px solid var(--border);border-radius:8px;
  color:var(--text);font-size:22px;font-weight:700;padding:7px 10px;font-family:ui-monospace,Menlo,Consolas,monospace}
.acc-sell{font-size:13px;font-weight:700;color:var(--text);font-family:ui-monospace,Menlo,Consolas,monospace;
  width:94px;text-align:right;flex-shrink:0}
.plan-unit{font-size:22px;color:var(--muted)}
.plan-pcts{display:flex;gap:6px;flex-wrap:wrap}
.pct{font-size:22px;font-weight:700;padding:7px 13px;border-radius:999px;
  border:1px solid var(--border);background:#1c2533;color:var(--muted);cursor:pointer;-webkit-tap-highlight-color:transparent}
.pct.on{border-color:var(--green);background:var(--green-dim);color:var(--green)}
.pct:active{opacity:.7}
.plan-out{display:flex;align-items:center;gap:8px;margin-top:10px}
.po{font-size:22px;color:var(--muted);font-weight:600}
.po b{color:var(--text);font-family:ui-monospace,Menlo,Consolas,monospace;font-size:22px;font-weight:800}
.sync-row{display:flex;align-items:center;gap:8px;margin-top:10px}
.sync-lbl{font-size:12px;color:var(--muted);flex-shrink:0}
.sync-key-wrap{flex:1;min-width:0;display:flex;align-items:center;gap:6px}
.sync-key-input{flex:1;min-width:0;background:#1c2533;border:1px solid var(--border);border-radius:8px;
  color:var(--text);font-size:14px;font-weight:600;padding:7px 10px;font-family:ui-monospace,Menlo,Consolas,monospace}
.sync-key-input::placeholder{color:#4b5563;font-weight:400}
.sync-toggle{flex-shrink:0;width:36px;height:36px;border-radius:8px;border:1px solid var(--border);
  background:#1c2533;color:var(--text);font-size:15px;cursor:pointer;line-height:1;-webkit-tap-highlight-color:transparent}
.sync-toggle:active{opacity:.7}
.sync-status{font-size:12px;font-weight:700;color:var(--green);flex-shrink:0;min-width:70px;text-align:right}
"""

# PWA 서비스 워커 등록 — Chrome '앱 설치' 기준 충족 (통과형 fetch, 캐시 없음)
# 5분 자동 새로고침 — meta refresh(<meta http-equiv="refresh">)는 설치형(standalone) 앱에서
# 창이 닫히고 Chrome 브라우저로 빠져나가는 안드로이드 문제가 있어 JS 방식으로 대체한다.
# (화면에 보일 때만 새로고침해 백그라운드에서 불필요한 갱신 방지)
_AUTO_RELOAD_JS = """
<script>
setTimeout(function () { if (!document.hidden) { location.reload(); } }, 300000);
</script>
"""


# 서비스워커는 OneSignal.init()이 /strategy_engine/OneSignalSDKWorker.js(?appId&sdkVersion)로 단일 등록한다.
# 별도로 등록하면 같은 스코프에 URL이 다른 워커가 매 페이지 로드마다 서로를 교체하는
# 'SW 교체 루프'가 생겨 앱이 불안정해지므로, 여기서는 등록하지 않는다. (OneSignal 등록이 곧 PWA 워커)
_SW_REGISTER = ""

# OneSignal 웹 푸시 — SDK 로드 + 구독 버튼 (알림 받기)
# - 하위 폴더(GitHub Pages /strategy_engine/) 배포를 위해 serviceWorkerPath/Scope 를
#   페이지 기준 상대 경로로 지정 (OneSignalSDKWorker.js 가 같은 폴더에 배포됨)
# - iOS(16.4+)는 홈 화면 추가 후 구독 가능 → 안내 문구 포함
_PUSH_SDK = """
<script src="https://cdn.onesignal.com/sdks/web/v16/OneSignalSDK.page.js" defer></script>
<script>
window.OneSignalDeferred = window.OneSignalDeferred || [];
OneSignalDeferred.push(async function(OneSignal) {
  const btn = document.getElementById('push-btn');
  try {
    await OneSignal.init({
      appId: "__OS_APP_ID__",
      // GitHub Pages 하위 폴더(project site) 배포 — SDK 소스 기준 정확한 조합:
      // ① serviceWorkerOverrideForTypical: true → 대시보드(Typical) 설정이어도 코드 옵션 우선
      // ② path: "/" + serviceWorkerPath(슬래시 없는 상대경로) → new URL(경로, location.origin)로
      //    /strategy_engine/OneSignalSDKWorker.js 조립 (절대경로 "/strategy_engine/..."는 path와 이중 결합됨)
      // ③ serviceWorkerParam.scope → v16의 정식 스코프 키 (serviceWorkerScope는 v16에서 미인식)
      serviceWorkerOverrideForTypical: true,
      path: "/",
      serviceWorkerPath: "strategy_engine/OneSignalSDKWorker.js",
      serviceWorkerParam: { scope: "/strategy_engine/" },
    });
    // init 성공 → 구독 상태 반영 + 버튼 활성화
    // v16(User Model)에서는 isPushEnabled()가 제거됨 → PushSubscription.optedIn 사용
    const isEnabled = OneSignal.User.PushSubscription.optedIn;
    if (btn) {
      btn.disabled = false;
      btn.textContent = isEnabled ? '🔔 알림 ON' : '🔔 알림 받기';
      if (isEnabled) btn.classList.add('on');
      btn.addEventListener('click', async function() {
        try {
          await OneSignal.Notifications.requestPermission();
        } catch (e) {
          // iOS: 홈 화면 추가 전이면 거부됨
        }
        const on = OneSignal.User.PushSubscription.optedIn;
        btn.textContent = on ? '🔔 알림 ON' : '🔔 알림 받기';
        if (on) btn.classList.add('on'); else btn.classList.remove('on');
      });
    }
    // (2026-08-12 단독 사용 전환: swing_zone_{TICKER} 태그 등록 제거 — 푸시가 태그 필터를
    // 쓰지 않아 불필요해짐. 전체 구독자 = 내 기기 대상 발송.)
  } catch (e) {
    // init/구독 조회 실패 시 원인을 화면+콘솔에 표시 (스마트폰 PWA에도 보이도록)
    console.error('OneSignal 초기화 실패:', e);
    if (btn) {
      btn.disabled = true;
      btn.textContent = '⚠️ 알림 설정 필요';
      btn.title = 'OneSignal 대시보드 웹 설정 확인: ' + (e && e.message ? e.message : e);
      // hover 없이도 보이는 화면 배너 (스마트폰/설치형 PWA 대응)
      const msg = (e && e.message) ? e.message : String(e);
      const banner = document.createElement('div');
      banner.className = 'push-err';
      banner.textContent = '🔕 푸시 설정 오류: ' + msg;
      btn.insertAdjacentElement('afterend', banner);
    }
  }
});
</script>
"""


# 계좌별 매수 예정가(사용자 입력) × (1 + 예상 수익률) → 계좌별 매도 예정가 자동 계산 (브라우저 localStorage 저장)
# 세븐 스플릿: 7개 계좌를 각각 추적 — 저장 키 swing_buy_{TICKER}_{ACCOUNT}, 푸시 태그 swing_sell_{TICKER}_{ACCOUNT}
# 매도 상태 칩/카운트는 사용자별 입력 기준으로 항상 재판정 — 서버 설정(BUY_PRICE) 없이도 동작한다.
# 구형 단일 키(swing_buy_{TICKER})는 1번 계좌로 자동 마이그레이션된다.
_PLAN_JS = """
<script>
(function() {
  // ── OneSignal 태그 일괄 동기화 (2026-08-12) ──
  // 입력 이벤트마다 개별 addTag를 호출하면 set-property PATCH가 폭주해
  // OneSignal 서버가 409 Conflict로 거부하던 문제 해결:
  // 입력 중에는 전송을 미루고, 입력이 멈춘 뒤(0.8s) 변경분만 addTags/removeTags로 한 번에 보낸다.
  // lastSentTags = 마지막으로 서버에 반영한 값 스냅샷 — 실제로 바뀐 키만 전송 (불필요 PATCH 제거)
  var lastSentTags = {};
  var tagTimer = null;
  function queueTagSync() {
    if (tagTimer) clearTimeout(tagTimer);
    tagTimer = setTimeout(flushTagSync, 800);
  }
  function flushTagSync() {
    tagTimer = null;
    var next = {};
    var remove = [];
    document.querySelectorAll('.card[data-ticker]').forEach(function(card) {
      var t = card.dataset.ticker;
      var sellPct = parseFloat(localStorage.getItem('swing_sell_' + t));
      if (isNaN(sellPct)) sellPct = parseFloat(card.dataset.sellDefault) || 10;
      next['swing_pct_' + t] = String(sellPct);
      card.querySelectorAll('.plan-acc').forEach(function(row) {
        var n = row.dataset.acc;
        var v = parseFloat(row.querySelector('.plan-buy-input').value);
        if (isNaN(v) || v <= 0) {
          // 미입력 계좌 — 태그 제거 (푸시/동기화 제외)
          remove.push('swing_buy_' + t + '_' + n);
          remove.push('swing_sell_' + t + '_' + n);
          if (n === '1') remove.push('swing_sell_' + t);
        } else {
          var sell = v * (1 + sellPct / 100);
          next['swing_buy_' + t + '_' + n] = String(v);
          next['swing_sell_' + t + '_' + n] = sell.toFixed(2);
          // 1번 계좌는 구형 단일 태그(swing_sell_{TICKER})에도 동일 기록 —
          // 아직 새 앱을 열지 않은 기기의 기존 태그와 호환
          if (n === '1') next['swing_sell_' + t] = sell.toFixed(2);
        }
      });
    });
    // 실제로 바뀐 키만 전송 (lastSentTags 대비)
    var add = {};
    Object.keys(next).forEach(function(k) { if (lastSentTags[k] !== next[k]) add[k] = next[k]; });
    var rm = remove.filter(function(k) { return lastSentTags[k] !== undefined; });
    if (Object.keys(add).length === 0 && rm.length === 0) return;
    if (!(window.OneSignalDeferred && window.OneSignalDeferred.push)) return;   // SDK 미로드 — 건너뜀
    window.OneSignalDeferred.push(function(OneSignal) {
      if (!OneSignal || !OneSignal.User) return;
      try {
        if (Object.keys(add).length && OneSignal.User.addTags) OneSignal.User.addTags(add);
        if (rm.length && OneSignal.User.removeTags) OneSignal.User.removeTags(rm);
      } catch (e) { /* OneSignal 미설정 — 무시 */ }
    });
    Object.keys(add).forEach(function(k) { lastSentTags[k] = add[k]; });
    rm.forEach(function(k) { delete lastSentTags[k]; });
  }

  // 카드별 저장 키: swing_buy_{TICKER}_{ACCOUNT}(계좌별 매수 예정가) / swing_sell_{TICKER}(예상 수익률, 카드 공용)
  function initSwingCard(card) {
    var ticker = card.dataset.ticker;
    var close = parseFloat(card.dataset.close);
    var gapPct = parseFloat(card.dataset.gap) || 5;
    var accRows = Array.prototype.slice.call(card.querySelectorAll('.plan-acc'));
    var pctBtns = card.querySelectorAll('.pct');
    var chip = card.querySelector('[data-sell-chip]');

    // 예상 수익률 → 서버 SWING_TARGET_PCT 기본값
    var sellPct = parseFloat(localStorage.getItem('swing_sell_' + ticker));
    if (isNaN(sellPct)) sellPct = parseFloat(card.dataset.sellDefault) || 10;

    // 계좌별 초기값 로드 — 1번 계좌는 구형 단일 키(swing_buy_{TICKER}) 폴백 (자동 마이그레이션)
    accRows.forEach(function(row) {
      var n = row.dataset.acc;
      var v = parseFloat(localStorage.getItem('swing_buy_' + ticker + '_' + n));
      if (isNaN(v) && n === '1') v = parseFloat(localStorage.getItem('swing_buy_' + ticker));
      var input = row.querySelector('.plan-buy-input');
      if (!isNaN(v) && v > 0) input.value = v;
    });

    // 페이지 로드 시 최초 판정에서만 진동 — 매수 예정가 입력/수익률 버튼 조작으로 상태가
    // 오락가락해도 잡음 진동이 울리지 않게 한다 (시장 가격은 서버 렌더 → 새로고침 시 갱신).
    var vibed = false;

    // 매도 알람/임박 시 스마트폰 진동 (Vibration API) — Android Chrome만 지원, iOS는 미지원(무시).
    // localStorage(swing_vibe_{TICKER})에 직전 상태를 기록해 5분 자동 새로고침 등으로 같은 상태가
    // 반복돼도 재진동하지 않는다. 🚨 매도는 강한 3연타, 🚀 임박은 짧은 2연타.
    function vibrateSell(stateClass) {
      if (vibed) return;
      vibed = true;
      try {
        if (!navigator.vibrate) return;                      // 미지원 브라우저
        var key = 'swing_vibe_' + ticker;
        var prev = localStorage.getItem(key);
        localStorage.setItem(key, stateClass);
        if (prev === stateClass) return;                     // 동일 상태 반복 — 재진동 없음
        if (stateClass === 'red' && prev !== 'red') {
          navigator.vibrate([300, 100, 300, 100, 600]);      // 🚨 매도 — 강한 3연타
        } else if (stateClass === 'amber' && prev !== 'red') {
          navigator.vibrate([150, 100, 300]);                // 🚀 임박 — 짧은 2연타
        }
      } catch (e) { /* 진동 미지원 — 무시 */ }
    }

    // 계좌 1개의 매도 상태 — 입력된 매수 예정가 × (1 + 예상 수익률) 기준으로 항상 재판정.
    // 미입력 계좌(active=false)는 계산/저장/태그에서 제외 — 매도 예정가도 비워둔다 (푸시 오발송 방지).
    function rowState(row) {
      var n = row.dataset.acc;
      var v = parseFloat(row.querySelector('.plan-buy-input').value);
      if (isNaN(v) || v <= 0) {
        // 미입력 — 매수/매도 예정가 모두 비움 (저장/태그 제외). 매수 예정가 입력 시에만 자동 계산.
        return { n: n, active: false, sell: null };
      }
      var sell = v * (1 + sellPct / 100);
      var remain = (sell - close) / sell * 100;   // 목표까지 남은 % (양수)
      var state = 'gray';
      if (close >= sell - 1e-9) state = 'red';
      else if (remain <= gapPct) state = 'amber';
      return { n: n, active: true, buy: v, sell: sell, state: state };
    }

    // 카드 상단 칩 — 입력된 계좌 중 하나라도 매도 도달(red) → 🚨, 임박(amber) → 🚀, 없으면 숨김
    function applyChip(anyRed, anyAmber) {
      if (!chip || isNaN(close)) return;
      chip.classList.remove('red', 'amber', 'gray');
      if (anyRed) {
        chip.classList.add('red');
        chip.textContent = '🚨 매도';
        chip.style.display = '';
      } else if (anyAmber) {
        chip.classList.add('amber');
        chip.textContent = '🚀 임박';
        chip.style.display = '';
      } else {
        chip.classList.add('gray');
        chip.textContent = '⏳ 대기';
        chip.style.display = 'none';                   // 기본(대기) 상태 — 칩 숨김
      }
      vibrateSell(anyRed ? 'red' : (anyAmber ? 'amber' : 'gray'));
    }

    // 상단 '🚨 매도 알람 N' — 사용자별 판정 결과로 카운트 갱신
    function refreshAlarmCount() {
      var cnt = 0;
      document.querySelectorAll('[data-sell-chip]').forEach(function(c) {
        if (c.classList.contains('red')) cnt++;
      });
      var el = document.getElementById('sell-alarm-cnt');
      if (el) {
        el.textContent = '🚨 매도 알람 ' + cnt;
        el.classList.remove('red', 'gray');
        el.classList.add(cnt ? 'red' : 'gray');
      }
    }

    function update() {
      if (isNaN(close)) return;   // 가격 데이터 없으면 계산 생략
      // 예상 수익률은 localStorage가 단일 소스 — 태그 동기화로 재초기화된 뒤에도
      // 기존(구) 클로저의 update()가 호출되면 최신 값으로 재계산한다 (채택 값이 뒤집히는 것 방지)
      sellPct = parseFloat(localStorage.getItem('swing_sell_' + ticker));
      if (isNaN(sellPct)) sellPct = parseFloat(card.dataset.sellDefault) || 10;
      var anyRed = false, anyAmber = false;
      accRows.forEach(function(row) {
        var sellEl = row.querySelector('.acc-sell');
        var r = rowState(row);
        if (!r.active) {
          // 미입력 — 매도 예정가 비움 (저장/태그 제외 — flushTagSync가 태그 정리)
          sellEl.textContent = '—';
          localStorage.removeItem('swing_buy_' + ticker + '_' + r.n);
          return;
        }
        sellEl.textContent = '$' + r.sell.toFixed(2);
        localStorage.setItem('swing_buy_' + ticker + '_' + r.n, String(r.buy));
        if (r.state === 'red') anyRed = true;
        else if (r.state === 'amber') anyAmber = true;
      });
      // 구형 단일 키는 1번 계좌로 마이그레이션 완료 후 정리
      localStorage.removeItem('swing_buy_' + ticker);
      localStorage.setItem('swing_sell_' + ticker, String(sellPct));
      queueTagSync();   // 🔄 태그 동기화 — 입력이 멈춘 뒤(0.8s) 변경분만 일괄 전송 (409 폭주 방지)
      pctBtns.forEach(function(b) {
        b.classList.toggle('on', parseFloat(b.dataset.pct) === sellPct);
      });
      applyChip(anyRed, anyAmber);
      refreshAlarmCount();
    }

    // 이벤트 리스너는 1회만 등록 — 태그 동기화 후 재초기화(adoptTags → initSwingCard) 시 중복 방지
    if (!card.dataset.inited) {
      accRows.forEach(function(row) {
        var inp = row.querySelector('.plan-buy-input');
        inp.addEventListener('input', update);
        // blur/Enter 시에도 재동기화 — 409 등으로 직전 flush가 실패해도 다음 기회 (2026-08-12)
        inp.addEventListener('change', function() { queueTagSync(); });
      });
      pctBtns.forEach(function(b) {
        b.addEventListener('click', function() {
          // localStorage에 먼저 기록 후 update() — update()가 localStorage를 재읽으므로
          // 어느 클로저가 호출돼도 같은 값으로 계산된다 (재초기화 안전).
          localStorage.setItem('swing_sell_' + ticker, String(parseFloat(b.dataset.pct)));
          update();
        });
      });
      card.dataset.inited = '1';
    }
    update();
  }

  // OneSignal 태그 → 로컬 채택 — 태그가 있으면 태그 값을 우선 적용 (마지막 변경 기기 기준).
  // 기기별 localStorage 값과 다를 때만 재초기화해 화면/매도 푸시 태그를 태그 기준으로 맞춘다.
  function adoptTags(OneSignal) {
    try {
      if (!OneSignal || !OneSignal.User || !OneSignal.User.getTags) return;
      return OneSignal.User.getTags().then(function(tags) {
        if (!tags) return;
        // 서버 태그 스냅샷 초기화 — 이미 서버에 있는 값은 재전송하지 않는다 (409 폭주 방지)
        Object.keys(tags).forEach(function(k) { if (k.indexOf('swing_') === 0) lastSentTags[k] = tags[k]; });
        var changed = false;
        document.querySelectorAll('.card[data-ticker]').forEach(function(card) {
          var t = card.dataset.ticker;
          var pv = tags['swing_pct_' + t];
          if (pv !== undefined) {
            var p = parseFloat(pv);
            if (!isNaN(p) && p > 0 && p <= 100) {
              if (localStorage.getItem('swing_sell_' + t) !== String(p)) changed = true;
              localStorage.setItem('swing_sell_' + t, String(p));
            }
          }
          card.querySelectorAll('.plan-acc').forEach(function(row) {
            var key = 'swing_buy_' + t + '_' + row.dataset.acc;
            var tv = tags[key];
            if (tv !== undefined) {
              var v = parseFloat(tv);
              if (!isNaN(v) && v > 0 && localStorage.getItem(key) !== String(v)) {
                localStorage.setItem(key, String(v));
                changed = true;
              }
            }
          });
        });
        if (changed) document.querySelectorAll('.card[data-ticker]').forEach(initSwingCard);
      });
    } catch (e) { /* OneSignal 미설정 — 기기별 동작 유지 */ }
  }

  // 🔄 동기화 코드 — 두 기기에 같은 코드를 입력하면 OneSignal 외부 ID로 연결돼 태그가 공유된다.
  // 연결된 기기에서는 예상 수익률/매수 예정가 태그가 로컬보다 우선이라 매도 예정가가 자동 일치한다.
  var keyInput = document.getElementById('sync-key-input');
  var syncStatus = document.getElementById('sync-status');
  var appliedKey = null;
  function applySyncKey(key) {
    key = (key || '').trim();
    localStorage.setItem('swing_sync_key', key);
    if (key === appliedKey) return;          // 같은 코드 재입력 — 중복 login 방지
    appliedKey = key;
    if (syncStatus) syncStatus.textContent = '연결 중…';
    window.OneSignalDeferred.push(async function(OneSignal) {
      try {
        if (!OneSignal || !OneSignal.User) return;
        if (key) {
          // 외부 ID 로그인 — 동일 코드를 쓴 기기들이 한 사용자로 병합 (v16: login / addAlias)
          if (OneSignal.login) await OneSignal.login(key);
          else if (OneSignal.User.addAlias) OneSignal.User.addAlias('external_id', key);
        } else if (OneSignal.logout) {
          await OneSignal.logout();          // 코드 삭제 — 연결 해제 (기기별 저장으로 복귀)
        }
        await adoptTags(OneSignal);          // 연결 직후 태그 반영
        if (syncStatus) syncStatus.textContent = key ? '✓ 연결됨' : '';
      } catch (e) {
        if (syncStatus) syncStatus.textContent = '⚠️ 연결 실패';
      }
    });
  }
  if (keyInput) {
    var savedKey = localStorage.getItem('swing_sync_key') || '';
    keyInput.value = savedKey;
    keyInput.addEventListener('change', function() { applySyncKey(keyInput.value); });
    if (savedKey) applySyncKey(savedKey);    // 저장된 코드 — 로드 시 자동 연결
  }
  // 🔒 코드 마스킹 토글 — 기본은 가려짐(password), 눈 아이콘으로 잠시 표시 (어깨 너머 노출 방지)
  var syncToggle = document.getElementById('sync-key-toggle');
  if (keyInput && syncToggle) {
    syncToggle.addEventListener('click', function() {
      var show = keyInput.type === 'password';
      keyInput.type = show ? 'text' : 'password';
      syncToggle.textContent = show ? '🙈' : '👁';
      keyInput.focus();
    });
  }

  // 탭이 다시 보일 때 태그 재동기화 — 세션 중 실패한 flush(409 등)의 재시도 경로 (2026-08-12)
  document.addEventListener('visibilitychange', function() {
    if (!document.hidden) queueTagSync();
  });

  // 카드 초기화 — 첫 로드 + 태그 채택 후 재판정
  document.querySelectorAll('.card[data-ticker]').forEach(initSwingCard);

  // SDK init 완료 후 태그 읽기 — 기기별 값이 태그와 다르면 태그를 따르도록 채택
  if (window.OneSignalDeferred && window.OneSignalDeferred.push) {
    window.OneSignalDeferred.push(async function(OneSignal) {
      if (!OneSignal || !OneSignal.User || !OneSignal.User.getTags) return;
      await adoptTags(OneSignal);
    });
  }
})();
</script>
"""


def _lvl_row(lvl: dict, next_fill: float | None = None) -> str:
    """래더 1줄 — hit: 초록 100% / next: 호박색 진행바(다음 구간 접근도) / wait: 회색."""
    if lvl["hit"]:
        cls, st_txt, fill = "hit", "경과", 100.0
    elif next_fill is not None:
        cls, st_txt, fill = "current", "대기", next_fill
    else:
        cls, st_txt, fill = "wait", "대기", 0.0
    return (
        f'<div class="lvl {cls}">'
        f'<span class="pct">-{lvl["pct"]:.0f}%</span>'
        f'<span class="val">${lvl["price"]:,.2f}</span>'
        f'<div class="bar"><i style="width:{fill:.0f}%"></i></div>'
        f'<span class="st">{st_txt}</span></div>'
    )


def render_dashboard(statuses: list[dict], cfg: dict, updated_at: str, as_of_ny: str) -> str:
    """스마트폰용 자체 완결 HTML 대시보드 생성 (외부 리소스 없음)."""
    cards = []
    # 개인 포지션은 공용 대시보드의 서버 렌더 매도 칩/카운트에서 제외 — 앱의 매도 상태는
    # 사용자별 localStorage 기준으로 JS(_PLAN_JS)가 항상 재판정하므로 서버 값은 필요 없다.
    sell_cnt = sum(1 for s in statuses
                   if s.get("sell_ready") and not s.get("error") and not s.get("personal"))
    buy_cnt = sum(1 for s in statuses if s.get("deepest_hit") and not s.get("error"))
    any_live = any(s.get("live") for s in statuses if not s.get("error"))
    pages_url = (cfg.get("PAGES_URL") or "").strip()
    live_link = (
        f'<span class="pages">· <a href="{pages_url}" target="_blank" rel="noopener">🌐 라이브 열기</a></span>'
        if pages_url else ""
    )
    # OneSignal 웹 푸시 — APP_ID 설정 시 SDK + 알림 받기 버튼 활성화
    app_id = (cfg.get("ONESIGNAL_APP_ID") or "").strip()
    push_sdk = _PUSH_SDK.replace("__OS_APP_ID__", app_id) if app_id else ""
    push_btn = (
        '<button id="push-btn" class="push-btn" disabled>🔔 알림 받기</button>'
        if app_id else ""
    )
    # 🔄 기기 간 동기화 코드 행 (2026-08-12) — OneSignal APP_ID 설정 시에만 표시.
    # 같은 코드를 두 기기에 입력하면 OneSignal 외부 ID로 연결되어 예상 수익률/매수 예정가 태그가 공유된다.
    # 🔒 코드는 마스킹(password) 표시 — 어깨 너머 노출 방지. 눈 아이콘 토글로 잠시 확인 가능 (2026-08-12)
    sync_row = (
        '<div class="sync-row">'
        '<span class="sync-lbl">🔄 동기화 코드</span>'
        '<div class="sync-key-wrap">'
        '<input id="sync-key-input" class="sync-key-input" type="password" maxlength="40" '
        'autocomplete="off" autocapitalize="off" spellcheck="false" '
        'placeholder="두 기기에 같은 코드를 입력하면 자동 동기화">'
        '<button type="button" id="sync-key-toggle" class="sync-toggle" title="코드 보기/숨기기">👁</button>'
        '</div>'
        '<span class="sync-status" id="sync-status"></span>'
        '</div>'
    ) if app_id else ""

    for st in statuses:
        if st.get("error"):
            cards.append(
                f'<div class="card"><div class="row"><span class="tick">{st["ticker"]}</span>'
                f'<span class="chip red">❌ {st["error"]}</span></div></div>'
            )
            continue

        # 전고가 대비 하락률 — 장중엔 라이브 가격 기준으로 표시 (표시 전용, 알림 판정은 종가 기준)
        dd_pct_disp = st.get("live_dd_pct", st["dd_pct"])
        dd_cls = "up" if dd_pct_disp > 0 else ("down" if dd_pct_disp < 0 else "flat")
        dd_sign = "🆕 +" if dd_pct_disp > 0 else ("▼ " if dd_pct_disp < 0 else "")

        # 전일 종가 대비 등락률 — 전일 종가 줄 끝 표시 (ATH 하락률과 별개 수치)
        dc = st.get("day_change_pct")
        if dc is None:
            day_span = ""
        else:
            day_cls = "up" if dc > 0 else ("down" if dc < 0 else "flat")
            day_sign = "▲ " if dc > 0 else ("▼ " if dc < 0 else "")
            day_span = f' 대비 <span class="dd {day_cls}">{day_sign}{abs(dc):.1f}%</span>'
        # 현재가 출처 줄 — 종가 기준이면 실제 직전 거래일 종가만 표시한다 (2026-08-12).
        # 현재 종가는 바로 위 큰 가격(36px)에 이미 표시되므로 메타 줄에서 반복하지 않는다
        # ('종가 $X (date)' 중복 제거). 전일 종가 조회 실패 시에만 현재 종가+날짜로 폴백.
        if st.get("live"):
            # 장중 실시간 줄에도 전일 종가 값을 함께 표시 — 비교 기준이 보이도록 (2026-08-12)
            pc = st.get("prior_close")
            if pc and pc > 0:
                meta_src = (f'🟢 실시간 ${st["price"]:,.2f} ({st["as_of"]}) · '
                            f'전일 종가 ${pc:,.2f}')
            else:
                meta_src = f'🟢 실시간 ${st["price"]:,.2f} ({st["as_of"]})'
        else:
            pc = st.get("prior_close")
            if pc and pc > 0:
                pcd = st.get("prior_close_date") or ""
                meta_src = f'전일 종가 ${pc:,.2f}' + (f' ({pcd})' if pcd else '')
            else:
                meta_src = f'종가 ${st["price"]:,.2f} ({st["as_of"]})'
        imminent = (not st["sell_ready"] and st["sell_target"] is not None
                    and st["sell_gap_pct"] is not None
                    and st["sell_gap_pct"] <= float(cfg.get("IMMINENT_GAP_PCT", 5)))
        # 기본(대기) 상태는 칩을 숨긴다 — 매도 예정가는 하단 계획 섹션에 항상 표시되므로
        # 화면의 고정 노이즈(⏳ 대기)를 제거하고 🚨 매도 / 🚀 임박 상태만 보여준다.
        sell_chip = (
            '<span class="chip gray" data-sell-chip>매도 미설정</span>' if st.get("personal")
            else '<span class="chip red" data-sell-chip>🚨 매도</span>' if st["sell_ready"]
            else '<span class="chip amber" data-sell-chip>🚀 임박</span>' if imminent
            else '<span class="chip gray" data-sell-chip>매도 미설정</span>' if st["sell_target"] is None
            else '<span class="chip gray" data-sell-chip style="display:none">⏳ 대기</span>'
        )



        # 다음 구간 접근 진행도: 현재 구간 상단(0%) → 다음 구간(100%)
        step = int(cfg.get("MDD_STEP_PCT", 5))
        nxt = st["next_zone"]
        next_fill = None
        if nxt:
            cur_pct = nxt["pct"] - step                       # 방금 도달한 구간 (현재 위치)
            top = st["ath"] * (1 - cur_pct / 100.0)           # 현재 구간 상단 가격
            bot = nxt["price"]                                # 다음 구간 가격
            if top > bot:
                next_fill = min(max((top - st["price"]) / (top - bot) * 100, 0), 100)

        rows = "".join(
            _lvl_row(lvl, next_fill if (nxt and lvl["pct"] == nxt["pct"]) else None)
            for lvl in st["ladder"]
        )

        # 계좌별 매수 예정가 입력 7행 — 세븐 스플릿 7개 계좌(각 $500). 미입력 계좌는 매수/매도
        # 예정가를 모두 비워두고(현재가 자동 표시 없음), 매수 예정가를 입력한 계좌만 매도 예정가
        # 자동 계산 + OneSignal 태그(swing_sell_{TICKER}_{N}) 동기화. (2026-08-12: 미입력 = 현재가
        # 기준 매도 예정가 표시 제거 — 실제 매수한 계좌만 입력하도록 라벨 변경)
        acc_rows = (
            '    <div class="plan-hd">\n'
            '      <span class="hd-no">#</span>\n'
            '      <span class="lbl">매수한 계좌만 입력 요망</span>\n'
            '      <span class="acc-sell">매도 예정가</span>\n'
            '    </div>\n'
        ) + "\n".join(
            f'''    <div class="plan-acc" data-acc="{n}">
      <span class="acc-no">{n}</span>
      <input class="plan-buy-input" type="number" min="0" step="0.01">
      <span class="acc-sell">—</span>
    </div>'''
            for n in range(1, 8)
        )

        cards.append(f"""
<div class="card" data-ticker="{st["ticker"]}" data-ath="{st["ath"]:.2f}" data-close="{st["price"]:.2f}" data-gap="{float(cfg.get("IMMINENT_GAP_PCT", 5)):g}" data-sell-default="{float(cfg.get("SWING_TARGET_PCT", 10)):g}">
  <div class="row">
    <span class="tick">{st["ticker"]}</span>
    {sell_chip}
  </div>
  <div class="price mono">${st["price"]:,.2f}</div>
  <div class="meta">{meta_src}{day_span}</div>
  <div class="row" style="margin-top:10px;justify-content:flex-end">
    <span class="chip {'green' if st['deepest_hit'] else 'gray'}">
      {'🟢 매수 구간 ' + str(len([l for l in st['ladder'] if l['hit']])) + '개 경과' if st['deepest_hit'] else '매수 구간 대기'}</span>
  </div>
  <div class="info">📊 전고가 ${st["ath"]:,.2f} ({st["ath_date"]}) 대비 <span class="dd {dd_cls}">{dd_sign}{abs(dd_pct_disp):.1f}%</span></div>
  <div class="plan">
    <div class="plan-head">
      <span class="pt">💰 계좌별 매수 예정가</span>
      <span class="ps">세븐 스플릿 — 7개 계좌</span>
    </div>
{acc_rows}
    <div class="plan-row">
      <label>📈 예상 수익률</label>
      <div class="plan-pcts">
        <button type="button" class="pct" data-pct="5">5%</button>
        <button type="button" class="pct" data-pct="10">10%</button>
        <button type="button" class="pct" data-pct="15">15%</button>
        <button type="button" class="pct" data-pct="20">20%</button>
      </div>
    </div>
  </div>
  <div class="ladder-title">📉 매수 구간 (전고가 대비 MDD)</div>
  <div class="ladder">{rows}</div>
</div>""")

    legend = ('<div class="legend"><span>🟢 경과</span>'
              '<span>🟡 대기</span></div>')

    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0b0e14">
{_AUTO_RELOAD_JS}
<!-- PWA: 홈 화면 추가(앱처럼 설치) 지원 -->
<link rel="manifest" href="manifest.webmanifest">
<link rel="icon" type="image/png" href="swing_icon.png?v=3">
<link rel="apple-touch-icon" href="swing_icon.png?v=3">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="스윙 알리미">
<meta name="mobile-web-app-capable" content="yes">
<title>스윙 투자 알리미</title>
{_SW_REGISTER}
{push_sdk}
<style>{_CSS}</style>
</head>
<body>
<header>
  <h1>📈 스윙 투자 알리미</h1>
  <div class="sub">업데이트 {updated_at} · {'실시간(15분 지연) 기준' if any_live else '종가 기준'} {as_of_ny} (미국){live_link}</div>
  <div class="chips">
    {push_btn}
    <span class="chip {'red' if sell_cnt else 'gray'}" id="sell-alarm-cnt">🚨 매도 알람 {sell_cnt}</span>
    <span class="chip {'green' if buy_cnt else 'gray'}">🟢 매수 경과 {buy_cnt}</span>
  </div>
  {sync_row}
</header>
<main>{''.join(cards)}</main>
{legend}
<footer>⚠️ 신호 알림·계산기용 — 자동매매가 아닙니다. 실제 매매는 본인이 직접 하세요.<br>
출처: 유튜브 'TQQQ 스윙 투자 전략' 스프레드시트 방식 재구현</footer>
{_PLAN_JS}
</body>
</html>"""


def write_dashboard(statuses: list[dict], cfg: dict, path: str) -> None:
    now_ny = datetime.now(NY_TZ)
    html = render_dashboard(
        statuses, cfg,
        updated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        as_of_ny=now_ny.strftime("%Y-%m-%d %H:%M"),
    )
    # 원자적 쓰기(임시 파일 → rename) — 스크립트가 쓰기 중 중단돼도 잘린 HTML이
    # 남지 않아, 봇이 배포용으로 복사하는 /tmp 사본이 깨질 일이 없다.
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html)
    os.replace(tmp, path)
    print(f"✅ 대시보드 저장: {path}")


# ═══════════════════════════════════════════════════════════
# 모바일 대시보드 서버 (--serve)
# ═══════════════════════════════════════════════════════════

_LAST_COMPUTE: dict = {"ts": 0.0, "statuses": None, "cfg": None}

# --serve 에서 함께 제공하는 정적 파일 (URL → (content_type, 로컬 파일명))
# 배포(gh-pages)에서는 manifest.webmanifest 로 이름이 바뀌므로 로컬 파일명을 별도 지정
_STATIC_FILES = {
    "/manifest.webmanifest": ("application/manifest+json; charset=utf-8", "swing_manifest.webmanifest"),
    "/swing_icon.png": ("image/png", "swing_icon.png"),
    "/swing_icon_192.png": ("image/png", "swing_icon.png"),
    "/sw.js": ("application/javascript; charset=utf-8", "sw.js"),
    "/OneSignalSDKWorker.js": ("application/javascript; charset=utf-8", "OneSignalSDKWorker.js"),
}


def _compute_all(cfg: dict, force: bool = False, live: bool = False) -> list[dict]:
    """전 티커 상태 계산 (60초 캐시 — 서버 폴링 부하 방지).

    live=True 면 장중 표시 가격을 실시간(15분 지연)으로 오버레이한다 (대시보드용).
    알림(--monitor) 경로는 live=False 로 종가 기준을 유지한다.
    """
    now = time.time()
    # 캐시 키에 live 플래그 포함 — 같은 60초 안에 live=True/False 호출이 섞여도
    # 잘못된 변형(실시간/종가)을 반환하지 않도록 한다. (2026-08-11)
    if not force and _LAST_COMPUTE["statuses"] is not None \
            and _LAST_COMPUTE.get("live") == live and now - _LAST_COMPUTE["ts"] < 60:
        return _LAST_COMPUTE["statuses"]
    statuses = []
    for ticker, pos in cfg.get("POSITIONS", {}).items():
        if not pos.get("ENABLED", True):
            continue
        statuses.append(compute_ticker(ticker, pos, cfg, live=live))
    _LAST_COMPUTE.update(ts=now, statuses=statuses, cfg=cfg, live=live)
    return statuses


class _DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # 요청 로그 최소화
        pass

    def _json(self, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        # PWA 정적 파일 (매니페스트/아이콘) 서빙
        sf = _STATIC_FILES.get(self.path.split("?", 1)[0])
        if sf and os.path.isfile(sf[1]):
            with open(sf[1], "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", sf[0])
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        cfg = load_config()
        statuses = _compute_all(cfg, live=True)
        if self.path.startswith("/api/status"):
            self._json({"updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "positions": statuses})
            return
        now_ny = datetime.now(NY_TZ)
        html = render_dashboard(
            statuses, cfg,
            updated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
            as_of_ny=now_ny.strftime("%Y-%m-%d %H:%M"),
        )
        self._html(html.encode("utf-8"))


def run_serve(port: int) -> None:
    server = ThreadingHTTPServer(("0.0.0.0", port), _DashboardHandler)
    print(f"🖥  스윙 알리미 대시보드 서버 시작 (포트 {port})")
    print("   스마트폰에서 같은 Wi-Fi로 접속:")
    try:
        import socket
        ips: set[str] = set()
        try:
            hostname = socket.gethostname()
            for ip in socket.gethostbyname_ex(hostname)[2]:
                ips.add(ip)
        except Exception:
            pass
        # UDP 커넥트로 실제 LAN IP 탐색 (호스트명 조회가 loopback만 주는 환경 대비)
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ips.add(s.getsockname()[0])
            s.close()
        except Exception:
            pass
        found = False
        for ip in sorted(ips):
            if ip.startswith(("192.168.", "10.", "172.")) and not ip.startswith("127."):
                print(f"   📱 http://{ip}:{port}")
                found = True
        if not found:
            print(f"   📱 http://<이 PC의 LAN IP>:{port}")
    except Exception:
        print(f"   📱 http://<이 PC의 LAN IP>:{port}")
    print("   (이 PC에서: http://localhost:{port} | Ctrl+C 로 종료)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n🛑 서버 종료")


# ═══════════════════════════════════════════════════════════
# 콘솔 출력 / 메인
# ═══════════════════════════════════════════════════════════

def print_console(statuses: list[dict], cfg: dict) -> None:
    gap = float(cfg.get("IMMINENT_GAP_PCT", 5))
    target_pct = float(cfg.get("SWING_TARGET_PCT", 10))
    print(f"\n📊 스윙 투자 알리미 — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)
    for st in statuses:
        if st.get("error"):
            print(f"❌ {st['ticker']}: {st['error']}")
            continue
        src = " · 실시간" if st.get("live") else ""
        print(f"[{st['ticker']}] 현재 ${st['price']:,.2f} ({st['as_of']}{src})")
        print(f"   ATH ${st['ath']:,.2f} ({st['ath_date']}) → 하락 {st['dd_pct']:+.1f}%")
        lots = st.get("lots") or []
        if st.get("personal"):
            if lots:
                print(f"   🎯 계좌별 매도 목표 (매수가 × {target_pct:+.0f}%) 🔒 개인")
                for lot in lots:
                    if not lot.get("sell_target"):
                        print(f"      {lot['account']}번 계좌: 매수 미입력")
                        continue
                    qty = f" × {lot['shares']:.0f}주" if lot.get("shares") else ""
                    print(f"      {lot['account']}번 계좌: 매수가 ${lot['buy_price']:,.2f}{qty}"
                          f" → 목표 ${lot['sell_target']:,.2f} ({_lot_chip(lot, gap)})")
            else:
                print(f"   🎯 계좌별 매도 목표 미입력 🔒 개인 (swing_personal.json LOTS — 실제 매수 후 매수가/수량 입력)")
        elif st["sell_target"] is not None:
            print(f"   🎯 매도 목표 ${st['sell_target']:,.2f} (매수가 ${st['buy_price']:,.2f} × {target_pct:+.0f}%) | {_sell_chip(st, gap)}")
        else:
            print("   🎯 매도 목표 미설정 (BUY_PRICE 입력 필요 — 실제 매수 후 설정)")
        print(f"   📊 {_ladder_summary(st)}")
        if not st.get("personal") and st["exp_profit"] is not None:
            print(f"   💰 매수 ${st['buy_price']:,.2f} × {st['shares']:.0f}주 → +${st['exp_profit']:,.2f} ({st['exp_roi']:+.1f}%)")
    print("=" * 60)


def auto_cycle_reset(st: dict, pos: dict) -> tuple[str | None, bool]:
    """전 계좌 매도 목표 도달(사이클 완료) 시 알림 상태 자동 리셋 (2026-08-11).

    LOTS 의 모든 계좌가 매도 목표에 도달(sell_ready)하면 reset_position() 과 동일한
    초기화(SELL 플래그/ZONE_ALERTS/ATH_CYCLE_BASE)를 자동 수행한다 — 매도 후 수동
    --reset 없이 다음 하락 사이클의 구간 도달/임박 알림이 다시 울리도록 재무장한다.

    - '매도 목표 도달'은 신호(종가 기준)일 뿐 실제 체결 여부는 봇이 모르므로,
      목표 도달만으로 리셋한다. LOTS(swing_personal.json)는 절대 건드리지 않는다.
    - CYCLE_RESET_DONE(봇 상태)로 중복 방지 — 리셋 후 LOTS 를 새 포지션으로 갱신해
      '미도달' 상태가 되면 자동 재무장되어 다음 사이클에서 다시 감지한다.
    - 엣지(허용): 리셋 후 LOTS 를 갱신하지 않은 채 가격이 매도 목표선을 찔렀다가 다시
      넘으면 재리셋이 한 번 더 발화할 수 있다 — 동작은 수동 --reset 과 동일하며 무해.
      LOTS 를 새 포지션으로 갱신하면 정상 재무장된다.
    - 미입력 계좌만 있거나 포지션(LOTS)이 없으면 아무것도 하지 않는다.

    반환: (알림 메시지 또는 None, 상태 변경 여부 — 변경 시 호출자가 저장해야 함)
    """
    if st.get("error"):
        return None, False
    open_lots = [l for l in (st.get("lots") or []) if l.get("sell_target")]
    if not open_lots:
        return None, False   # 매도 목표가 설정된 계좌 없음 — 판단 불가
    all_sold = all(l.get("sell_ready") for l in open_lots)
    if pos.get("CYCLE_RESET_DONE"):
        if not all_sold:
            pos["CYCLE_RESET_DONE"] = False   # 재무장 — 다음 사이클에서 다시 감지
            return None, True
        return None, False   # 이미 이번 사이클에 리셋 완료 — 중복 방지
    if not all_sold:
        return None, False
    # --reset 과 동일한 초기화
    pos["SELL_ALARM_SENT"] = False
    pos["SELL_IMMINENT_SENT"] = False
    pos["ZONE_ALERTS"] = {"hit": [], "imminent": []}
    pos.pop("ATH_CYCLE_BASE", None)
    pos.pop("ZONE_PUSH_PENDING", None)   # 이전 사이클의 미전송 푸시 대기 큐 정리 (2026-08-11)
    pos["CYCLE_RESET_DONE"] = True
    return (f"🔄 **{st['ticker']} 전 계좌 매도 목표 도달 — 새 사이클 자동 리셋 완료**\n"
            "   매수 구간/매도 알림 상태가 초기화되었습니다.\n"
            "   새 포지션을 swing_personal.json 과 앱에 기록하고 새 사이클을 시작하세요."), True


def reset_position(ticker: str) -> None:
    cfg = load_config()
    if ticker not in cfg.get("POSITIONS", {}):
        raise SystemExit(f"❌ POSITIONS 에 '{ticker}' 이(가) 없습니다: {CONFIG_PATH}")
    pos = cfg["POSITIONS"][ticker]
    pos["SELL_ALARM_SENT"] = False
    pos["SELL_IMMINENT_SENT"] = False
    pos["ZONE_ALERTS"] = {"hit": [], "imminent": []}
    pos.pop("ATH_CYCLE_BASE", None)   # 새 사이클 기준도 초기화 (다음 실행에서 재설정)
    pos.pop("ZONE_PUSH_PENDING", None)  # 미전송 매수 구간 푸시 대기 큐 정리 (2026-08-11)
    pos["CYCLE_RESET_DONE"] = False  # 자동 리셋 감지 재무장 (2026-08-11)
    save_config(cfg)
    print(f"✅ {ticker} 알림 플래그 초기화 완료 — 새 포지션 진입 후 사용하세요.")


def main() -> None:
    parser = argparse.ArgumentParser(description="스윙 투자 알리미 (MDD 기반 매수/매도 알림 + 모바일 대시보드)")
    parser.add_argument("--discord", action="store_true", help="상태 출력 + Discord 일일 브리핑 발송")
    parser.add_argument("--monitor", action="store_true", help="실시간 모니터 — 변경분(매수/임박/매도) 알림만 발송")
    parser.add_argument("--serve", nargs="?", const=8080, type=int, metavar="PORT",
                        help="스마트폰용 대시보드 HTTP 서버 실행 (기본 포트 8080)")
    parser.add_argument("--reset", metavar="TICKER", help="티커 알림 플래그 초기화 (전 계좌 매도 시 자동 리셋 — 특수 상황 수동 사용)")
    parser.add_argument("--dashboard", default=DASHBOARD_PATH, help=f"대시보드 저장 경로 (기본 {DASHBOARD_PATH})")
    parser.add_argument("--test-push", action="store_true", help="OneSignal 테스트 푸시 발송 (구독자 전체)")
    args = parser.parse_args()

    if args.reset:
        reset_position(args.reset)
        return
    if args.serve is not None:
        run_serve(args.serve)
        return
    if args.test_push:
        cfg = load_config()
        app_id, api_key = _resolve_onesignal(cfg)
        if not app_id or not api_key:
            raise SystemExit("❌ ONESIGNAL_APP_ID / ONESIGNAL_REST_API_KEY 가 설정되지 않았습니다.")
        code, resp = send_onesignal_push(
            app_id, api_key,
            title="🔔 스윙 알리미 테스트",
            body="매도 알림이 정상적으로 수신되고 있습니다.",
            url=cfg.get("PAGES_URL") or None,
        )
        print(f"OneSignal 응답: HTTP {code} — {resp}")
        if str(code).startswith("2"):
            print("✅ 테스트 푸시 발송 완료 (스마트폰 알림 확인)")
        else:
            print("⚠️ 발송 실패 — 코드/키/앱 설정 확인 필요")
        return

    cfg = load_config()
    if not cfg.get("ENABLED", True):
        raise SystemExit("ℹ️  swing_config.json 의 ENABLED=false — 종료합니다.")
    if not cfg.get("POSITIONS"):
        raise SystemExit("ℹ️  POSITIONS 이 비어 있습니다. swing_config.json 에 티커를 추가하세요.")

    # 라이브 표시 가격은 모든 경로에 적용한다 — 알림 판정 필드는 항상 종가 기준이라
    # --monitor 도 live=True 로 계산해도 알림 시점은 변하지 않는다 (2026-08-11).
    statuses = _compute_all(cfg, force=True, live=True)
    webhook, user_id = _resolve_discord(cfg)
    send_user_sell_pushes(statuses, cfg)   # 사용자별 매도 푸시 (1일 1회, 앱 등록 태그 기준)

    # 전 계좌 매도 목표 도달(사이클 완료) → 알림 상태 자동 리셋 — 수동 --reset 불필요 (2026-08-11)
    cycle_msgs: list[str] = []
    state_changed = False
    for st in statuses:
        msg, changed = auto_cycle_reset(st, cfg["POSITIONS"][st["ticker"]])
        if msg:
            cycle_msgs.append(msg)
        state_changed = state_changed or changed
    if state_changed:
        save_config(cfg)   # 자동 리셋/재무장 상태 영속화 (중복 방지 플래그 포함)

    if args.monitor:
        # ── 실시간 모니터: 상태 변경분만 알림 ──
        # 주의: _compute_all 이 비활성 포지션을 건너뛰므로 zip 대신
        # statuses 의 ticker 로 포지션을 찾아야 상태 기록이 틀어지지 않는다.
        alerts: list[str] = list(cycle_msgs)   # 사이클 자동 리셋 알림 포함
        zone_msgs: dict[str, list[str]] = {}   # 매수 구간 푸시용 (티커별 — 2026-08-11)
        changed = state_changed
        for st in statuses:
            ticker = st["ticker"]
            pos = cfg["POSITIONS"][ticker]
            msgs, z_msgs = detect_alerts(st, pos, cfg)
            if z_msgs:
                zone_msgs[ticker] = z_msgs
            if msgs:
                changed = True
                alerts.extend([f"**{ticker}**"] + msgs)
        if changed:
            save_config(cfg)  # 알림 플래그 영속화 (중복 방지)
        # 매수 구간 도달/임박 푸시 — 전체 구독자(= 내 기기)에게 (2026-08-12 단독 사용 전환,
        # swing_zone 태그 필터 제거). 새 이벤트만 담겨 있어 중복 발송이 없다 (ZONE_ALERTS dedup).
        send_zone_pushes(zone_msgs, cfg)
        # 장중 실시간 대시보드 갱신 — 디스패치마다 신선한 HTML 을 만들어 gh-pages 에 재배포한다.
        # Discord 발송보다 먼저 수행해 알림 전송 실패가 대시보드 갱신을 막지 않는다.
        # (2026-08-11: 스마트폰 앱이 장중 가격을 따라가도록)
        write_dashboard(statuses, cfg, args.dashboard)
        content = "\n\n".join(alerts)
        if content:
            print(content)
            if webhook:
                _send_discord(webhook, user_id, "📈 스윙 알리미 신호", content)
                print("✅ Discord 알림 발송 완료")
            else:
                print("⚠️ DISCORD_WEBHOOK 미설정 — 콘솔 출력만 표시 (로컬 테스트용)")
            # ── OneSignal 전역 푸시: 제거 (2026-08-10) ──
            # 기존에는 신호 요약을 구독자 전체(지인 포함)에게 발송했으나, 내 매수 정보 기반
            # 신호가 지인에게 노출되는 문제가 있어 차단했다. 개인 알림은 main() 에서 먼저 호출되는
            # send_user_sell_pushes() 가 각자 등록한 매도 예정가 태그(swing_sell_{TICKER})로만
            # 발송하므로, 지인은 자기 기준 신호만 받는다. 공용 Discord 알림은 그대로 유지된다.
        else:
            print("✅ 신규 스윙 알림 없음 (매수 구간/임박/매도 변화 없음)")
        return

    # ── 기본 실행 / 일일 브리핑 ──
    if cycle_msgs:
        print("\n" + "\n\n".join(cycle_msgs) + "\n")
    print_console(statuses, cfg)
    write_dashboard(statuses, cfg, args.dashboard)

    if args.discord:
        title = f"📈 스윙 투자 알리미 브리핑 — {datetime.now(NY_TZ).strftime('%m-%d')}"
        content = build_briefing_text(statuses, cfg)
        if cycle_msgs:
            content = "\n\n".join(cycle_msgs) + "\n\n" + content   # 사이클 자동 리셋 안내 선두
        print(f"\n📨 Discord 브리핑 ({len(statuses)}개 티커)")
        if webhook:
            _send_discord(webhook, user_id, title, content)
            print("✅ Discord 발송 완료")
        else:
            print("⚠️ DISCORD_WEBHOOK 미설정 — 발송 생략")


if __name__ == "__main__":
    main()
