import os
import json
import logging
import shutil
import tempfile
import requests
import pandas as pd
import yfinance as yf
from datetime import datetime
from typing import Optional, Dict

from LOC_DCA_strategy import load_portfolio, resolve_discord_config

# ====================== 설정 ======================
STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "market_state.json")

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')

STAGE5_RESET_DAYS = 30

# 하단 래더 1단계 진입 조건 + 무효화 (2026-09-29 재설계 — 근거는 MarketBottomTracker 참고)
BOTTOM_DRAWDOWN_PCT = 0.10      # 6개월 고점 대비 낙폭
BOTTOM_RSI_MAX = 35             # RSI(14) 과매도
BOTTOM_EXPIRY_STAGES = (1, 2)   # 3단계는 제외 — 만료시키면 4단계(20일 고점 돌파)를 못 기다린다
BOTTOM_EXPIRY_BARS = 30

# 상단 래더 무효화 (2026-09-29 — 근거는 MarketTopTracker 참고)
TOP_EXPIRY_STAGES = (4,)
TOP_EXPIRY_BARS = 120           # 4단계(분산) 체류 상한 — 평균 8~15개월·최장 2.4년을 약 6개월로 제한
# ⚠️ 데이터 창이 6개월(약 126봉)이라 만료값이 126 이상이면 아무 일도 하지 않는다.


# ====================== 기술적 지표 ======================
def calculate_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss_clean = avg_loss.replace(0, float('nan'))
    rs = avg_gain / avg_loss_clean
    result: pd.Series = 100 - (100 / (1 + rs))  # type: ignore[assignment]
    return result


def calculate_macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line, signal_line


def calculate_bollinger_upper(close: pd.Series, period: int = 20, num_std: float = 2.0) -> pd.Series:
    mid = close.rolling(period).mean()
    std = close.rolling(period).std()
    return mid + num_std * std


# ====================== 트래커 베이스 ======================
class MarketStageTracker:
    MIN_ROWS = 80

    def __init__(self, stage: int = 0, stage5_entered_date: Optional[str] = None, stage_entered_date: Optional[str] = None):
        self.stage = stage
        self.stage5_entered_date = stage5_entered_date
        # 현재 단계(1~5) 진입일 — 리포트에 '언제부터 이 단계인지'를 표시하기 위한 값.
        # 단계는 되돌아가지 않으므로 몇 주 전 이벤트인지 날짜 없이는 알 수 없어 오해를 샀다 (2026-09-29).
        # ponytail: 5단계에선 stage5_entered_date 와 같은 값이지만, 그쪽은 리셋 판정 전용이라 건드리지 않는다.
        self.stage_entered_date = stage_entered_date

    def _expire_if_stale(self, df: pd.DataFrame, stages: tuple, max_bars: int, label: str) -> bool:
        """지정 단계가 max_bars 봉 동안 다음 단계로 못 가면 0으로 리셋 (2026-09-29).

        단계가 올라갈 때마다 기산일이 갱신되므로, "현재 단계에서 N봉 무진전"이 만료 조건이다.
        기산일은 이미 저장되는 stage_entered_date 를 그대로 쓴다(별도 필드 불필요, 멱등 —
        같은 날 여러 번 실행해도 같은 봉 수가 나온다).
        """
        if self.stage not in stages:
            return False
        if self.stage_entered_date is None:
            self.stage_entered_date = self._get_last_date(df)   # 날짜 없는 구버전 상태 → 지금부터 기산
            return False
        idx = pd.to_datetime(df.index, errors="coerce")
        elapsed = int((idx > pd.Timestamp(self.stage_entered_date)).sum())
        if elapsed >= max_bars:
            self.stage = 0
            self.stage_entered_date = None
            self.stage5_entered_date = None
            logging.info(f"🔄 {label} 무진전 만료 → 0 리셋 ({elapsed}봉 경과)")
            return True
        return False

    def _prepare_df(self, df: pd.DataFrame) -> Optional[pd.DataFrame]:
        required_cols = {'close', 'volume'}
        if not required_cols.issubset(df.columns):
            logging.warning(f"필요한 컬럼 누락: {required_cols - set(df.columns)}")
            return None
        clean_df: pd.DataFrame = df[['close', 'volume']].dropna().copy()  # type: ignore[assignment]
        if len(clean_df) < self.MIN_ROWS:
            logging.warning(f"데이터 부족: {len(clean_df)}행")
            return None
        return clean_df

    def _vol_ma20(self, df: pd.DataFrame) -> pd.Series:
        result: pd.Series = df['volume'].shift(1).rolling(20).mean()  # type: ignore[assignment]
        return result

    def _check_ma_alignment(self, df: pd.DataFrame, bullish: bool = True) -> bool:
        ma5: float = df['close'].rolling(5).mean().iloc[-1]  # type: ignore[arg-type]
        ma20: float = df['close'].rolling(20).mean().iloc[-1]  # type: ignore[arg-type]
        ma60: float = df['close'].rolling(60).mean().iloc[-1]  # type: ignore[arg-type]
        if pd.isna(ma5) or pd.isna(ma20) or pd.isna(ma60):
            return False
        return bool(ma5 > ma20 > ma60) if bullish else bool(ma5 < ma20 < ma60)

    def _get_last_date(self, df: pd.DataFrame) -> str:
        last_date = df.index[-1]
        if isinstance(last_date, pd.Timestamp):
            return last_date.strftime("%Y-%m-%d")
        return str(last_date)

    def _check_stage5_reset(self, df: pd.DataFrame) -> bool:
        """Stage 5 진입 후 STAGE5_RESET_DAYS 경과 시 0으로 리셋."""
        if self.stage != 5:
            return False
        if self.stage5_entered_date is None:
            self.stage5_entered_date = self._get_last_date(df)
            return False

        last_ts = df.index[-1]
        last_date = last_ts.date() if isinstance(last_ts, pd.Timestamp) else datetime.strptime(str(last_ts), "%Y-%m-%d").date()
        entered = datetime.strptime(self.stage5_entered_date, "%Y-%m-%d").date()
        elapsed = (last_date - entered).days

        if elapsed >= STAGE5_RESET_DAYS:
            self.stage = 0
            self.stage5_entered_date = None
            self.stage_entered_date = None
            logging.info(f"🔄 Stage 5 → 0 리셋 ({elapsed}일 경과)")
            return True
        return False


class MarketBottomTracker(MarketStageTracker):
    STAGE_NAMES = {0: "초기 상태", 1: "매도세 소진", 2: "재테스트", 3: "트랩", 4: "추세 전환", 5: "🔥 최종 매수 신호"}

    def _is_exhaustion(self, df: pd.DataFrame) -> bool:
        """1단계 진입 = 6개월 고점 대비 깊은 낙폭 + 과매도 RSI (2026-09-29 재설계).

        구 구현(최근 5봉 순하락 + 좁은 변동폭, 임계 TQQQ 0.16)은 발동률 37%·필터가
        사실상 무효였고, 래칫(만료 없음)까지 겹쳐 TQQQ 이력의 97%가 1단계 이상이었다.
        백테스트(16.5년, 6개월 창): 발동률 4.8% · 발동일 이후 20일 +10.11% (+6.06pp vs 기준).
        ⚠️ 근거는 지수/지수 레버리지 ETF — 개별주(NVDA·AAPL)에서는 기대초과가 음수였다.
        """
        close = df['close']
        drawdown = close.iloc[-1] / close.max() - 1   # 창(6개월) 고점 대비 — _is_retest 와 같은 창 기준
        rsi = calculate_rsi(close).dropna()
        if len(rsi) == 0:
            return False
        return bool(drawdown <= -BOTTOM_DRAWDOWN_PCT and rsi.iloc[-1] < BOTTOM_RSI_MAX)

    def _is_retest(self, df: pd.DataFrame) -> bool:
        prior_low = df['close'].iloc[:-1].min()
        vol_ma20 = self._vol_ma20(df)
        is_near_low = df['close'].iloc[-1] <= prior_low * 1.045
        is_low_vol = df['volume'].iloc[-1] < vol_ma20.iloc[-1] * 0.85
        return bool(is_near_low and is_low_vol)

    def _is_trap(self, df: pd.DataFrame) -> bool:
        if len(df) < 5:
            return False
        prev_low = df['close'].iloc[:-2].min()
        broke_low_yest = df['close'].iloc[-2] < prev_low
        recovered_today = df['close'].iloc[-1] > prev_low * 0.99
        return bool(broke_low_yest and recovered_today)

    def _is_shift(self, df: pd.DataFrame) -> bool:
        recent_high = df['close'].rolling(20).max().shift(1)
        vol_ma20 = self._vol_ma20(df)
        breakout = df['close'].iloc[-1] > recent_high.iloc[-1] * 1.005
        high_vol = df['volume'].iloc[-1] > vol_ma20.iloc[-1] * 1.4
        return bool(breakout and high_vol)

    def _is_buy_signal(self, df: pd.DataFrame) -> bool:
        vol_ma20 = self._vol_ma20(df)
        high_vol = df['volume'].iloc[-1] > vol_ma20.iloc[-1] * 1.75
        alignment = self._check_ma_alignment(df, bullish=True)
        return bool(high_vol and alignment)

    def update(self, df: pd.DataFrame) -> int:
        clean_df = self._prepare_df(df)
        if clean_df is None:
            return self.stage
        if self.stage == 5 and self._check_stage5_reset(clean_df):
            return self.stage
        if self._expire_if_stale(clean_df, BOTTOM_EXPIRY_STAGES, BOTTOM_EXPIRY_BARS, "하단 1~2단계"):
            return self.stage

        logic = {0: self._is_exhaustion, 1: self._is_retest, 2: self._is_trap, 3: self._is_shift}
        if self.stage in logic and logic[self.stage](clean_df):
            self.stage += 1
            self.stage_entered_date = self._get_last_date(clean_df)
            if self.stage == 5:
                self.stage5_entered_date = self.stage_entered_date
        elif self.stage == 4 and self._is_buy_signal(clean_df):
            self.stage = 5
            self.stage_entered_date = self._get_last_date(clean_df)
            self.stage5_entered_date = self.stage_entered_date
        return self.stage


class MarketTopTracker(MarketStageTracker):
    STAGE_NAMES = {0: "초기 상태", 1: "🌡️ 과열", 2: "📉 다이버전스", 3: "🪤 밴드 트랩", 4: "📊 분산", 5: "🔻 최종 매도 신호"}

    def _is_overheat(self, df: pd.DataFrame) -> bool:
        rsi = calculate_rsi(df['close']).dropna()  # type: ignore[arg-type]
        if len(rsi) < 6:
            return False
        recent = rsi.tail(6)
        return bool(recent.iloc[:-1].max() >= 60 and recent.iloc[-1] < 60)

    def _is_dead_cross(self, df: pd.DataFrame) -> bool:
        recent_high = df['close'].rolling(20).max().shift(1)
        made_new_high = (df['close'].tail(5) > recent_high.tail(5)).any()
        macd, signal = calculate_macd(df['close'])  # type: ignore[arg-type]
        macd = macd.dropna()
        signal = signal.dropna()
        if len(macd) < 2:
            return False
        dead_cross = (macd.iloc[-2] >= signal.iloc[-2]) and (macd.iloc[-1] < signal.iloc[-1])
        return bool(made_new_high and dead_cross)

    def _is_band_trap(self, df: pd.DataFrame) -> bool:
        if len(df) < 5:
            return False
        upper = calculate_bollinger_upper(df['close']).dropna()  # type: ignore[arg-type]
        if len(upper) < 6:
            return False
        touched_upper = (df['close'].tail(6).iloc[:-1] > upper.tail(6).iloc[:-1]).any()
        back_inside = df['close'].iloc[-1] < upper.iloc[-1]
        return bool(touched_upper and back_inside)

    def _is_distribution(self, df: pd.DataFrame) -> bool:
        vol_ma20 = self._vol_ma20(df)
        price_change = df['close'].pct_change().iloc[-1]
        high_vol = df['volume'].iloc[-1] > vol_ma20.iloc[-1] * 1.4
        return bool(price_change <= 0.003 and high_vol)

    def _is_sell_signal(self, df: pd.DataFrame) -> bool:
        dropping = df['close'].pct_change().iloc[-1] < -0.001
        vol_ma20 = self._vol_ma20(df)
        high_vol = df['volume'].iloc[-1] > vol_ma20.iloc[-1] * 1.75
        bearish_align = self._check_ma_alignment(df, bullish=False)
        return bool(dropping and high_vol and bearish_align)

    def update(self, df: pd.DataFrame) -> int:
        clean_df = self._prepare_df(df)
        if clean_df is None:
            return self.stage
        if self.stage == 5 and self._check_stage5_reset(clean_df):
            return self.stage
        if self._expire_if_stale(clean_df, TOP_EXPIRY_STAGES, TOP_EXPIRY_BARS, "상단 4단계"):
            return self.stage

        logic = {0: self._is_overheat, 1: self._is_dead_cross, 2: self._is_band_trap, 3: self._is_distribution}
        if self.stage in logic and logic[self.stage](clean_df):
            self.stage += 1
            self.stage_entered_date = self._get_last_date(clean_df)
            if self.stage == 5:
                self.stage5_entered_date = self.stage_entered_date
        elif self.stage == 4 and self._is_sell_signal(clean_df):
            self.stage = 5
            self.stage_entered_date = self._get_last_date(clean_df)
            self.stage5_entered_date = self.stage_entered_date
        return self.stage


def _stage_entry_note(stage: int, entered: Optional[str]) -> str:
    """단계 진입일 표시 문자열 (2026-09-29 추가).

    단계는 오르기만 해서(만료는 5단계 30일·하단 1~2단계 30봉뿐), 날짜가 없으면 '1단계'가
    오늘 발생한 신호인지 몇 주 전 신호인지 구분할 수 없다 — 리포트 오해의 원인.
    """
    if stage == 0:
        return ""
    if not entered:
        return " · 진입일 미기록"
    try:
        entered_date = datetime.strptime(str(entered), "%Y-%m-%d").date()
    except ValueError:
        return f" · 진입 {entered}"
    elapsed = (datetime.now().date() - entered_date).days
    days = f", {elapsed}일 전" if elapsed > 0 else ""
    return f" · {entered} 진입{days}"


# ====================== 메인 트래커 ======================
class DiscordMarketTracker:
    def __init__(self):
        self.config = load_portfolio()
        self.webhook_url, self.user_id = resolve_discord_config(self.config)

        # Read ticker list from portfolio_config.json → POSITIONS keys
        positions = self.config.get("POSITIONS", {})
        if isinstance(positions, dict) and len(positions) > 0:
            self.tickers = list(positions.keys())
        else:
            raise ValueError("❌ portfolio_config.json에 'POSITIONS' 설정이 없습니다.")

        self.bottom_trackers: Dict[str, MarketBottomTracker] = {}
        self.top_trackers: Dict[str, MarketTopTracker] = {}
        self._load_state()

    def _load_state(self):
        state = {}
        if os.path.exists(STATE_PATH):
            try:
                with open(STATE_PATH, "r", encoding="utf-8") as f:
                    state = json.load(f)
            except Exception as exc:
                logging.warning(f"상태 로드 실패: {exc}")

        for ticker in self.tickers:
            saved = state.get(ticker, {}) if isinstance(state, dict) else {}
            self.bottom_trackers[ticker] = MarketBottomTracker(
                stage=saved.get("bottom", 0),
                stage5_entered_date=saved.get("bottom_stage5_date"),
                stage_entered_date=saved.get("bottom_stage_entered_date"),
            )
            self.top_trackers[ticker] = MarketTopTracker(
                stage=saved.get("top", 0),
                stage5_entered_date=saved.get("top_stage5_date"),
                stage_entered_date=saved.get("top_stage_entered_date"),
            )

    def _save_state(self):
        state = {
            ticker: {
                "bottom": self.bottom_trackers[ticker].stage,
                "bottom_stage5_date": self.bottom_trackers[ticker].stage5_entered_date,
                "bottom_stage_entered_date": self.bottom_trackers[ticker].stage_entered_date,
                "top": self.top_trackers[ticker].stage,
                "top_stage5_date": self.top_trackers[ticker].stage5_entered_date,
                "top_stage_entered_date": self.top_trackers[ticker].stage_entered_date,
            }
            for ticker in self.tickers
        }
        try:
            # Atomic write: write to temp file first, then move into place.
            # Prevents corrupt JSON if the script crashes mid-write.
            with tempfile.NamedTemporaryFile(
                "w", delete=False, suffix=".json", encoding="utf-8"
            ) as tmp:
                json.dump(state, tmp, ensure_ascii=False, indent=2)
                tmp_path = tmp.name
            shutil.move(tmp_path, STATE_PATH)
        except Exception as e:
            logging.error(f"상태 저장 실패: {e}")

    def _send_discord(self, message: str) -> bool:
        if not self.webhook_url:
            logging.warning("⚠️ DISCORD_WEBHOOK이 비어있습니다 (env var / config 확인 필요)")
            return False
        try:
            content = f"<@{self.user_id}> {message}" if self.user_id else message
            resp = requests.post(self.webhook_url, json={"content": content}, timeout=10)
            resp.raise_for_status()
            logging.info(f"✅ Discord 전송 성공 (status={resp.status_code})")
            return True
        except Exception as exc:
            logging.error(f"Discord 전송 실패: {exc}")
            return False

    def get_data(self, ticker: str) -> Optional[pd.DataFrame]:
        try:
            df = yf.download(ticker, period="6mo", interval="1d", progress=False, auto_adjust=True)
            if df is None or df.empty:
                return None

            if isinstance(df.columns, pd.MultiIndex):
                df = df.droplevel(1, axis=1)

            df = df.copy()
            df.columns = [str(col).lower() for col in df.columns]
            return df
        except Exception as exc:
            logging.error(f"{ticker} 데이터 다운로드 실패: {exc}")
            return None

    def update_all(self):
        lines = ["📊 **[시장 단계 리포트]**"]
        has_strong_signal = False

        for ticker in self.tickers:
            df = self.get_data(ticker)
            if df is None or df.empty:
                lines.append(f"• **{ticker}**: 데이터 조회 실패")
                continue

            bottom_stage = self.bottom_trackers[ticker].update(df)
            top_stage = self.top_trackers[ticker].update(df)

            bottom_name = MarketBottomTracker.STAGE_NAMES.get(bottom_stage, "알 수 없음")
            top_name = MarketTopTracker.STAGE_NAMES.get(top_stage, "알 수 없음")
            bottom_note = _stage_entry_note(bottom_stage, self.bottom_trackers[ticker].stage_entered_date)
            top_note = _stage_entry_note(top_stage, self.top_trackers[ticker].stage_entered_date)

            lines.append(f"• **{ticker}**")
            lines.append(f"   ㄴ 바닥: {bottom_stage}단계 ({bottom_name}{bottom_note})")
            lines.append(f"   ㄴ 천장: {top_stage}단계 ({top_name}{top_note})")

            if bottom_stage == 5:
                lines.append("   **🔥 강력 매수 추천!** (최종 매수 신호 발생)")
                has_strong_signal = True
            if top_stage == 5:
                lines.append("   **🔻 강력 매도 추천!** (최종 매도 신호 발생)")
                has_strong_signal = True

        if has_strong_signal:
            lines.append("⚠️ **강력 신호 종목이 있습니다. 주의 깊게 확인하세요!**")

        self._send_discord("\n".join(lines))
        self._save_state()
        logging.info("시장 단계 업데이트 완료")


if __name__ == "__main__":
    try:
        tracker = DiscordMarketTracker()
        tracker.update_all()
        print("✅ 시스템 실행 완료")
    except Exception as e:
        logging.error(f"치명적 오류: {e}")