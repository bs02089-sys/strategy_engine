# -*- coding: utf-8 -*-
"""
==================================================================
  FVG(공정가치 갭) 반자동 매매 스캐너 — 유튜브 전략 이식
==================================================================
전략: "This Boring Strategy Made Me $53,478 In A Month" (Craig Percoo)
  https://youtu.be/kngWJvQNrgQ
  시장 구조(break of structure / change of character) + 모멘텀(FVG) 2축 전략.
영상 로직 → 코드 구현 매핑:
  1. HTF(15분봉) 시장 구조 분석 (analyze_htf_trend)
     - BOS/CHoCH 기반 추세 방향 판별 — 상승장일 때만 롱 시그널 허용
     - 상승 추세에서 최근 구조 저점 아래로 종가 이탈 시 보수적 스킵
  2. 1분봉 진입 모델 (build_long_signal)
     - Step 1: CHoCH — 하락 구조(낮은 고점) 후 종가가 이전 스윙 고점 위로 마감
     - Step 2: 그 CHoCH 돌파가 남긴 최근 FVG (c3 저점 > c1 고점)
     - Step 3: FVG 중간점(50%)으로의 풀백 — 현재 종가가 박스 내 중간점 영역 도달
     - ⚠️ "가격이 그냥 통과해버린 FVG는 진입 존이 아님" → 채워진(하단 이탈) 박스 제외
  3. 구조 기반 손절 (영상: "stop loss underneath the last low on that trend")
     - CHoCH 이전 마지막 스윙 저점 아래에 손절 배치, 익절 = 리스크 × 3.5 (영상 3~4R)
  4. 데이 트레이딩 운용 (백테스트 근거 — fvg_bot_backtest.py)
     - 5분봉 근사 백테스트(60일)에서 당일 마감 모델은 수익(PF 2.0, MDD -2.3%)인 반면
       overnight 보유는 레버리지 ETF 야간 갭에 구조 손절이 깨져 손실(SOXL -14.8%)
       → 당일 마감은 체크리스트 ③ MOC(16:00 마감 경매 자동 청산)로 반영
  5. 청산(매도) 알림 — 진입 알림 시 포지션을 fvg_positions.json에 기록하고, 이후
     익절(TP) 도달·손절(SL) 도달·당일 마감 임박(ET 15:40) 시 매도 알림 자동 전송
     (영상 청산 로직: 익절 = 리스크 × 3.5 / 손절 = 구조 저점 아래 + 백테스트 결론: 당일 마감)
  6. 무인(수면) 운용 — 나무증권(NH) 기준 주문 3건: ① 시세포착주문 신규편입(손실제한+이익실현
     % 동시 등록, 매수 체결 순간 서버 감시 자동 시작 — 바이&셀은 손절 없음으로 주력 제외) + ②
     지정가 매수(FVG 중간점) + ③ MOC(마감 경매 자동 청산)를 걸면 자는 동안 청산이 자동
     처리된다 → 이후 TP/손절/MOC 알림은 아침에 확인하는 기록. 상세: FVG_NAMYU_SETUP.md

  - 알림은 Discord Webhook으로만 전송 — 실제 주문 자동 실행 없음 (수동 매매)
  - DISCORD_USER_ID 설정 시 알림에 @멘션 3회 포함 — 알림이 강하게/여러 번 울리도록
    웨이크업 강화 (멘션 3회 패턴)
  - 시초가 창 필터 — 진입 알림은 개장 후 2시간(ET 09:30~11:30) 안에서만 발송.
    창 밖(한국 심야~새벽)에는 진입 신호를 스킵해 잠든 사이 알림/주문 입력 부담을
    없앤다. 창은 portfolio_config.json > FVG > ENTRY_WINDOW 에서 설정(ENABLED/START/END).
    청산(TP/손절/당일 마감) 알림은 창과 무관하게 장중 내내 동작한다 (자동 청산 기록).
  - 청산 알림 무음 시간대 — 밤~새벽(한국 시간, 기본 00:00~07:00)에는 청산 알림을
    @멘션 없이 조용히 전송해 잠을 깨우지 않는다 (알림 자체는 계속 발송 — 아침 기록).
    설정: portfolio_config.json > FVG > EXIT_ALERT_QUIET_HOURS (ENABLED/START/END, KST).
  - 파일 기반 쿨다운(fvg_alerts.json)으로 동일 FVG 중복 알림 방지 — 로컬 크론과
    GitHub Actions(--once 백업)가 git으로 상태를 공유해 프로세스를 넘나드는
    교차 중복 알림도 차단한다 (loop 모드에서도 동일하게 동작)
  - HTF(15분봉) 데이터는 15분 캐시 — 실행 간 불필요한 재다운로드 방지
  - 실행: python3 fvg_signal_bot.py [--once]  (--once = 1회 스캔 후 종료)
           python3 fvg_signal_bot.py [--test-alert]  (Discord 웹훅+멘션 경로 검증용 테스트 발송)

Dependencies: pandas, requests, yfinance (requirements.txt에 포함)
==================================================================
"""
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests
import yfinance as yf

# ==========================================
# [사용자 설정 영역]
# ==========================================
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "YOUR_DISCORD_WEBHOOK")
# 웨이크업 멘션: DISCORD_USER_ID 설정 시 시그널 알림에 @멘션 3회 포함 (멘션 3회 패턴)
DISCORD_USER_ID = os.getenv("DISCORD_USER_ID", "YOUR_DISCORD_USER_ID")
# 모니터링할 종목 (단일 TQQQ — 60일 백테스트에서 창 내 진입 효율·승률·MDD 모두
# SOXL 대비 우위 확인, 2026-08 의사결정. SOXL 제거: 개장 직후 손절 취약 + 신호 빈도 낮음)
TICKERS = ["TQQQ"]

# 데이터: 15분봉 = HTF 컨텍스트(영상: 2~3일치), 1분봉 = 진입 모델 실행 차트
HTF_INTERVAL = "15m"
HTF_PERIOD = "5d"  # 15분봉 2~3일 컨텍스트 — yfinance 5d로 여유 확보
LTF_INTERVAL = "1m"
LTF_PERIOD = "5d"

# 시장 구조 (스윙 탐지 프랙탈 윈도우)
SWING_LEFT = 2
SWING_RIGHT = 2
MIN_LTF_BARS = 80  # 1분봉 최소 봉 수 (스윙 탐지 가능 수준)

# 진입 모델 파라미터
RR_TARGET = 3.5            # 익절 배수 — 영상: 3~4R (고정 1:4 예시)
MIDPOINT_TOL = 0.002       # FVG 중간점 도달 허용 오차 (0.2%)
MIN_FVG_HEIGHT_PCT = 0.0005  # 너무 좁은 노이즈 갭 제외 (가격 대비 0.05%)
MAX_FVG_AGE_BARS = 180     # 1분봉 FVG 신선도 — 최근 3시간 이내 생성분만
MAX_CHOCH_FVG_GAP_BARS = 45  # CHoCH 돌파 후 45분 이내 생성된 FVG만 ("FVG produced inside that CHoCH")

# 실행 설정
SCAN_INTERVAL_SECONDS = 60   # 1분봉 기준 스캔 주기
ALERT_COOLDOWN_SECONDS = 3600  # 동일 FVG 재알림 쿨다운 (1시간)
HTF_CACHE_SECONDS = 900      # HTF(15분봉) 재수집 주기 — API 부하 절감

# 시초가 창 (진입 알림 허용 시간대) — 기본: 개장 후 2시간(ET 09:30~11:30).
# portfolio_config.json > FVG > ENTRY_WINDOW 에서 설정 (ENABLED/START/END, ET 24h 형식).
# 진입 알림만 이 창 안에서 발송 — 잠자는 시간(한국 심야~새벽)의 진입 알림/멘션
# 웨이크업과 주문 입력 부담을 없앤다. 청산(TP/손절/당일 마감) 알림은 제한 대상이
# 아니므로 장중 내내 동작한다.
ENTRY_WINDOW_DEFAULT = {"ENABLED": True, "START": "09:30", "END": "11:30"}

# 청산 알림 무음 시간대 — 밤~새벽(한국 시간)에는 청산(TP/손절/당일 마감) 알림을
# @멘션 없이 조용히 전송한다 (진입 알림과 달리 청산은 브로커가 자동 처리 — 알림은
# 아침에 확인하는 기록이므로 잠을 깨울 필요가 없다).
# portfolio_config.json > FVG > EXIT_ALERT_QUIET_HOURS 에서 설정 (ENABLED/START/END, KST 24h).
# 자정을 넘는 구간(예: 22:00~06:00)도 지원한다.
EXIT_ALERT_QUIET_HOURS_DEFAULT = {"ENABLED": True, "START": "00:00", "END": "07:00"}


def send_discord_webhook(message, mention=True):
  """디스코드 채널로 메시지를 전송 (웹훅 미설정 시 stdout 출력).

  mention=False면 @멘션 없이 메시지만 전송 — 무음 시간대(KST 밤~새벽)의 청산
  알림에 사용해 잠을 깨우지 않는다 (알림 자체는 계속 발송, 아침 기록용).
  반환: 실제 Discord 전송 성공 여부(True/False). 웹훅 미설정(로컬 크론처럼
  알림 불필요 경로)이면 False를 반환해, 호출 측이 포지션 기록/쿨다운을
  건너뛰도록 한다 — 미전송 알림이 쿨다운을 선점해 GHA 알림을 막는 문제 방지.
  """
  if not DISCORD_WEBHOOK or DISCORD_WEBHOOK == "YOUR_DISCORD_WEBHOOK":
    print(message)
    return False
  # 웨이크업 강화: 멘션을 3회 반복 — 잠든 사이에도 Discord 모바일 알림이 강하게
  # 울리도록 (사용자가 직접 매매를 진행해야 하므로).
  # placeholder("YOUR_...")는 미설정 상태이므로 멘션에서 제외한다.
  mention_text = (
      " ".join([f"<@{DISCORD_USER_ID}>"] * 3)
      if mention and DISCORD_USER_ID and DISCORD_USER_ID != "YOUR_DISCORD_USER_ID"
      else ""
  )
  content = (mention_text + "\n" + message) if mention_text else message
  payload = {"content": content}
  try:
    response = requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
    if response.status_code == 204:
      print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 알림 전송 성공")
      return True
    print(f"전송 실패: {response.status_code}, {response.text}")
  except Exception as e:
    print(f"웹훅 전송 중 에러 발생: {e}")
  return False


def in_trading_session(ts):
  """마지막 1분봉이 오늘 뉴욕 정규 세션(09:30~16:00 ET)에 해당하는지.

  영상은 세션 개장(9:30) 이후에만 진입 모델을 실행한다. 주말/야간/오래된 봉으로
  시그널이 발생하는 것을 방지. 타임존 정보가 없으면 필터 생략.
  """
  try:
    if ts.tzinfo is None:
      return True
    today_et = pd.Timestamp.now(tz=ts.tz)
    if ts.date() != today_et.date():
      return False
    return (9 <= ts.hour <= 15) and not (ts.hour == 9 and ts.minute < 30)
  except Exception:
    return True


def _parse_time_str(time_str, default_minutes):
  """'HH:MM' 형식의 시간 문자열을 총 분(minutes) 단위로 변환 (공통 헬퍼)."""
  try:
    h, m = map(int, time_str.split(":"))
    return h * 60 + m
  except (ValueError, AttributeError):
    return default_minutes


def load_config_section(section_name, default_dict):
  """portfolio_config.json에서 특정 설정 섹션을 안전하게 로드하는 공통 헬퍼."""
  try:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
      cfg = json.load(f)
    w = (cfg.get("FVG") or {}).get(section_name) or {}
    return {**default_dict, **{k: v for k, v in w.items() if k in default_dict}}
  except (OSError, json.JSONDecodeError, AttributeError):
    return default_dict


def load_entry_window():
  """portfolio_config.json의 FVG.ENTRY_WINDOW 설정 로드 — 시초가 창 (ET, 24h 형식)."""
  return load_config_section("ENTRY_WINDOW", ENTRY_WINDOW_DEFAULT)


def in_entry_window(ts, window=None):
  """마지막 1분봉 시각이 시초가 창(진입 허용 시간대)에 해당하는지."""
  if ts.tzinfo is None:
    return True
  if window is None:
    window = load_entry_window()
  if not window.get("ENABLED", True):
    return True
  
  start_str = window.get("START", ENTRY_WINDOW_DEFAULT["START"])
  end_str = window.get("END", ENTRY_WINDOW_DEFAULT["END"])
  
  # 파싱 실패 시 기본값 보호 적용
  try:
    sh, sm = map(int, start_str.split(":"))
    eh, em = map(int, end_str.split(":"))
    start_min = sh * 60 + sm
    end_min = eh * 60 + em
  except (ValueError, KeyError, AttributeError):
    print(
        f"[경고] ENTRY_WINDOW 설정 오류 — 기본 시초가 창 적용 "
        f"({ENTRY_WINDOW_DEFAULT['START']}~{ENTRY_WINDOW_DEFAULT['END']})"
    )
    start_min = _parse_time_str(ENTRY_WINDOW_DEFAULT["START"], 9 * 60 + 30)
    end_min = _parse_time_str(ENTRY_WINDOW_DEFAULT["END"], 11 * 60 + 30)

  minutes = ts.hour * 60 + ts.minute
  return start_min <= minutes < end_min


def load_quiet_hours():
  """portfolio_config.json의 FVG.EXIT_ALERT_QUIET_HOURS 설정 로드 — 청산 알림 무음 시간대."""
  return load_config_section("EXIT_ALERT_QUIET_HOURS", EXIT_ALERT_QUIET_HOURS_DEFAULT)


def in_quiet_hours(quiet=None, now=None):
  """현재 시각이 청산 알림 무음 시간대(KST)에 해당하는지."""
  if quiet is None:
    quiet = load_quiet_hours()
  if not quiet.get("ENABLED", True):
    return False

  start_str = quiet.get("START", EXIT_ALERT_QUIET_HOURS_DEFAULT["START"])
  end_str = quiet.get("END", EXIT_ALERT_QUIET_HOURS_DEFAULT["END"])

  try:
    sh, sm = map(int, start_str.split(":"))
    eh, em = map(int, end_str.split(":"))
    start = sh * 60 + sm
    end = eh * 60 + em
  except (ValueError, KeyError, AttributeError):
    print(
        f"[경고] EXIT_ALERT_QUIET_HOURS 설정 오류 — 기본 무음 시간대 적용 "
        f"({EXIT_ALERT_QUIET_HOURS_DEFAULT['START']}~{EXIT_ALERT_QUIET_HOURS_DEFAULT['END']})"
    )
    start = _parse_time_str(EXIT_ALERT_QUIET_HOURS_DEFAULT["START"], 0)
    end = _parse_time_str(EXIT_ALERT_QUIET_HOURS_DEFAULT["END"], 7 * 60)

  if now is None:
    now = datetime.now(timezone(timedelta(hours=9)))
  minutes = now.hour * 60 + now.minute

  if start <= end:
    return start <= minutes < end
  return minutes >= start or minutes < end


def find_swings(df, left=SWING_LEFT, right=SWING_RIGHT):
  """프랙탈 기반 스윙 고점/저점 탐지 — 좌/우 n개 봉 중 유일한 최댓값/최솟값.

  시장 구조(BOS/CHoCH) 판별의 기초. 반환: [{idx, price, kind('high'/'low'), time}]
  """
  high = df["High"].to_numpy()
  low = df["Low"].to_numpy()
  n = len(df)
  swings = []
  for i in range(left, n - right):
    hi = high[i]
    if (
        hi == high[i - left:i + right + 1].max()
        and hi > high[i - left]
        and hi > high[i + right]
    ):
      swings.append({"idx": i, "price": hi, "kind": "high", "time": df.index[i]})
    lo = low[i]
    if (
        lo == low[i - left:i + right + 1].min()
        and lo < low[i - left]
        and lo < low[i + right]
    ):
      swings.append({"idx": i, "price": lo, "kind": "low", "time": df.index[i]})
  swings.sort(key=lambda s: s["idx"])
  return swings


def analyze_htf_trend(df):
  """HTF(15분봉) 시장 구조 분석 — BOS/CHoCH 기반 추세 방향 판별 (영상: market structure).

  규칙 (영상 내용 그대로):
    - 상승 추세에서 저점을 갱신(돌파) → CHoCH_DOWN (매도자 장악) → 방향 'down'
    - 하락 추세에서 고점을 갱신(돌파) → CHoCH_UP (매수자 복귀) → 방향 'up'
    - 추세 방향과 같은 돌파는 BOS_UP/BOS_DOWN (추세 지속)
  반환: direction('up'/'down'/'none'), last_event, last_low_price(최근 확정 스윙 저점)
  """
  swings = find_swings(df)
  if not swings:
    return {"direction": "none", "last_event": None, "last_low_price": None}

  direction = "none"
  last_event = None
  last_high = None
  last_low = None

  for s in swings:
    if s["kind"] == "high":
      if last_high is None:
        last_high = s
      elif s["price"] > last_high["price"]:
        last_event = "CHoCH_UP" if direction == "down" else "BOS_UP"
        direction = "up"
        last_high = s
      else:
        last_high = s
    else:
      if last_low is None:
        last_low = s
      elif s["price"] < last_low["price"]:
        last_event = "CHoCH_DOWN" if direction == "up" else "BOS_DOWN"
        direction = "down"
        last_low = s
      else:
        last_low = s

  return {
      "direction": direction,
      "last_event": last_event,
      "last_low_price": last_low["price"] if last_low else None,
  }


def find_bullish_fvgs(df):
  """상승 FVG 탐지 — 3개 캔들 시퀀스에서 c1의 고점 < c3의 저점 (영상 정의)."""
  high = df["High"].to_numpy()
  low = df["Low"].to_numpy()
  fvgs = []
  for i in range(2, len(df)):
    c1_high = high[i - 2]
    c3_low = low[i]
    if c3_low <= c1_high:
      continue
    midpoint = (c1_high + c3_low) / 2
    if (c3_low - c1_high) / midpoint < MIN_FVG_HEIGHT_PCT:
      continue
    fvgs.append({
        "time": df.index[i],
        "c3_idx": i,
        "bottom": c1_high,
        "top": c3_low,
        "midpoint": midpoint,
    })
  return fvgs


def find_bullish_choch(df, swings):
  """1분봉 상승 CHoCH(캐릭터 변화) 탐지 — 영상 진입 모델 Step 1.

  [Phase 1 수정 완료]
  - 이전 고점을 비교할 때 `next(...)` 대신 `max(..., key=lambda p: p["idx"])`를 사용하여
    “가장 최근의 이전 고점”을 정확히 기준으로 삼도록 개선.
  """
  highs = [s for s in swings if s["kind"] == "high"]
  lows = [s for s in swings if s["kind"] == "low"]
  if not highs:
    return None

  closes = df["Close"].to_numpy()
  best = None
  for h in highs:
    # 가장 최근의 이전 고점(h보다 인덱스가 작은 고점 중 가장 큰 인덱스)을 정확히 탐색
    older_highs = [p for p in highs if p["idx"] < h["idx"]]
    if not older_highs:
      continue
    prev_high = max(older_highs, key=lambda p: p["idx"])

    if h["price"] >= prev_high["price"]:
      continue

    # h 이후 종가가 h의 고점 위로 마감한 첫 봉 = CHoCH 확정 지점
    break_idx = None
    for j in range(h["idx"] + 1, len(closes)):
      if closes[j] > h["price"]:
        break_idx = j
        break
    if break_idx is None:
      continue

    last_low = next((l for l in reversed(lows) if l["idx"] < h["idx"]), None)
    if last_low is None:
      continue

    lows_before = [l for l in lows if l["idx"] < h["idx"]]
    if (
        len(lows_before) >= 2
        and lows_before[-1]["price"] >= lows_before[-2]["price"]
    ):
      continue

    cand = {
        "break_idx": break_idx,
        "high_price": h["price"],
        "low_price": last_low["price"],
    }
    if best is None or cand["break_idx"] > best["break_idx"]:
      best = cand
  return best


def fvg_aligned_with_htf(fvg, htf_fvgs):
  """1분봉 FVG 박스가 최근 HTF(15분) 상승 FVG 존과 겹치는지 (참고 정보용)."""
  for g in htf_fvgs:
    if fvg["bottom"] <= g["top"] and fvg["top"] >= g["bottom"]:
      return True
  return False


def build_long_signal(df_ltf, df_htf):
  """1분봉 롱 진입 모델: HTF 추세 필터 → CHoCH → FVG → 풀백 → 구조 손절."""
  if df_ltf is None or df_htf is None or len(df_ltf) < MIN_LTF_BARS:
    return None

  htf = analyze_htf_trend(df_htf)
  if htf["direction"] != "up":
    return None
  if (
      htf["last_low_price"] is not None
      and df_htf["Close"].iloc[-1] < htf["last_low_price"]
  ):
    return None

  swings = find_swings(df_ltf)
  choch = find_bullish_choch(df_ltf, swings)
  if choch is None:
    return None
  if df_ltf["Low"].iloc[choch["break_idx"]:].min() < choch["low_price"]:
    return None

  candidate = None
  for f in reversed(find_bullish_fvgs(df_ltf)):
    if f["c3_idx"] < choch["break_idx"]:
      continue
    if f["c3_idx"] - choch["break_idx"] > MAX_CHOCH_FVG_GAP_BARS:
      continue
    if len(df_ltf) - f["c3_idx"] > MAX_FVG_AGE_BARS:
      continue
    after_high = df_ltf["High"].iloc[f["c3_idx"] + 1:]
    if after_high.empty or after_high.max() <= f["top"]:
      continue
    if df_ltf["Low"].iloc[f["c3_idx"] + 1:].min() < f["bottom"]:
      continue
    candidate = f
    break
  if candidate is None:
    return None

  fvg = candidate
  entry = fvg["midpoint"]
  bottom, top = fvg["bottom"], fvg["top"]

  close = df_ltf["Close"].iloc[-1]
  if not (bottom <= close <= entry * (1 + MIDPOINT_TOL)):
    return None

  stop_loss = choch["low_price"] - max(0.01, entry * 0.0005)
  risk = entry - stop_loss
  if risk <= 0 or entry <= 0:
    return None
  take_profit = entry + risk * RR_TARGET

  return {
      "ticker": None,
      "time": df_ltf.index[-1],
      "entry": entry,
      "stop_loss": stop_loss,
      "take_profit": take_profit,
      "risk_reward_ratio": f"1 : {RR_TARGET:g}",
      "fvg_time": fvg["time"],
      "choch_break_time": df_ltf.index[choch["break_idx"]],
      "htf_direction": htf["direction"],
      "htf_event": htf["last_event"],
      "htf_align": fvg_aligned_with_htf(fvg, find_bullish_fvgs(df_htf)),
  }


def _download(ticker, period, interval):
  """yfinance 단일 다운로드 — MultiIndex 컬럼 평탄화 + 오류 시 None 반환."""
  try:
    df = yf.download(ticker, period=period, interval=interval, progress=False)
    if df.empty:
      return None
    if isinstance(df.columns, pd.MultiIndex):
      df.columns = df.columns.get_level_values(0)
    return df
  except Exception as exc:
    print(f"[{ticker}] yfinance 수집 실패 ({interval}): {exc}")
    return None


_htf_cache = {}


def _drop_incomplete_htf_bar(df):
  """진행 중인(미완성) 마지막 15분봉 제외."""
  if df.index.tz is not None:
    last_end = df.index[-1] + pd.Timedelta(minutes=15)
    if last_end > pd.Timestamp.now(tz=df.index.tz):
      return df.iloc[:-1]
  return df


def fetch_htf_data(ticker):
  now = time.time()
  cached = _htf_cache.get(ticker)
  if cached and now - cached[0] < HTF_CACHE_SECONDS:
    return cached[1]
  df = _download(ticker, HTF_PERIOD, HTF_INTERVAL)
  if df is not None and not df.empty:
    df = _drop_incomplete_htf_bar(df)
    _htf_cache[ticker] = (now, df)
  return df


def fetch_ltf_data(ticker):
  return _download(ticker, LTF_PERIOD, LTF_INTERVAL)


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "portfolio_config.json")


def build_message(signal):
  htf_align = "✅ HTF 존 정렬" if signal["htf_align"] else "HTF 존 미정렬"
  entry = float(signal["entry"])
  sl_pct = (float(signal["stop_loss"]) - entry) / entry * 100 if entry > 0 else 0.0
  tp_pct = (float(signal["take_profit"]) - entry) / entry * 100 if entry > 0 else 0.0
  return (
      f"🚨 **[매수(Long) 시그널 포착]** `{signal['ticker']}`\n"
      f"• **시간:** `{str(signal['time'])}`\n"
      f"• **HTF(15분) 추세:** `{signal['htf_direction']}`"
      f" (`{signal['htf_event']}`) | {htf_align}\n"
      f"• **추천 진입가 (FVG 중간점):** `{signal['entry']:.2f}`\n"
      f"• **손절가 (구조 저점 아래):** `{signal['stop_loss']:.2f}` 🛑\n"
      f"• **목표 익절가 (Take Profit):** `{signal['take_profit']:.2f}` 🎯\n"
      f"• **손익비 (RR):** `{signal['risk_reward_ratio']}`\n\n"
      f"📋 **나무증권 주문 체크리스트** (주문 3건)\n"
      f"  ① 신규편입 — 손절+익실현 % 등록 (매수 체결 시 감시 자동 시작):\n"
      f"    · 손실제한: `{sl_pct:.2f}%` (SL `{signal['stop_loss']:.2f}`) 🛑\n"
      f"    · 이익실현: `{tp_pct:+.2f}%` (TP `{signal['take_profit']:.2f}`) 🎯\n"
      f"  ② 지정가 매수: `{signal['entry']:.2f}` (중간점)\n"
      f"  ③ MOC 매도 **ON** — 미체결 시 16:00 마감 경매 자동 청산\n\n"
      f"👉 ①은 시세포착주문 > 신규편입 — 매수가 체결되는 순간 서버가 감시를 시작해 "
      f"손절/익절을 자동 실행합니다\n"
      f"   (직접 % 입력, 트레일링 체크 해제).\n"
      f"⏰ 이후 TP/손절/MOC 알림은 아침에 확인할 기록입니다."
  )


ALERT_STATE_PATH = os.path.join(BASE_DIR, "fvg_alerts.json")
ALERT_STATE_PRUNE_HOURS = 48
POSITIONS_PATH = os.path.join(BASE_DIR, "fvg_positions.json")
POSITIONS_PRUNE_HOURS = 120 * 24
DAY_CLOSE_ALERT_MINUTE = 15 * 60 + 40


def _load_alert_state():
  try:
    with open(ALERT_STATE_PATH, "r", encoding="utf-8") as f:
      data = json.load(f)
    return {str(k): float(v) for k, v in data.items()}
  except (OSError, json.JSONDecodeError, TypeError, ValueError):
    return {}


def _save_alert_state(state):
  cutoff = time.time() - ALERT_STATE_PRUNE_HOURS * 3600
  state = {k: v for k, v in state.items() if v >= cutoff}
  try:
    with tempfile.NamedTemporaryFile(
        "w", delete=False, suffix=".json", encoding="utf-8"
    ) as tmp:
      json.dump(state, tmp, ensure_ascii=False, indent=2)
      tmp_path = tmp.name
    shutil.move(tmp_path, ALERT_STATE_PATH)
  except OSError:
    print("[경고] 알림 상태 파일 저장 실패 (무시하고 계속)")


def send_test_alert():
  msg = (
      "🧪 **FVG 봇 테스트 알림** — Discord 웹훅 + @멘션 경로 점검\n"
      "이 메시지가 보이면 실전 시그널 알림도 동일 경로로 도착합니다. "
      "(자동 매매 없음 — 실제 주문은 직접 실행)"
  )
  send_discord_webhook(msg)


def _load_positions():
  try:
    with open(POSITIONS_PATH, "r", encoding="utf-8") as f:
      return json.load(f)
  except (OSError, json.JSONDecodeError):
    return {}


def _save_positions(positions):
  cutoff = time.time() - POSITIONS_PRUNE_HOURS * 3600
  positions = {
      k: v for k, v in positions.items()
      if v.get("status") != "CLOSED" or (v.get("closed_at") or 0) >= cutoff
  }
  try:
    with tempfile.NamedTemporaryFile(
        "w", delete=False, suffix=".json", encoding="utf-8"
    ) as tmp:
      json.dump(positions, tmp, ensure_ascii=False, indent=2)
      tmp_path = tmp.name
    shutil.move(tmp_path, POSITIONS_PATH)
  except OSError:
    print("[경고] 포지션 상태 파일 저장 실패 (무시하고 계속)")


def record_position(ticker, signal):
  return {
      "ticker": ticker,
      "fvg_time": str(signal["fvg_time"]),
      "entry_time": str(signal["time"]),
      "entry": float(signal["entry"]),
      "sl": float(signal["stop_loss"]),
      "tp": float(signal["take_profit"]),
      "status": "OPEN",
  }


def build_exit_message(pos, exit_price, reason):
  heads = {
      "TP": "✅ **익절(TP) 도달 — 매도(청산) 알림**",
      "SL": "🛑 **손절(SL) 도달 — 매도(청산) 알림**",
      "DAY_CLOSE": "⏰ **당일 마감 — 매도(청산) 알림**",
  }
  notes = {
      "TP": "목표 익절가 도달 — 수익 확정",
      "SL": "구조 저점 아래 손절 체결",
      "DAY_CLOSE": "15:40 기준가 — MOC(16:00 마감 경매) 걸려 있으면 자동 청산, 아니면 직접 청산",
  }
  ticker = pos.get("ticker", "?")
  entry = float(pos.get("entry", 0) or 0)
  pnl = exit_price / entry - 1 if entry > 0 else 0.0
  return (
      f"{heads[reason]} `{ticker}`\n"
      f"• **진입가 (FVG 중간점):** `{entry:.2f}`\n"
      f"• **청산가:** `{exit_price:.2f}` ({notes[reason]})\n"
      f"• **손익:** `{pnl * 100:+.1f}%`\n"
      f"• 진입: `{pos.get('entry_time', '?')}`"
  )


def check_exit_alerts(ticker, df_ltf, positions, now_et=None, quiet_hours=None):
  open_pos = [
      p for p in positions.values()
      if p.get("ticker") == ticker and p.get("status") == "OPEN"
  ]
  if not open_pos:
    return 0
  quiet = in_quiet_hours(quiet_hours)
  bar = df_ltf.iloc[-1]
  high, low, close = bar["High"], bar["Low"], bar["Close"]
  if now_et is None:
    now_et = pd.Timestamp.now(tz=df_ltf.index.tz)
  now_min = now_et.hour * 60 + now_et.minute
  day_close_near = now_min >= DAY_CLOSE_ALERT_MINUTE

  sent = 0
  for p in open_pos:
    sl, tp = p.get("sl"), p.get("tp")
    if sl is None or tp is None:
      continue
    exit_p, reason = None, None
    if low <= sl:
      exit_p, reason = sl, "SL"
    elif high >= tp:
      exit_p, reason = tp, "TP"
    elif day_close_near:
      exit_p, reason = float(close), "DAY_CLOSE"
    if exit_p is None:
      continue
    if send_discord_webhook(build_exit_message(p, exit_p, reason), mention=not quiet):
      p.update({
          "status": "CLOSED", "exit_price": exit_p,
          "exit_reason": reason, "closed_at": time.time(),
      })
      entry = float(p.get("entry", 0) or 0)
      pnl = exit_p / entry - 1 if entry > 0 else 0.0
      quiet_note = " (무음 시간대 — 멘션 없음)" if quiet else ""
      print(f"[{ticker}] 청산 알림: {reason} @ {exit_p:.2f} (손익 {pnl:+.2%}){quiet_note}")
      sent += 1
    else:
      print(f"[{ticker}] 청산 알림 미전송 — CLOSED 처리 보류 (다음 실행에서 재시도)")
  return sent


def run_strategy():
  print(
      f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
      "FVG 전략 스캐너 실행 중... (HTF 15분 추세 필터 + 1분봉 CHoCH/FVG 진입 모델)"
  )
  alert_state = _load_alert_state()
  alerted_any = False
  positions = _load_positions()
  positions_changed = False
  entry_window = load_entry_window()
  quiet_hours = load_quiet_hours()
  for idx, ticker in enumerate(TICKERS):
    if idx > 0:
      time.sleep(1.5)
    try:
      df_ltf = fetch_ltf_data(ticker)
      if df_ltf is None or len(df_ltf) < MIN_LTF_BARS:
        print(f"[{ticker}] 1분봉 데이터 부족 — 분석 생략")
        continue
      if not in_trading_session(df_ltf.index[-1]):
        print(f"[{ticker}] 장중 세션 아님 — 스킵 (마지막 봉: {df_ltf.index[-1]})")
        continue

      if check_exit_alerts(ticker, df_ltf, positions, quiet_hours=quiet_hours):
        positions_changed = True

      if not in_entry_window(df_ltf.index[-1], entry_window):
        print(
            f"[{ticker}] 시초가 창 밖 — 진입 신호 스킵 "
            f"(허용 {entry_window['START']}~{entry_window['END']} ET, "
            f"마지막 봉 {df_ltf.index[-1]})"
        )
        continue

      df_htf = fetch_htf_data(ticker)
      if df_htf is None or df_htf.empty:
        print(f"[{ticker}] 15분봉 데이터 부족 — 분석 생략")
        continue

      signal = build_long_signal(df_ltf, df_htf)
      if signal is None:
        print(f"[{ticker}] 조건 부합 시그널 없음")
        continue

      signal["ticker"] = ticker
      key = f"{ticker}|{str(signal['fvg_time'])}"
      last = alert_state.get(key)
      if last is not None and time.time() - last < ALERT_COOLDOWN_SECONDS:
        print(f"[{ticker}] 동일 FVG 재알림 쿨다운 중 — 중복 방지")
        continue

      if send_discord_webhook(build_message(signal)):
        positions[key] = record_position(ticker, signal)
        positions_changed = True
        alert_state[key] = time.time()
        alerted_any = True
      else:
        print(f"[{ticker}] 알림 미전송 — 포지션/쿨다운 기록 생략 (다음 실행에서 재시도)")

    except Exception as e:
      print(f"[{ticker}] 분석 중 오류 발생: {e}")

  if alerted_any:
    _save_alert_state(alert_state)
  if positions_changed:
    _save_positions(positions)


def main():
  if "--test-alert" in sys.argv:
    send_test_alert()
    return
  if "--once" in sys.argv:
    run_strategy()
    return
  while True:
    run_strategy()
    time.sleep(SCAN_INTERVAL_SECONDS)


if __name__ == "__main__":
  main()