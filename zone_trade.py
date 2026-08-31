"""
거래량 기반 지지/저항 매매법 (영상: "지지저항 하나로 월 1억 벌게 해준다는 트레이더의 매매법")

영상이 설명한 6가지 핵심 요소를 그대로 구현한다:
  1) 존(zone) 산정 기준 = 단순 고가/저가가 아니라 "거래량이 집중된 구간"
  2) 신호 발생 = 존 도달 + 반전 확인(압력 소화 후 반대 압력 발생) 캔들이 나올 때만
  3) 존 신선도 = 같은 존을 여러 번 테스트할수록 신뢰도 하락, 처음 1~2회만 유효
  4) 플립(flip) = 저항이 뚫리면 그 구간이 새로운 지지가 되고, 지지가 뚫리면 저항이 됨
  5) 손절/익절 = 손절가는 존 경계, 익절가는 손절폭의 1.5배 (R:R = 1:1.5)
  6) 규율 = 목표가 도달 시 추가 보유 없이 정직하게 익절 (알림 문구로만 안내, 매매 자체는 사람이 집행)

[중요한 한계]
- 이 스크립트는 일봉(OHLCV) 데이터만 사용한다. 영상 속 지표는 실시간 체결/틱 데이터 기반
  거래량 프로파일일 가능성이 높은데, 여기서는 일봉의 고가~저가 구간에 그날 거래량을
  균등 분포시켜 근사한 "거래량 프로파일"을 만든다. 실제 지표와 완전히 동일하지는 않다.
- "승률 99%"는 검증되지 않은 마케팅성 수치일 가능성이 높다. 이 구현이 그 수치를
  재현한다는 보장은 전혀 없다.
"""

import os
import json
import requests
import numpy as np
import pandas as pd
import yfinance as yf

CONFIG_PATH = os.environ.get("ZONE_CONFIG_PATH", "zone_trade_config.json")


# ----------------------------------------------------------------------
# 디스코드 알림
# ----------------------------------------------------------------------
def send_discord_alert(message):
    webhook_url = os.environ.get("DISCORD_WEBHOOK")
    if not webhook_url:
        print(f"[알림 미발송 - 웹훅 URL 없음]\n{message}")
        return
    try:
        response = requests.post(webhook_url, json={"content": message})
        if response.status_code != 204:
            print(f"디스코드 전송 실패: 상태 코드 {response.status_code}")
    except Exception as e:
        print(f"디스코드 전송 중 에러 발생: {e}")


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ----------------------------------------------------------------------
# [영상 요소 1] 거래량 집중 구간(존) 산출 — 거래량 프로파일 근사
# ----------------------------------------------------------------------
def build_volume_profile(df_hist, num_bins):
    """
    df_hist: 오늘을 제외한 과거 구간 (룩어헤드 방지)
    각 날의 [Low, High] 구간에 그날 Volume을 균등 분포시켜 가격대별 누적 거래량을 만든다.
    """
    price_min = df_hist["Low"].min()
    price_max = df_hist["High"].max()

    if not np.isfinite(price_min) or not np.isfinite(price_max) or price_max <= price_min:
        return None, None

    edges = np.linspace(price_min, price_max, num_bins + 1)
    bin_volume = np.zeros(num_bins)

    lows = df_hist["Low"].to_numpy()
    highs = df_hist["High"].to_numpy()
    vols = df_hist["Volume"].to_numpy()

    for low, high, vol in zip(lows, highs, vols):
        day_range = high - low
        if day_range <= 0 or vol <= 0:
            continue
        # 이 날의 구간과 겹치는 모든 bin에 거래량을 겹치는 비율만큼 분배
        overlap_lo = np.maximum(edges[:-1], low)
        overlap_hi = np.minimum(edges[1:], high)
        overlap = np.clip(overlap_hi - overlap_lo, 0, None)
        bin_volume += vol * (overlap / day_range)

    return edges, bin_volume


def extract_zones(edges, bin_volume, volume_percentile):
    """
    거래량이 상위 percentile 이상인 bin들을 찾아 인접한 bin끼리 하나의 존으로 병합한다.
    반환: [{'low': float, 'high': float, 'volume': float}, ...]
    """
    if edges is None:
        return []

    threshold = np.percentile(bin_volume, volume_percentile)
    is_hvn = bin_volume >= threshold

    zones = []
    start = None
    for i, flag in enumerate(is_hvn):
        if flag and start is None:
            start = i
        elif not flag and start is not None:
            zones.append({
                "low": edges[start],
                "high": edges[i],
                "volume": float(bin_volume[start:i].sum()),
            })
            start = None
    if start is not None:
        zones.append({
            "low": edges[start],
            "high": edges[len(is_hvn)],
            "volume": float(bin_volume[start:].sum()),
        })
    return zones


def sorted_zone_candidates(zones, ref_price):
    """
    [영상 요소 4] 플립(flip)은 별도 상태 저장 없이, 매일 기준가(ref_price) 대비
    존의 위치를 다시 분류하는 것만으로 자연스럽게 구현된다.
    - 예전엔 저항이었던 존이라도, 가격이 그 존을 뚫고 올라가면 다음 날부턴
      ref_price보다 아래에 있으므로 자동으로 '지지 후보'로 재분류된다.

    ref_price에 가까운 순서로 정렬한 (저항 후보 목록, 지지 후보 목록)을 반환한다.
    급락/급등으로 하루 만에 여러 존을 통과하는 경우를 감안해, 가장 가까운 후보부터
    순서대로 검증할 수 있도록 리스트 전체를 넘긴다 (단일 최근접 존만 보면
    "존을 그냥 뚫고 지나간 것"과 "존에서 반등한 것"을 구분하지 못하는 문제가 있었음).
    """
    resistance_candidates = sorted([z for z in zones if z["low"] > ref_price], key=lambda z: z["low"])
    support_candidates = sorted([z for z in zones if z["high"] < ref_price], key=lambda z: -z["high"])
    return resistance_candidates, support_candidates


def pick_nearest_zones(zones, ref_price):
    """HOLD 메시지 등 단순 정보 표시용 — 가장 가까운 저항/지지 존 하나씩만 반환."""
    res_list, sup_list = sorted_zone_candidates(zones, ref_price)
    resistance_zone = res_list[0] if res_list else None
    support_zone = sup_list[0] if sup_list else None
    return resistance_zone, support_zone


# ----------------------------------------------------------------------
# [영상 요소 3] 존 신선도 — 반복 테스트된 존은 신뢰도 하락
# ----------------------------------------------------------------------
def count_touches(df_hist, zone):
    """
    df_hist(오늘 제외) 구간에서 존을 "몇 번 테스트했는지" 센다.
    단순히 겹친 날짜 수를 세면, 추세 중에 존을 며칠에 걸쳐 통과만 한 경우도
    여러 번 테스트한 것처럼 과다 계산된다. 영상 취지("같은 선을 여러 번 테스트하면
    힘이 빠진다")는 개별 접근-이탈 이벤트를 말하는 것이므로, 연속으로 겹친 날들은
    하나의 터치로 묶어서(연속 구간의 개수만) 센다.
    """
    lows = df_hist["Low"].to_numpy()
    highs = df_hist["High"].to_numpy()
    overlap = (highs >= zone["low"]) & (lows <= zone["high"])

    touches = 0
    prev = False
    for flag in overlap:
        if flag and not prev:
            touches += 1
        prev = flag
    return touches


# ----------------------------------------------------------------------
# [영상 요소 2] 반전 확인 캔들 — 압력이 소화된 뒤 반대 압력이 나타났는가
# ----------------------------------------------------------------------
def check_confirmation(today_row, avg_volume, direction, wick_ratio, volume_multiplier):
    """
    direction: 'long' (지지에서 반등 확인) 또는 'short' (저항에서 반락 확인)
    - 몸통(body) 대비 반대쪽 꼬리가 wick_ratio배 이상 길고
    - 방향에 맞는 양봉/음봉이며
    - 거래량이 최근 평균의 volume_multiplier배 이상이어야 '압력 소화 후 반전'으로 인정
    """
    o, h, l, c, v = (today_row["Open"], today_row["High"],
                     today_row["Low"], today_row["Close"], today_row["Volume"])

    body = abs(c - o)
    if body == 0:
        body = (h - l) * 0.01 or 1e-9  # 도지 캔들 방어용 최소 몸통

    lower_wick = min(o, c) - l
    upper_wick = h - max(o, c)

    volume_ok = avg_volume > 0 and v >= avg_volume * volume_multiplier

    if direction == "long":
        return (c > o) and (lower_wick >= body * wick_ratio) and volume_ok
    elif direction == "short":
        return (c < o) and (upper_wick >= body * wick_ratio) and volume_ok
    return False


# ----------------------------------------------------------------------
# [영상 요소 5] 손절/익절 — 손절은 존 경계, 익절은 손절폭의 1.5배
# ----------------------------------------------------------------------
def compute_sl_tp(direction, zone, entry_price, rr_ratio):
    if direction == "long":
        sl = zone["low"]
        risk = entry_price - sl
        tp = entry_price + risk * rr_ratio
    else:
        sl = zone["high"]
        risk = sl - entry_price
        tp = entry_price - risk * rr_ratio
    return sl, tp


# ----------------------------------------------------------------------
# 종목 하나에 대한 전체 분석 파이프라인
# ----------------------------------------------------------------------
def analyze_ticker(ticker, cfg):
    period = cfg.get("period", "180d")
    num_bins = int(cfg.get("num_bins", 24))
    volume_percentile = float(cfg.get("volume_percentile", 80))
    max_touches = int(cfg.get("max_touches", 2))
    wick_ratio = float(cfg.get("wick_ratio", 1.5))
    volume_multiplier = float(cfg.get("volume_multiplier", 1.2))
    rr_ratio = float(cfg.get("rr_ratio", 1.5))
    vol_avg_window = int(cfg.get("vol_avg_window", 20))

    df = yf.download(ticker, period=period, interval="1d", progress=False)
    if df.empty:
        return {"ticker": ticker, "signal": "ERROR",
                "message": f"[{ticker}] 데이터를 불러오지 못했습니다. 종목 코드를 확인해주세요."}

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    required = {"Open", "High", "Low", "Close", "Volume"}
    missing = required - set(df.columns)
    if missing:
        return {"ticker": ticker, "signal": "ERROR",
                "message": f"[{ticker}] 필수 컬럼 누락({missing}) - 스킵합니다."}

    # yfinance는 미국 장이 아직 열리지 않았거나 당일 데이터가 아직 반영 안 됐을 때
    # 마지막 행에 NaN이 섞인 "미완결 봉"을 끼워 넣는 경우가 있다.
    # 그 상태로 분석하면 오늘자 종가가 NaN이 되어버리므로, 완전히 마감된
    # 마지막 거래일만 남도록 결측 행을 제거한다.
    before_drop = len(df)
    df = df.dropna(subset=list(required))
    if len(df) < before_drop:
        print(f"[{ticker}] 미완결/결측 봉 {before_drop - len(df)}개 제거 (당일 미마감 데이터 방어)")

    if df.empty:
        return {"ticker": ticker, "signal": "ERROR",
                "message": f"[{ticker}] 유효한(완결된) 데이터가 없습니다."}

    min_len = max(num_bins, vol_avg_window) + 5
    if len(df) < min_len:
        return {"ticker": ticker, "signal": "ERROR",
                "message": f"[{ticker}] 데이터가 {len(df)}일뿐이라 분석에 부족합니다(최소 {min_len}일 필요)."}

    i = len(df) - 1
    df_hist = df.iloc[:i]              # 오늘을 제외한 과거 구간 (룩어헤드 방지)
    today_row = df.iloc[i]
    ref_price = df["Close"].iloc[i - 1]  # 어제 종가 기준으로 존을 저항/지지로 분류

    # 1) 거래량 프로파일 → 존 추출, ref_price 기준 가까운 순서로 후보 정렬
    edges, bin_volume = build_volume_profile(df_hist, num_bins)
    zones = extract_zones(edges, bin_volume, volume_percentile)
    resistance_candidates, support_candidates = sorted_zone_candidates(zones, ref_price)
    resistance_zone = resistance_candidates[0] if resistance_candidates else None
    support_zone = support_candidates[0] if support_candidates else None

    avg_volume = df_hist["Volume"].tail(vol_avg_window).mean()
    entry_price = today_row["Close"]

    # 2) 가까운 존부터 순서대로: 실제로 닿았는지 + 종가가 존 안/근처로 되돌아왔는지
    #    (그냥 뚫고 지나간 것과 존에서 반등/반락한 것을 구분하기 위한 조건)
    #    + 반전 확인 캔들까지 모두 만족해야 신호로 인정한다.
    signal = "HOLD"
    detail = None

    for zone in resistance_candidates:
        touched = today_row["High"] >= zone["low"]
        reverted = entry_price <= zone["high"]  # 종가가 저항을 완전히 뚫고 위에 머물지 않았는지
        touches_before = count_touches(df_hist, zone)
        if touched and reverted and touches_before < max_touches:
            if check_confirmation(today_row, avg_volume, "short", wick_ratio, volume_multiplier):
                sl, tp = compute_sl_tp("short", zone, entry_price, rr_ratio)
                signal = "SHORT"
                detail = {"zone": zone, "touches": touches_before,
                          "entry": entry_price, "sl": sl, "tp": tp}
                break

    if signal == "HOLD":
        for zone in support_candidates:
            touched = today_row["Low"] <= zone["high"]
            reverted = entry_price >= zone["low"]  # 종가가 지지를 완전히 뚫고 아래에 머물지 않았는지
            touches_before = count_touches(df_hist, zone)
            if touched and reverted and touches_before < max_touches:
                if check_confirmation(today_row, avg_volume, "long", wick_ratio, volume_multiplier):
                    sl, tp = compute_sl_tp("long", zone, entry_price, rr_ratio)
                    signal = "LONG"
                    detail = {"zone": zone, "touches": touches_before,
                              "entry": entry_price, "sl": sl, "tp": tp}
                    break

    if signal == "SHORT":
        z, d = detail["zone"], detail
        message = (f"[{ticker}] 저항 존({z['low']:.2f}~{z['high']:.2f}) 반전 확인 -> 숏 검토 "
                   f"(진입: {d['entry']:.2f}, 손절: {d['sl']:.2f}, 목표: {d['tp']:.2f}, "
                   f"기존 터치: {d['touches']}회)")
    elif signal == "LONG":
        z, d = detail["zone"], detail
        message = (f"[{ticker}] 지지 존({z['low']:.2f}~{z['high']:.2f}) 반전 확인 -> 롱 검토 "
                   f"(진입: {d['entry']:.2f}, 손절: {d['sl']:.2f}, 목표: {d['tp']:.2f}, "
                   f"기존 터치: {d['touches']}회)")
    else:
        res_str = f"{resistance_zone['low']:.2f}~{resistance_zone['high']:.2f}" if resistance_zone else "없음"
        sup_str = f"{support_zone['low']:.2f}~{support_zone['high']:.2f}" if support_zone else "없음"
        message = (f"[{ticker}] HOLD - 종가: {entry_price:.2f}, "
                   f"저항존: {res_str}, 지지존: {sup_str}")

    return {"ticker": ticker, "signal": signal, "message": message, "detail": detail}


def run_daily_swing_simulation(config_path=CONFIG_PATH):
    config = load_config(config_path)
    tickers_cfg = config.get("tickers", [])
    alert_on_hold = config.get("alert_on_hold", False)

    if not tickers_cfg:
        print("[경고] 설정 파일에 종목이 없습니다.")
        return {}

    results = {}
    action_messages = []
    hold_messages = []

    for entry in tickers_cfg:
        ticker = entry["ticker"]
        merged_cfg = {**config.get("defaults", {}), **entry}
        print(f"[{ticker}] 분석 중...")
        result = analyze_ticker(ticker, merged_cfg)
        results[ticker] = result

        if result["signal"] in ("LONG", "SHORT", "ERROR"):
            action_messages.append(result["message"])
        else:
            hold_messages.append(result["message"])

    messages_to_send = list(action_messages)
    if alert_on_hold and hold_messages:
        messages_to_send.extend(hold_messages)
    elif hold_messages:
        hold_tickers = ", ".join(m.split("]")[0].lstrip("[") for m in hold_messages)
        messages_to_send.append(f"(HOLD {len(hold_messages)}종목: {hold_tickers})")

    if messages_to_send:
        send_discord_alert("\n".join(messages_to_send))

    return results


if __name__ == "__main__":
    print("=" * 50)
    print("거래량 기반 지지/저항 매매법 - 멀티 종목 분석 시작")
    print("=" * 50)

    result_map = run_daily_swing_simulation()

    for ticker, r in result_map.items():
        print(f"  {r['message']}")

    print("=" * 50)
    print("분석 완료, 디스코드 알림 발송 완료.")
    print("=" * 50)