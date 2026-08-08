# -*- coding: utf-8 -*-
"""
==================================================================
 fvg_bot_backtest.py — FVG 전략 백테스트 (독립 실행 도구)
==================================================================
실전 봇(fvg_signal_bot.py)의 진입 로직을 그대로 미러링해 과거 성과를 검증한다.
  - 데이터 제약: yfinance 1분봉은 최근 7~8일만 제공(그 구간 시그널 0회)이라
    통계 검증이 불가능 → 기본은 5분봉(최근 60일 한도) 근사 백테스트.
    시간 기반 상수(신선도 3시간, CHoCH-FVG 간격 45분 등)를 봉 길이에 맞춰
    환산해 전략 구조(HTF 필터 → CHoCH → FVG → 풀백 → 구조 손절)는 동일 유지.
  - 진입: 시그널 발생 봉에 FVG 중간점(50%) 지정가 체결 가정 (봇과 동일)
  - 청산: 손절(구조 저점 아래) 우선 — 익절 = 리스크 × RR (기본 3.5, 영상 3~4R)
    - 기본: 당일 장중 해결 못 하면 장 마감 종가로 정리 (데이 트레이딩 모델)
    - --overnight: SL/TP 도달까지 다음 날 보유 허용
  - 지표: 승률/평균/총수익/MDD/PF/최대손실/MAE/MFE (프로젝트 백테스트 표준 양식)
  - 시초가 창 진입 비교 — 창 내(portfolio_config.json > FVG > ENTRY_WINDOW, 기본
    ET 09:30~11:30) 진입 vs 창 외 진입 트레이드의 승률/평균/합계/PF를 나란히 출력
  - HTF(기본 1시간봉)는 미완성 봉 제외 — 미래 정보 유출 방지 (실전 봇과 동일 방침)

모델링 유의점:
  - 5분봉 근사 모델 — 1분봉 원본 전략 자체의 검증이 아니라 구조 동일 근사치다.
  - 체결은 중간점/손절/익절 가격 기준 — 슬리피지·수수료 미반영 (프로젝트 표준).
  - 신호는 최근 60일(5분봉 한도)만 — 장기 경기순환/레짐 편향이 있을 수 있다.

사용 예:
  python3 fvg_bot_backtest.py                       # TQQQ, 5m+1h HTF, 60일 (실전 봇과 동일 종목)
  python3 fvg_bot_backtest.py --ticker TQQQ --rr 2.0
  python3 fvg_bot_backtest.py --overnight           # 익일 보유 허용
  python3 fvg_bot_backtest.py --ltf 15m --days 60   # 더 느린 근사
==================================================================
"""
import argparse
import os
import sys
import warnings
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fvg_signal_bot as bot  # 진입 로직(HTF 필터/CHoCH/FVG/풀백) 재사용


def scale_constants(ltf_minutes):
  """1분봉 기준 시간 상수 → 봉 길이(분)에 맞춘 봉 수 환산.

  MAX_FVG_AGE_BARS(3시간 신선도), MAX_CHOCH_FVG_GAP_BARS(45분)만 시간 의존이고,
  나머지(프랙탈 창, 최소 갭 높이, 중간점 허용 오차, RR)는 가격/봉 수 기준이라
  그대로 유지된다.
  """
  bot.MIN_LTF_BARS = max(16, 80 // ltf_minutes)
  bot.MAX_FVG_AGE_BARS = max(2, 180 // ltf_minutes)
  bot.MAX_CHOCH_FVG_GAP_BARS = max(1, 45 // ltf_minutes)


def load_bars(ticker, interval, days):
  """yfinance OHLCV 로드 — period 토큰 대신 start/end 직접 지정 (5m=60일 한도)."""
  end = datetime.now()
  start = end - timedelta(days=days)
  df = yf.download(ticker, start=start, end=end, interval=interval,
                   progress=False, auto_adjust=True)
  if isinstance(df.columns, pd.MultiIndex):
    df.columns = df.columns.get_level_values(0)
  return df[["Open", "High", "Low", "Close", "Volume"]].dropna()


def htf_slice_up_to(df_htf, ts, htf_minutes):
  """현재 시각까지 완성된 HTF 봉만 — 미완성 봉(라벨+봉길이 > 현재) 제외.

  실전 봇의 _drop_incomplete_htf_bar와 같은 방침: 진행 중인 봉을 구조 판별에
  쓰면 미래 정보가 유출되므로 완성 봉만 사용한다 (룩어헤드 방지).
  """
  return df_htf[df_htf.index + pd.Timedelta(minutes=htf_minutes) <= ts]


def backtest(df_ltf, df_htf, rr, overnight, htf_minutes):
  """봇 로직 미러 시뮬레이션.

  반환: trades = [(진입t, 진입p, 청산t, 청산p, 수익률, 보유봉수, MAE, MFE)], eq = 지분곡선
  """
  trades, eq = [], []
  equity = 1.0
  entry_p = entry_t = entry_idx = sl = tp = entry_date = None
  eq_at_entry, mae, mfe = 1.0, 0.0, 0.0

  n = len(df_ltf)
  i = bot.MIN_LTF_BARS
  while i < n:
    row = df_ltf.iloc[i]
    close, high, low = row["Close"], row["High"], row["Low"]

    if entry_p is not None:  # 보유 중: 손절/익절/장마감만 평가
      # 당일 마감 모델: 다음 날 첫 봉에 도달하면 진입일 마지막 종가로 정리.
      # 익일 봉의 장중값(갭 이동)이 MAE/MFE·수익률에 섞이지 않도록 먼저 처리한다.
      if not overnight and df_ltf.index[i].date() != entry_date:
        exit_p = df_ltf["Close"].iloc[i - 1]
        exit_t = df_ltf.index[i - 1]
        eq.append(eq_at_entry * (exit_p / entry_p))
        trades.append((entry_t, entry_p, exit_t, exit_p,
                       exit_p / entry_p - 1, (i - 1) - entry_idx, mae, mfe))
        equity = eq[-1]
        entry_p = entry_t = entry_idx = sl = tp = entry_date = None
        i += 1
        continue
      mae = min(mae, low / entry_p - 1)
      mfe = max(mfe, high / entry_p - 1)
      exit_p = None
      if low <= sl:  # 한 봉에 겹치면 손절 우선 (보수적)
        exit_p = sl
      elif high >= tp:
        exit_p = tp
      if exit_p is not None:
        eq.append(eq_at_entry * (exit_p / entry_p))
        trades.append((entry_t, entry_p, df_ltf.index[i], exit_p,
                       exit_p / entry_p - 1, i - entry_idx, mae, mfe))
        equity = eq[-1]
        entry_p = entry_t = entry_idx = sl = tp = entry_date = None
        i += 1
        continue
      eq.append(eq_at_entry * (close / entry_p))
      i += 1
      continue

    # 미보유: 시그널 스캔 (봇 build_long_signal 그대로)
    ltf_slice = df_ltf.iloc[: i + 1]
    htf_slice = htf_slice_up_to(df_htf, ltf_slice.index[-1], htf_minutes)
    if len(htf_slice) < 2:  # HTF 데이터 부족이면 스캔 생략
      eq.append(equity)
      i += 1
      continue
    sig = bot.build_long_signal(ltf_slice, htf_slice)
    if sig is not None:
      entry_p, sl, tp = sig["entry"], sig["stop_loss"], sig["take_profit"]
      entry_t, entry_idx = df_ltf.index[i], i
      entry_date = entry_t.date()
      eq_at_entry = equity
      mae, mfe = low / entry_p - 1, high / entry_p - 1
      eq.append(equity)  # 진입 봉은 플랫 지분 유지 — 백테스트 표준 처리
      i += 1
      continue
    eq.append(equity)
    i += 1

  if entry_p is not None:  # 미청산 포지션은 마지막 종가로 정리 (백테스트 표준)
    final_p = df_ltf["Close"].iloc[-1]
    eq.append(eq_at_entry * (final_p / entry_p))
    trades.append((entry_t, entry_p, df_ltf.index[-1], final_p,
                   final_p / entry_p - 1, n - 1 - entry_idx, mae, mfe))
  return trades, np.array(eq)


def classify_entry_window(ts, window):
  """진입 시각을 시초가 창(ET 벽시각) 안/밖으로 분류 — 'in'/'out'.

  백테스트 분석이므로 ENABLED는 무시하고 START/END 시각 경계만 사용한다
  (fvg_bot_eval.py의 실전 분류와 동일 의미). naive 시각은 창 외로 취급.
  """
  if ts.tzinfo is None:
    return "out"
  try:
    sh, sm = map(int, window["START"].split(":"))
    eh, em = map(int, window["END"].split(":"))
  except (ValueError, KeyError, AttributeError):
    sh, sm = map(int, bot.ENTRY_WINDOW_DEFAULT["START"].split(":"))
    eh, em = map(int, bot.ENTRY_WINDOW_DEFAULT["END"].split(":"))
  minutes = ts.hour * 60 + ts.minute
  start = sh * 60 + sm
  end = eh * 60 + em
  return "in" if start <= minutes < end else "out"


def summarize(ticker, label, trades, eq, ltf_minutes):
  print(f"\n[{ticker}] {label}")
  if not trades:
    print("  신호 없음 (조회 기간 내 진입 조건 미충족)")
    return
  rets = np.array([t[4] for t in trades])
  wins, losses = rets[rets > 0], rets[rets <= 0]
  cum = eq[-1] / eq[0] - 1
  dd = (eq / np.maximum.accumulate(eq) - 1).min() * 100
  pf = wins.sum() / abs(losses.sum()) if len(losses) else float("inf")
  hold_hours = np.array([t[5] * ltf_minutes / 60 for t in trades])
  mfe_avg, ret_avg = np.mean([t[7] for t in trades]) * 100, rets.mean() * 100

  print(f"  트레이드 {len(trades)}회 | 승률 {len(wins) / len(rets) * 100:.0f}% | "
        f"평균 {ret_avg:+.1f}% | 총수익 {cum * 100:+.1f}% | MDD {dd:.1f}%")
  print(f"  평균손실 {losses.mean() * 100:+.1f}% | 최대손실 {rets.min() * 100:+.1f}% | "
        f"PF {pf:.1f} | 평균보유 {hold_hours.mean():.1f}시간")
  if mfe_avg > 0 and ret_avg >= 0:
    giveback = 100 * (1 - ret_avg / mfe_avg)
    print(f"  참고: MFE 평균 {mfe_avg:+.1f}% vs 실현 평균 {ret_avg:+.1f}% "
          f"(최대 이익의 {giveback:.0f}% 반납)")
  else:
    print(f"  참고: MFE 평균 {mfe_avg:+.1f}% vs 실현 평균 {ret_avg:+.1f}% (손실 구간)")

  # 시초가 창 진입 vs 창 외 진입 성과 비교 (portfolio_config.json FVG.ENTRY_WINDOW)
  window = bot.load_entry_window()
  groups = {"in": [], "out": []}
  for t in trades:
    groups[classify_entry_window(t[0], window)].append(t[4])
  print(f"\n  [시초가 창 진입 비교] (기준 {window['START']}~{window['END']} ET)")
  for name, key in (("창 내 진입", "in"), ("창 외 진입", "out")):
    arr = np.array(groups[key])
    if not len(arr):
      print(f"    {name:<6} 0회")
      continue
    wins = arr[arr > 0]
    losses = arr[arr <= 0]
    pf = wins.sum() / abs(losses.sum()) if len(losses) else float("inf")
    print(f"    {name:<6} {len(arr):>3}회 | 승률 {len(wins) / len(arr) * 100:.0f}% | "
          f"평균 {arr.mean() * 100:+.1f}% | 합계 {arr.sum() * 100:+.1f}% | PF {pf:.1f}")

  print(f"  {'진입':>16} {'청산':>16} {'보유h':>5} {'수익':>7} {'MAE':>7} {'MFE':>7}")
  for t in trades:
    print(f"  {t[0].strftime('%y-%m-%d %H:%M'):>16} {t[2].strftime('%y-%m-%d %H:%M'):>16} "
          f"{t[5] * ltf_minutes / 60:>5.1f} {t[4] * 100:+6.1f}% {t[6] * 100:+6.1f}% "
          f"{t[7] * 100:+6.1f}%")


def run_ticker(ticker, ltf, htf, days, rr, overnight):
  ltf_minutes = int(ltf[:-1])
  df_ltf = load_bars(ticker, ltf, days)
  df_htf = load_bars(ticker, htf, days)
  if len(df_ltf) < bot.MIN_LTF_BARS:
    print(f"[{ticker}] 데이터 부족({len(df_ltf)}봉 < {bot.MIN_LTF_BARS}) — 분석 생략")
    return
  trades, eq = backtest(df_ltf, df_htf, rr, overnight, tf_to_minutes(htf))
  label = (f"FVG {ltf}+{htf} HTF, {days}일, RR 1:{rr:g}, "
           + ("overnight" if overnight else "당일 마감"))
  summarize(ticker, label, trades, eq, ltf_minutes)


def tf_to_minutes(s):
  """타임프레임 문자열 → 분 (1h→60, 5m→5). HTF 완성 필터·상수 환산에 사용."""
  s = s.lower().strip()
  if s.endswith("h") and s[:-1].isdigit():
    return int(s[:-1]) * 60
  if s.endswith("m") and s[:-1].isdigit():
    return int(s[:-1])
  raise SystemExit(f"[오류] 지원하지 않는 타임프레임: {s} (예: 5m/15m/1h)")


def parse_tf(s):
  s = s.lower().strip()
  if s.endswith("m") and s[:-1].isdigit() and s[:-1] in ("5", "15"):
    return s
  raise SystemExit(f"[오류] 지원하지 않는 타임프레임: {s} (5m/15m)")


def main():
  p = argparse.ArgumentParser(description="FVG 전략 백테스트 (fvg_signal_bot.py 로직 미러)")
  p.add_argument("--ticker", default="TQQQ", help="종목 (콤마 구분, 기본 TQQQ — 실전 봇 단일 종목) — 과거 SOXL 비교는 --ticker SOXL")
  p.add_argument("--ltf", default="5m", help="진입 타임프레임 (5m/15m, 기본 5m — yfinance 1m은 8일 한도)")
  p.add_argument("--htf", default="1h", help="HTF 추세 필터 타임프레임 (기본 1h)")
  p.add_argument("--days", type=int, default=60, help="조회 기간(일, 기본 60 — 5m 최대)")
  p.add_argument("--rr", type=float, default=bot.RR_TARGET,
                 help=f"익절 배수 (기본 {bot.RR_TARGET:g} — 영상 3~4R)")
  p.add_argument("--overnight", action="store_true",
                 help="당일 해결 안 되면 익일까지 보유 허용 (기본: 당일 마감 정리)")
  args = p.parse_args()

  ltf = parse_tf(args.ltf)
  scale_constants(int(ltf[:-1]))
  bot.RR_TARGET = args.rr
  print(f"[설정] LTF={ltf} HTF={args.htf} 조회={args.days}일 RR=1:{args.rr:g} "
        f"모드={'overnight' if args.overnight else '당일 마감'}")
  for ticker in [t.strip().upper() for t in args.ticker.split(",") if t.strip()]:
    try:
      run_ticker(ticker, ltf, args.htf, args.days, args.rr, args.overnight)
    except Exception as exc:  # 한 종목 실패가 다른 종목을 막지 않도록
      print(f"[{ticker}] 백테스트 중 오류: {exc}")


if __name__ == "__main__":
  main()
