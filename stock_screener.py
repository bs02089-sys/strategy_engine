# -*- coding: utf-8 -*-
"""
S&P 500 주식 스크리너 (Streamlit)
- 조건: PER <= 20, 현재 거래량 >= 20일 평균 거래량의 2배, RSI(14) >= 50
- 매수 전 참고: 섹터 평균 PER 비교, 최근 뉴스 헤드라인
- 실행: streamlit run stock_screener.py

필요 라이브러리:
    uv pip install streamlit yfinance pandas numpy requests
"""

import io
import time
import concurrent.futures as cf
from collections import Counter
from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd
import requests
import streamlit as st
import yfinance as yf

st.set_page_config(page_title="US Stock Screener", layout="wide")

# 테스트 결과를 바탕으로 고정한 값 (필요 시 여기서만 수정)
FIXED_MAX_WORKERS = 7
FIXED_MAX_RETRIES = 0
FIXED_REQUEST_DELAY = 0.0


# ----------------------------------------------------------------------------
# 1) S&P 500 티커 목록 가져오기 (위키피디아, 1일 캐시)
# ----------------------------------------------------------------------------
@st.cache_data(ttl=60 * 60 * 24)
def get_sp500_tickers() -> list[str]:
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    headers = {"User-Agent": "Mozilla/5.0"}
    resp = requests.get(url, headers=headers, timeout=10)
    resp.raise_for_status()
    tables = pd.read_html(io.StringIO(resp.text))
    df = tables[0]
    tickers = df["Symbol"].astype(str).str.replace(".", "-", regex=False).tolist()
    return sorted(tickers)


# ----------------------------------------------------------------------------
# 2) RSI 계산 (Wilder's smoothing)
# ----------------------------------------------------------------------------
def compute_rsi(close: pd.Series, period: int = 14) -> float:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.fillna(100)  # avg_loss가 0이면 RSI=100
    return float(rsi.iloc[-1])


# ----------------------------------------------------------------------------
# 3) 종목 하나에 대한 지표 계산
# ----------------------------------------------------------------------------
@dataclass
class ScreenResult:
    ticker: str
    price: float
    pe_ratio: float
    volume: int
    avg_volume_20d: float
    volume_ratio: float
    rsi: float
    sector: str


def _fetch_once(ticker: str) -> tuple[ScreenResult | None, str]:
    """한 번의 시도로 데이터를 가져온다. (결과, 실패사유) 튜플을 반환. 성공 시 사유는 '성공'."""
    tk = yf.Ticker(ticker)
    hist = tk.history(period="3mo", interval="1d", auto_adjust=True)
    if hist.empty or len(hist) < 21:
        return None, "가격 데이터 부족"

    info = tk.get_info()
    pe_ratio = info.get("trailingPE") or info.get("forwardPE")
    if pe_ratio is None or pe_ratio <= 0:
        return None, "PER 정보 없음"

    current_volume = hist["Volume"].iloc[-1]
    avg_volume_20d = hist["Volume"].iloc[-21:-1].mean()  # 당일 제외 20일 평균
    if avg_volume_20d <= 0:
        return None, "거래량 데이터 부족"
    volume_ratio = current_volume / avg_volume_20d

    rsi = compute_rsi(hist["Close"])
    price = hist["Close"].iloc[-1]
    sector = info.get("sector") or "Unknown"

    result = ScreenResult(
        ticker=ticker,
        price=round(float(price), 2),
        pe_ratio=round(float(pe_ratio), 2),
        volume=int(current_volume),
        avg_volume_20d=round(float(avg_volume_20d), 0),
        volume_ratio=round(float(volume_ratio), 2),
        rsi=round(rsi, 1),
        sector=sector,
    )
    return result, "성공"


def analyze_ticker(
    ticker: str, max_retries: int = 1, retry_delay: float = 1.0, request_delay: float = 0.0
) -> tuple[ScreenResult | None, str]:
    """
    지정한 종목의 지표를 계산한다.
    - max_retries: 네트워크/기타 오류 발생 시 추가로 재시도할 횟수 (데이터 부족 사유는 재시도해도 소용없으므로 재시도하지 않음)
    - retry_delay: 재시도 사이 대기 시간(초), 재시도 횟수에 비례해 늘어남 (백오프)
    - request_delay: 매 요청 전 대기 시간(초) - 동시 요청 부담을 줄이기 위한 완만한 스로틀링
    """
    if request_delay > 0:
        time.sleep(request_delay)

    last_reason = "네트워크/기타 오류"
    for attempt in range(max_retries + 1):
        try:
            result, reason = _fetch_once(ticker)
            return result, reason  # 성공이든 '데이터 부족류' 실패든 재시도 불필요 - 바로 반환
        except Exception:
            last_reason = "네트워크/기타 오류"
            if attempt < max_retries:
                time.sleep(retry_delay * (attempt + 1))
                continue
    return None, last_reason


# ----------------------------------------------------------------------------
# 3-1) 최근 뉴스 헤드라인 조회 (최종 결과 종목에만 사용 - 개수가 적어 빠름)
# ----------------------------------------------------------------------------
def _extract_related_tickers(item: dict) -> list[str]:
    """뉴스 항목에서 관련 티커 목록을 최대한 뽑아낸다. yfinance 버전에 따라 필드 위치가 다를 수 있다."""
    content = item.get("content", item)
    for key in ("relatedTickers", "tickers", "symbols"):
        candidates = item.get(key) or content.get(key)
        if candidates:
            # 문자열 리스트이거나, {"symbol": "..."} 형태의 dict 리스트인 경우 모두 처리
            result = []
            for c in candidates:
                if isinstance(c, str):
                    result.append(c)
                elif isinstance(c, dict) and c.get("symbol"):
                    result.append(c["symbol"])
            if result:
                return result
    return []


def get_latest_headline(ticker: str) -> str:
    try:
        news = yf.Ticker(ticker).news
        if not news:
            return "관련 뉴스 없음"

        for item in news:
            related = _extract_related_tickers(item)
            # 관련 티커 정보가 아예 없는 경우엔 판단할 수 없으므로 보수적으로 건너뜀
            if related and ticker.upper() in [r.upper() for r in related]:
                content = item.get("content", item)
                title = content.get("title") or item.get("title") or "제목 없음"
                return title

        return "관련 뉴스 없음 (제공된 기사가 해당 종목과 직접 관련 없음)"
    except Exception:
        return "뉴스 조회 실패"


# ----------------------------------------------------------------------------
# 4) 전체 스크리닝 실행 (병렬 처리 + 진행률 표시)
# ----------------------------------------------------------------------------
def run_screen(
    tickers: list[str],
    max_workers: int = 10,
    max_retries: int = 1,
    retry_delay: float = 1.0,
    request_delay: float = 0.0,
) -> tuple[pd.DataFrame, int, Counter]:
    results: list[ScreenResult] = []
    reason_counts: Counter = Counter()
    progress = st.progress(0.0, text="스크리닝 시작...")
    done = 0
    total = len(tickers)

    with cf.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(analyze_ticker, t, max_retries, retry_delay, request_delay): t for t in tickers
        }
        for future in cf.as_completed(futures):
            t = futures[future]
            r, reason = future.result()
            reason_counts[reason] += 1
            if r is not None:
                results.append(r)
            done += 1
            progress.progress(done / total, text=f"스크리닝 중... ({done}/{total}) {t}")

    progress.empty()
    failed = sum(v for k, v in reason_counts.items() if k != "성공")
    if not results:
        return pd.DataFrame(), failed, reason_counts
    return pd.DataFrame([asdict(r) for r in results]), failed, reason_counts


# ----------------------------------------------------------------------------
# 5) Streamlit UI
# ----------------------------------------------------------------------------
def main():
    st.title("🇺🇸 미국 주식 스크리너 (S&P 500)")
    st.caption("조건: PER ≤ 20  |  거래량 ≥ 20일 평균의 2배  |  RSI(14) ≥ 50")

    with st.sidebar:
        st.header("필터 설정")
        pe_max = st.number_input("PER 상한", min_value=0.0, value=20.0, step=1.0)
        volume_multiplier = st.number_input("거래량 배수 (20일 평균 대비)", min_value=1.0, value=2.0, step=0.1)
        rsi_min = st.number_input("RSI 하한", min_value=0.0, max_value=100.0, value=50.0, step=1.0)
        sample_size = st.slider(
            "스캔할 종목 수 (테스트용, 0=전체 500개)", min_value=0, max_value=500, value=50, step=10
        )
        top_n = st.number_input(
            "최종 결과 개수 (거래량 배수 상위, 0=전체 표시)", min_value=0, max_value=100, value=3, step=1
        )
        run_button = st.button("스크리닝 실행", type="primary")

    if run_button:
        tickers = get_sp500_tickers()
        if sample_size and sample_size > 0:
            tickers = tickers[:sample_size]

        st.write(f"대상 종목 수: **{len(tickers)}**")
        start = time.time()
        df, failed, reason_counts = run_screen(
            tickers,
            max_workers=FIXED_MAX_WORKERS,
            max_retries=FIXED_MAX_RETRIES,
            retry_delay=1.0,
            request_delay=FIXED_REQUEST_DELAY,
        )
        elapsed = time.time() - start

        if not df.empty:
            success_rate = len(df) / len(tickers) * 100
            st.caption(
                f"⚙️ 요청 결과: 성공 {len(df)}개 / 실패(제외) {failed}개  "
                f"(성공률 {success_rate:.0f}%, 동시 요청 수 {FIXED_MAX_WORKERS}, "
                f"재시도 {FIXED_MAX_RETRIES}회, 지연시간 {FIXED_REQUEST_DELAY}초 — 테스트용 고정값)"
            )
        if failed > 0:
            reason_lines = [f"- {reason}: {count}개" for reason, count in reason_counts.items() if reason != "성공"]
            with st.expander(f"실패 사유 세부 내역 ({failed}개)"):
                st.markdown("\n".join(reason_lines))
                st.caption(
                    "'가격 데이터 부족'/'PER 정보 없음'/'거래량 데이터 부족'은 재시도해도 해결되지 않는 "
                    "정상적인 필터링입니다. '네트워크/기타 오류'만 재시도·지연시간 조정으로 줄일 수 있습니다."
                )

        if df.empty:
            st.warning("데이터를 가져오지 못했습니다. 잠시 후 다시 시도해보세요.")
            return

        # 섹터 평균 PER (이번에 스캔한 종목 범위 내 평균 - S&P 500 전체 섹터 평균은 아님)
        df["sector_avg_pe"] = df.groupby("sector")["pe_ratio"].transform("mean").round(2)
        df["업종대비저평가"] = df["pe_ratio"] < df["sector_avg_pe"]

        filtered_all = df[
            (df["pe_ratio"] <= pe_max)
            & (df["volume_ratio"] >= volume_multiplier)
            & (df["rsi"] >= rsi_min)
        ].sort_values("volume_ratio", ascending=False)

        filtered = filtered_all.head(top_n).copy() if top_n > 0 else filtered_all.copy()

        st.success(
            f"조건 충족 종목: {len(filtered_all)}개 중 상위 {len(filtered)}개 표시  "
            f"(전체 스캔 {len(df)}개, {elapsed:.1f}초 소요)"
        )

        if not filtered.empty:
            # 최근 뉴스 헤드라인 (최종 결과만 - 개수가 적어 순차 조회해도 빠름)
            with st.spinner("최근 뉴스 조회 중..."):
                filtered["최근뉴스"] = filtered["ticker"].apply(get_latest_headline)

        st.dataframe(
            filtered.rename(
                columns={
                    "ticker": "티커",
                    "price": "현재가",
                    "pe_ratio": "PER",
                    "volume": "거래량",
                    "avg_volume_20d": "20일평균거래량",
                    "volume_ratio": "거래량배수",
                    "rsi": "RSI",
                    "sector": "섹터",
                    "sector_avg_pe": "섹터평균PER",
                }
            ),
            use_container_width=True,
            hide_index=True,
        )
        st.caption(
            "⚠️ '섹터평균PER'은 이번 스캔 범위 내 동일 섹터 종목들의 평균이며, "
            "S&P 500 전체 섹터 평균과는 다를 수 있습니다."
        )

        with st.expander("전체 스캔 결과 보기"):
            st.dataframe(df, use_container_width=True, hide_index=True)

        csv = filtered.to_csv(index=False).encode("utf-8-sig")
        st.download_button("CSV로 다운로드", data=csv, file_name="screened_stocks.csv", mime="text/csv")
    else:
        st.info("왼쪽 사이드바에서 조건을 설정하고 '스크리닝 실행' 버튼을 눌러주세요.")


if __name__ == "__main__":
    main()