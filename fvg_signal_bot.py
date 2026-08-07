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
       → 진입 후 당일 장 마감(15:55~16:00 ET) 전 미해결 시 청산 권장 (알림 메시지에 포함)

  - 알림은 Discord Webhook으로만 전송 — 실제 주문 자동 실행 없음 (수동 매매)
  - DISCORD_USER_ID 설정 시 알림에 @멘션 3회 포함 — 잠든 사이에도 모바일 알림이
    강하게/여러 번 울리도록 웨이크업 강화 (멘션 3회 패턴)
  - 파일 기반 쿨다운(fvg_alerts.json)으로 동일 FVG 중복 알림 방지 — 로컬 크론과
    GitHub Actions(--once 백업)가 git으로 상태를 공유해 프로세스를 넘나드는
    교차 중복 알림도 차단한다 (loop 모드에서도 동일하게 동작)
  - HTF(15분봉) 데이터는 15분 캐시 — 실행 간 불필요한 재다운로드 방지
  - 실행: python3 fvg_signal_bot.py [--once]  (--once = 1회 스캔 후 종료)

Dependencies: pandas, requests, yfinance (requirements.txt에 포함)
==================================================================
"""
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime

import pandas as pd
import requests
import yfinance as yf

# ==========================================
# [사용자 설정 영역]
# ==========================================
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "YOUR_DISCORD_WEBHOOK")
# 웨이크업 멘션: DISCORD_USER_ID 설정 시 시그널 알림에 @멘션 3회 포함 (멘션 3회 패턴)
DISCORD_USER_ID = os.getenv("DISCORD_USER_ID", "YOUR_DISCORD_USER_ID")
# 모니터링할 종목 리스트 (3배 레버리지 ETF — 영상의 고변동성 종목과 유사)
TICKERS = ["TQQQ", "SOXL"]

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
HTF_CACHE_SECONDS = 900       # HTF(15분봉) 재수집 주기 — API 부하 절감


def send_discord_webhook(message):
  """디스코드 채널로 메시지를 전송하는 함수 (웹훅 미설정 시 stdout 출력)"""
  if not DISCORD_WEBHOOK or DISCORD_WEBHOOK == "YOUR_DISCORD_WEBHOOK":
    print(message)
    return
  # 웨이크업 강화: 멘션을 3회 반복 — 잠든 사이에도 Discord 모바일 알림이 강하게
  # 울리도록 (사용자가 직접 매매를 진행해야 하므로).
  # placeholder("YOUR_...")는 미설정 상태이므로 멘션에서 제외한다.
  mention = (
      " ".join([f"<@{DISCORD_USER_ID}>"] * 3)
      if DISCORD_USER_ID and DISCORD_USER_ID != "YOUR_DISCORD_USER_ID"
      else ""
  )
  content = (mention + "\n" + message) if mention else message
  payload = {"content": content}
  try:
    response = requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
    if response.status_code == 204:
      print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 알림 전송 성공")
    else:
      print(f"전송 실패: {response.status_code}, {response.text}")
  except Exception as e:
    print(f"웹훅 전송 중 에러 발생: {e}")


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
        # 상승 고점 돌파 — 하락장이었다면 반전(CHoCH_UP), 아니면 추세 지속(BOS_UP)
        last_event = "CHoCH_UP" if direction == "down" else "BOS_UP"
        direction = "up"
        last_high = s
      else:
        last_high = s
    else:
      if last_low is None:
        last_low = s
      elif s["price"] < last_low["price"]:
        # 하락 저점 돌파 — 상승장이었다면 반전(CHoCH_DOWN), 아니면 추세 지속(BOS_DOWN)
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
  """상승 FVG 탐지 — 3개 캔들 시퀀스에서 c1의 고점 < c3의 저점 (영상 정의).

  노이즈 방지: 가격 대비 최소 높이(0.05%) 미만 갭 제외. 연속 임펄스로 겹치는
  박스는 허용하되, 진입 후보 선정(find_bullish_choch 연계)에서 신선도·CHoCH
  연관성·모멘텀(상단 돌파 후 되돌림) 검증으로 걸러낸다.
  """
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

  정의: 하락 구조(이전 고점보다 낮은 고점) 후, 종가가 그 스윙 고점 위로 마감.
  반환: dict(break_idx, high_idx, high_price, low_price) — 가장 최근 CHoCH
    - high_price: 돌파된 스윙 고점
    - low_price: 그 고점 직전 마지막 스윙 저점 (구조 기반 손절 기준)
  """
  highs = [s for s in swings if s["kind"] == "high"]
  lows = [s for s in swings if s["kind"] == "low"]
  if not highs:
    return None

  closes = df["Close"].to_numpy()
  best = None
  for h in highs:
    # CHoCH는 '낮은 고점'을 돌파하는 반전 — 이전 고점보다 높으면 BOS(추세 지속)라 제외
    prev_high = next((p for p in highs if p["idx"] < h["idx"]), None)
    if prev_high is None or h["price"] >= prev_high["price"]:
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

    # 영상 Step 1 "higher high after a lower low": 낮은 고점 직전의 최근 두 저점이
    # 하락 구조(저점 하락)여야 반전 CHoCH로 인정 — 상승 추세 내 풀백 돌파는 BOS(추세 지속)
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
  """1분봉 FVG 박스가 최근 HTF(15분) 상승 FVG 존과 겹치는지 (참고 정보용).

  영상 예제: 1분봉 CHoCH가 15분봉 FVG 존에서 반응하며 시작 — 고품질 셋업의 신호.
  """
  for g in htf_fvgs:
    if fvg["bottom"] <= g["top"] and fvg["top"] >= g["bottom"]:
      return True
  return False


def build_long_signal(df_ltf, df_htf):
  """1분봉 롱 진입 모델: HTF 추세 필터 → CHoCH → FVG → 풀백 → 구조 손절.

  모든 조건 충족 시 시그널 dict 반환, 아니면 None. (영상 3단계 진입 모델)
  """
  if df_ltf is None or df_htf is None or len(df_ltf) < MIN_LTF_BARS:
    return None

  # ① HTF(15분) 추세 필터 — 상승장일 때만 롱 허용
  htf = analyze_htf_trend(df_htf)
  if htf["direction"] != "up":
    return None
  # 보수 가드: 최근 확정 구조 저점 아래로 종가 이탈 = CHoCH 직전 상황, 스킵
  if (
      htf["last_low_price"] is not None
      and df_htf["Close"].iloc[-1] < htf["last_low_price"]
  ):
    return None

  # ② Step 1: 1분봉 CHoCH
  swings = find_swings(df_ltf)
  choch = find_bullish_choch(df_ltf, swings)
  if choch is None:
    return None
  # 구조 연속성: CHoCH 이후 현재까지 CHoCH 저점을 이탈한 적이 없어야 함.
  # 중간에 구조 반전(저점 이탈)이 있었다면 오래된 CHoCH와 최근 FVG의 잘못된
  # 페어링을 방지하고 시그널을 무효화한다 (손절 과대화 방지).
  if df_ltf["Low"].iloc[choch["break_idx"]:].min() < choch["low_price"]:
    return None

  # ③ Step 2: CHoCH 돌파가 남긴 최근 FVG ("FVG produced inside that change of character")
  candidate = None
  for f in reversed(find_bullish_fvgs(df_ltf)):
    if f["c3_idx"] < choch["break_idx"]:
      continue  # CHoCH 이전에 생성된 FVG는 무관
    if f["c3_idx"] - choch["break_idx"] > MAX_CHOCH_FVG_GAP_BARS:
      continue  # CHoCH와 너무 동떨어진 FVG
    if len(df_ltf) - f["c3_idx"] > MAX_FVG_AGE_BARS:
      continue  # 오래된 FVG (신선도)
    # 모멘텀 검증: FVG 생성 후 가격이 박스 상단 위로 돌파한 적이 있어야
    # '돌파 → 되돌림(풀백)' 시퀀스가 성립 (영상: we break out, price retraces into this area)
    # 주의: 빈 슬라이스(마지막 봉 생성 FVG)는 NaN 비교 우회가 되므로 명시적으로 배제
    after_high = df_ltf["High"].iloc[f["c3_idx"] + 1:]
    if after_high.empty or after_high.max() <= f["top"]:
      continue
    # 갭 무결성: 생성 후 어떤 봉의 저점도 하단을 이탈하지 않아야 함
    # (채워진 갭 = '그냥 통과한 FVG' — 영상 명시 제외 케이스)
    if df_ltf["Low"].iloc[f["c3_idx"] + 1:].min() < f["bottom"]:
      continue
    candidate = f
    break
  if candidate is None:
    return None

  fvg = candidate
  entry = fvg["midpoint"]
  bottom, top = fvg["bottom"], fvg["top"]

  # ④ Step 3: 풀백 검증 — 종가가 위에서 FVG 중간점 영역까지 되돌아옴.
  #    하단(채워진 갭) 이탈 시 제외 — "그냥 통과해버린 FVG는 진입 존이 아님"
  close = df_ltf["Close"].iloc[-1]
  if not (bottom <= close <= entry * (1 + MIDPOINT_TOL)):
    return None

  # ⑤ 구조 기반 손절 — CHoCH 이전 마지막 스윙 저점 아래 (영상: last low on the trend)
  stop_loss = choch["low_price"] - max(0.01, entry * 0.0005)
  risk = entry - stop_loss
  if risk <= 0 or entry <= 0:
    return None
  take_profit = entry + risk * RR_TARGET

  return {
      "ticker": None,  # run_strategy에서 채움
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


_htf_cache = {}  # ticker -> (수집 시각, df) — HTF 데이터 15분 캐시


def _drop_incomplete_htf_bar(df):
  """진행 중인(미완성) 마지막 15분봉 제외 — 구조 판별은 완성 봉 기준 (룩어헤드 방지)."""
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
    _htf_cache[ticker] = (now, df)  # 실패(None)는 캐시하지 않음 — 다음 실행에서 재시도
  return df


def fetch_ltf_data(ticker):
  return _download(ticker, LTF_PERIOD, LTF_INTERVAL)


def build_message(signal):
  htf_align = "✅ HTF 존 정렬" if signal["htf_align"] else "HTF 존 미정렬"
  return (
      f"🚨 **[매수(Long) 시그널 포착]** `{signal['ticker']}`\n"
      f"• **시간:** `{str(signal['time'])}`\n"
      f"• **HTF(15분) 추세:** `{signal['htf_direction']}`"
      f" (`{signal['htf_event']}`) | {htf_align}\n"
      f"• **추천 진입가 (FVG 중간점):** `{signal['entry']:.2f}`\n"
      f"• **손절가 (구조 저점 아래):** `{signal['stop_loss']:.2f}` 🛑\n"
      f"• **목표 익절가 (Take Profit):** `{signal['take_profit']:.2f}` 🎯\n"
      f"• **손익비 (RR):** `{signal['risk_reward_ratio']}`\n\n"
      f"👉 1분봉 CHoCH → FVG 중간점 풀백 진입 모델입니다. "
      f"중간점 지정가 진입 후 구조 저점 아래 손절을 배치하세요!\n"
      f"⏰ **데이 모델: 장 마감 전(15:55~16:00 ET) 미해결 시 청산 권장** "
      f"(야간 보유는 갭 리스크 — 백테스트 근거)"
  )


ALERT_STATE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "fvg_alerts.json"
)
ALERT_STATE_PRUNE_HOURS = 48  # 상태 파일 정리 기준 — 2일 지난 항목 제거


def _load_alert_state():
  """알림 상태(fvg_alerts.json) 로드 — 없거나 손상 시 빈 dict 반환.

  key = "TICKER|FVG생성시각(ISO)", value = 마지막 알림 epoch 초.
  로컬 크론과 GitHub Actions(--once)가 이 파일을 git으로 공유해
  프로세스를 넘나드는 중복 알림을 차단한다.
  """
  try:
    with open(ALERT_STATE_PATH, "r", encoding="utf-8") as f:
      data = json.load(f)
    return {str(k): float(v) for k, v in data.items()}
  except (OSError, json.JSONDecodeError, TypeError, ValueError):
    return {}


def _save_alert_state(state):
  """알림 상태를 fvg_alerts.json에 원자적(atomic) 저장 — 2일 지난 항목 정리.

  tempfile+shutil.move로 크래시 시 파일 손상 방지 (원자적 저장).
  """
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


def run_strategy():
  print(
      f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
      "FVG 전략 스캐너 실행 중... (HTF 15분 추세 필터 + 1분봉 CHoCH/FVG 진입 모델)"
  )
  # 파일 기반 알림 상태 — --once(로컬 크론/GHA)와 loop 모드가 동일하게 공유
  alert_state = _load_alert_state()
  alerted_any = False
  for idx, ticker in enumerate(TICKERS):
    if idx > 0:
      time.sleep(1.5)  # yfinance 레이트 리밋 완화 — 종목별 요청 분산
    try:
      df_ltf = fetch_ltf_data(ticker)
      if df_ltf is None or len(df_ltf) < MIN_LTF_BARS:
        print(f"[{ticker}] 1분봉 데이터 부족 — 분석 생략")
        continue
      if not in_trading_session(df_ltf.index[-1]):
        print(f"[{ticker}] 장중 세션 아님 — 스킵 (마지막 봉: {df_ltf.index[-1]})")
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

      send_discord_webhook(build_message(signal))
      alert_state[key] = time.time()
      alerted_any = True

    except Exception as e:
      # 한 종목의 실패가 다른 종목 분석을 막지 않도록 방어
      print(f"[{ticker}] 분석 중 오류 발생: {e}")

  if alerted_any:
    _save_alert_state(alert_state)


def main():
  if "--once" in sys.argv:
    run_strategy()
    return
  while True:
    run_strategy()
    time.sleep(SCAN_INTERVAL_SECONDS)


if __name__ == "__main__":
  main()
