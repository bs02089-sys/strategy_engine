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
  - 손익: (청산가 - 진입가) / 진입가 — 백테스트와 동일한 수식에
    수수료 반영 (--fee, 기본 0.0007 = 나무멤버스 0.07%. 매수·매도 각각 부과 → 왕복 0.14%)
      순수익 = 청산가×(1-fee) / 진입가×(1+fee) - 1   (--fee 0 = 수수료 미반영, 백테스트 동일)
  - 손실제한/이익실현 % 필드가 있으면 함께 표시 (나무 신규편입 입력값 확인용)
  - 시초가 창 진입 비교 — 창 내(portfolio_config.json > FVG > ENTRY_WINDOW, 기본
    ET 09:30~11:30) 진입 vs 창 외 진입 포지션의 승률/평균/합계 비교 표시
    (시초가 창 필터의 실전 성과 영향을 데이터로 확인)

사용 예:
  python3 fvg_bot_eval.py                       # 전체 평가 리포트 (나무멤버스 0.07% 반영)
  python3 fvg_bot_eval.py --fee 0               # 수수료 미반영 (백테스트와 동일 기준)
  python3 fvg_bot_eval.py --fee 0.0025          # 다른 수수료 (예: 일반 0.25%)
  python3 fvg_bot_eval.py --days 30             # 최근 30일 청산분만
  python3 fvg_bot_eval.py --ticker TQQQ         # 특정 종목만
  python3 fvg_bot_eval.py --path 다른경로.json  # 다른 상태 파일 테스트
  python3 fvg_bot_eval.py --discord             # 리포트를 Discord 웹훅으로 전송 (아침 자동화)
  python3 fvg_bot_eval.py --weekly              # 주간 요약 (주별 승률/총수익/PF/MDD)
  python3 fvg_bot_eval.py --monthly             # 월간 요약 (월별 승률/총수익/PF/MDD)
  python3 fvg_bot_eval.py --monthly --days 90   # 최근 90일(분기)만 월별로 집계
  python3 fvg_bot_eval.py --weekly --discord    # 주간 요약을 Discord로 전송
  python3 fvg_bot_eval.py --monthly-trigger     # 매월 1일 월간 요약 자동 전송 (GHA 아침 트리거)

월간 자동 트리거 (--monthly-trigger):
  - 캘린더 월 기준 — 첫 트레이드가 있던 달의 다음 달 1일부터, 매월 1일 첫 실행 시
    월간 요약을 Discord로 전송 (한국 시간 KST 기준 새 달 진입 감지)
  - 마지막 전송 월은 fvg_eval_state.json에 기록 (로컬↔GHA git 공유, 중복 전송 방지)
  - 전송 성공 시에만 상태 갱신 — 웹훅 미설정 시 상태 불변 (재실행 안전)

Discord 전송:
  - 표준 라이브러리(urllib)만 사용 — 의존성 설치 불필요 (GHA에서 바로 실행)
  - 메시지는 Discord 2000자 제한 안에 요약(통계/분포/종목별/최근 10건)으로 구성
  - 웹훅 미설정(환경변수 DISCORD_WEBHOOK 없음) 시 stdout 출력으로 동작 확인

Dependencies: 없음 (표준 라이브러리만)
==================================================================
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

POSITIONS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "fvg_positions.json"
)

# 월간 자동 트리거 상태 파일 — 마지막 전송일 기록 (로컬↔GHA git 공유)
EVAL_STATE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "fvg_eval_state.json"
)
# 트리거 기준 — 캘린더 월(매월 1일 첫 실행)마다 월간 요약 전송

# 수수료 기본값 — 나무멤버스 해외주식 0.07% (매수·매도 각각 부과 → 왕복 0.14%)
FEE_DEFAULT = 0.0007

# exit_reason 코드 → 한국어 라벨
REASON_LABEL = {
    "TP": "익절(TP)",
    "SL": "손절(SL)",
    "DAY_CLOSE": "당일 마감",
}

# 시초가 창 (진입 시간대 분류) — portfolio_config.json > FVG > ENTRY_WINDOW (ET 시각)
# fvg_signal_bot.py에도 같은 로더가 있지만, 이 파일은 표준 라이브러리 전용이라
# pandas/yfinance를 끌어들이는 fvg_signal_bot을 import할 수 없다 — 의도적 중복.
CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "portfolio_config.json"
)
ENTRY_WINDOW_DEFAULT = {"ENABLED": True, "START": "09:30", "END": "11:30"}


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


def trade_ret(p, fee=FEE_DEFAULT):
  """개별 트레이드 순수익률 (진입가 대비 청산가, 수수료 왕복 반영).

  매수 시 수수료(fee)는 진입가에 더해지고, 매도 시 수수료는 청산가에서
  차감된다 → 순수익률 = 청산가×(1-fee) / 진입가×(1+fee) - 1.
  fee=0이면 백테스트와 동일한 수수료 미반영 수치. 유효하지 않으면 None.
  """
  try:
    entry = float(p.get("entry", 0) or 0)
    exit_p = float(p.get("exit_price", 0) or 0)
    if entry <= 0 or exit_p <= 0:
      return None
    return exit_p * (1 - fee) / (entry * (1 + fee)) - 1
  except (TypeError, ValueError):
    return None


def fmt_pct(v, signed=True):
  return f"{v * 100:+.1f}%" if signed else f"{v * 100:.1f}%"


def load_entry_window():
  """portfolio_config.json의 FVG.ENTRY_WINDOW 설정 로드 — 시초가 창 (ET, HH:MM).

  '창 내/창 외 진입' 분류 기준. 없거나 손상 시 기본값(09:30~11:30)으로 동작
  (fvg_signal_bot.py와 동일한 폴백).
  """
  default = dict(ENTRY_WINDOW_DEFAULT)
  try:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
      cfg = json.load(f)
    w = (cfg.get("FVG") or {}).get("ENTRY_WINDOW") or {}
    return {**default, **{k: v for k, v in w.items() if k in default}}
  except (OSError, json.JSONDecodeError, AttributeError):
    return default


def classify_entry_window(t, window):
  """진입 시각을 시초가 창(ET 벽시각) 안/밖으로 분류 — 'in'/'out' 반환.

  naive 시각(타임존 없음)이거나 파싱 불가(진입 시각 누락)면 None 반환 (분류 제외).
  ET가 아닌 타임존 기록은 America/New_York로 변환해 벽시각 비교. 설정값이
  망가졌으면 기본 창(09:30~11:30)으로 폴백.
  """
  if t is None or t.tzinfo is None:
    return None
  try:
    sh, sm = map(int, window["START"].split(":"))
    eh, em = map(int, window["END"].split(":"))
  except (ValueError, KeyError, AttributeError):
    sh, sm = map(int, ENTRY_WINDOW_DEFAULT["START"].split(":"))
    eh, em = map(int, ENTRY_WINDOW_DEFAULT["END"].split(":"))
  try:
    from zoneinfo import ZoneInfo
    et = t.astimezone(ZoneInfo("America/New_York"))
  except Exception:
    # zoneinfo 불가 환경 폴백: 오프셋이 ET(-4/-5)와 일치하는 기록만 벽시각 사용
    if t.utcoffset() not in (timedelta(hours=-4), timedelta(hours=-5)):
      return None
    et = t
  minutes = et.hour * 60 + et.minute
  start = sh * 60 + sm
  end = eh * 60 + em
  return "in" if start <= minutes < end else "out"


def build_window_compare(rows, window):
  """시초가 창 진입 vs 창 외 진입 성과 비교 — 승률/평균/합계.

  rows: build_report의 rows (ret 포함). 반환:
    {"label": "09:30~11:30 ET", "in": stats, "out": stats, "na": 분류 불가 수}
  stats = {n, win_rate, avg, total} — naive/누락 진입 시각은 na로 집계.
  """
  groups = {"in": [], "out": []}
  na = 0
  for r in rows:
    bucket = classify_entry_window(r["entry_t"], window)
    if bucket is None:
      na += 1
    else:
      groups[bucket].append(r["ret"])

  def _stats(rets):
    st = bucket_stats(rets)  # n/승률/합계 재사용 (기간 집계와 동일 산식)
    n = st["n"]
    return {
        "n": n,
        "win_rate": st["win_rate"],
        "avg": st["total"] / n if n else 0.0,
        "total": st["total"],
    }

  return {
      "label": f"{window['START']}~{window['END']} ET",
      "in": _stats(groups["in"]),
      "out": _stats(groups["out"]),
      "na": na,
  }


def build_report(positions, days=None, ticker=None, fee=FEE_DEFAULT):
  """평가 데이터 수집 — 필터(일수/종목) 적용 후 CLOSED 집계.

  fee: 매수/매도 각각 부과되는 수수료율 (왕복 적용, 기본 나무멤버스 0.07%).
  반환: dict(rows, stats, mdd, by_reason, by_ticker, open_pos, fee)
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
    ret = trade_ret(p, fee)
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

  # 시초가 창 진입 vs 창 외 진입 비교 (분류 기준: portfolio_config.json FVG.ENTRY_WINDOW)
  by_window = build_window_compare(rows, load_entry_window())

  return {
      "rows": rows, "stats": stats, "mdd": mdd,
      "by_reason": by_reason, "by_ticker": by_ticker, "open_pos": open_pos,
      "by_window": by_window, "fee": fee,
  }


PERIOD_LABEL = {"weekly": "주간", "monthly": "월간"}


def period_bucket(rows, mode):
  """트레이드를 주간(ISO)/월간 단위로 묶기 — 최신순 [(key, label, rets)] 반환.

  진입 시각 기준 그룹핑 (타임존은 제거해 비교 통일). 주간은 ISO 주(월~일),
  월간은 연-월. 시각이 없는 항목(손상)은 기간 집계에서 제외.
  """
  buckets = {}
  for r in rows:
    t = r["entry_t"]
    if t is None:
      continue
    t = t.replace(tzinfo=None) if t.tzinfo else t
    if mode == "weekly":
      iso = t.isocalendar()
      key, label = f"{iso[0]}-W{iso[1]:02d}", f"{iso[0]}년 {iso[1]:02d}주"
    else:
      key, label = f"{t.year:04d}-{t.month:02d}", f"{t.year}년 {t.month:02d}월"
    buckets.setdefault(key, {"label": label, "rets": []})
    buckets[key]["rets"].append(r["ret"])  # rows가 시간순이므로 내부도 시간순
  ordered = sorted(buckets.items(), key=lambda kv: kv[0], reverse=True)  # 최신순
  return [(key, meta["label"], meta["rets"]) for key, meta in ordered]


def bucket_stats(rets):
  """기간별 트레이드 묶음의 통계 — 거래수/승률/총수익/PF/MDD."""
  n = len(rets)
  wins = sum(1 for x in rets if x > 0)
  total = sum(rets)
  loss_sum = abs(sum(x for x in rets if x <= 0))
  pf = ((total + loss_sum) / loss_sum) if loss_sum > 0 \
      else (float("inf") if wins else 0.0)
  eq, peak, mdd = 1.0, 1.0, 0.0
  for x in rets:
    eq *= (1 + x)
    peak = max(peak, eq)
    mdd = min(mdd, eq / peak - 1)
  return {
      "n": n,
      "win_rate": wins / n * 100 if n else 0.0,
      "total": total,
      "pf": pf,
      "mdd": mdd,
  }


def build_period(rep, mode):
  """기간별 집계 구성 — (items=[(key,label,stats)] 최신순, overall=전체합계).

  overall은 전체 트레이드를 시간순으로 집계해 일일 리포트(stats/MDD)와 수치를
  일치시킨다 (버킷 연결 순서가 아니라 실제 청산 순서 기준).
  """
  items = []
  for key, label, rets in period_bucket(rep["rows"], mode):
    items.append((key, label, bucket_stats(rets)))
  rows = rep["rows"]
  overall = bucket_stats([r["ret"] for r in rows]) if rows else {
      "n": 0, "win_rate": 0.0, "total": 0.0, "pf": 0.0, "mdd": 0.0}
  return items, overall


def print_period_report(rep, mode, path, days, ticker):
  """주간/월간 요약 리포트 (콘솔) — 기간별 표 + 전체 합계."""
  label = PERIOD_LABEL[mode]
  items, overall = build_period(rep, mode)
  fee = rep["fee"]
  fee_txt = f"수수료 {fee * 100:.2f}% (왕복 {fee * 2 * 100:.2f}%)" if fee > 0 else "수수료 미반영"
  print(f"\n=== FVG 봇 {label} 요약 리포트 ===")
  print(f"출처: {path}  |  " + (f"필터: 최근 {days}일  |  " if days else "")
        + (f"종목: {ticker}  |  " if ticker else "") + f"집계 시각: {datetime.now():%Y-%m-%d %H:%M}")
  print(f"{fee_txt}")

  if not items:
    print("\n  청산(CLOSED)된 트레이드 없음 — 실전 진입/청산 후 다시 실행하세요.")
    if rep["open_pos"]:
      print(f"  (현재 미청산 OPEN {len(rep['open_pos'])}건 — 아직 평가 대상 아님)")
    return

  print(f"\n  {'기간':<10} {'트레이드':>4} {'승률':>5} {'총수익':>7} {'PF':>5} {'MDD':>7}")
  for _, l, st in items:
    pf_txt = f"{st['pf']:.1f}" if st["pf"] != float("inf") else "∞"
    print(f"  {l:<10} {st['n']:>4} {st['win_rate']:>4.0f}% "
          f"{fmt_pct(st['total']):>7} {pf_txt:>5} {st['mdd'] * 100:>6.1f}%")
  pf_txt = f"{overall['pf']:.1f}" if overall["pf"] != float("inf") else "∞"
  print(f"  {'──────':<10} {'────':>4} {'────':>5} {'───────':>7} {'───':>5} {'──────':>7}")
  print(f"  {'합계':<10} {overall['n']:>4} {overall['win_rate']:>4.0f}% "
        f"{fmt_pct(overall['total']):>7} {pf_txt:>5} {overall['mdd'] * 100:>6.1f}%")
  if rep["open_pos"]:
    print(f"\n  (미청산 OPEN {len(rep['open_pos'])}건 — 평가 제외)")


def build_period_discord_message(rep, mode, days, ticker):
  """주간/월간 요약의 Discord 메시지 — 기간별 한 줄 + 전체 합계 (2000자 내)."""
  label = PERIOD_LABEL[mode]
  items, overall = build_period(rep, mode)
  fee = rep["fee"]
  fee_txt = f"수수료 {fee * 100:.2f}% 반영" if fee > 0 else "수수료 미반영"
  head = f"📊 **FVG 봇 {label} 요약 리포트**"
  meta = f"`{datetime.now():%Y-%m-%d %H:%M}` | {fee_txt}" + (
      f" | 최근 {days}일" if days else ""
  ) + (f" | {ticker}" if ticker else "")

  if not items:
    msg = f"{head}\n{meta}\n\n청산된 트레이드 없음 — 실전 진입/청산 후 요약이 쌓입니다."
    if rep["open_pos"]:
      msg += f"\n(미청산 OPEN {len(rep['open_pos'])}건 — 평가 제외)"
    return msg

  lines = [head, meta, ""]
  for _, l, st in items:
    pf_txt = f"{st['pf']:.1f}" if st["pf"] != float("inf") else "∞"
    lines.append(f"· {l} {st['n']}회 | 승률 {st['win_rate']:.0f}% | "
                 f"총수익 {fmt_pct(st['total'])} | PF {pf_txt} | MDD {st['mdd'] * 100:.1f}%")
  pf_txt = f"{overall['pf']:.1f}" if overall["pf"] != float("inf") else "∞"
  lines.append(f"· **합계 {overall['n']}회 | 승률 {overall['win_rate']:.0f}% | "
               f"총수익 {fmt_pct(overall['total'])} | PF {pf_txt} | "
               f"MDD {overall['mdd'] * 100:.1f}%**")
  if rep["open_pos"]:
    lines.append("")
    lines.append(f"미청산 OPEN {len(rep['open_pos'])}건 — 평가 제외")

  msg = "\n".join(lines)
  if len(msg) > 1950:  # Discord 2000자 하드 리밋 안전장치
    msg = msg[:1950] + "\n… (길이 제한으로 생략 — 전체: python3 fvg_bot_eval.py)"
  return msg


def print_report(rep, path, days, ticker):
  stats, rows = rep["stats"], rep["rows"]
  fee = rep["fee"]
  fee_txt = f"수수료 {fee * 100:.2f}% (왕복 {fee * 2 * 100:.2f}%)" if fee > 0 else "수수료 미반영"
  print(f"\n=== FVG 봇 실전 평가 리포트 ===")
  print(f"출처: {path}  |  " + (f"필터: 최근 {days}일  |  " if days else "")
        + (f"종목: {ticker}  |  " if ticker else "") + f"집계 시각: {datetime.now():%Y-%m-%d %H:%M}")
  print(f"{fee_txt}")

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

  # 시초가 창 진입 비교
  bw = rep["by_window"]
  print(f"\n  [시초가 창 진입 비교] (기준 {bw['label']})")
  if bw["in"]["n"] == 0 and bw["out"]["n"] == 0:
    print(f"    (진입 시각 분류 불가 {bw['na']}건 — 타임존 없는 기록)")
  else:
    for label, key in (("창 내 진입", "in"), ("창 외 진입", "out")):
      s = bw[key]
      if s["n"] == 0:
        print(f"    {label:<6} 0회")
        continue
      print(f"    {label:<6} {s['n']:>3}회 | 승률 {s['win_rate']:.0f}% | "
            f"평균 {fmt_pct(s['avg'])} | 합계 {fmt_pct(s['total'])}")
    if bw["na"]:
      print(f"    (진입 시각 분류 불가 {bw['na']}건 — 타임존 없는 기록)")

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


def _kst_today():
  """한국 시간(UTC+9, 고정 — 한국은 서머타임 없음) 기준 오늘.

  '매월 1일 아침(KST)' 판정 기준 — 워크플로우가 23:00 UTC(=익일 08:00 KST)에
  도는 시점에도 KST 날짜로 새 달 진입을 정확히 감지한다.
  """
  return (datetime.now(timezone.utc) + timedelta(hours=9)).date()


def _month_key(s):
  """상태 값 → 'YYYY-MM' — 날짜(YYYY-MM-DD) 또는 월(YYYY-MM) 형식 모두 허용.

  구버전 상태(전송일 날짜)와 신버전(전송 월)을 함께 처리하는 하위 호환용.
  """
  for fmt in ("%Y-%m-%d", "%Y-%m"):
    try:
      return datetime.strptime(s, fmt).strftime("%Y-%m")
    except ValueError:
      continue
  return None


def _load_state(path):
  """트리거 상태(fvg_eval_state.json) 로드 — 없거나 손상 시 빈 dict."""
  try:
    with open(path, "r", encoding="utf-8") as f:
      data = json.load(f)
    return data if isinstance(data, dict) else {}
  except (OSError, json.JSONDecodeError):
    return {}


def _save_state(path, state):
  """트리거 상태를 원자적(atomic) 저장 — 크래시 시 파일 손상 방지."""
  try:
    import tempfile
    import shutil
    with tempfile.NamedTemporaryFile(
        "w", delete=False, suffix=".json", encoding="utf-8"
    ) as tmp:
      json.dump(state, tmp, ensure_ascii=False, indent=2)
      tmp_path = tmp.name
    shutil.move(tmp_path, path)
  except OSError:
    print("[경고] 트리거 상태 파일 저장 실패 (무시하고 계속)")


def monthly_trigger_due(positions, last_sent, today=None):
  """월간 요약 전송 조건 판정 — 캘린더 월 기준(매월 1일 첫 실행) 시 True.

  - 전송 대상: 이번 달(KST)이 직전 전송 월과 달라진 첫 실행
    (첫 전송은 첫 트레이드가 있던 달의 다음 달 1일부터)
  - 트레이드가 하나도 없으면 False (아직 평가 데이터 없음)
  - 손상된 상태 값은 첫 트레이드 달 기준으로 폴백
  """
  first_trade = None
  for p in positions.values():
    t = parse_entry_time(p)
    if t is None:
      continue
    t = t.replace(tzinfo=None) if t.tzinfo else t
    if first_trade is None or t < first_trade:
      first_trade = t
  if first_trade is None:
    return False
  now = today or _kst_today()
  today_d = now.date() if hasattr(now, "date") else now  # datetime/date 둘 다 허용
  cur_month = today_d.strftime("%Y-%m")
  # 첫 트레이드 기준은 ET(진입 시각), 전송 판정은 KST — 월 단위 비교라 경계
  # 근처(월말~월초 9시간)에서만 달라질 수 있어 실질 영향 없음
  first_month = first_trade.date().strftime("%Y-%m")
  sent_month = _month_key(last_sent) if last_sent else None
  if sent_month is None:  # 미전송 또는 손상 값 → 첫 트레이드 달 기준
    return cur_month != first_month
  return cur_month != sent_month


def run_monthly_trigger(positions, state_path, fee):
  """캘린더 월(매월 1일) 기준으로 월간 요약을 Discord로 전송 — 성공 시에만 상태 갱신.

  반환: True(전송 성공) / False(미전송 — 대기 또는 웹훅 미설정).
  """
  state = _load_state(state_path)
  last_sent = state.get("monthly_last_sent")
  if not monthly_trigger_due(positions, last_sent):
    print("[트리거] 아직 새 달 진입 전 — 월간 요약 대기 중 (매월 1일 전송)")
    return False
  rep = build_report(positions, fee=fee)
  if not send_discord_webhook(build_period_discord_message(rep, "monthly", None, None)):
    print("[트리거] 웹훅 미설정/실패 — 상태 미갱신 (재실행 시 재시도)")
    return False
  state["monthly_last_sent"] = _kst_today().strftime("%Y-%m")
  _save_state(state_path, state)
  print("[트리거] 월간 요약 전송 완료 — 다음 트리거는 다음 달 1일")
  return True


def send_discord_webhook(message):
  """Discord 웹훅으로 메시지 전송 (표준 라이브러리 urllib만 사용).

  의존성(requests 등) 없이 동작해 GHA에서 pip install 없이 바로 실행된다.
  웹훅 미설정 시 stdout 출력 후 False 반환 (동작 확인용).
  """
  webhook = os.getenv("DISCORD_WEBHOOK", "")
  if not webhook or webhook == "YOUR_DISCORD_WEBHOOK":
    print(message)
    return False
  import urllib.request
  payload = json.dumps({"content": message}).encode("utf-8")
  req = urllib.request.Request(
      webhook, data=payload,
      headers={"Content-Type": "application/json"}, method="POST")
  try:
    with urllib.request.urlopen(req, timeout=10) as resp:
      if resp.status == 204:  # Discord 웹훅 정상 응답
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 알림 전송 성공")
        return True
      print(f"전송 실패: HTTP {resp.status}")
  except Exception as exc:
    print(f"웹훅 전송 중 에러 발생: {exc}")
  return False


def build_discord_message(rep, days, ticker):
  """Discord용 요약 메시지 — 2000자 제한 안에 통계/분포/종목별/최근 트레이드.

  콘솔 리포트(print_report)의 전체 트레이드 표 대신 최근 10건만 포함해
  아침 알림이 길어지지 않게 한다. 전체 보기는 로컬 실행으로 대체.
  """
  stats, rows = rep["stats"], rep["rows"]
  fee = rep["fee"]
  fee_txt = f"수수료 {fee * 100:.2f}% 반영" if fee > 0 else "수수료 미반영"
  pf_txt = f"{stats['pf']:.1f}" if stats["pf"] != float("inf") else "∞"
  head = f"📊 **FVG 봇 실전 평가 리포트**"
  meta = f"`{datetime.now():%Y-%m-%d %H:%M}` | {fee_txt}" + (
      f" | 최근 {days}일" if days else ""
  ) + (f" | {ticker}" if ticker else "")

  if not rows:
    msg = f"{head}\n{meta}\n\n청산된 트레이드 없음 — 실전 진입/청산 후 리포트가 쌓입니다."
    if rep["open_pos"]:
      msg += f"\n(미청산 OPEN {len(rep['open_pos'])}건 — 평가 제외)"
    return msg

  lines = [head, meta, "",
           f"**트레이드 {stats['n']}회 | 승률 {stats['win_rate']:.0f}% | "
           f"평균 {fmt_pct(stats['avg'])} | 총수익 {fmt_pct(stats['total'])} | "
           f"MDD {rep['mdd'] * 100:.1f}%**",
           f"평균익절 {fmt_pct(stats['avg_win'])} | 평균손절 {fmt_pct(stats['avg_loss'])} | "
           f"최대손실 {fmt_pct(stats['max_loss'])} | PF {pf_txt}", "", "[청산 사유 분포]"]
  for reason in sorted(rep["by_reason"], key=lambda k: -rep["by_reason"][k]["n"]):
    r = rep["by_reason"][reason]
    avg = sum(r["rets"]) / len(r["rets"])
    lines.append(f"· {REASON_LABEL.get(reason, reason)} {r['n']}회 "
                 f"({r['n'] / stats['n'] * 100:.0f}%) 평균 {fmt_pct(avg)}")
  if len(rep["by_ticker"]) > 1:
    lines.append("")
    lines.append("[종목별]")
    for t in sorted(rep["by_ticker"], key=lambda k: -rep["by_ticker"][k]["n"]):
      r = rep["by_ticker"][t]
      avg = sum(r["rets"]) / len(r["rets"])
      lines.append(f"· {t} {r['n']}회 | 승률 "
                   f"{sum(1 for x in r['rets'] if x > 0) / len(r['rets']) * 100:.0f}% | "
                   f"합계 {fmt_pct(sum(r['rets']))}")
  bw = rep["by_window"]
  lines.append("")
  lines.append(f"[시초가 창 진입 비교] (기준 {bw['label']})")
  if bw["in"]["n"] == 0 and bw["out"]["n"] == 0:
    lines.append(f"· (진입 시각 분류 불가 {bw['na']}건)")
  else:
    for label, key in (("창 내", "in"), ("창 외", "out")):
      s = bw[key]
      if s["n"] == 0:
        lines.append(f"· {label} 진입 0회")
        continue
      lines.append(f"· {label} 진입 {s['n']}회 | 승률 {s['win_rate']:.0f}% | "
                   f"평균 {fmt_pct(s['avg'])} | 합계 {fmt_pct(s['total'])}")
    if bw["na"]:
      lines.append(f"· (진입 시각 분류 불가 {bw['na']}건)")
  lines.append("")
  lines.append("[최근 트레이드]")
  for r in list(reversed(rows[-10:])):
    p = r["pos"]
    t_in = (r["entry_t"] or datetime.min).strftime("%m-%d %H:%M")
    reason = REASON_LABEL.get(p.get("exit_reason", "?"), p.get("exit_reason", "?"))
    lines.append(f"· {t_in} {reason} {r['ret'] * 100:+.1f}% "
                 f"({p.get('ticker', '?')} {float(p.get('entry', 0)):.2f}→"
                 f"{float(p.get('exit_price', 0)):.2f})")
  if len(rows) > 10:
    lines.append(f"· … 나머지 {len(rows) - 10}건 (전체: python3 fvg_bot_eval.py)")
  if rep["open_pos"]:
    lines.append("")
    lines.append(f"미청산 OPEN {len(rep['open_pos'])}건 — 평가 제외")

  msg = "\n".join(lines)
  if len(msg) > 1950:  # Discord 2000자 하드 리밋 안전장치
    msg = msg[:1950] + "\n… (길이 제한으로 생략 — 전체: python3 fvg_bot_eval.py)"
  return msg


def main():
  p = argparse.ArgumentParser(description="FVG 봇 실전 평가 (fvg_positions.json)")
  p.add_argument("--days", type=int, default=None, help="최근 N일 청산분만 평가")
  p.add_argument("--ticker", default=None, help="특정 종목만 평가 (예: TQQQ)")
  p.add_argument("--path", default=POSITIONS_PATH, help="포지션 파일 경로 (기본 fvg_positions.json)")
  p.add_argument("--fee", type=float, default=FEE_DEFAULT,
                 help=f"매수·매도 각각 부과 수수료율 (기본 {FEE_DEFAULT * 100:g}%% = 나무멤버스, --fee 0 = 미반영)")
  p.add_argument("--discord", action="store_true",
                 help="리포트를 Discord 웹훅으로 전송 (GHA 아침 자동화용, 의존성 불필요)")
  p.add_argument("--state", default=EVAL_STATE_PATH,
                 help="트리거 상태 파일 경로 (기본 fvg_eval_state.json)")
  period = p.add_mutually_exclusive_group()
  period.add_argument("--weekly", action="store_true",
                      help="주간 요약 리포트 (주별 승률/총수익/PF/MDD)")
  period.add_argument("--monthly", action="store_true",
                      help="월간 요약 리포트 (월별 승률/총수익/PF/MDD)")
  period.add_argument("--monthly-trigger", action="store_true",
                      help="매월 1일 월간 요약 자동 전송 (상태 파일로 중복 방지, GHA 아침 트리거)")
  args = p.parse_args()
  if args.fee < 0:
    print(f"[오류] --fee는 음수일 수 없습니다: {args.fee}")
    sys.exit(1)

  positions = load_positions(args.path)
  if not positions:
    if args.discord:
      # 빈 상태라도 한 줄 전송 — 아침 파이프라인이 살아있음을 확인하는 alive-check
      send_discord_webhook(
          "📊 **FVG 봇 실전 평가 리포트**\n"
          f"`{datetime.now():%Y-%m-%d %H:%M}` | 수수료 {args.fee * 100:.2f}% 반영\n\n"
          "아직 포지션 기록이 없습니다 — 실전 진입 알림이 발생하면 "
          "fvg_positions.json에 자동으로 쌓여 아침 리포트가 시작됩니다."
      )
      return
    print("[안내] 포지션 기록이 없습니다 — 실전 진입 알림이 발생하면 fvg_positions.json에 "
          "자동으로 쌓입니다.")
    return

  if args.monthly_trigger:
    run_monthly_trigger(positions, args.state, args.fee)
    return

  rep = build_report(positions, days=args.days, ticker=args.ticker, fee=args.fee)
  if args.weekly or args.monthly:
    mode = "weekly" if args.weekly else "monthly"
    if args.discord:
      send_discord_webhook(
          build_period_discord_message(rep, mode, args.days, args.ticker))
    else:
      print_period_report(rep, mode, args.path, args.days, args.ticker)
    return
  if args.discord:
    send_discord_webhook(build_discord_message(rep, args.days, args.ticker))
    return
  print_report(rep, args.path, args.days, args.ticker)


if __name__ == "__main__":
  main()
