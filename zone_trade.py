import os
import json
import requests
import pandas as pd
import yfinance as yf

CONFIG_PATH = os.environ.get("ZONE_CONFIG_PATH", "zone_trade_config.json")


def send_discord_alert(message):
    """
    디스코드 웹훅으로 알림을 전송합니다.
    """
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


def analyze_ticker(ticker, window, buffer_pct):
    """
    단일 종목의 일봉 지지/저항 존을 계산하고 최신 완결 봉 기준 신호를 판단합니다.
    반환값: (plan_text, df) — 데이터 문제로 분석 불가 시 df는 None.
    """
    df = yf.download(ticker, period="60d", interval="1d", progress=False)

    if df.empty:
        return f"[{ticker}] 데이터를 불러오지 못했습니다. 종목 코드를 확인해주세요.", None

    # MultiIndex 컬럼 구조 대응 (yfinance 버전별 구조 차이 방어)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    required_cols = {"High", "Low", "Close"}
    missing = required_cols - set(df.columns)
    if missing:
        return f"[{ticker}] 필수 컬럼 누락({missing}) - 스킵합니다.", None

    if len(df) < window + 1:
        return f"[{ticker}] 데이터가 {len(df)}일뿐이라 window={window} 계산에 부족합니다.", None

    # shift(1)로 "당일 자신의 고가/저가"가 그날의 존 계산에 섞이지 않게 함.
    # Resistance_Zone[i] = max(High[i-window .. i-1])  → i일 자신의 데이터는 미포함.
    df["Resistance_Zone"] = df["High"].shift(1).rolling(window=window).max()
    df["Support_Zone"] = df["Low"].shift(1).rolling(window=window).min()

    i = len(df) - 1

    # [수정된 버그] 원본 코드는 Close.iloc[i-1] 을 Resistance_Zone.iloc[i] 와 비교했음.
    # Resistance_Zone.iloc[i]에는 i-1일의 고가가 이미 포함되어 있어서,
    # "i-1일 종가가 i-1일 자신의 고가로 만든 존 근처인가"를 묻는 순환 참조가 됐었음.
    # 최신 완결 봉(i)의 종가를, 그 이전 window일로 계산된 존(i일 자신은 미포함)과 비교해야 함.
    ref_close = df["Close"].iloc[i]
    res_zone = df["Resistance_Zone"].iloc[i]
    sup_zone = df["Support_Zone"].iloc[i]

    if pd.isna(res_zone) or pd.isna(sup_zone):
        return f"[{ticker}] 존 계산에 필요한 데이터가 부족합니다(NaN).", None

    if ref_close >= res_zone * (1 - buffer_pct):
        plan = (f"[{ticker}] 일봉 저항 존 도달 -> 익일 숏 지정가 예약 주문 검토 "
                f"(저항선: {res_zone:.2f}, 종가: {ref_close:.2f})")
    elif ref_close <= sup_zone * (1 + buffer_pct):
        plan = (f"[{ticker}] 일봉 지지 존 도달 -> 익일 롱 지정가 예약 주문 검토 "
                f"(지지선: {sup_zone:.2f}, 종가: {ref_close:.2f})")
    else:
        plan = (f"[{ticker}] 조건에 부합하는 존 없음 (HOLD) "
                f"- 종가: {ref_close:.2f}, 지지: {sup_zone:.2f}, 저항: {res_zone:.2f}")

    df.loc[df.index[i], "Strategy_Plan"] = plan
    return plan, df


def run_daily_swing_simulation(config_path=CONFIG_PATH):
    config = load_config(config_path)
    zones = config.get("zones", [])
    alert_on_hold = config.get("alert_on_hold", False)

    if not zones:
        print("[경고] 설정 파일에 종목이 없습니다.")
        return {}

    results = {}
    action_messages = []
    hold_messages = []

    for entry in zones:
        ticker = entry["ticker"]
        window = int(entry.get("window", config.get("default_window", 10)))
        buffer_pct = float(entry.get("buffer_pct", config.get("default_buffer_pct", 0.01)))

        print(f"[{ticker}] 야후 파이낸스에서 실제 일봉 데이터를 불러오는 중... (window={window})")
        plan, df = analyze_ticker(ticker, window, buffer_pct)
        results[ticker] = df

        if df is None:
            action_messages.append(plan)  # 에러/데이터 부족은 항상 전달
        elif "HOLD" in plan:
            hold_messages.append(plan)
        else:
            action_messages.append(plan)

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
    print("멀티 종목 일봉 스윙 존 전략 시뮬레이션 및 디스코드 알림 봇 구동 시작")
    print("=" * 50)

    result_map = run_daily_swing_simulation()

    ok = sum(1 for df in result_map.values() if df is not None)
    print(f"분석 완료: {ok}/{len(result_map)} 종목 성공, 디스코드 알림 발송 완료.")
    print("=" * 50)