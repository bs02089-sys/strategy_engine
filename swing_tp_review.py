# -*- coding: utf-8 -*-
"""
==================================================================
 swing_tp_review.py — TP 승수 분기 재평가 (3개월 주기 자동 실행)
==================================================================
익절(TP) 승수(swing_config.json의 TAKE_PROFIT_ATR_BY_TICKER)가 실전에서
여전히 합리적인지 분기마다 자동으로 재평가한다. **조건부 자동 적용**:
표본 충분 + 실전 성과가 백테스트 기대 대비 저조(ADJUST)일 때만 스윕 최적
승수를 swing_config.json에 자동 반영한다(워크플로우가 커밋). 변경 근거는
Discord 보고서에 명시된다. AUTO_UPDATE=false 로 설정하면 보고만 하고
자동 변경하지 않는다 (수동 모드).

평가 논리:
  1. 실전 신호 저널(swing_signals.jsonl)에서 종목별 청산 트레이드 수 확인
  2. 표본 부족(< MIN_TRADES) → "데이터 부족 — 승수 유지 권장" 보고 후 종료
  3. 표본 충분 → 실전 성과(승률/평균/총수익) vs 백테스트 기대 비교
  4. 백테스트 TP 스윕 재실행 → 현재 승수의 순위/최적 승수 확인
  5. 종합 권고:
       - 실전 성과가 백테스트 기대와 유사 → KEEP (현행 유지)
       - 실전이 기대보다 크게 나쁨(예: 평균수익이 절반 이하) → ADJUST 권고
       - 스윕 최적 승수와 현재 승수가 크게 다르면 참고 제시 (실전 데이터 우선)

실행:
  python3 swing_tp_review.py                 # 콘솔 출력
  python3 swing_tp_review.py --discord       # Discord 전송 (GitHub Actions)
  python3 swing_tp_review.py --min-trades 3  # 표본 기준 조정 (기본 5)

Dependencies: pandas, numpy, yfinance (requirements.txt에 포함)
==================================================================
"""
import argparse
import json
import os
import shutil
import sys
import tempfile
import warnings

import numpy as np

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import swing_bot as bot          # TAKE_PROFIT_ATR_BY_TICKER 참조
import swing_bot_backtest as sbt  # 백테스트 재사용
import swing_bot_eval as ev       # 실전 신호 평가 재사용

MIN_TRADES = 5          # 종목당 청산 트레이드 수 기준 — 미만이면 표본 부족
TP_SWEEP = [0.0, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0]
PERF_GAP = 0.5          # 실전 평균수익이 백테스트 기대의 이 비율 미만이면 조정 권고


def decide_apply(result, auto_update):
  """ADJUST 결과에 대한 자동 적용 대상 승수 결정 (순수 함수 — 테스트 용이).

  적용 조건: ADJUST 판정 + 자동 적용 활성 + 스윕 최적값이 현행과 다르고
  개선 폭이 1%p를 초과할 때. 적용 대상이 없으면 None 반환 (승수 유지).
  """
  if not auto_update or result.get("verdict") != "ADJUST":
    return None
  bt = result.get("backtest") or {}
  best_tp = bt.get("best_tp")
  if best_tp is None or abs(best_tp - result["current_tp"]) < 1e-9:
    return None
  # 개선 폭이 미미하면 변경하지 않음 — 표본이 작아 최적값이 노이즈일 수 있으므로
  if bt.get("best_total", 0.0) <= bt.get("cur_total", 0.0) + 1.0:
    return None
  return best_tp


def apply_tp_changes(changes, path=None):
  """종목별 승수 변경을 swing_config.json에 반영 (원자적 저장).

  changes: {ticker: new_multiplier}. 기존 파일의 다른 키(_comment 등)는 보존.
  path를 주면 해당 경로에 저장(테스트용). 성공 시 True 반환.
  """
  path = path or bot.CONFIG_PATH
  try:
    with open(path, "r", encoding="utf-8") as f:
      cfg = json.load(f)
  except (json.JSONDecodeError, OSError):
    cfg = {}
  # 기본 키 보장(AUTO_UPDATE/TAKE_PROFIT_ATR 등) + 사용자 키(_comment 등) 보존
  merged = dict(bot._load_config(path))
  merged.update({k: v for k, v in cfg.items()})
  by_ticker = dict(merged.get("TAKE_PROFIT_ATR_BY_TICKER") or {})
  by_ticker.update({str(t).upper(): float(v) for t, v in changes.items()})
  merged["TAKE_PROFIT_ATR_BY_TICKER"] = by_ticker
  try:
    with tempfile.NamedTemporaryFile(
        "w", delete=False, suffix=".json", encoding="utf-8"
    ) as tmp:
      json.dump(merged, tmp, ensure_ascii=False, indent=4)
      tmp_path = tmp.name
    shutil.move(tmp_path, path)
    return True
  except OSError as exc:
    print(f"[오류] swing_config.json 저장 실패: {exc}")
    return False


def sweep_tp(df, tp_values):
  """로드된 4h 데이터로 TP 승수 스윕 — 총수익/MDD/PF/평균수익/트레이드 수 반환.

  df: sbt.add_indicators(sbt.load_bars(...)) 결과 (호출부에서 1회만 로드)
  """
  if len(df) < 51:
    return {}
  out = {}
  for tp in tp_values:
    trades, eq = sbt.backtest(df, "ema20", 0.0, "close", entry="contr", tp_atr=tp)
    if not trades:
      out[tp] = {"total": 0.0, "mdd": 0.0, "pf": 0.0, "avg": 0.0, "n": 0}
      continue
    rets = np.array([t[4] for t in trades])
    wins, losses = rets[rets > 0], rets[rets <= 0]
    cum = eq[-1] / eq[0] - 1
    dd = (eq / np.maximum.accumulate(eq) - 1).min() * 100
    pf = wins.sum() / abs(losses.sum()) if len(losses) else float("inf")
    out[tp] = {
        "total": cum * 100, "mdd": dd, "pf": pf,
        "avg": rets.mean() * 100, "n": len(trades),
    }
  return out


def review_ticker(ticker, records, min_trades=MIN_TRADES):
  """종목 1개의 재평가 결과 dict 생성."""
  rep = ev.build_report(ticker, records)
  stats = rep["stats"]
  current_tp = bot.TAKE_PROFIT_ATR_BY_TICKER.get(ticker, bot.TAKE_PROFIT_ATR)

  result = {
      "ticker": ticker,
      "current_tp": current_tp,
      "n_closed": stats.get("n_closed", 0),
      "verdict": "INSUFFICIENT_DATA",
      "detail": "",
  }

  if stats.get("none") or stats["n_closed"] < min_trades:
    result["detail"] = (
        f"표본 부족({result['n_closed']}건 < {min_trades}건) — "
        "현행 승수 유지 권장, 다음 분기에 재평가"
    )
    return result

  # 실전 성과
  live_avg = stats["avg_ret"]
  live_win = stats["win_rate"]
  live_total = stats["total_return"]

  # 백테스트: 데이터 1회 로드 → 현재 승수 vs 스윕 (중복 다운로드 방지)
  df = sbt.add_indicators(sbt.load_bars(ticker, "4h"))
  sweep = sweep_tp(df, TP_SWEEP)
  cur = sweep.get(current_tp, {})
  cur_total = cur.get("total", 0.0)
  # 트레이드가 전혀 없는 승수(전부 total 0.0)는 후보에서 제외 — 0×ATR(익절 끔)이
  # 잘못 최적으로 뽑히는 것을 방지
  cands = {k: v for k, v in sweep.items() if v.get("n", 0) > 0}
  best_tp = max(cands, key=lambda k: cands[k]["total"]) if cands else None
  best_total = cands[best_tp]["total"] if best_tp is not None else 0.0
  # 백테스트 기대값 (현재 승수) — 스윕 결과에서 직접 참조
  exp_avg = cur.get("avg") if cur else None

  # 판정: 실전 평균수익이 백테스트 기대의 PERF_GAP 미만이면 조정 권고
  if exp_avg is not None and exp_avg > 0 and live_avg < exp_avg * PERF_GAP:
    verdict = "ADJUST"
    detail = (
        f"실전 평균수익 {live_avg:+.1f}% vs 백테스트 기대 {exp_avg:+.1f}% — "
        f"기대의 {PERF_GAP*100:.0f}% 미만. 승수 재검토 권장."
    )
  else:
    verdict = "KEEP"
    detail = (
        f"실전 평균수익 {live_avg:+.1f}% (기대 {exp_avg:+.1f}%) — "
        "현행 승수 유지 권장."
    )

  result.update({
      "verdict": verdict,
      "detail": detail,
      "live": {"avg": live_avg, "win": live_win, "total": live_total},
      "backtest": {
          "cur_total": cur_total, "cur_mdd": cur.get("mdd", 0.0),
          "cur_pf": cur.get("pf", 0.0), "cur_n": cur.get("n", 0),
          "best_tp": best_tp, "best_total": best_total,
          "exp_avg": exp_avg,
      },
  })
  # 조건부 자동 적용: ADJUST + 자동 적용 활성 + 개선 폭 충분할 때만 대상 승수 결정
  result["applied_tp"] = decide_apply(result, bot.AUTO_UPDATE)
  result["applied"] = result["applied_tp"] is not None
  return result


def format_result(r):
  """평가 결과 dict → Discord용 문자열."""
  t = r["ticker"]
  lines = [f"🔁 **[{t}] TP 승수 분기 재평가** (현행 {r['current_tp']:g}×ATR)"]
  lines.append(f"- 청산 트레이드 {r['n_closed']}건 | {r['detail']}")
  if r["verdict"] == "INSUFFICIENT_DATA":
    return "\n".join(lines)

  b = r["backtest"]
  verdict_icon = "✅ 유지" if r["verdict"] == "KEEP" else "⚠️ 조정 권고"
  lines.append(f"- 판정: **{verdict_icon}**")
  lines.append(
      f"- 실전: 승률 {r['live']['win']:.0f}% | 평균 {r['live']['avg']:+.1f}% | "
      f"총수익 {r['live']['total']:+.1f}%"
  )
  lines.append(
      f"- 백테스트(2y, contr): 현재 {r['current_tp']:g}×ATR → "
      f"총 {b['cur_total']:+.1f}% / MDD {b['cur_mdd']:.1f}% / PF {b['cur_pf']:.1f}"
  )
  if b["best_tp"] is not None:
    best_note = "(현행)" if abs(b["best_tp"] - r["current_tp"]) < 1e-9 else ""
    lines.append(
        f"- 스윕 최적: {b['best_tp']:g}×ATR → 총 {b['best_total']:+.1f}% {best_note}"
    )
  if r.get("applied"):
    lines.append(
        f"- ✅ **자동 적용**: {r['current_tp']:g}×ATR → **{r['applied_tp']:g}×ATR**"
        " (swing_config.json 반영·커밋)"
    )
  elif not bot.AUTO_UPDATE:
    lines.append("- 🔒 자동 적용 OFF (`AUTO_UPDATE=false`) — 승수 변경 시 수동 반영")
  else:
    lines.append("- ✋ 승수 변경 없음")
  return "\n".join(lines)


def main():
  p = argparse.ArgumentParser(description="TP 승수 분기 재평가 (3개월 주기)")
  p.add_argument("--min-trades", type=int, default=MIN_TRADES,
                 help=f"종목당 최소 청산 트레이드 수 (기본 {MIN_TRADES})")
  p.add_argument("--discord", action="store_true", help="결과를 Discord로 전송")
  args = p.parse_args()

  records = ev.load_signals()
  if not records:
    print("신호 저널이 비어 있습니다 — 아직 실전 신호가 없습니다.")
    return

  tickers = sorted({r["ticker"] for r in records})
  results = [review_ticker(t, records, min_trades=args.min_trades) for t in tickers]

  # 조건부 자동 적용: ADJUST 확정된 종목만 swing_config.json에 반영
  changes = {r["ticker"]: r["applied_tp"] for r in results if r.get("applied")}
  if changes:
    if apply_tp_changes(changes):
      print(f"✅ swing_config.json 자동 갱신: {changes}")
    else:
      # 저장 실패 시 보고서의 '적용됨' 표시도 지운다 — 실제 반영되지 않은
      # 변경을 적용된 것처럼 보고하지 않도록 (정직한 보고)
      for r in results:
        r["applied"] = False
      print("❌ swing_config.json 저장 실패 — 변경 미반영 (수동 확인 필요)")

  lines = ["📊 **스윙 봇 TP 승수 분기 재평가**"]
  for r in results:
    lines.append(format_result(r))
  message = "\n\n".join(lines)

  if args.discord:
    ev.send_discord(message)
  else:
    print(message)


if __name__ == "__main__":
  main()
