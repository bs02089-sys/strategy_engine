# -*- coding: utf-8 -*-
"""
==================================================================
 fvg_bot_eval.py — FVG 봇 실전 평가 리포트 (fvg_positions.json 기반)
==================================================================
fvg_signal_bot.py가 진입 알림 시 fvg_positions.json에 기록한 포지션을 읽어,
실전 트레이딩 성과를 자동으로 평가한다. 백테스트(fvg_bot_backtest.py)와 같은
지표 양식(승률/평균/총수익/MDD/PF/TP·SL·마감 분포)을 실전 데이터로 재현.

  - 데이터: fvg_positions.json (봇이 진입/청산 시 자동 기록)
      key = "TICKER|FVG생성시각", value = {ticker, entry, sl, tp, status,
             exit_price, exit_reason(TP/SL/DAY_CLOSE), closed_at, ...}
  - 통계: CLOSED 포지션만 집계 (OPEN은 미청산 상태로 따로 표시)
  - 손익: (청산가 - 진입가) / 진입가 — 백테스트와 동일, 수수료/슬리피지 미반영
  - 손실제한/이익실현 % 필드가 있으면 함께 표시 (나무 신규편입 입력값 확인용)

사용 예:
  python3 fvg_bot_eval.py                       # 전체 평가 리포트
  python3 fvg_bot_eval.py --days 30             # 최근 30일 청산분만
  python3 fvg_bot_eval.py --ticker TQQQ         # 특정 종목만
  python3 fvg_bot_eval.py --path 다른경로.json  # 다른 상태 파일 테스트

Dependencies: 없음 (표준 라이브러리만)
==================================================================
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta

POSITIONS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "fvg_positions.json"
)

# exit_reason 코드 → 한국어 라벨
REASON_LABEL = {
    "TP": "익절(TP)",
    "SL": "손절(SL)",
    "DAY_CLOSE": "당일 마감",
}


def load_positions(path):
  """fvg_positions.json 로드 — 없거나 손상 시 빈 dict 반환 (크래시 방지)."""
  try:
    with open(path, "r", encoding="utf-8") as f:
      data = json.load(f)
    if not isinstance(data, dict):
      print(f"[경고] {path}: dict 형식 아님 — 빈 결과로 처리")
      return {}
    return data
  except (OSError, json.JSONDecodeError) as exc:
    print(f"[경고] {path} 읽기 실패: {exc}")
    return {}


def parse_entry_time(p):
  """포지션 진입 시각 파싱 — 실패 시 None (손상 항목 내성)."""
  try:
    return datetime.fromisoformat(str(p["entry_time"]))
  except (KeyError, TypeError, ValueError):
    return None


def trade_ret(p):
  """개별 트레이드 수익률 (진입가 대비 청산가). 유효하지 않으면 None."""
  try:
    entry = float(p.get("entry", 0) or 0)
    exit_p = float(p.get("exit_price", 0) or 0)
    if entry <= 0 or exit_p <= 0:
      return None
    return exit_p / entry - 1
  except (TypeError, ValueError):
    return None


def fmt_pct(v, signed=True):
  return f"{v * 100:+.1f}%" if signed else f"{v * 100:.1f}%"


def build_report(positions, days=None, ticker=None):
  """평가 데이터 수집 — 필터(일수/종목) 적용 후 CLOSED 집계.

  반환: dict(rows, stats, mdd, by_reason, by_ticker, open_pos)
  """
  now = datetime.now()
  closed, open_pos = [], []
  for p in positions.values():
    if not isinstance(p, dict):
      continue  # 손상 항목 건너뜀
    if ticker and p.get("ticker") != ticker:
      continue
    if days is not None:
      t = parse_entry_time(p)
      if t is None:
        continue
      # 타임존 내성: 진입 시각이 offset-aware(-04:00 등)면 now도 동일 타임존으로
      # 변환해 비교 — naive(타임존 없는 ISO)는 그대로 사용
      now_cmp = datetime.now(tz=t.tzinfo) if t.tzinfo else now
      if now_cmp - t > timedelta(days=days):
        continue
    if p.get("status") == "OPEN":
      open_pos.append(p)
    else:
      closed.append(p)

  # CLOSED 집계 (손익 계산 가능한 항목만)
  rows = []
  for p in closed:
    ret = trade_ret(p)
    if ret is None:
      continue
    rows.append({"pos": p, "ret": ret, "entry_t": parse_entry_time(p)})

  def _sort_key(r):
    """정렬 키 — 혼합 타임존(naive/aware)이어도 비교 크래시 없도록 통일."""
    t = r["entry_t"]
    if t is None:
      return (datetime.min, r["pos"].get("entry", 0))
    t = t.replace(tzinfo=None) if t.tzinfo else t
    return (t, r["pos"].get("entry", 0))

  rows.sort(key=_sort_key)

  rets = [r["ret"] for r in rows]
  wins = [r for r in rows if r["ret"] > 0]
  losses = [r for r in rows if r["ret"] <= 0]
  loss_sum = abs(sum(r["ret"] for r in losses))

  stats = {
      "n": len(rows),
      "win_rate": len(wins) / len(rows) * 100 if rows else 0.0,
      "avg": sum(rets) / len(rets) if rows else 0.0,
      "avg_win": sum(r["ret"] for r in wins) / len(wins) if wins else 0.0,
      "avg_loss": sum(r["ret"] for r in losses) / len(losses) if losses else 0.0,
      "max_loss": min(rets) if rets else 0.0,
      "total": sum(rets),
      "pf": (sum(r["ret"] for r in wins) / loss_sum) if loss_sum > 0
            else (float("inf") if wins else 0.0),
  }

  # 지분곡선 기반 MDD (복리 가정, 트레이드 순서대로)
  eq, peak, mdd = 1.0, 1.0, 0.0
  for r in rows:
    eq *= (1 + r["ret"])
    peak = max(peak, eq)
    mdd = min(mdd, eq / peak - 1)

  by_reason = {}
  for r in rows:
    reason = r["pos"].get("exit_reason", "?")
    by_reason.setdefault(reason, {"n": 0, "rets": []})
    by_reason[reason]["n"] += 1
    by_reason[reason]["rets"].append(r["ret"])

  by_ticker = {}
  for r in rows:
    t = r["pos"].get("ticker", "?")
    by_ticker.setdefault(t, {"n": 0, "rets": []})
    by_ticker[t]["n"] += 1
    by_ticker[t]["rets"].append(r["ret"])

  return {
      "rows": rows, "stats": stats, "mdd": mdd,
      "by_reason": by_reason, "by_ticker": by_ticker, "open_pos": open_pos,
  }


def print_report(rep, path, days, ticker):
  stats, rows = rep["stats"], rep["rows"]
  print(f"\n=== FVG 봇 실전 평가 리포트 ===")
  print(f"출처: {path}  |  " + (f"필터: 최근 {days}일  |  " if days else "")
        + (f"종목: {ticker}  |  " if ticker else "") + f"집계 시각: {datetime.now():%Y-%m-%d %H:%M}")

  if not rows:
    print("\n  청산(CLOSED)된 트레이드 없음 — 실전 진입/청산 후 다시 실행하세요.")
    if rep["open_pos"]:
      print(f"  (현재 미청산 OPEN {len(rep['open_pos'])}건 — 아직 평가 대상 아님)")
    return

  pf_txt = f"{stats['pf']:.1f}" if stats["pf"] != float("inf") else "∞"
  print(f"\n  트레이드 {stats['n']}회 | 승률 {stats['win_rate']:.0f}% | "
        f"평균 {fmt_pct(stats['avg'])} | 총수익 {fmt_pct(stats['total'])} | MDD {rep['mdd'] * 100:.1f}%")
  print(f"  평균익절 {fmt_pct(stats['avg_win'])} | 평균손절 {fmt_pct(stats['avg_loss'])} | "
        f"최대손실 {fmt_pct(stats['max_loss'])} | PF {pf_txt}")

  # 청산 사유 분포
  print(f"\n  [청산 사유 분포]")
  for reason in sorted(rep["by_reason"], key=lambda k: -rep["by_reason"][k]["n"]):
    r = rep["by_reason"][reason]
    avg = sum(r["rets"]) / len(r["rets"])
    label = REASON_LABEL.get(reason, reason)
    print(f"    {label:<8} {r['n']:>3}회 ({r['n'] / stats['n'] * 100:>3.0f}%) "
          f"평균 {fmt_pct(avg)}")

  # 종목별
  if len(rep["by_ticker"]) > 1:
    print(f"\n  [종목별]")
    for t in sorted(rep["by_ticker"], key=lambda k: -rep["by_ticker"][k]["n"]):
      r = rep["by_ticker"][t]
      avg = sum(r["rets"]) / len(r["rets"])
      print(f"    {t:<6} {r['n']:>3}회 | 승률 "
            f"{sum(1 for x in r['rets'] if x > 0) / len(r['rets']) * 100:.0f}% | "
            f"평균 {fmt_pct(avg)} | 합계 {fmt_pct(sum(r['rets']))}")

  # 개별 트레이드
  print(f"\n  {'진입':>16} {'청산':>16} {'종목':>5} {'사유':<8} {'수익':>7} {'진입가':>8} {'청산가':>8}")
  for r in rows:
    p = r["pos"]
    t_in = (r["entry_t"] or datetime.min).strftime("%y-%m-%d %H:%M")
    reason = p.get("exit_reason", "?")
    print(f"  {t_in:>16} {reason:<8} {p.get('ticker', '?'):>5} "
          f"{r['ret'] * 100:+6.1f}% {float(p.get('entry', 0)):>8.2f} "
          f"{float(p.get('exit_price', 0)):>8.2f}")

  if rep["open_pos"]:
    print(f"\n  [미청산 OPEN {len(rep['open_pos'])}건 — 평가 제외]")
    for p in rep["open_pos"]:
      t = parse_entry_time(p)
      print(f"    {p.get('ticker', '?'):>5} 진입 {str(t or '?'):>16} @ "
            f"{float(p.get('entry', 0)):.2f} (SL {float(p.get('sl', 0)):.2f} / "
            f"TP {float(p.get('tp', 0)):.2f})")


def main():
  p = argparse.ArgumentParser(description="FVG 봇 실전 평가 (fvg_positions.json)")
  p.add_argument("--days", type=int, default=None, help="최근 N일 청산분만 평가")
  p.add_argument("--ticker", default=None, help="특정 종목만 평가 (예: TQQQ)")
  p.add_argument("--path", default=POSITIONS_PATH, help="포지션 파일 경로 (기본 fvg_positions.json)")
  args = p.parse_args()

  positions = load_positions(args.path)
  if not positions:
    print("[안내] 포지션 기록이 없습니다 — 실전 진입 알림이 발생하면 fvg_positions.json에 "
          "자동으로 쌓입니다.")
    return

  rep = build_report(positions, days=args.days, ticker=args.ticker)
  print_report(rep, args.path, args.days, args.ticker)


if __name__ == "__main__":
  main()
