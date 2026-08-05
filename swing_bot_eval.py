# -*- coding: utf-8 -*-
"""
==================================================================
 swing_bot_eval.py — 스윙 봇 실전 성과 평가 (독립 실행 도구)
==================================================================
swing_bot.py가 기록한 신호 저널(swing_signals.jsonl)을 읽어
실전 신호 기반 성과를 산출한다. 실제 매매는 수동이므로 이 보고서는
'신호를 그대로 따랐다면'의 성과를 보여준다 (체결가 보정은 저널 수정).

- 저널 형식 (JSONL, 한 줄 = 이벤트 1건):
    {"ticker": "TQQQ", "event": "BUY", "time": "2026-08-04T13:30:00-04:00", "price": 85.12}
  * event: BUY(포지션 오픈) | SELL(포지션 클로즈)
  * 실제 체결가가 신호가와 다른 경우 price를 직접 수정해 보정 가능.
  * SELL 미포함 BUY = 아직 보유 중 → 현재가로 평가(mark-to-market) 가능.

- 산출 지표: 트레이드별 수익률 / 승률 / 평균수익·손실 / 총수익(복리) /
  최대낙폭(MDD) / 평균 보유기간 / 이익팩터(PF) / 미청산 포지션

사용 예:
  python3 swing_bot_eval.py                        # 전체 종목 전체 기간
  python3 swing_bot_eval.py --ticker TQQQ          # 특정 종목만
  python3 swing_bot_eval.py --since 3m             # 최근 3개월만 (1m/3m/6m/1y/all)
  python3 swing_bot_eval.py --no-mark              # 미청산 포지션 현재가 미반영
  python3 swing_bot_eval.py --discord              # 결과를 Discord로 전송
  python3 swing_bot_eval.py --save                 # swing_performance.json에 월별 스냅샷 저장
  python3 swing_bot_eval.py --save --discord       # 저장 + Discord 전송 (월간 워크플로우 기본)

- swing_performance.json: 월 단위 스냅샷 누적 — 저장소에 커밋되어 실전 성과가
  자동 반영된다 (같은 달 재실행 시 해당 월 스냅샷 갱신, 이력 보존).
- 스냅샷 키는 '실행 월'이며 CI는 항상 --since all로 저장하므로, 로컬에서
  --since 3m 등으로 실행해도 CI 월간 스냅샷을 덮어쓰지 않도록 주의할 것.
- 저널이 비어 있으면(신호 없음) 스냅샷은 기록되지 않는다 — 신호가 없는 달은
  월간 이력에서 누락될 수 있다 (의도된 동작).
==================================================================
"""
import argparse
import json
import os
import shutil
import sys
import tempfile
import warnings
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests

warnings.filterwarnings("ignore")

# ==========================================
# [설정]
# ==========================================
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "YOUR_DISCORD_WEBHOOK")
DISCORD_USER_ID = os.getenv("DISCORD_USER_ID", "YOUR_DISCORD_USER_ID")
SIGNALS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "swing_signals.jsonl"
)
# 성과 누적 파일: 평가 실행마다 월별 스냅샷을 저장 → 저장소에 커밋되어 자동 반영
PERF_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "swing_performance.json"
)
REQUEST_TIMEOUT = 15

# ==========================================
# [저널 로드]
# ==========================================
def load_signals(path=SIGNALS_PATH):
  """JSONL 저널 로드 — 손상 라인은 건너뛰고 (ticker, event, time, price)만 유지."""
  records = []
  if not os.path.exists(path):
    return records
  with open(path, "r", encoding="utf-8") as f:
    for lineno, line in enumerate(f, 1):
      line = line.strip()
      if not line:
        continue
      try:
        rec = json.loads(line)
        if not isinstance(rec, dict):
          raise ValueError("객체가 아님")
        records.append({
            "ticker": rec["ticker"],
            "event": rec["event"],
            "time": pd.Timestamp(rec["time"]),
            "price": float(rec["price"]),
        })
      except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
        print(f"[경고] {lineno}번째 줄 파싱 실패 (무시): {exc}")
  return records


def pair_trades(records, ticker):
  """이벤트를 BUY→SELL 쌍으로 페어링.

  반환: (closed_trades, open_position)
    closed_trades: [(매수시각, 매수가, 매도시각, 매도가, 보유일수)]
    open_position: (매수시각, 매수가) 또는 None — SELL 없는 BUY
  """
  closed, open_pos = [], None
  for rec in sorted(
      (r for r in records if r["ticker"] == ticker), key=lambda r: r["time"]
  ):
    if rec["event"] == "BUY":
      # 중복 BUY는 첫 BUY를 유지하고 경고만 (손익 왜곡 최소화)
      if open_pos is not None:
        print(
            f"[경고] {ticker}: {open_pos[0].strftime('%Y-%m-%d')} BUY 후 또 BUY — "
            "중복으로 무시합니다 (저널 수정 권장)."
        )
        continue
      open_pos = (rec["time"], rec["price"])
    elif rec["event"] == "SELL":
      if open_pos is None:
        print(
            f"[경고] {ticker}: {rec['time'].strftime('%Y-%m-%d')} SELL인데 "
            "대응 BUY 없음 — 무시합니다."
        )
        continue
      buy_t, buy_p = open_pos
      closed.append((buy_t, buy_p, rec["time"], rec["price"],
                     (rec["time"] - buy_t).days))
      open_pos = None
  return closed, open_pos


def fetch_current_price(ticker):
  """현재가 조회 (yfinance) — 미청산 포지션 평가용. 실패 시 None."""
  try:
    import yfinance as yf
    d = yf.download(ticker, period="5d", interval="1d",
                    progress=False, auto_adjust=True)
    d.columns = [c[0] for c in d.columns]
    return float(d["Close"].dropna().iloc[-1])
  except Exception as exc:
    print(f"[경고] {ticker} 현재가 조회 실패: {exc}")
    return None


def build_report(ticker, records, mark_open=True, since=None):
  """종목 1개의 성과 보고서 dict 생성."""
  closed, open_pos = pair_trades(records, ticker)
  # 기간 필터는 페어링 뒤에 적용 — 기간 경계에 걸친 트레이드가
  # 'BUY 누락 → SELL 오경고'로 깨지지 않도록 매도 시각 기준으로 거른다.
  if since is not None:
    closed = [t for t in closed if t[2] >= since]
    if open_pos is not None and open_pos[0] < since:
      open_pos = None
  report = {"ticker": ticker, "trades": [], "open": None, "stats": {}}

  if not closed and open_pos is None:
    report["stats"]["none"] = True
    return report

  eq, rets = 1.0, []
  peak = 1.0
  mdd = 0.0
  buy_dates = []
  for buy_t, buy_p, sell_t, sell_p, hold_days in closed:
    ret = sell_p / buy_p - 1
    rets.append(ret)
    eq *= (1 + ret)
    peak = max(peak, eq)
    mdd = min(mdd, eq / peak - 1)
    buy_dates.append((buy_t, sell_t, hold_days, ret))
    report["trades"].append({
        "buy_time": buy_t.strftime("%Y-%m-%d %H:%M"),
        "buy_price": buy_p,
        "sell_time": sell_t.strftime("%Y-%m-%d %H:%M"),
        "sell_price": sell_p,
        "hold_days": hold_days,
        "return_pct": ret * 100,
    })

  open_info = None
  if open_pos is not None:
    buy_t, buy_p = open_pos
    cur = fetch_current_price(ticker) if mark_open else None
    open_ret = (cur / buy_p - 1) if cur else None
    open_info = {
        "buy_time": buy_t.strftime("%Y-%m-%d %H:%M"),
        "buy_price": buy_p,
        "current_price": cur,
        "open_return_pct": open_ret * 100 if open_ret is not None else None,
    }
    report["open"] = open_info
    if open_ret is not None:
      # 미청산 포지션도 총수익/MDD/승률 산정에 포함 (현재가 기준)
      rets = rets + [open_ret]
      eq *= (1 + open_ret)
      peak = max(peak, eq)
      mdd = min(mdd, eq / peak - 1)

  rets_arr = np.array(rets)
  wins = rets_arr[rets_arr > 0]
  losses = rets_arr[rets_arr <= 0]
  report["stats"] = {
      "none": False,
      "n_trades": len(rets_arr),
      "n_closed": len(closed),
      "win_rate": len(wins) / len(rets_arr) * 100 if len(rets_arr) else 0.0,
      "avg_ret": rets_arr.mean() * 100 if len(rets_arr) else 0.0,
      "avg_win": wins.mean() * 100 if len(wins) else 0.0,
      "avg_loss": losses.mean() * 100 if len(losses) else 0.0,
      "total_return": (eq - 1) * 100,
      "mdd": mdd * 100,
      "pf": (wins.sum() / abs(losses.sum())) if len(losses) else float("inf"),
      "avg_hold_days": np.mean([t[2] for t in buy_dates]) if buy_dates else 0.0,
      "max_hold_days": max((t[2] for t in buy_dates), default=0),
  }
  return report


def save_performance(reports, since):
  """평가 결과를 swing_performance.json에 월별 스냅샷으로 누적 저장.

  구조: {"updated": ..., "months": {"2026-08": {since, tickers: {...stats}}}}
  같은 달에 재실행하면 해당 달 스냅샷을 갱신(업서트) — 이력은 월 단위로 유지.
  """
  month = datetime.now(timezone.utc).strftime("%Y-%m")
  snapshot = {"since": since, "tickers": {}}
  for rep in reports:
    s = rep["stats"]
    ticker_snap = {
        "n_trades": s["n_trades"],
        "n_closed": s["n_closed"],
        "win_rate": round(s["win_rate"], 1),
        "avg_ret": round(s["avg_ret"], 2),
        "total_return": round(s["total_return"], 2),
        "mdd": round(s["mdd"], 2),
        "pf": round(s["pf"], 2) if np.isfinite(s["pf"]) else None,
        "avg_hold_days": round(s["avg_hold_days"], 1),
        # 승리/손실이 없으면 nan → null로 저장 (JSON 안전)
        "avg_win": round(s["avg_win"], 2) if np.isfinite(s["avg_win"]) else None,
        "avg_loss": round(s["avg_loss"], 2) if np.isfinite(s["avg_loss"]) else None,
        "max_hold_days": s["max_hold_days"],
    }
    if rep["open"]:
      o = rep["open"]
      ticker_snap["open"] = {
          "buy_time": o["buy_time"],
          "buy_price": o["buy_price"],
          "open_return_pct": (
              round(o["open_return_pct"], 2)
              if o["open_return_pct"] is not None else None
          ),
      }
    snapshot["tickers"][rep["ticker"]] = ticker_snap

  data = {}
  if os.path.exists(PERF_PATH):
    try:
      with open(PERF_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    except (json.JSONDecodeError, OSError):
      data = {}
  data.setdefault("months", {})[month] = snapshot
  data["updated"] = datetime.now(timezone.utc).isoformat()

  try:
    with tempfile.NamedTemporaryFile(
        "w", delete=False, suffix=".json", encoding="utf-8"
    ) as tmp:
      json.dump(data, tmp, ensure_ascii=False, indent=2)
      tmp_path = tmp.name
    shutil.move(tmp_path, PERF_PATH)
    print(f"[저장] 성과 스냅샷 반영: swing_performance.json ({month})")
  except OSError as exc:
    print(f"[오류] 성과 파일 저장 실패: {exc}")


def format_report(report):
  """보고서 dict → 사람이 읽을 수 있는 문자열."""
  t = report["ticker"]
  s = report["stats"]
  if s.get("none"):
    return f"[{t}] 신호 기록 없음"

  lines = [f"📊 **[{t}] 실전 신호 성과**"]
  lines.append(
      f"- 매매 {s['n_trades']}회(청산 {s['n_closed']}) | 승률 {s['win_rate']:.0f}% | "
      f"평균 {s['avg_ret']:+.1f}%"
  )
  if np.isfinite(s["pf"]):
    lines.append(f"- 총수익(현재가 반영) {s['total_return']:+.1f}% | MDD {s['mdd']:.1f}% | PF {s['pf']:.2f}")
  else:
    lines.append(f"- 총수익(현재가 반영) {s['total_return']:+.1f}% | MDD {s['mdd']:.1f}% | PF ∞(손실 없음)")
  if s["n_closed"]:
    lines.append(
        f"- 평균승리 {s['avg_win']:+.1f}% | 평균손실 {s['avg_loss']:+.1f}% | "
        f"평균보유 {s['avg_hold_days']:.0f}일 (최대 {s['max_hold_days']}일)"
    )
  if report["open"]:
    o = report["open"]
    if o["current_price"]:
      lines.append(
          f"- 🟢 미청산: {o['buy_time']} 매수 ${o['buy_price']:.2f} → "
          f"현재 ${o['current_price']:.2f} ({o['open_return_pct']:+.1f}%)"
      )
    else:
      lines.append(
          f"- 🟢 미청산: {o['buy_time']} 매수 ${o['buy_price']:.2f} (현재가 조회 실패)"
      )
  lines.append("```")
  lines.append(f"{'매수':<12}{'매도':<12}{'보유':>4}{'수익률':>9}")
  for tr in report["trades"]:
    lines.append(
        f"{tr['buy_time'][5:16]:<12}{tr['sell_time'][5:16]:<12}"
        f"{tr['hold_days']:>4}{tr['return_pct']:>+8.1f}%"
    )
  lines.append("```")
  return "\n".join(lines)


def send_discord(message):
  """평가 결과를 Discord로 전송 (설정 없으면 콘솔 출력만)."""
  if not DISCORD_WEBHOOK or DISCORD_WEBHOOK == "YOUR_DISCORD_WEBHOOK":
    print(message)
    return
  content = (f"<@{DISCORD_USER_ID}>\n{message}" if DISCORD_USER_ID else message)
  try:
    requests.post(DISCORD_WEBHOOK, json={"content": content}, timeout=REQUEST_TIMEOUT)
  except requests.RequestException as exc:
    print(f"[오류] Discord 전송 실패: {exc}")


SESSION_TZ = "America/New_York"  # 저널 타임스탬프와 동일 타임존


def parse_since(value):
  """'3m'/'6m'/'1y'/'all' 또는 'YYYY-MM-DD' → 시작 시각 (None = 전체).

  저널의 time은 tz-aware(뉴욕)이므로 결과도 항상 aware로 맞춘다.
  """
  v = value.strip().lower()
  if v == "all":
    return None
  if v.endswith("m") and v[:-1].isdigit():
    return pd.Timestamp.now(tz=SESSION_TZ) - timedelta(days=30 * int(v[:-1]))
  if v.endswith("y") and v[:-1].isdigit():
    return pd.Timestamp.now(tz=SESSION_TZ) - timedelta(days=365 * int(v[:-1]))
  try:
    ts = pd.Timestamp(v)
    return ts.tz_localize(SESSION_TZ) if ts.tzinfo is None else ts
  except ValueError:
    raise SystemExit(f"[오류] --since 형식 오류: {value} (예: 3m, 6m, 1y, 2026-01-01, all)")


def main():
  p = argparse.ArgumentParser(description="스윙 봇 실전 성과 평가 (swing_signals.jsonl)")
  p.add_argument("--ticker", default="", help="종목 (콤마 구분, 기본: 저널 전체)")
  p.add_argument("--since", default="all", help="평가 기간 (1m/3m/6m/1y/YYYY-MM-DD/all, 기본 all)")
  p.add_argument("--no-mark", action="store_true", help="미청산 포지션 현재가 미반영")
  p.add_argument("--discord", action="store_true", help="결과를 Discord로 전송")
  p.add_argument("--save", action="store_true",
                 help="결과를 swing_performance.json에 월별 스냅샷으로 저장")
  args = p.parse_args()

  records = load_signals()
  if not records:
    print("신호 저널이 비어 있습니다 — 아직 BUY/SELL 신호가 없습니다.")
    return

  since = parse_since(args.since)
  # 기간 필터는 페어링 이후(build_report 내부)에 적용 — 경계 트레이드 보존

  tickers = [t.strip().upper() for t in args.ticker.split(",") if t.strip()]
  if not tickers:
    tickers = sorted({r["ticker"] for r in records})

  reports = [build_report(t, records, mark_open=not args.no_mark, since=since)
             for t in tickers]
  if args.save:
    save_performance(reports, args.since)
  if args.discord:
    # 종목 전체를 하나의 메시지로 — Discord 메시지 길이 제한(2000자)을 넘지 않게
    # '\n\n' 단위로 누적하고, 초과 직전에 잘라 새 메시지로 분할한다.
    header = "📈 **스윙 봇 실전 성과 평가**" + (f" ({since.strftime('%Y-%m-%d')}~)" if since else "")
    parts, cur = [], header
    for rep in reports:
      chunk = format_report(rep)
      # 단일 종목 보고서가 너무 길면 트레이드 표를 절반씩 나눠 붙인다
      while len(chunk) > 1900 and "\n" in chunk:
        split_at = chunk.rfind("\n", 0, 1900)
        parts.append(cur + "\n\n" + chunk[:split_at])
        cur, chunk = header, chunk[split_at + 1:]
      if len(cur) + len(chunk) + 2 > 1900 and cur != header:
        parts.append(cur)
        cur = header
      cur += ("\n\n" if cur != header else "") + chunk
    parts.append(cur)
    for part in parts:
      send_discord(part)
  else:
    print("\n\n".join(format_report(r) for r in reports))


if __name__ == "__main__":
  main()
