# -*- coding: utf-8 -*-
"""
==================================================================
 swing_bot_backtest.py — 스윙 전략 백테스트 (독립 실행 도구)
==================================================================
실전 봇(swing_bot.py)의 진입 로직을 그대로 미러링해 과거 성과를 검증한다.
  - 진입: 3중 EMA(10/20/50) 정배열 + 거래량 돌파(20봉 평균+1σ) + 최근 5봉 고점 돌파,
          하락 후 첫 신호는 필터링 → 두 번째 신호에서 진입 (봇과 동일)
  - 익절(TP, --tp-atr): 보유 중 종가 ≥ 진입가 + tp-atr×진입 ATR 도달 시 수익 확정 청산
    (ema20 청산 전용. 기본 auto — 실전 봇의 TAKE_PROFIT_ATR_BY_TICKER를 자동 미러.
     공통 float(3.0) / 종목별 'TQQQ:1.5,SOXL:3.5' / 0 = 비활성화 중 선택)
  - 청산 규칙 (--exit / --atr-k / --trigger):
      ema20  : 종가 < 20EMA (TP 미도달 시의 손절/추세 청산 — 현행 봇 기본)
      chan   : 샹들리에 트레일링 — 최고가(진입 후) - k×ATR 이탈
      stop   : 고정 스톱 — 진입가 - k×ATR 이탈
      trigger: close(종가 기준 — 수동 알림 봇에 현실적) / intra(장중 저가 기준 — 스톱 주문)
  - 진입 필터 (--entry, 기본 none): 신호 정의에 추가 조건을 얹어 거짓 돌파를 거름
      adx20/adx25 : ADX(14) ≥ 20/25 (추세 강도)
      contr       : 직전 봉 ATR < 20봉 평균 ATR × (1--contr-ratio) (변동성 수축 후 돌파)
                    예: --contr-ratio 0.9 → ATR이 평균의 90% 미만일 때만 인정(더 엄격)
      vol15x      : 거래량 ≥ 20봉 평균 × 1.5 (거래량 확인 강화)
      margin1     : 종가가 최근 5봉 고점보다 ≥ 1% 위에서 돌파
  - 타임프레임 (--tf): 1h / 2h / 4h / 6h (1h 수집 → 세션 정렬 리샘플) 또는 1D (일봉)
  - 데이터: yfinance (requirements.txt에 포함), ATR = Wilder(14)

모델링 유의점 (실전 봇과의 차이):
  - 실전 봇은 매 실행마다 60일 전체를 재스캔하므로, 매도 후 다음 돌파가
    '두 번째 신호'로 분류될 수 있다. 반면 이 백테스트는 매 청산 후 상태를
    WAITING으로 초기화해 다음 돌파를 항상 '첫 신호'로 필터링한다 →
    백테스트가 실전보다 보수적(진입 횟수 적음)이다.
  - intraday(1h) 타임프레임은 yfinance 한도로 최대 약 730일(2년)까지만 조회된다.
  - 진입/청산은 봉 종가 또는 스톱가 기준 — 실제 체결 슬리피지·수수료 미반영.

사용 예:
  python3 swing_bot_backtest.py                          # TQQQ+SOXL, 4h, ema20 + TP(봇 설정 미러)
  python3 swing_bot_backtest.py --tp-atr 0               # 익절 비활성화 (20EMA 단독)
  python3 swing_bot_backtest.py --tp-atr 3.0             # 공통 승수
  python3 swing_bot_backtest.py --ticker TQQQ --tf 4h --exit chan --atr-k 2.5 --trigger intra
  python3 swing_bot_backtest.py --ticker SOXL --tf 1D --exit ema20
  python3 swing_bot_backtest.py --tf 2h --exit stop --atr-k 2.0
==================================================================
"""
import argparse
import os
import sys
import warnings

import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import swing_bot as bot  # 세션 정렬 origin 로직 재사용 (swing_bot.py)

SESSION_TZ = bot.SESSION_TZ
DEFAULT_PERIOD = "2y"  # yfinance 1시간봉 최대 조회 기간
# yfinance auto_adjust 옵션: 기본 False — 사용자가 보고서 확인 후 결정하도록 기본 비활성화
AUTO_ADJUST = False


def load_bars(ticker, tf, period=DEFAULT_PERIOD):
  """타임프레임별 OHLCV 로드. tf: '1h'|'2h'|'4h'|'6h'|'1D'"""
  if tf == "1D":
    d = yf.download(ticker, period=period, interval="1d", progress=False, auto_adjust=AUTO_ADJUST)
    # yfinance는 단일-티커 호출 시 컬럼이 단일 레벨(str), 멀티-티커 시 MultiIndex를 반환할 수 있음
    if isinstance(d.columns, pd.MultiIndex):
      d.columns = d.columns.get_level_values(0)
    # 인덱스 타임존 정규화: UTC(naive) -> SESSION_TZ
    if d.index.tz is None:
      try:
        d = d.tz_localize("UTC").tz_convert(SESSION_TZ)
      except Exception:
        pass
    else:
      d = d.tz_convert(SESSION_TZ)
    return d[["Open", "High", "Low", "Close", "Volume"]].dropna()

  h = yf.download(ticker, period=period, interval="1h", progress=False, auto_adjust=AUTO_ADJUST)
  if isinstance(h.columns, pd.MultiIndex):
    h.columns = h.columns.get_level_values(0)
  h = h[["Open", "High", "Low", "Close", "Volume"]].dropna()
  # 타임존 정규화: yfinance는 보통 naive(로컬) 또는 UTC이므로 UTC로 로컬라이즈 후 ET로 변환
  if h.index.tz is None:
    try:
      h = h.tz_localize("UTC").tz_convert(SESSION_TZ)
    except Exception:
      pass
  else:
    h = h.tz_convert(SESSION_TZ)
  hours = int(tf[:-1])
  if hours == 1:
    return h
  origin = bot.session_resample_origin(h)
  df = h.resample(f"{hours}h", origin=origin).agg({
      "Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum",
  }).dropna()
  # 미완성 봉 방어 (봇과 동일)
  if len(df) and pd.Timestamp(df.index[-1]) + pd.Timedelta(hours=hours) > pd.Timestamp.now(tz=SESSION_TZ):
    df = df.iloc[:-1]
  return df


def add_indicators(df):
  """EMA/거래량 돌파/ATR 계산 (봇 build_4h_frame과 동일 공식)"""
  df = df.copy()
  df["EMA_10"] = df["Close"].ewm(span=10, adjust=False).mean()
  df["EMA_20"] = df["Close"].ewm(span=20, adjust=False).mean()
  df["EMA_50"] = df["Close"].ewm(span=50, adjust=False).mean()
  df["Vol_SMA"] = df["Volume"].rolling(window=20).mean()
  df["Vol_Std"] = df["Volume"].rolling(window=20).std()
  df["Vol_Breakout"] = df["Volume"] > (df["Vol_SMA"] + df["Vol_Std"])
  pc = df["Close"].shift(1)
  tr = pd.concat([df["High"] - df["Low"], (df["High"] - pc).abs(), (df["Low"] - pc).abs()], axis=1).max(axis=1)
  df["ATR"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
  df["ATR_MA20"] = df["ATR"].rolling(window=20).mean()
  # ADX(14) — Wilder 스무딩
  up = df["High"].diff()
  down = -df["Low"].diff()
  plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
  minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
  atr_w = tr.ewm(alpha=1 / 14, adjust=False).mean()
  plus_di = 100 * plus_dm.ewm(alpha=1 / 14, adjust=False).mean() / atr_w
  minus_di = 100 * minus_dm.ewm(alpha=1 / 14, adjust=False).mean() / atr_w
  dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
  df["ADX"] = dx.ewm(alpha=1 / 14, adjust=False).mean()
  return df


def entry_filter_ok(df, i, row, entry, recent_highs, contr_ratio=1.0):
  """진입 필터 조건 — 신호 정의(첫 신호/두 번째 신호 모두)에 적용"""
  if entry == "none":
    return True
  if entry.startswith("adx"):
    return bool(row["ADX"] >= float(entry[3:]))
  if entry == "contr":
    # 직전 봉 변동성이 20봉 평균보다 낮은(수축) 상태에서의 돌파
    prev_atr = df["ATR"].iloc[i - 1]
    prev_atr_ma = df["ATR_MA20"].iloc[i - 1]
    return bool(pd.notna(prev_atr_ma) and prev_atr < prev_atr_ma * contr_ratio)
  if entry == "vol15x":
    return bool(row["Volume"] >= 1.5 * row["Vol_SMA"])
  if entry == "margin1":
    return bool(row["Close"] >= recent_highs * 1.01)
  raise ValueError(f"알 수 없는 진입 필터: {entry}")


def backtest(df, exit_mode, k, trigger, entry="none", contr_ratio=1.0, tp_atr=3.0):
  """봇 로직 미러 시뮬레이션.

  exit_mode: ema20|chan|stop, trigger: close|intra
  tp_atr: >0이면 종가 ≥ 진입가 + tp_atr×진입 ATR 시 익절 청산 (기본 3.0 — 실전 봇과 동일)
  반환: trades = [(진입t, 진입p, 청산t, 청산p, 수익률, 보유, MAE, MFE)], eq = 지분곡선
  """
  trades, eq = [], []
  state = "WAITING"  # WAITING → FIRST_FOUND(필터) → READY_FOR_BUY(진입)
  entry_p = entry_t = None
  hh = 0.0
  atr_entry = None
  equity = 1.0
  eq_at_entry = 1.0
  mae, mfe = 0.0, 0.0  # 보유 시작 시 초기화 (pyright: possibly unbound 방지)

  for i in range(50, len(df)):
    row = df.iloc[i]
    close, high, low = row["Close"], row["High"], row["Low"]

    if entry_p is not None:  # 보유 중: 청산 조건만 평가 (봇의 IN_POSITION과 동일)
      mae = min(mae, low / entry_p - 1)
      mfe = max(mfe, high / entry_p - 1)
      hh = max(hh, high)
      exit_p, do_exit = None, False
      # 익절(TP) 우선: 종가 ≥ 진입가 + tp_atr×진입 시점 ATR → 수익 확정 청산 (봇과 동일)
      # ema20 청산 전용 — 실전 봇은 ema20만 사용하므로 chan/stop과의 순수 비교를 유지
      if (tp_atr > 0 and exit_mode == "ema20"
          and atr_entry is not None and close >= entry_p + tp_atr * atr_entry):
        do_exit, exit_p = True, close
      elif exit_mode == "ema20":
        do_exit, exit_p = close < row["EMA_20"], close
      elif exit_mode == "chan":
        trail = hh - k * row["ATR"]
        if trigger == "intra":
          do_exit, exit_p = low <= trail, max(trail, low)
        else:
          do_exit, exit_p = close < trail, close
      elif exit_mode == "stop":
        stop = entry_p - k * atr_entry
        if trigger == "intra":
          do_exit, exit_p = low <= stop, stop
        else:
          do_exit, exit_p = close < stop, close
      if do_exit:
        bar_eq = eq_at_entry * (exit_p / entry_p)
        eq.append(bar_eq)
        trades.append((entry_t, entry_p, df.index[i], exit_p, exit_p / entry_p - 1,
                       (df.index[i] - entry_t), mae, mfe))
        equity = bar_eq
        entry_p = entry_t = None
        state = "WAITING"
      else:
        eq.append(eq_at_entry * (close / entry_p))
      continue

    # 미보유
    if close < row["EMA_50"]:  # 장기 이평선 아래 = 하락/조정 구간 → 리셋
      state = "WAITING"
    recent_highs = df["High"].iloc[max(0, i - 5):i].max()
    is_aligned = row["EMA_10"] > row["EMA_20"] > row["EMA_50"]
    is_breakout = (
        is_aligned
        and bool(row["Vol_Breakout"])
        and close > recent_highs
        and entry_filter_ok(df, i, row, entry, recent_highs, contr_ratio)
    )
    if is_breakout:
      if state == "WAITING":
        state = "FIRST_FOUND"  # 첫 신호는 필터링 (봇과 동일)
      elif state == "FIRST_FOUND":
        state = "READY_FOR_BUY"  # 두 번째 신호 → 진입
        entry_p, entry_t = close, df.index[i]
        hh, atr_entry = high, row["ATR"]
        eq_at_entry = equity
        mae, mfe = low / entry_p - 1, high / entry_p - 1
    eq.append(equity)

  if entry_p is not None:  # 미청산 포지션은 마지막 종가로 정리
    final_p = df.iloc[-1]["Close"]
    eq.append(eq_at_entry * (final_p / entry_p))
    trades.append((entry_t, entry_p, df.index[-1], final_p, final_p / entry_p - 1,
                   df.index[-1] - entry_t, mae, mfe))
  return trades, np.array(eq)


def summarize(ticker, tf, label, trades, eq):
  print(f"\n[{ticker}] {tf} — {label}")
  if not trades:
    print("  신호 없음 (2년간 진입 조건 미충족)")
    return
  rets = np.array([t[4] for t in trades])
  wins, losses = rets[rets > 0], rets[rets <= 0]
  cum = eq[-1] / eq[0] - 1
  dd = (eq / np.maximum.accumulate(eq) - 1).min() * 100
  pf = wins.sum() / abs(losses.sum()) if len(losses) else float("inf")
  hold_days = pd.Series([t[5].days for t in trades])
  mfe_avg, ret_avg = np.mean([t[7] for t in trades]) * 100, rets.mean() * 100

  print(f"  트레이드 {len(trades)}회 | 승률 {len(wins)/len(rets)*100:.0f}% | "
        f"평균 {ret_avg:+.1f}% | 총수익 {cum*100:+.1f}% | MDD {dd:.1f}%")
  print(f"  평균손실 {losses.mean()*100:+.1f}% | 최대손실 {rets.min()*100:+.1f}% | "
        f"PF {pf:.1f} | 평균보유 {hold_days.mean():.0f}일")
  if mfe_avg > 0 and ret_avg >= 0:
    giveback = 100 * (1 - ret_avg / mfe_avg)
    print(f"  참고: MFE 평균 {mfe_avg:+.1f}% vs 실현 평균 {ret_avg:+.1f}% "
          f"(최대 이익의 {giveback:.0f}% 반납)")
  else:
    print(f"  참고: MFE 평균 {mfe_avg:+.1f}% vs 실현 평균 {ret_avg:+.1f}% (손실 구간)")
  print(f"  {'진입':>10} {'청산':>10} {'보유일':>5} {'수익':>7} {'MAE':>7} {'MFE':>7}")
  for t in trades:
    print(f"  {t[0].strftime('%y-%m-%d'):>10} {t[2].strftime('%y-%m-%d'):>10} "
          f"{t[5].days:>5} {t[4]*100:+6.1f}% {t[6]*100:+6.1f}% {t[7]*100:+6.1f}%")


def run_ticker(ticker, tf, exit_mode, k, trigger, entry, contr_ratio, period, tp_spec, tp_default):
  df = load_bars(ticker, tf, period)
  if len(df) < 51:
    print(f"[{ticker}] 데이터 부족({len(df)}봉 < 51) — 분석 생략")
    return
  df = add_indicators(df)
  # 종목별 승수 우선 (실전 봇 TAKE_PROFIT_ATR_BY_TICKER 미러)
  tp_atr = tp_spec.get(ticker, tp_default)
  trades, eq = backtest(df, exit_mode, k, trigger, entry, contr_ratio, tp_atr)
  label = f"entry={entry}"
  if entry == "contr":
    label += f"(x{contr_ratio:g})"
  if entry != "none":
    label += " "
  label += f"tp={tp_atr:g} " if tp_atr > 0 else ""
  label += f"exit={exit_mode}" + (f" k={k}" if exit_mode != "ema20" else "") \
      + f" trigger={trigger}"
  summarize(ticker, tf, label, trades, eq)


def parse_tf(s):
  s = s.lower().strip()
  if s in ("1d", "d", "daily", "day"):
    return "1D"
  if s.endswith("h") and s[:-1].isdigit() and s[:-1] in ("1", "2", "4", "6"):
    return s
  raise SystemExit(f"[오류] 지원하지 않는 타임프레임: {s} (1h/2h/4h/6h/1D)")


def parse_tp_spec(value):
  """--tp-atr 파싱: 공통 float / 종목별 dict(예: 'TQQQ:1.5,SOXL:3.5') / 'auto'.

  반환: (ticker→승수 dict, 공통 기본값 float)
  - 'auto'/'': 실전 봇(swing_bot.py)의 TAKE_PROFIT_ATR_BY_TICKER를 그대로 사용 (단일 소스)
  """
  value = value.strip()
  if value.lower() in ("auto", ""):  # 실전 봇 설정 미러 (기본)
    spec = dict(bot.TAKE_PROFIT_ATR_BY_TICKER)
    return spec, float(bot.TAKE_PROFIT_ATR)
  if ":" in value:  # 종목별: 'TQQQ:1.5,SOXL:3.5' (미등록 종목은 기본값으로 대체)
    spec = {}
    for part in value.split(","):
      part = part.strip()
      if not part:
        continue
      if ":" in part:
        tk, val = part.split(":", 1)
        spec[tk.strip().upper()] = float(val)
    return spec, float(bot.TAKE_PROFIT_ATR)
  return {}, float(value)  # 공통 승수


def main():
  p = argparse.ArgumentParser(description="스윙 전략 백테스트 (swing_bot.py 로직 미러)")
  p.add_argument("--ticker", default=",".join(bot.TICKERS), help="종목 (콤마 구분, 기본 TQQQ,SOXL)")
  p.add_argument("--tf", default="4h", help="타임프레임 (1h/2h/4h/6h/1D, 기본 4h)")
  p.add_argument("--exit", default="ema20", choices=["ema20", "chan", "stop"],
                 help="청산 규칙 (기본 ema20 — 현행 봇)")
  p.add_argument("--atr-k", type=float, default=None,
                 help="ATR 승수 (chan 기본 2.5, stop 기본 2.0)")
  p.add_argument("--trigger", default="close", choices=["close", "intra"],
                 help="청산 기준: close(종가)/intra(장중 저가), 기본 close")
  p.add_argument("--entry", default="none",
                 choices=["none", "adx20", "adx25", "contr", "vol15x", "margin1"],
                 help="진입 필터 (기본 none)")
  p.add_argument("--contr-ratio", type=float, default=1.0,
                 help="수축 강도 임계값: 직전 ATR < 20봉 평균 × 비율 (기본 1.0, 0.9면 더 엄격)")
  p.add_argument("--tp-atr", default="auto",
                 help="익절(TP, ema20 청산 전용): 종가 ≥ 진입가 + tp-atr×진입 ATR 시 청산. "
                      "기본 auto(실전 봇 설정 미러). 공통 float(3.0), 종목별 'TQQQ:1.5,SOXL:3.5', "
                      "0 = 끔 중 선택")
  p.add_argument("--period", default=DEFAULT_PERIOD, help="조회 기간 (기본 2y)")
  p.add_argument("--auto-adjust", action="store_true", help="yfinance auto_adjust 사용 (기본: False)")
  args = p.parse_args()

  # auto_adjust 전역 설정 반영
  global AUTO_ADJUST
  AUTO_ADJUST = bool(args.auto_adjust)

  tf = parse_tf(args.tf)
  if args.exit == "ema20" and args.trigger == "intra":
    print("[참고] ema20 청산은 종가 기준만 존재 — --trigger intra는 무시됩니다.")
  k = args.atr_k if args.atr_k is not None else (2.5 if args.exit == "chan" else 2.0)
  tp_spec, tp_default = parse_tp_spec(args.tp_atr)
  for ticker in [t.strip().upper() for t in args.ticker.split(",") if t.strip()]:
    try:
      run_ticker(ticker, tf, args.exit, k, args.trigger, args.entry, args.contr_ratio,
                 args.period, tp_spec, tp_default)
    except Exception as exc:  # 한 종목 실패가 다른 종목을 막지 않도록
      print(f"[{ticker}] 백테스트 중 오류: {exc}")


if __name__ == "__main__":
  main()
