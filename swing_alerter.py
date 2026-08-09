#!/usr/bin/env python3
"""
─────────────────────────────────────────────────────────────
스윙 투자 알리미 (Swing Alerter) — TQQQ 스윙 전략 계산기 & 알리미
─────────────────────────────────────────────────────────────
유튜브 'TQQQ 스윙 투자 전략 / 스윙 투자 계산기&매수 매도 시점 알리미'
(구글 스프레드시트 버전)의 로직을 자체 엔진으로 재구현한 도구입니다.

전략 요약 (스프레드시트 기준):
  - 매수: 역대 최고가(ATH) 대비 MDD 5% 단위 구간(-5% ~ -95%)에
    현재가가 도달하면 해당 구간이 '매수' 상태가 됩니다.
  - 매도: 매수 시점의 전고가(ATH_AT_BUY) 대비 스윙 목표(SWING_TARGET_PCT,
    기본 -10%) 회복 시 매도 알람이 울립니다 (예: $140 → $126).
  - 계산기: 매수가 × 보유수량 → 목표 매도 시 예상 수익금/수익률 자동 계산.

기능:
  - MDD 래더 상태 계산 (매수/대기) — yfinance 종가 기준
  - 매수 구간 도달 / 임박 / 매도 알림을 Discord로 발송 (--monitor)
  - 일일 종합 브리핑 Discord 발송 (--discord)
  - 모바일 대시보드 HTML 생성 + 로컬 HTTP 서버 (--serve) — 스마트폰 확인용
  - 설정/상태는 단일 파일 swing_config.json (설정 단일 소스)

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
DASHBOARD_PATH = "swing_dashboard.html"
PORTFOLIO_CONFIG_PATH = "portfolio_config.json"
NY_TZ = ZoneInfo("America/New_York")

# ── 기본 설정 (swing_config.json 에서 덮어쓸 수 있음) ──────────────
DEFAULT_CFG = {
    "ENABLED": True,
    "REFERENCE_HIGH": "ATH",        # 기준 전고가 (ATH = 역대 최고가)
    "MDD_START_PCT": 5,             # 매수 구간 시작 (-5%)
    "MDD_END_PCT": 95,              # 매수 구간 종료 (-95%)
    "MDD_STEP_PCT": 5,              # 구간 간격
    "SWING_TARGET_PCT": -10,        # 스윙 목표 — 매수시 전고가 대비 회복 목표(%)
    "IMMINENT_GAP_PCT": 5,          # 임박 알림 기준 (구간/매도 목표까지 %p)
    "PAGES_URL": "",               # GitHub Pages 주소 — 설정 시 대시보드에 라이브 링크 표시
    "ONESIGNAL_APP_ID": "",        # OneSignal 웹 푸시 앱 ID (대시보드 SDK 초기화용, 공개값)
    "POSITIONS": {},
}

# ═══════════════════════════════════════════════════════════
# 설정 로드/저장
# ═══════════════════════════════════════════════════════════

def load_config() -> dict:
    """swing_config.json 로드. 없으면 기본 템플릿 생성 후 안내."""
    if not os.path.isfile(CONFIG_PATH):
        _write_json(DEFAULT_CFG)
        print(f"ℹ️  {CONFIG_PATH} 이(가) 없어 기본 템플릿을 생성했습니다.")
        print("   POSITIONS 에 모니터링할 티커를 추가한 뒤 다시 실행하세요.")
        return json.loads(json.dumps(DEFAULT_CFG))
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    # 기본값 병합 (새 필드 추가 시 하위 호환)
    merged = {**DEFAULT_CFG, **cfg}
    merged["POSITIONS"] = dict(DEFAULT_CFG["POSITIONS"], **cfg.get("POSITIONS", {}))
    return merged


def _write_json(cfg: dict) -> None:
    """원자적 저장 — 임시 파일 후 rename (깨진 파일 방지)."""
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=4)
    os.replace(tmp, CONFIG_PATH)


def save_config(cfg: dict) -> None:
    _write_json(cfg)


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
                        url: str | None = None) -> tuple[int, str]:
    """OneSignal REST API로 웹 푸시 발송 — 구독자 전체(Subscribed Users) 대상.

    반환: (HTTP 상태코드, 응답 본문)
    """
    if not app_id or not api_key:
        return 0, "ONESIGNAL_APP_ID / ONESIGNAL_REST_API_KEY 미설정"
    payload: dict = {
        "app_id": app_id,
        "included_segments": ["Subscribed Users"],
        "target_channel": "push",
        "headings": {"en": title},
        "contents": {"en": body},
    }
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


# ═══════════════════════════════════════════════════════════
# 데이터 조회
# ═══════════════════════════════════════════════════════════

def get_ath(ticker: str, max_retries: int = 3) -> tuple[float | None, str | None]:
    """역대 최고 종가(ATH) 조회 — 최근 액면분할 이후 원시 종가 기준.

    yfinance 원시(auto_adjust=False) 종가의 분할 전 값은 현재 주식 수 기준과
    다르므로(예: TQQQ 2025-11-20 2:1 분할), 마지막 분할 이후 데이터만
    사용해 '증권사 화면과 같은' 실거래 기준 ATH를 구한다.
    """
    last_err: Exception | None = None
    for attempt in range(max_retries):
        try:
            stock = yf.Ticker(ticker)
            hist = stock.history(period="max", interval="1d", auto_adjust=False)
            if hist.empty:
                raise ValueError("Data empty.")
            closes = hist["Close"].dropna()
            if closes.empty:
                raise ValueError("No close data.")

            splits = stock.splits
            if len(splits):
                cutoff = splits.index[-1].date()   # 마지막 분할일 (당일 포함)
                mask = [d >= cutoff for d in closes.index.date]
                closes = closes[mask]
                if closes.empty:
                    closes = hist["Close"].dropna()

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
    ladder = build_ladder(ath, cfg)
    for lvl in ladder:
        lvl["hit"] = bool(price <= lvl["price"] + 1e-9)

    hit_levels = [l for l in ladder if l["hit"]]
    deepest_hit = hit_levels[-1]["pct"] if hit_levels else None
    next_zone = next((l for l in ladder if not l["hit"]), None)

    # 매도 목표 — 매수 시점 전고가 × (1 + 스윙 목표)
    ath_at_buy = pos.get("ATH_AT_BUY")
    sell_target = None
    sell_ready = False
    sell_gap_pct = None
    if ath_at_buy:
        sell_target = float(ath_at_buy) * (1 + float(cfg.get("SWING_TARGET_PCT", -10)) / 100.0)
        sell_ready = bool(price >= sell_target - 1e-9)
        if not sell_ready:
            sell_gap_pct = (sell_target - price) / sell_target * 100.0  # 양수 = 목표까지 남은 %
            if sell_gap_pct < 0:
                sell_gap_pct = 0.0

    # 계산기 — 목표 매도 시 예상 손익
    buy_price = pos.get("BUY_PRICE")
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
        "deepest_hit": deepest_hit,
        "next_zone": next_zone,
        "ladder": ladder,
        "ath_at_buy": float(ath_at_buy) if ath_at_buy else None,
        "sell_target": sell_target,
        "sell_ready": sell_ready,
        "sell_gap_pct": sell_gap_pct,
        "buy_price": float(buy_price) if buy_price else None,
        "shares": float(shares),
        "exp_profit": exp_profit,
        "exp_roi": exp_roi,
        "zone_alerts": pos.setdefault("ZONE_ALERTS", {"hit": [], "imminent": []}),
    })
    return st


# ═══════════════════════════════════════════════════════════
# 알림 감지 (--monitor) — 상태를 변경하며 1회성 알림 메시지 생성
# ═══════════════════════════════════════════════════════════

def detect_alerts(st: dict, pos: dict, cfg: dict) -> list[str]:
    """새로 도달한 매수 구간 / 임박 / 매도 알림을 감지해 메시지 목록 반환.

    pos(ZONE_ALERTS/SELL_*) 상태를 갱신하므로 재폴링 시 중복 알림이 없습니다.
    """
    msgs: list[str] = []
    if st.get("error"):
        return msgs
    zone_alerts = st["zone_alerts"]
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

    # 3) 매도 목표 임박
    if st["sell_target"] and st["sell_gap_pct"] is not None and not pos.get("SELL_IMMINENT_SENT"):
        if 0 < st["sell_gap_pct"] <= gap_p:
            pos["SELL_IMMINENT_SENT"] = True
            msgs.append(
                f"🚀 **{tick} 매도 목표 임박**\n"
                f"현재가 ${price:.2f} | 매도 목표 ${st['sell_target']:.2f} "
                f"(남은 {st['sell_gap_pct']:.1f}%)"
            )

    # 4) 매도 알람
    if st["sell_ready"] and not pos.get("SELL_ALARM_SENT"):
        pos["SELL_ALARM_SENT"] = True
        msgs.append(
            f"🚨 **{tick} 매도 알람 — 목표 도달!**\n"
            f"현재가 ${price:.2f} ≥ 매도 목표 ${st['sell_target']:.2f}\n"
            f"매도 검토 필요 (스윙 목표 {cfg.get('SWING_TARGET_PCT', -10):+.0f}%)"
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
    """일일 종합 브리핑 (Discord 설명란용)."""
    gap = float(cfg.get("IMMINENT_GAP_PCT", 5))
    lines = []
    for st in statuses:
        if st.get("error"):
            lines.append(f"**{st['ticker']}** ❌ {st['error']}")
            continue
        pnl = ""
        if st["exp_profit"] is not None:
            pnl = (f"💰 매수 ${st['buy_price']:.2f} × {st['shares']:.0f}주 → "
                   f"목표 매도 +${st['exp_profit']:,.2f} ({st['exp_roi']:+.1f}%)")
        sell_part = "매도 목표 미설정 (ATH_AT_BUY 입력 필요)"
        if st["sell_target"] is not None:
            sell_part = f"매도 목표 ${st['sell_target']:.2f} (매수시 전고가 ${st['ath_at_buy']:.2f}) → {_sell_chip(st, gap)}"
        lines.extend([
            f"**{st['ticker']}** {st['label']}",
            f"현재가 ${st['price']:.2f} ({st['as_of']}) | ATH ${st['ath']:.2f} "
            f"({st['ath_date']}) → 하락 **{st['dd_pct']:+.1f}%**",
            f"🎯 {sell_part}",
            f"📊 {_ladder_summary(st)}",
        ])
        if pnl:
            lines.append(pnl)
        lines.append("")
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
body{max-width:640px;margin:0 auto;padding:0 14px 40px}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
header{position:sticky;top:0;z-index:5;background:rgba(11,14,20,.92);
  backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);
  padding:16px 0 12px;border-bottom:1px solid var(--border);margin-bottom:14px}
header h1{font-size:20px;letter-spacing:-.3px}
header .sub{color:var(--muted);font-size:12px;margin-top:4px}
.chips{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap}
.chip{font-size:12px;padding:5px 10px;border-radius:999px;font-weight:600}
.chip.gray{background:#1c2533;color:var(--muted)}
.chip.green{background:var(--green-dim);color:var(--green)}
.chip.red{background:var(--red-dim);color:var(--red)}
.chip.amber{background:var(--amber-dim);color:var(--amber)}
.push-btn{font-size:13px;font-weight:700;padding:8px 14px;border-radius:999px;border:1px solid var(--blue);
  background:var(--blue-dim);color:var(--blue);cursor:pointer;-webkit-tap-highlight-color:transparent}
.push-btn:active{opacity:.7}
.push-btn:disabled{opacity:.4;cursor:default}
.push-btn.on{border-color:var(--green);background:var(--green-dim);color:var(--green)}
.push-err{font-size:11px;color:var(--amber);background:var(--amber-dim);border:1px solid var(--amber);
  border-radius:8px;padding:5px 9px;margin-top:6px;width:100%;word-break:break-all;line-height:1.5}
.card{background:var(--card);border:1px solid var(--border);border-radius:16px;
  padding:16px;margin-bottom:16px}
.row{display:flex;align-items:center;justify-content:space-between;gap:8px}
.tick{font-size:18px;font-weight:800}
.tag{font-size:11px;color:var(--muted);font-weight:400;margin-left:6px}
.dd{font-size:15px;font-weight:800}
.dd.down{color:var(--blue)} .dd.up{color:var(--red)} .dd.flat{color:var(--muted)}
.price{font-size:34px;font-weight:800;margin:10px 0 2px}
.meta{color:var(--muted);font-size:12px}
.info{background:#0f1420;border:1px solid var(--border);border-radius:12px;
  padding:10px 12px;margin-top:12px;font-size:13px;line-height:1.7}
.info b{color:var(--text)}
.ladder{margin-top:14px}
.lvl{display:grid;grid-template-columns:52px 1fr 1.4fr 60px;gap:8px;
  align-items:center;padding:7px 0;border-bottom:1px solid #1a2231;font-size:13px}
.lvl:last-child{border-bottom:none}
.lvl .pct{font-weight:700;color:var(--muted)}
.lvl.hit .pct,.lvl.current .pct{color:var(--green)}
.lvl .val{color:var(--muted);font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px}
.lvl.hit{background:linear-gradient(90deg,rgba(63,185,80,.08),transparent 70%);border-radius:6px}
.lvl.current{background:linear-gradient(90deg,rgba(210,153,34,.10),transparent 70%);border-radius:6px}
.bar{height:6px;border-radius:3px;background:#1c2533;overflow:hidden}
.bar i{display:block;height:100%;border-radius:3px;background:var(--green)}
.lvl.current .bar i{background:var(--amber)}
.lvl .st{text-align:right;font-size:12px;font-weight:700}
.lvl.hit .st,.lvl.current .st{color:var(--amber)}
.lvl.wait .st{color:#4b5563}
.lvl.wait .bar i{background:#2a3344}
footer{color:#4b5563;font-size:11px;text-align:center;margin-top:8px;line-height:1.8}
.legend{display:flex;gap:14px;justify-content:center;color:var(--muted);font-size:11px;margin-top:6px}
.pages a{color:var(--blue);text-decoration:none;font-weight:700;font-size:12px}
.pages a:active{opacity:.7}
"""

# PWA 서비스 워커 등록 — Chrome '앱 설치' 기준 충족 (통과형 fetch, 캐시 없음)
_SW_REGISTER = """
<script>
if ('serviceWorker' in navigator) { navigator.serviceWorker.register('sw.js'); }
</script>
"""

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
      serviceWorkerPath: "OneSignalSDKWorker.js",
      serviceWorkerScope: "./",
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


def _lvl_row(lvl: dict, next_fill: float | None = None) -> str:
    """래더 1줄 — hit: 초록 100% / next: 호박색 진행바(다음 구간 접근도) / wait: 회색."""
    if lvl["hit"]:
        cls, st_txt, fill = "hit", "매수", 100.0
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
    sell_cnt = sum(1 for s in statuses if s.get("sell_ready") and not s.get("error"))
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
        sell_chip = (
            '<span class="chip red">🚨 매도</span>' if st["sell_ready"]
            else '<span class="chip gray">⏳ 대기</span>' if st["sell_target"] is not None
            else '<span class="chip gray">매도 미설정</span>'
        )
        if not st["sell_ready"] and st["sell_target"] is not None and st["sell_gap_pct"] is not None \
                and st["sell_gap_pct"] <= float(cfg.get("IMMINENT_GAP_PCT", 5)):
            sell_chip = '<span class="chip amber">🚀 임박</span>'

        pnl_html = ""
        if st["exp_profit"] is not None:
            pnl_html = (f'<div class="info">💰 매수가 <b>${st["buy_price"]:.2f}</b> × '
                        f'<b>{st["shares"]:.0f}주</b> → 목표 매도 시 '
                        f'<b style="color:var(--green)">+${st["exp_profit"]:,.2f}</b> '
                        f'(<b style="color:var(--green)">{st["exp_roi"]:+.1f}%</b>)</div>')

        sell_info = "매수 시점 전고가 미입력 (ATH_AT_BUY)"
        if st["sell_target"] is not None:
            sell_info = (f'매도 목표 <b>${st["sell_target"]:.2f}</b> '
                         f'(매수시 전고가 ${st["ath_at_buy"]:.2f} × {cfg.get("SWING_TARGET_PCT", -10):+.0f}%)')

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
<div class="card">
  <div class="row">
    <span class="tick">{st["ticker"]}<span class="tag">{st["label"]}</span></span>
    {sell_chip}
  </div>
  <div class="price mono">${st["price"]:,.2f}</div>
  <div class="meta">종가 기준 {st["as_of"]} · ATH ${st["ath"]:,.2f} ({st["ath_date"]})</div>
  <div class="row" style="margin-top:10px">
    <span class="dd {dd_cls}">{dd_sign}{abs(st["dd_pct"]):.1f}%</span>
    <span class="chip {'green' if st['deepest_hit'] else 'gray'}">
      {'🟢 매수 구간 ' + str(len([l for l in st['ladder'] if l['hit']])) + '개 도달' if st['deepest_hit'] else '매수 구간 대기'}</span>
  </div>
  <div class="info">🎯 {sell_info}<br>📊 기준 전고가: {cfg.get('REFERENCE_HIGH', 'ATH')} ({st['ath_date']})</div>
  {pnl_html}
  <div class="ladder">{rows}</div>
</div>""")

    legend = ('<div class="legend"><span>🟢 매수 도달</span>'
              '<span>🟡 다음 구간 접근</span><span>⚪ 대기</span></div>')

    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0b0e14">
<meta http-equiv="refresh" content="300">
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
    <span class="chip {'red' if sell_cnt else 'gray'}">🚨 매도 알람 {sell_cnt}</span>
    <span class="chip {'green' if buy_cnt else 'gray'}">🟢 매수 구간 {buy_cnt}</span>
  </div>
</header>
<main>{''.join(cards)}</main>
{legend}
<footer>⚠️ 신호 알림·계산기용 — 자동매매가 아닙니다. 실제 매매는 본인이 직접 하세요.<br>
출처: 유튜브 'TQQQ 스윙 투자 전략' 스프레드시트 방식 재구현</footer>
</body>
</html>"""


def write_dashboard(statuses: list[dict], cfg: dict, path: str) -> None:
    now_ny = datetime.now(NY_TZ)
    html = render_dashboard(
        statuses, cfg,
        updated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        as_of_ny=now_ny.strftime("%Y-%m-%d %H:%M"),
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
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
            print(f"   🎯 매도 목표 ${st['sell_target']:,.2f} (매수시 전고가 ${st['ath_at_buy']:,.2f}) | {_sell_chip(st, gap)}")
        else:
            print("   🎯 매도 목표 미설정 (ATH_AT_BUY 입력 필요)")
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
            # ── OneSignal 웹 푸시 (지인 스마트폰 알림) ──
            app_id, api_key = _resolve_onesignal(cfg)
            if app_id and api_key:
                # 마크다운(** ) 제거 + 첫 줄 요약 (푸시는 120자 내외 권장)
                plain = " | ".join(a.replace("**", "").replace("\n", " · ") for a in alerts)
                code, resp = send_onesignal_push(
                    app_id, api_key,
                    title="📈 스윙 알리미 신호",
                    body=plain[:200],
                    url=cfg.get("PAGES_URL") or None,
                )
                print(f"OneSignal 푸시: HTTP {code} — {resp[:120]}")
            else:
                print("ℹ️ ONESIGNAL 미설정 — 푸시 발송 생략")
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
