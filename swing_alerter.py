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
import sys
import time
from datetime import datetime, timedelta
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
_STATE_KEYS = ("SELL_ALARM_SENT", "SELL_IMMINENT_SENT", "SELL_PUSH_LAST_AT", "ZONE_ALERTS", "ATH_CYCLE_BASE")
_STATE_DEFAULTS = {
    "SELL_ALARM_SENT": False,
    "SELL_IMMINENT_SENT": False,
    "SELL_PUSH_LAST_AT": None,
    "ZONE_ALERTS": {"hit": [], "imminent": []},
    # ATH_CYCLE_BASE: None — '부재 = 첫 실행' 계약 (save_config 가 None 을 걸러내므로 파일엔 안 쓰임)
    "ATH_CYCLE_BASE": None,
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

    BUY_PRICE/SHARES 는 사용자 개인 정보라 공용 설정(swing_config.json)에 두지 않고
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
    # 🔒 개인 매수 포지션 오버레이 — BUY_PRICE/SHARES 는 개인 파일(swing_personal.json)에서만
    # 가져오고, _PERSONAL 마커를 붙여 공용 알림(Discord 브리핑/전역 푸시/대시보드)에서 제외한다.
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
        if pp.get("BUY_PRICE") is not None or pp.get("SHARES") is not None:
            if pp.get("BUY_PRICE") is not None:
                pos["BUY_PRICE"] = pp["BUY_PRICE"]
            if pp.get("SHARES") is not None:
                pos["SHARES"] = pp["SHARES"]
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
    # OneSignal 인증 헤더: 구식 REST 키는 Basic, 신식 API 키는 Key 를 사용
    # → 401 이면 다른 형식으로 재시도 (키 유형 자동 대응)
    for scheme in ("Basic", "Key"):
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
            if resp.status_code != 401:
                return resp.status_code, resp.text[:500]
        except Exception as e:  # noqa: BLE001
            return 0, f"전송 예외: {e}"
    return 401, "인증 실패 (Basic/Key 모두 401) — API 키 형식 확인 필요"


def send_user_sell_pushes(statuses: list[dict], cfg: dict) -> bool:
    """사용자별 매도 푸시 — 앱이 등록한 매도 예정가 태그(swing_sell_{TICKER})가
    현재가 이하인 구독자에게만 발송한다. 1일 1회(SELL_PUSH_LAST_AT) 중복 방지.

    푸시 본문의 매도 예정가는 Liquid({{ user.tags.swing_sell_{TICKER} }})로
    구독자마다 자기 값이 렌더링된다. (OneSignal 공식 개인화 문법)
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
        if pos.get("SELL_PUSH_LAST_AT") == today:
            continue  # 오늘 이미 발송 (1일 1회)
        # 매도 예정가(target) ≤ 현재가 인 사용자만 — OneSignal tag 필터는 <,> 만 지원하므로
        # target < (현재가+0.01) 을 찾으면 target ≤ 현재가 와 동치 (소수 2자리 가격 기준)
        filters = [{
            "field": "tag",
            "key": f"swing_sell_{ticker}",
            "relation": "<",
            "value": f"{st['price'] + 0.01:.2f}",
        }]
        body = (f"내 매도 예정가(${{{{ user.tags.swing_sell_{ticker} }}}})에 도달했습니다 — 매도 검토가 필요해요."
                + (f"\n앱에서 확인: {pages}" if pages else ""))
        code, resp = send_onesignal_push(
            app_id, api_key,
            title=f"📈 {ticker} 매도 신호",
            body=body,
            url=pages or None,
            filters=filters,
        )
        # 성공(2xx)일 때만 발송일 기록 — 실패 시 당일 재시도 가능 (알림 누락 방지)
        if str(code).startswith("2"):
            pos["SELL_PUSH_LAST_AT"] = today
            changed = True
        print(f"   📣 {ticker} 사용자별 매도 푸시: HTTP {code} — {resp[:80]}")
    if changed:
        save_config(cfg)
    return changed


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


def get_prior_close(ticker: str, as_of: str, max_retries: int = 3) -> float | None:
    """마지막 확정 종가(as_of, 'MM-DD') 세션의 직전 거래일 종가.

    전일 종가 대비 등락률 표시용 — get_prev_close()가 반환한 최종 세션의
    바로 앞 세션 종가를 같은 yfinance 1개월 데이터에서 찾는다.
    as_of에 해당하는 행이 없으면(데이터 변경 등) 마지막 두 행을 사용한다.
    """
    try:
        as_month, as_day = int(as_of[:2]), int(as_of[3:5])
    except (ValueError, TypeError, IndexError):
        return None
    last_err: Exception | None = None
    for attempt in range(max_retries):
        try:
            hist = yf.Ticker(ticker).history(period="1mo", interval="1d", auto_adjust=False)
            closes = hist["Close"].dropna()
            if len(closes) < 2:
                raise ValueError("Need at least 2 sessions.")
            for i in range(len(closes) - 1, 0, -1):
                d = closes.index[i].date() if hasattr(closes.index[i], "date") else None
                if d is not None and d.month == as_month and d.day == as_day:
                    return float(closes.iloc[i - 1])
            # as_of 미발견(새 fetch가 한 세션 뒤처진 데이터 지연) → 마지막 행을
            # 직전 종가로 사용 (표시 가격의 직전 세션 = 데이터의 마지막 세션)
            return float(closes.iloc[-1])
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < max_retries - 1:
                time.sleep(2.0)
    print(f"   ⚠️ {ticker} 직전 종가 조회 실패: {last_err}")
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


def compute_ticker(ticker: str, pos: dict, cfg: dict) -> dict:
    """티커 1개의 현재 상태 계산 (가격/ATH/래더/매도/손익)."""
    st: dict = {"ticker": ticker, "error": None}

    price, as_of = get_prev_close(ticker)
    if price is None:
        st["error"] = f"{ticker} 가격 조회 실패"
        return st

    ath, ath_date = get_ath(ticker)
    if ath is None or ath <= 0:
        st["error"] = f"{ticker} ATH 조회 실패"
        return st

    dd_pct = (price - ath) / ath * 100.0  # ATH 대비 하락율 (음수 = 하락)

    # 전일 종가 대비 등락률 — 직전 거래일 종가 대비 (대시보드 전일 종가 줄 표시용)
    prior_close = get_prior_close(ticker, as_of)
    day_change_pct = None
    if prior_close and prior_close > 0:
        day_change_pct = (price - prior_close) / prior_close * 100.0

    ladder = build_ladder(ath, cfg)
    for lvl in ladder:
        lvl["hit"] = bool(price <= lvl["price"] + 1e-9)

    hit_levels = [l for l in ladder if l["hit"]]
    deepest_hit = hit_levels[-1]["pct"] if hit_levels else None
    next_zone = next((l for l in ladder if not l["hit"]), None)

    # 매도 목표 — 실제 매수가(BUY_PRICE) × (1 + 스윙 목표 수익률)
    buy_price = pos.get("BUY_PRICE")
    sell_target = None
    sell_ready = False
    sell_gap_pct = None
    if buy_price:
        sell_target = float(buy_price) * (1 + float(cfg.get("SWING_TARGET_PCT", 10)) / 100.0)
        sell_ready = bool(price >= sell_target - 1e-9)
        if not sell_ready:
            sell_gap_pct = (sell_target - price) / sell_target * 100.0  # 양수 = 목표까지 남은 %
            if sell_gap_pct < 0:
                sell_gap_pct = 0.0

    # 계산기 — 목표 매도 시 예상 손익
    shares = pos.get("SHARES") or 0   # null/미입력 시 0 처리 (모니터링 전용 포지션)
    exp_profit = exp_roi = None
    if buy_price and sell_target and shares > 0:
        exp_profit = (sell_target - float(buy_price)) * float(shares)
        exp_roi = (sell_target / float(buy_price) - 1.0) * 100.0

    st.update({
        "label": pos.get("LABEL") or ticker,
        "price": price,
        "as_of": as_of,
        "ath": ath,
        "ath_date": ath_date,
        "dd_pct": dd_pct,
        "day_change_pct": day_change_pct,
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
        "zone_alerts": pos.setdefault("ZONE_ALERTS", {"hit": [], "imminent": []}),
    })
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
        pos["ATH_CYCLE_BASE"] = round(ath, 2)
        msgs.append(
            f"🆕 **{st['ticker']} 신규 전고가 갱신 ${ath:,.2f} ({st.get('ath_date', '')})**\n"
            "   매수 구간이 새 전고가 기준으로 초기화되었습니다 (새 하락 사이클)."
        )


def detect_alerts(st: dict, pos: dict, cfg: dict) -> list[str]:
    """새로 도달한 매수 구간 / 임박 / 매도 알림을 감지해 메시지 목록 반환.

    pos(ZONE_ALERTS/SELL_*) 상태를 갱신하므로 재폴링 시 중복 알림이 없습니다.
    """
    msgs: list[str] = []
    if st.get("error"):
        return msgs
    zone_alerts = st["zone_alerts"]
    # 신규 전고가 확인 → 기록된 구간 상태 리셋 (알림 삼킴 방지)
    _handle_ath_cycle_reset(st, pos, zone_alerts, msgs)
    tick = st["ticker"]
    price = st["price"]
    gap_p = float(cfg.get("IMMINENT_GAP_PCT", 5))
    dd_abs = abs(st["dd_pct"])

    # 1) 새로 도달한 매수 구간
    for lvl in st["ladder"]:
        p = lvl["pct"]
        if lvl["hit"] and p not in zone_alerts["hit"]:
            zone_alerts["hit"].append(p)
            zone_alerts["imminent"] = [x for x in zone_alerts["imminent"] if x != p]
            msgs.append(
                f"🔻 **{tick} -{p:.0f}% 매수 구간 도달**\n"
                f"현재가 ${price:.2f} | 목표가 ${lvl['price']:.2f} (하락 {st['dd_pct']:.1f}%)"
            )

    # 2) 다음 구간 임박
    nxt = st["next_zone"]
    if nxt and nxt["pct"] not in zone_alerts["imminent"]:
        remain = nxt["pct"] - dd_abs          # 다음 구간까지 남은 %p
        if 0 <= remain <= gap_p:
            zone_alerts["imminent"].append(nxt["pct"])
            msgs.append(
                f"📡 **{tick} -{nxt['pct']:.0f}% 매수 구간 임박**\n"
                f"현재 하락 {st['dd_pct']:.1f}% (남은 {remain:.1f}%p) | 목표가 ${nxt['price']:.2f}"
            )

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
    return msgs


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
        lines.extend([
            f"**{st['ticker']}** · {sell_txt}",
            f"- 현재가 ${st['price']:.2f} (종가 기준 {st['as_of']})",
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
.plan-buy-input{flex:1;min-width:0;background:#1c2533;border:1px solid var(--border);border-radius:8px;
  color:var(--text);font-size:22px;font-weight:700;padding:7px 10px;font-family:ui-monospace,Menlo,Consolas,monospace}
.plan-unit{font-size:22px;color:var(--muted)}
.plan-pcts{display:flex;gap:6px;flex-wrap:wrap}
.pct{font-size:22px;font-weight:700;padding:7px 13px;border-radius:999px;
  border:1px solid var(--border);background:#1c2533;color:var(--muted);cursor:pointer;-webkit-tap-highlight-color:transparent}
.pct.on{border-color:var(--green);background:var(--green-dim);color:var(--green)}
.pct:active{opacity:.7}
.plan-out{display:flex;align-items:center;gap:8px;margin-top:10px}
.po{font-size:22px;color:var(--muted);font-weight:600}
.po b{color:var(--text);font-family:ui-monospace,Menlo,Consolas,monospace;font-size:22px;font-weight:800}
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


# 매수 예정가(사용자 입력, 없으면 현재가) × (1 + 예상 수익률) → 매도 예정가 자동 계산 (브라우저 localStorage 저장)
# 매도 상태 칩/카운트는 사용자별 입력 기준으로 항상 재판정 — 서버 설정(BUY_PRICE) 없이도 동작한다.
_PLAN_JS = """
<script>
(function() {
  // 카드별 저장 키: swing_buy_{TICKER}(매수 예정가) / swing_sell_{TICKER}(예상 수익률)
  document.querySelectorAll('.card[data-ath]').forEach(function(card) {
    var ticker = card.dataset.ticker;
    var close = parseFloat(card.dataset.close);
    var gapPct = parseFloat(card.dataset.gap) || 5;
    var buyInput = card.querySelector('.plan-buy-input');
    var pctBtns = card.querySelectorAll('.pct');
    var sellEl = card.querySelector('.plan-sell');
    var chip = card.querySelector('[data-sell-chip]');

    // 저장값 로드 (기본: 매수 예정가 미입력 → 현재가 기준, 예상 수익률 → 서버 SWING_TARGET_PCT 기본값)
    var buyVal = parseFloat(localStorage.getItem('swing_buy_' + ticker));
    if (isNaN(buyVal)) buyVal = 0;   // 0 = 미입력
    var sellPct = parseFloat(localStorage.getItem('swing_sell_' + ticker));
    if (isNaN(sellPct)) sellPct = parseFloat(card.dataset.sellDefault) || 10;

    // 유효 매수 예정가 — 입력값(>0)이 없으면 현재가(지금 매수 시) 기준
    function buyBase() {
      var v = parseFloat(buyInput.value);
      if (isNaN(v) || v <= 0) v = close;
      return v;
    }

    // 페이지 로드 시 최초 판정에서만 진동 — 매수 예정가 입력/수익률 버튼 조작으로 상태가
    // 오락가락해도 잡음 진동이 울리지 않게 한다 (시장 가격은 서버 렌더 → 새로고침 시 갱신).
    var vibed = false;

    // 매도 알람/임박 시 스마트폰 진동 (Vibration API) — Android Chrome만 지원, iOS는 미지원(무시).
    // localStorage(swing_vibe_{TICKER})에 직전 상태를 기록해 5분 자동 새로고침 등으로 같은 상태가
    // 반복돼도 재진동하지 않는다. 🚨 매도는 강한 3연타, 🚀 임박은 짧은 2연타.
    function vibrateSell(ticker, stateClass) {
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

    // 사용자별 매도 상태 — 내 매수 예정가 × (1 + 예상 수익률) 기준으로 항상 재판정
    function applySellStatus() {
      if (!chip || isNaN(close)) return;
      var target = buyBase() * (1 + sellPct / 100);
      var remain = (target - close) / target * 100;   // 목표까지 남은 % (양수)
      chip.classList.remove('red', 'amber', 'gray');
      var stateClass;
      if (close >= target - 1e-9) {
        stateClass = 'red';
        chip.classList.add('red');
        chip.textContent = '🚨 매도';
        chip.style.display = '';                       // 알람 상태만 표시
      } else if (remain <= gapPct) {
        stateClass = 'amber';
        chip.classList.add('amber');
        chip.textContent = '🚀 임박';
        chip.style.display = '';                       // 임박 상태만 표시
      } else {
        stateClass = 'gray';
        chip.classList.add('gray');
        chip.textContent = '⏳ 대기';
        chip.style.display = 'none';                   // 기본(대기) 상태 — 칩 숨김
      }
      vibrateSell(ticker, stateClass);
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

    // 매도 예정가 → OneSignal 태그 동기화 — 서버가 '내 매도 예정가 ≤ 현재가' 사용자에게만
    // 사용자별 매도 푸시를 보낼 때 기준으로 사용한다 (푸시 미설정/미구독 환경에서는 무시됨)
    function syncSellTag(ticker, sell) {
      try {
        if (window.OneSignalDeferred && window.OneSignalDeferred.push) {
          window.OneSignalDeferred.push(function(OneSignal) {
            if (OneSignal && OneSignal.User && OneSignal.User.addTag) {
              OneSignal.User.addTag('swing_sell_' + ticker, sell.toFixed(2));
            }
          });
        }
      } catch (e) { /* OneSignal 미설정 — 무시 */ }
    }

    function update() {
      if (isNaN(close)) return;   // 가격 데이터 없으면 계산 생략
      var base = buyBase();
      // 매도 예정가 = 매수 예정가 × (1 + 예상 수익률/100)
      var sell = base * (1 + sellPct / 100);
      sellEl.textContent = '$' + sell.toFixed(2);
      syncSellTag(ticker, sell);
      // 매수 예정가는 직접 입력한 양수 값만 저장 (비우거나 0이면 현재가 기준으로 초기화)
      var pv = parseFloat(buyInput.value);
      if (!isNaN(pv) && pv > 0) {
        localStorage.setItem('swing_buy_' + ticker, String(pv));
      } else {
        localStorage.removeItem('swing_buy_' + ticker);
      }
      localStorage.setItem('swing_sell_' + ticker, String(sellPct));
      pctBtns.forEach(function(b) {
        b.classList.toggle('on', parseFloat(b.dataset.pct) === sellPct);
      });
      applySellStatus();
      refreshAlarmCount();
    }

    if (buyVal > 0) buyInput.value = buyVal;
    buyInput.addEventListener('input', update);
    pctBtns.forEach(function(b) {
      b.addEventListener('click', function() {
        sellPct = parseFloat(b.dataset.pct);
        update();
      });
    });
    update();
  });
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

    for st in statuses:
        if st.get("error"):
            cards.append(
                f'<div class="card"><div class="row"><span class="tick">{st["ticker"]}</span>'
                f'<span class="chip red">❌ {st["error"]}</span></div></div>'
            )
            continue

        dd_cls = "up" if st["dd_pct"] > 0 else ("down" if st["dd_pct"] < 0 else "flat")
        dd_sign = "🆕 +" if st["dd_pct"] > 0 else ("▼ " if st["dd_pct"] < 0 else "")

        # 전일 종가 대비 등락률 — 전일 종가 줄 끝 표시 (ATH 하락률과 별개 수치)
        dc = st.get("day_change_pct")
        if dc is None:
            day_span = ""
        else:
            day_cls = "up" if dc > 0 else ("down" if dc < 0 else "flat")
            day_sign = "▲ " if dc > 0 else ("▼ " if dc < 0 else "")
            day_span = f' 대비 <span class="dd {day_cls}">{day_sign}{abs(dc):.1f}%</span>'
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

        cards.append(f"""
<div class="card" data-ticker="{st["ticker"]}" data-ath="{st["ath"]:.2f}" data-close="{st["price"]:.2f}" data-gap="{float(cfg.get("IMMINENT_GAP_PCT", 5)):g}" data-sell-default="{float(cfg.get("SWING_TARGET_PCT", 10)):g}">
  <div class="row">
    <span class="tick">{st["ticker"]}</span>
    {sell_chip}
  </div>
  <div class="price mono">${st["price"]:,.2f}</div>
  <div class="meta">전일 종가 ${st["price"]:,.2f} ({st["as_of"]}){day_span}</div>
  <div class="row" style="margin-top:10px;justify-content:flex-end">
    <span class="chip {'green' if st['deepest_hit'] else 'gray'}">
      {'🟢 매수 구간 ' + str(len([l for l in st['ladder'] if l['hit']])) + '개 경과' if st['deepest_hit'] else '매수 구간 대기'}</span>
  </div>
  <div class="info">📊 전고가 ${st["ath"]:,.2f} ({st["ath_date"]}) 대비 <span class="dd {dd_cls}">{dd_sign}{abs(st["dd_pct"]):.1f}%</span></div>
  <div class="plan">
    <div class="plan-row">
      <label for="buy-{st["ticker"]}">💰 매수 예정가</label>
      <span class="plan-unit">$</span>
      <input id="buy-{st["ticker"]}" class="plan-buy-input" type="number" min="0" step="0.01" placeholder="{st["price"]:.2f}">
    </div>
    <div class="plan-row">
      <label>📈 예상 수익률</label>
      <div class="plan-pcts">
        <button type="button" class="pct" data-pct="5">5%</button>
        <button type="button" class="pct" data-pct="10">10%</button>
        <button type="button" class="pct" data-pct="15">15%</button>
        <button type="button" class="pct" data-pct="20">20%</button>
      </div>
    </div>
    <div class="plan-out">
      <span class="po">🎯 매도 예정가 <b class="plan-sell">-</b></span>
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
  <div class="sub">업데이트 {updated_at} · 종가 기준 {as_of_ny} (미국){live_link}</div>
  <div class="chips">
    {push_btn}
    <span class="chip {'red' if sell_cnt else 'gray'}" id="sell-alarm-cnt">🚨 매도 알람 {sell_cnt}</span>
    <span class="chip {'green' if buy_cnt else 'gray'}">🟢 매수 경과 {buy_cnt}</span>
  </div>
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


def _compute_all(cfg: dict, force: bool = False) -> list[dict]:
    """전 티커 상태 계산 (60초 캐시 — 서버 폴링 부하 방지)."""
    now = time.time()
    if not force and _LAST_COMPUTE["statuses"] is not None and now - _LAST_COMPUTE["ts"] < 60:
        return _LAST_COMPUTE["statuses"]
    statuses = []
    for ticker, pos in cfg.get("POSITIONS", {}).items():
        if not pos.get("ENABLED", True):
            continue
        statuses.append(compute_ticker(ticker, pos, cfg))
    _LAST_COMPUTE.update(ts=now, statuses=statuses, cfg=cfg)
    return statuses


class _DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 요청 로그 최소화
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
        statuses = _compute_all(cfg)
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
    print(f"\n📊 스윙 투자 알리미 — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)
    for st in statuses:
        if st.get("error"):
            print(f"❌ {st['ticker']}: {st['error']}")
            continue
        print(f"[{st['ticker']}] 현재 ${st['price']:,.2f} ({st['as_of']})")
        print(f"   ATH ${st['ath']:,.2f} ({st['ath_date']}) → 하락 {st['dd_pct']:+.1f}%")
        if st["sell_target"] is not None:
            tag = " 🔒 개인" if st.get("personal") else ""
            print(f"   🎯 매도 목표 ${st['sell_target']:,.2f} (매수가 ${st['buy_price']:,.2f} × {cfg.get('SWING_TARGET_PCT', 10):+.0f}%){tag} | {_sell_chip(st, gap)}")
        else:
            print("   🎯 매도 목표 미설정 (BUY_PRICE 입력 필요 — 실제 매수 후 설정)")
        print(f"   📊 {_ladder_summary(st)}")
        if st["exp_profit"] is not None:
            print(f"   💰 매수 ${st['buy_price']:,.2f} × {st['shares']:.0f}주 → +${st['exp_profit']:,.2f} ({st['exp_roi']:+.1f}%)")
    print("=" * 60)


def reset_position(ticker: str) -> None:
    cfg = load_config()
    if ticker not in cfg.get("POSITIONS", {}):
        raise SystemExit(f"❌ POSITIONS 에 '{ticker}' 이(가) 없습니다: {CONFIG_PATH}")
    pos = cfg["POSITIONS"][ticker]
    pos["SELL_ALARM_SENT"] = False
    pos["SELL_IMMINENT_SENT"] = False
    pos["ZONE_ALERTS"] = {"hit": [], "imminent": []}
    pos.pop("ATH_CYCLE_BASE", None)   # 새 사이클 기준도 초기화 (다음 실행에서 재설정)
    save_config(cfg)
    print(f"✅ {ticker} 알림 플래그 초기화 완료 — 새 포지션 진입 후 사용하세요.")


def main() -> None:
    parser = argparse.ArgumentParser(description="스윙 투자 알리미 (MDD 기반 매수/매도 알림 + 모바일 대시보드)")
    parser.add_argument("--discord", action="store_true", help="상태 출력 + Discord 일일 브리핑 발송")
    parser.add_argument("--monitor", action="store_true", help="실시간 모니터 — 변경분(매수/임박/매도) 알림만 발송")
    parser.add_argument("--serve", nargs="?", const=8080, type=int, metavar="PORT",
                        help="스마트폰용 대시보드 HTTP 서버 실행 (기본 포트 8080)")
    parser.add_argument("--reset", metavar="TICKER", help="티커 알림 플래그 초기화 (새 포지션 진입 후)")
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

    statuses = _compute_all(cfg, force=True)
    webhook, user_id = _resolve_discord(cfg)
    send_user_sell_pushes(statuses, cfg)   # 사용자별 매도 푸시 (1일 1회, 앱 등록 태그 기준)

    if args.monitor:
        # ── 실시간 모니터: 상태 변경분만 알림 ──
        # 주의: _compute_all 이 비활성 포지션을 건너뛰므로 zip 대신
        # statuses 의 ticker 로 포지션을 찾아야 상태 기록이 틀어지지 않는다.
        alerts: list[str] = []
        changed = False
        for st in statuses:
            ticker = st["ticker"]
            pos = cfg["POSITIONS"][ticker]
            msgs = detect_alerts(st, pos, cfg)
            if msgs:
                changed = True
                alerts.extend([f"**{ticker}**"] + msgs)
        if changed:
            save_config(cfg)  # 알림 플래그 영속화 (중복 방지)
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
    print_console(statuses, cfg)
    write_dashboard(statuses, cfg, args.dashboard)

    if args.discord:
        title = f"📈 스윙 투자 알리미 브리핑 — {datetime.now(NY_TZ).strftime('%m-%d')}"
        content = build_briefing_text(statuses, cfg)
        print(f"\n📨 Discord 브리핑 ({len(statuses)}개 티커)")
        if webhook:
            _send_discord(webhook, user_id, title, content)
            print("✅ Discord 발송 완료")
        else:
            print("⚠️ DISCORD_WEBHOOK 미설정 — 발송 생략")


if __name__ == "__main__":
    main()
