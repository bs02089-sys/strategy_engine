"""SOXL / TQQQ 최근 1년 롤링 2σ 매수 신호 횟수 집계.

- 다운로드 기간: 실행 시점(PC 기준) 기준 최근 1년 — `LOOKBACK_DAYS` 상수.
- 신호 정의: 당일 수익률 < −(20일 롤링 수익률 표준편차 × 2.0)
  → `soxl_2sigma_alert.py` 와 같은 지표(설정도 `soxl_tqqq_config.json` 공유).
- 롤링 창이 채워지지 않는 첫 ~20거래일은 σ 가 없어 집계에서 빠진다(집계 구간을 함께 출력).

ponytail: 조회·집계만 하는 단발 도구 — 알림/워크플로우에 연결하지 않는다.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

LOOKBACK_DAYS = 365  # 달력일 기준 (실행 시점 = PC 접속 시각)


def load_config() -> dict:
    config_path = Path(__file__).parent / "soxl_tqqq_config.json"
    if not config_path.exists():
        return {}
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def count_signals(ticker: str, window: int, multiplier: float) -> dict:
    end = datetime.now()
    start = end - timedelta(days=LOOKBACK_DAYS)
    df = yf.download(
        ticker,
        start=start.strftime("%Y-%m-%d"),
        end=end.strftime("%Y-%m-%d"),
        progress=False,
        multi_level_index=False,  # 단일 종목 → df["Close"] 가 항상 Series (컬럼 구조 변동 차단)
    )
    if df is None or df.empty:
        raise RuntimeError(f"[{ticker}] 데이터를 가져오지 못했습니다.")

    close = df["Close"].dropna()
    ret = close.pct_change()
    sigma = ret.rolling(window).std()

    # 기준 A: 알림 스크립트와 동일(당일 수익률이 당일 σ 계산에 포함됨)
    # 기준 B: 전일까지의 σ 만 사용(룩어헤드 없음 — 실제 주문 시점에 알 수 있는 값)
    variants = {
        "당일 σ 포함 (알림 지표)": sigma,
        "전일까지 σ (룩어헤드 없음)": sigma.shift(1),
    }
    signals = {}
    for label, sig in variants.items():
        hit = (ret < -sig * multiplier) & sig.notna()
        # ponytail: index[bool Series] 대신 items() 순회 — 부울 인덱싱은 타입 스텁에서 오류로 뜬다
        signals[label] = [d.strftime("%Y-%m-%d") for d, h in hit.items() if h]

    covered = sigma.dropna()  # 롤링 창이 채워진 구간 (앞 ~20거래일 제외)
    if covered.empty:
        raise RuntimeError(f"[{ticker}] 롤링 {window}일 σ 계산에 필요한 데이터가 부족합니다.")
    return {
        "ticker": ticker,
        "rows": len(close),
        # ponytail: .strftime() 대신 f-string 포맷 — Index.min()/max() 는 스텁상 넓은 유니온이라
        # 속성 호출이 편집기에서 오류로 뜨고, 포맷 지정은 어떤 타입에도 통과한다
        "covered": (f"{covered.index.min():%Y-%m-%d}", f"{close.index.max():%Y-%m-%d}"),
        "covered_days": len(covered),
        "avg_sigma_pct": float(covered.mean() * 100),
        "signals": signals,
    }


def main() -> None:
    config = load_config()
    tickers = config.get("tickers") or [config["ticker"]]
    window = config.get("rolling_window", 20)
    multiplier = config.get("sigma_multiplier", 2.0)

    print(f"다운로드 기간: 최근 {LOOKBACK_DAYS}일 (실행 시각 {datetime.now():%Y-%m-%d %H:%M:%S})")
    print(f"지표: {window}일 롤링 수익률 σ × {multiplier}\n")

    for ticker in tickers:
        try:
            r = count_signals(ticker, window, multiplier)
        except RuntimeError as e:
            print(f"❌ {e}\n")
            continue

        print("=" * 60)
        print(f"[{r['ticker']}] {r['rows']}거래일 수신 · 집계 구간 {r['covered'][0]} ~ {r['covered'][1]}")
        print(f"평균 {window}일 롤링 σ: {r['avg_sigma_pct']:.2f}% "
              f"(매수 임계 = -{r['avg_sigma_pct'] * multiplier:.2f}%, σ는 매일 변동)")
        for label, dates in r["signals"].items():
            rate = len(dates) / r["covered_days"] * 100 if r["covered_days"] else 0.0
            print(f"  · {label}: {len(dates)}회 / {r['covered_days']}거래일 ({rate:.1f}%)")
            for d in dates:
                print(f"      - {d}")
        print("=" * 60 + "\n")


if __name__ == "__main__":
    main()