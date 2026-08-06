# -*- coding: utf-8 -*-
"""
==================================================================
 SOXL/TQQQ 4시간봉 스윙 봇 — 골드핑거식 전략 (다중 종목 지원)
==================================================================
전략 (유튜브 골드핑거 영상 로직 이식):
  1. Finnhub 1시간봉 60일 수집 → 장 개장(09:30 ET) 기준 4시간봉 리샘플
  2. 3중 EMA(10/20/50) 정배열 + 거래량 돌파(20봉 평균+1σ) + 최근 5봉 고점 돌파
  3. 변동성 수축 필터: 직전 봉 ATR이 20봉 평균보다 낮은(수축) 상태에서의 돌파만 인정
     (유튜버 영상의 '변동성 수축' 조건 구현 — 2년 백테스트에서 두 종목 성과/MDD 개선)
  4. 하락 후 '첫 번째 신호'는 필터링(매수 스킵) → 두 번째 신호에서만 매수
  5. 익절(TP): 보유 중 종가가 진입가 + TAKE_PROFIT_ATR×진입 ATR에 도달하면 수익 확정 청산
     (이익 반납 방지 — 백테스트 검증: 20EMA 단독 대비 총수익 개선, MFE 반납 90%→2~70%)
  6. 20 EMA 이탈 시 매도/청산 신호 (TP 미도달 시의 손절·추세 청산)
  - 알림은 Discord Webhook으로만 전송 — 실제 주문 자동 실행 없음 (수동 매매)
  - 종목별 신호 상태는 swing_state.json에 영속화 — 중복 BUY/SELL 알림 방지
  - TICKERS 환경변수로 종목 목록 변경 가능 (콤마 구분, 기본: TQQQ,SOXL)

Dependencies: pandas, requests (requirements.txt에 포함)
실행: python3 swing_bot.py
==================================================================
"""
import json
import os
import shutil
import tempfile
import time

import pandas as pd
import requests

# ==========================================
# [사용자 설정 영역]
# ==========================================
# GitHub Actions 시크릿은 공백/줄바꿈을 자동 제거하지 않아(원문 보존),
# 붙여넣기 시 앞뒤 공백이 함께 저장되면 키가 거부된다(403/401).
# 원본을 먼저 보관해 진단에 쓰고, 실제 사용은 strip()으로 보정한 값만 쓴다.
_RAW_FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "YOUR_FINNHUB_API_KEY")
FINNHUB_API_KEY = _RAW_FINNHUB_API_KEY.strip()
_API_KEY_HAD_WHITESPACE = bool(_RAW_FINNHUB_API_KEY) and _RAW_FINNHUB_API_KEY != FINNHUB_API_KEY
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "YOUR_DISCORD_WEBHOOK")
DISCORD_USER_ID = os.getenv("DISCORD_USER_ID", "YOUR_DISCORD_USER_ID")
# 다중 종목: TICKERS 환경변수로 변경 가능 (콤마 구분, 기본 TQQQ+SOXL)
TICKERS = [
    t.strip().upper()
    for t in os.getenv("TICKERS", "TQQQ,SOXL").split(",")
    if t.strip()
]
# 스윙 봇 설정(TP 승수·AUTO_UPDATE)은 swing_config.json에서 관리 — 코드 수정 없이
# 값을 바꿀 수 있다. (파일 없거나 손상 시 아래 기본값 폴백. TAKE_PROFIT_ATR 환경변수는
# 배포 레벨 오버라이드)
CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "swing_config.json"
)


def _load_config(path=None):
  """swing_config.json 로드 — 없거나 손상 시 기본값 사용.

  path를 주면 해당 경로에서 읽는다(테스트용). 반환 dict는 항상 기본 키를 포함한다.
  """
  defaults = {
      "TAKE_PROFIT_ATR": 3.0,
      "TAKE_PROFIT_ATR_BY_TICKER": {"TQQQ": 1.5, "SOXL": 3.5},
      "AUTO_UPDATE": True,
  }
  path = path or CONFIG_PATH
  if not os.path.exists(path):
    return defaults
  try:
    with open(path, "r", encoding="utf-8") as f:
      data = json.load(f)
  except (json.JSONDecodeError, OSError):
    print(f"[경고] {path} 로드 실패 — 기본값 사용")
    return defaults
  merged = dict(defaults)
  merged.update({k: data[k] for k in defaults if k in data})
  return merged


def _to_float(val, default):
  """숫자/숫자 문자열 → float. 변환 불가(설정 오타) 시 default 반환 — 봇 크래시 방지."""
  try:
    return float(val)
  except (TypeError, ValueError):
    return default


def _to_bool(val, default=True):
  """설정 값 → bool. 'false'/'0'/'off' 문자열도 False로 인식 (수동 편집 오타 방어)."""
  if isinstance(val, bool):
    return val
  if isinstance(val, str):
    return val.strip().lower() not in ("false", "0", "no", "off")
  if isinstance(val, (int, float)):
    return val != 0
  return default


_TP_CONFIG = _load_config()
# 익절(TP): 보유 중 종가 ≥ 진입가 + TP 승수 × 진입 시점 ATR 이면 익절 매도 신호
# (기본 3.0 — 더블 볼린저 영상의 손익비 3:1 개념과 동일. 0 = 비활성화)
# 환경변수 TAKE_PROFIT_ATR이 설정되면 그 값 우선(배포 레벨 오버라이드)
TAKE_PROFIT_ATR = _to_float(
    os.getenv("TAKE_PROFIT_ATR", _TP_CONFIG.get("TAKE_PROFIT_ATR", 3.0)), 3.0
)
# 종목별 TP 승수 — 2년 백테스트 스윙 최적값 (swing_config.json에서 관리):
#   TQQQ 1.5: +15.3%/-1.1% MDD (contr) | SOXL 3.5: +18.6%/-10.2% MDD (contr)
# 미등록 종목은 TAKE_PROFIT_ATR 기본값 사용. 잘못된 값은 건너뛰어 기본값 폴백.
TAKE_PROFIT_ATR_BY_TICKER = {}
for _k, _v in _TP_CONFIG.get("TAKE_PROFIT_ATR_BY_TICKER", {}).items():
  _fv = _to_float(_v, None)
  if _fv is not None:
    TAKE_PROFIT_ATR_BY_TICKER[str(_k).upper()] = _fv
# 자동 갱신 스위치: false면 분기 재평가(swing_tp_review.py)가 보고만 하고 승수를
# 자동 변경하지 않는다 (수동 모드 — 사용자가 swing_config.json을 직접 수정)
AUTO_UPDATE = _to_bool(_TP_CONFIG.get("AUTO_UPDATE", True))

STATE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "swing_state.json"
)
# 신호 저널: BUY/SELL 이벤트를 가격과 함께 append-only로 기록 (성과 평가용)
# — 실행마다 덮어써지는 swing_log.txt와 달리 누적되며, GitHub Actions에서 커밋된다.
SIGNALS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "swing_signals.jsonl"
)
REQUEST_TIMEOUT = 15  # Finnhub API 타임아웃(초)
SESSION_TZ = "America/New_York"  # 거래 세션 타임존
SESSION_OPEN = pd.Timedelta(hours=9, minutes=30)  # 장 개장 시각 → 4h 버킷 기준
MIN_4H_BARS = 51  # EMA50 워밍업 + 첫 신호 스캔용 최소 4시간봉 수


def session_resample_origin(df_1h):
  """장 개장(09:30 ET) 기준 4시간봉 버킷 origin.

  resample(origin=...)의 날짜가 데이터 범위와 달라지면 버킷 정렬이 어긋나므로,
  데이터 첫 봉의 날짜 09:30(ET)을 기준으로 잡는다. 결과적으로 TradingView의
  미국 주식 4h 봉([09:30~13:30), [13:30~17:30) ET)과 동일한 세션 정렬이 된다.
  """
  if len(df_1h) == 0:
    raise ValueError("1시간봉 데이터가 비어 있음")
  first = df_1h.index[0]
  if first.tz is None:
    return first.normalize().tz_localize(SESSION_TZ) + SESSION_OPEN
  return first.normalize() + SESSION_OPEN


def _mask_api_key(api_key):
  """API 키 상태를 로그용으로 마스킹 — 키 전체를 노출하지 않고 상태만 알려준다.

  - 비어 있음: 시크릿이 빈 값/미설정 (GitHub Actions에서 키가 안 넘어온 상태)
  - 기본값 placeholder: 코드 기본값 그대로 (환경변수 미설정)
  - 설정됨: 마지막 4자리만 노출 — 값 자체는 비밀로 유지
  """
  if not api_key:
    return "(비어 있음 — 시크릿 미설정/빈 값)"
  if api_key == "YOUR_FINNHUB_API_KEY":
    return "(기본값 placeholder — 환경변수 미설정)"
  if len(api_key) <= 4:  # 극단적 짧은 값 방어 — 마지막 4자리 노출 시 키 전체가 드러남
    return "(설정됨)"
  return f"(설정됨 …{api_key[-4:]})"


def fetch_finnhub_hourly_data(ticker, api_key):
  """Finnhub 1시간봉 수집 (최근 60일) — 타임아웃·재시도·오류 방어 포함"""
  end_time = int(time.time())
  start_time = end_time - (60 * 24 * 60 * 60)  # 60일 전

  url = (
      f"https://finnhub.io/api/v1/stock/candle?symbol={ticker}"
      f"&resolution=60&from={start_time}&to={end_time}&token={api_key}"
  )
  for attempt in range(3):
    try:
      response = requests.get(url, timeout=REQUEST_TIMEOUT)
      if response.status_code == 429:  # 무료 티어 요청 한도 초과 → 백오프 후 재시도
        print(f"[{ticker}] [경고] Finnhub 요청 한도(429) 초과 — 백오프 후 재시도")
        time.sleep(2 ** (attempt + 1))
        continue
      if response.status_code in (401, 403):
        # 인증/권한 오류 — 재시도해도 해결되지 않으므로 즉시 종료.
        # Finnhub는 응답 본문에 실제 거부 사유({"error": ...})를 담아 오는데,
        # 상태 코드만 출력하면 원인을 알 수 없어 본문도 함께 남긴다.
        reason = ""
        try:
          body = response.json()
          reason = body.get("error", "") if isinstance(body, dict) else ""
        except ValueError:
          pass
        hint = (
            "API 키가 잘못됨 — GitHub Actions 시크릿(FINNHUB_API_KEY) 값 확인"
            if response.status_code == 401
            else "API 키 접근 거부 — 키 재발급 또는 Finnhub 계정 상태(정지·요금제) 확인"
        )
        detail = f" | 서버 메시지: {reason}" if reason else ""
        print(f"[{ticker}] [오류] Finnhub {response.status_code}: {hint}{detail}")
        return pd.DataFrame()
      if response.status_code >= 400:  # 기타 오류 — 재시도 의미 없음, 즉시 종료
        print(f"[{ticker}] [오류] Finnhub 응답 {response.status_code}")
        return pd.DataFrame()
      response.raise_for_status()
      data = response.json()
      if data.get("s") != "ok":
        # API가 명시적으로 거부(no_data 등) — 재시도 의미 없음
        print(f"[{ticker}] [오류] 데이터 로드 실패: {data.get('s')}")
        return pd.DataFrame()
      return pd.DataFrame({
          "Open": data["o"],
          "High": data["h"],
          "Low": data["l"],
          "Close": data["c"],
          "Volume": data["v"],
      }, index=pd.to_datetime(data["t"], unit="s", utc=True)
            .tz_convert("America/New_York"))
    except (requests.RequestException, ValueError) as exc:
      if attempt == 2:
        print(f"[{ticker}] [오류] Finnhub 요청 실패: {exc}")
      else:
        time.sleep(2 ** attempt)  # 일시적 오류(타임아웃/연결)만 백오프 재시도
  print(f"[{ticker}] [오류] Finnhub 데이터 수집 실패 (3회 시도 후 종료)")
  return pd.DataFrame()


def send_discord_alert(message):
  """디스코드 웹훅 알림 전송 (웨이크업 강화 포함)"""
  if not DISCORD_WEBHOOK or DISCORD_WEBHOOK == "YOUR_DISCORD_WEBHOOK":
    print(message)
    return

  # 웨이크업 강화: 멘션을 3회 반복 — 잠든 사이에도 Discord 모바일 알림이
  # 강하게/여러 번 울리도록 (사용자가 직접 매매를 진행해야 하므로)
  mention = (
      " ".join([f"<@{DISCORD_USER_ID}>"] * 3)
      if DISCORD_USER_ID
      else ""
  )
  content = (mention + "\n" + message) if mention else message
  requests.post(DISCORD_WEBHOOK, json={"content": content}, timeout=10)


def default_ticker_state():
  """종목별 기본 상태 — TP 판정에 쓰는 진입가/진입 ATR 키 포함"""
  return {
      "state": "WAITING",
      "last_buy": None,
      "last_sell": None,
      "entry_price": None,
      "entry_atr": None,
  }


def load_state():
  """swing_state.json 로드 — 없거나 손상 시 기본 상태 생성"""
  default = {t: default_ticker_state() for t in TICKERS}
  if not os.path.exists(STATE_PATH):
    return default
  try:
    with open(STATE_PATH, "r", encoding="utf-8") as f:
      data = json.load(f)
  except (json.JSONDecodeError, OSError):
    return default
  for t in TICKERS:
    # 기존 state(TP 추가 전 저장분)에도 키를 채워 넣음 — IN_POSITION 이력 보존
    data.setdefault(t, default_ticker_state())
    for key, val in default_ticker_state().items():
      data[t].setdefault(key, val)
  return data


def save_state(state):
  """swing_state.json 원자적(atomic) 저장 — 크래시 시 파일 손상 방지"""
  try:
    with tempfile.NamedTemporaryFile(
        "w", delete=False, suffix=".json", encoding="utf-8"
    ) as tmp:
      json.dump(state, tmp, ensure_ascii=False, indent=2)
      tmp_path = tmp.name
    shutil.move(tmp_path, STATE_PATH)
  except OSError:
    print("[오류] 상태 파일 저장 실패 (무시하고 계속)")


def log_signal(ticker, event, signal_time, price):
  """BUY/SELL 신호를 swing_signals.jsonl에 append (성과 평가용 저널).

  event: 'BUY' | 'SELL' — 상태 전이가 확정된 시점에 기록한다.
  Discord 전송 성공 여부와 무관하게 기록 (전송 실패 시에도 신호는 유효).
  """
  record = {
      "ticker": ticker,
      "event": event,
      "time": signal_time.isoformat(),
      "price": round(float(price), 2),
  }
  try:
    with open(SIGNALS_PATH, "a", encoding="utf-8") as f:
      f.write(json.dumps(record, ensure_ascii=False) + "\n")
  except OSError as exc:
    print(f"[{ticker}] [오류] 신호 저널 기록 실패: {exc}")


def build_4h_frame(df_1h):
  """1시간봉 → 장 개장(09:30 ET) 기준 4시간봉 리샘플 + 지표 계산"""
  origin = session_resample_origin(df_1h)
  df_4h = df_1h.resample("4h", origin=origin).agg({
      "Open": "first",
      "High": "max",
      "Low": "min",
      "Close": "last",
      "Volume": "sum",
  }).dropna()

  # 미완성 봉 방어: 마지막 버킷의 마지막 1시간봉이 아직 진행 중이면(봉 종료 시각 > 현재)
  # 그 버킷은 부분 봉이므로 제외 — 연중(겨울 13:00 ET 실행 등) 언제든 안전
  last_candle_end = df_1h.index[-1] + pd.Timedelta(hours=1)
  now_et = pd.Timestamp.now(tz=SESSION_TZ)
  if len(df_4h) and last_candle_end > now_et:
    df_4h = df_4h.iloc[:-1]
  if len(df_4h) < MIN_4H_BARS:
    return pd.DataFrame()

  df_4h["EMA_10"] = df_4h["Close"].ewm(span=10, adjust=False).mean()
  df_4h["EMA_20"] = df_4h["Close"].ewm(span=20, adjust=False).mean()
  df_4h["EMA_50"] = df_4h["Close"].ewm(span=50, adjust=False).mean()

  df_4h["Vol_SMA"] = df_4h["Volume"].rolling(window=20).mean()
  df_4h["Vol_Std"] = df_4h["Volume"].rolling(window=20).std()
  df_4h["Vol_Breakout"] = df_4h["Volume"] > (
      df_4h["Vol_SMA"] + df_4h["Vol_Std"]
  )

  # ATR(Wilder 14) + 20봉 평균 — 변동성 수축(진입 필터) 판정용
  prev_close = df_4h["Close"].shift(1)
  true_range = pd.concat([
      df_4h["High"] - df_4h["Low"],
      (df_4h["High"] - prev_close).abs(),
      (df_4h["Low"] - prev_close).abs(),
  ], axis=1).max(axis=1)
  df_4h["ATR"] = true_range.ewm(alpha=1 / 14, adjust=False).mean()
  df_4h["ATR_MA20"] = df_4h["ATR"].rolling(window=20).mean()
  return df_4h


def classify_signal_state(df_4h):
  """전체 기간 스캔 — '하락 후 첫 신호' 카운트/필터링 (원본 로직 유지)

  signal_state: WAITING(하락/대기) → FIRST_FOUND(첫 신호, 거름) → READY_FOR_BUY(매수 가능)
  """
  signal_state = "WAITING"

  for i in range(50, len(df_4h)):
    sub_df = df_4h.iloc[: i + 1]
    latest = sub_df.iloc[-1]

    # 조건 체크
    is_aligned = (
        latest["EMA_10"] > latest["EMA_20"]
    ) and (latest["EMA_20"] > latest["EMA_50"])
    recent_highs = sub_df["High"].iloc[-6:-1].max()
    # 변동성 수축 필터: 직전 봉 ATR이 20봉 평균보다 낮은(수축) 상태에서의 돌파만 인정
    # (analyze_ticker의 최종 판별과 동일한 직전 봉 기준)
    prev_row = df_4h.iloc[i - 1]
    is_contracted = bool(
        pd.notna(prev_row["ATR_MA20"]) and prev_row["ATR"] < prev_row["ATR_MA20"]
    )
    is_breakout = (
        is_aligned
        and bool(latest["Vol_Breakout"])
        and (latest["Close"] > recent_highs)
        and is_contracted
    )

    # 장기 이평선(EMA 50) 아래에 있다면 확실한 하락/조정 구간으로 판단하여 상태 리셋
    if latest["Close"] < latest["EMA_50"]:
      signal_state = "WAITING"

    # 매수 조건 만족 시
    if is_breakout:
      if signal_state == "WAITING":
        # ⚠️ 하락 추세 직후 나타난 '첫 번째 매수 신호' -> 영상 내용대로 과감히 건너뜀!
        signal_state = "FIRST_FOUND"
      elif signal_state == "FIRST_FOUND":
        # 첫 번째 신호를 거른 뒤 나타난 '두 번째 진입 타이밍' -> 실전 매수 인정!
        signal_state = "READY_FOR_BUY"

  return signal_state


def analyze_ticker(ticker, df_4h, state):
  """4시간봉 최종 판별 + 종목별 상태 영속화 (중복 알림 방지)"""
  print(
      f"[{ticker}] 3중 EMA + 변동성 수축 + [첫 번째 신호 필터링] 전략 분석 시작..."
  )

  signal_state = classify_signal_state(df_4h)

  # 최신 4시간봉 최종 판별
  latest = df_4h.iloc[-1]
  current_close = latest["Close"]
  time_str = df_4h.index[-1].strftime("%Y-%m-%d %H:%M %Z")

  is_aligned_latest = (
      latest["EMA_10"] > latest["EMA_20"]
  ) and (latest["EMA_20"] > latest["EMA_50"])
  recent_highs_latest = df_4h["High"].iloc[-6:-1].max()
  # 변동성 수축 필터 (classify_signal_state와 동일 조건 — 직전 봉 기준)
  prev_row = df_4h.iloc[-2]
  is_contracted = bool(
      pd.notna(prev_row["ATR_MA20"]) and prev_row["ATR"] < prev_row["ATR_MA20"]
  )
  is_current_breakout = bool(
      is_aligned_latest
      and latest["Vol_Breakout"]
      and (current_close > recent_highs_latest)
      and is_contracted
  )
  is_sell_signal = current_close < latest["EMA_20"]

  print(f"[{ticker}] 분석 기준 4시간봉: {time_str} | 종가: ${current_close:.2f}")

  prev = state.get("state", "WAITING")

  # 0) 보유 중 → 청산(익절/20EMA) 우선 평가
  #    ⚠️ 돌파(매수) 신호가 TP 목표 도달 봉에서 같이 발생해도 익절이 가로막히지 않도록
  #    (TP 목표 = 진입가 + 3×ATR는 대부분 5봉 고점 돌파를 동반 — 청산을 먼저 본다)
  if prev == "IN_POSITION":
    # 0-1) 익절(TP) 우선: 진입가 + TAKE_PROFIT_ATR×진입 ATR 도달 → 수익 확정 청산
    #      (이익 반납 방지 — 20EMA 이탈까지 버티면 최대 이익의 ~90%를 반납)
    entry_price = state.get("entry_price")
    entry_atr = state.get("entry_atr")
    # 종목별 승수 (기본값보다 우선) — TQQQ=1.5 / SOXL=3.5 (백테스트 최적값)
    tp_atr = TAKE_PROFIT_ATR_BY_TICKER.get(ticker, TAKE_PROFIT_ATR)
    is_tp_signal = (
        entry_price is not None
        and entry_atr is not None
        and TAKE_PROFIT_ATR > 0  # 전역 OFF 스위치: TAKE_PROFIT_ATR=0이면 종목별 설정도 무시
        and tp_atr > 0
        and current_close >= entry_price + tp_atr * entry_atr
    )
    if is_tp_signal:
      msg = (
          f"✅✅✅ **[{ticker}] 4시간봉 스윙 익절(TAKE PROFIT) 시그널 포착!**\n"
          f"- 시간: {time_str}\n"
          f"- 가격: ${current_close:.2f}\n"
          f"- 상태: 진입가 ${entry_price:.2f} + {tp_atr:g}×ATR 목표 도달"
          " — 수익 확정 청산\n"
          f"- 💡 지금 깨어나셔서 매도 주문을 직접 실행해 주세요 (자동 매매 없음)"
      )
      state.update({"state": "WAITING", "last_sell": time_str})
      log_signal(ticker, "SELL", df_4h.index[-1], current_close)
      try:
        send_discord_alert(msg)
      except requests.RequestException as exc:
        print(f"[{ticker}] [오류] Discord 알림 전송 실패: {exc}")
      return

    # 0-2) 20EMA 이탈 — TP 미도달 시의 손절/추세 청산
    if is_sell_signal:
      msg = (
          f"⚠️⚠️⚠️ **[{ticker}] 4시간봉 스윙 매도(SELL) 시그널 포착!**\n"
          f"- 시간: {time_str}\n"
          f"- 가격: ${current_close:.2f}\n"
          f"- 상태: 20 EMA 이탈 (익절 목표 미도달) — 보유 포지션 정리\n"
          f"- 💡 지금 깨어나셔서 매도 주문을 직접 실행해 주세요 (자동 매매 없음)"
      )
      state.update({"state": "WAITING", "last_sell": time_str})
      log_signal(ticker, "SELL", df_4h.index[-1], current_close)
      try:
        send_discord_alert(msg)
      except requests.RequestException as exc:
        print(f"[{ticker}] [오류] Discord 알림 전송 실패: {exc}")
      return

    # 청산 신호 없음 — 보유 유지 (아래 매수 분기는 진입하지 않음)
    print(f"[{ticker}] 보유 중 — 익절/청산 조건 미충족 (HOLD 상태)")
    return

  # 1) 매수 신호 분기 (미보유일 때만 — 보유 중 청산은 0)에서 처리)
  if is_current_breakout:
    if signal_state == "FIRST_FOUND":
      # ⚠️ 하락 후 첫 번째 반등 신호 → 필터링
      if prev != "FIRST_FOUND":
        print(
            f"[{ticker}] [필터링됨] 하락 후 첫 번째 반등 신호입니다."
            " 영상 가이드에 따라 매수를 건너뜁니다."
        )
        state["state"] = "FIRST_FOUND"
      else:
        print(f"[{ticker}] 첫 번째 신호 필터링 상태 유지 중 (매수 스킵)")
    elif signal_state == "READY_FOR_BUY":
      # 두 번째 진입 타이밍 — 정식 매수 알림
      msg = (
          f"🚨🚨🚨 **[{ticker}] 4시간봉 스윙 매수(BUY) 시그널 포착!**\n"
          f"- 시간: {time_str}\n"
          f"- 가격: ${current_close:.2f}\n"
          f"- 상태: 첫 번째 속임수 신호 필터링 완료 후, 정식 돌파 매수 타점 도달\n"
          f"- 💡 지금 깨어나셔서 매수 주문을 직접 실행해 주세요 (자동 매매 없음)"
      )
      # 중복 알림 방지를 위해 상태를 먼저 확정한 뒤 전송 — 전송 실패 시에도
      # 다음 실행에서 같은 신호를 다시 쏘지 않는다 (실패는 로그에 남김)
      # 익절(TP) 기준: 진입가 + 진입 시점 ATR을 상태에 저장 — 이후 익절 판정에 사용
      state.update({
          "state": "IN_POSITION",
          "last_buy": time_str,
          "entry_price": float(current_close),
          "entry_atr": float(latest["ATR"]),
      })
      log_signal(ticker, "BUY", df_4h.index[-1], current_close)
      try:
        send_discord_alert(msg)
      except requests.RequestException as exc:
        print(f"[{ticker}] [오류] Discord 알림 전송 실패: {exc}")
    return

  # 2) 시그널 없음
  print(f"[{ticker}] 현재 조건에 부합하는 새로운 시그널이 없습니다. (HOLD 상태)")

  # 3) 장기 이평선(EMA50) 아래 = 확실한 하락/조정 구간 → 상태 리셋 (미보유 시)
  if current_close < latest["EMA_50"]:
    state["state"] = "WAITING"


def run_swing_strategy():
  # 시작 진단: 키가 실제로 전달됐는지(마스킹) — 403/401 발생 시 원인 파악 1순위 단서
  print(f"[설정] Finnhub API 키 상태: {_mask_api_key(FINNHUB_API_KEY)}")
  if _API_KEY_HAD_WHITESPACE:
    print("[경고] FINNHUB_API_KEY에 앞뒤 공백/줄바꿈이 포함되어 있었습니다 — 자동 제거했습니다.")
    print("        GitHub 시크릿도 깔끔하게 다시 저장하면 영구 해결됩니다.")
  print(f"[설정] 대상 종목: {', '.join(TICKERS)}")
  state = load_state()
  changed = False

  for ticker in TICKERS:
    try:
      df_1h = fetch_finnhub_hourly_data(ticker, FINNHUB_API_KEY)
      if df_1h.empty:
        continue

      df_4h = build_4h_frame(df_1h)
      if df_4h.empty:
        print(
            f"[{ticker}] 4시간봉 데이터 부족({MIN_4H_BARS}개 미만)"
            " — 분석 생략"
        )
        continue

      ticker_state = state.setdefault(ticker, default_ticker_state())
      for key, val in default_ticker_state().items():
        ticker_state.setdefault(key, val)
      before = json.dumps(ticker_state, ensure_ascii=False, sort_keys=True)
      analyze_ticker(ticker, df_4h, ticker_state)
      after = json.dumps(ticker_state, ensure_ascii=False, sort_keys=True)
      if before != after:
        changed = True
    except Exception as exc:
      # 한 종목의 실패가 다른 종목 분석을 막지 않도록 방어
      print(f"[{ticker}] 분석 중 오류 발생: {exc}")

  if changed:
    save_state(state)


if __name__ == "__main__":
  run_swing_strategy()
