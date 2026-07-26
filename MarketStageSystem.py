import os
import json
import logging
import requests
import pandas as pd
import yfinance as yf
from datetime import datetime
from typing import Optional, Dict

# ====================== 설정 ======================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "MarketStage_config.json")
STATE_PATH = os.path.join(BASE_DIR, "market_state.json")

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')

STAGE5_RESET_DAYS = 30


# ====================== 기술적 지표 ======================
def calculate_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, float('nan'))
    return 100 - (100 / (1 + rs))


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

    def __init__(self, stage: int = 0, stage5_entered_date: Optional[str] = None):
        self.stage = stage
        self.stage5_entered_date = stage5_entered_date

    def _prepare_df(self, df: pd.DataFrame) -> Optional[pd.DataFrame]:
        required_cols = {'close', 'volume'}
        if not required_cols.issubset(df.columns):
            logging.warning(f"필요한 컬럼 누락: {required_cols - set(df.columns)}")
            return None

        clean_df = df[['close', 'volume']].dropna().copy()
        if len(clean_df) < self.MIN_ROWS:
            logging.warning(f"데이터 부족: {len(clean_df)}행")
            return None
        return clean_df

    def _vol_ma20(self, df: pd.DataFrame) -> pd.Series:
        return df['volume'].shift(1).rolling(20).mean()

    def _check_ma_alignment(self, df: pd.DataFrame, bullish: bool = True) -> bool:
        ma5 = df['close'].rolling(5).mean().iloc[-1]
        ma20 = df['close'].rolling(20).mean().iloc[-1]
        ma60 = df['close'].rolling(60).mean().iloc[-1]

        if pd.isna(ma5) or pd.isna(ma20) or pd.isna(ma60):
            return False

        if bullish:
            return bool(ma5 > ma20 and ma20 > ma60)
        else:
            return bool(ma5 < ma20 and ma20 < ma60)

    def _get_last_date(self, df: pd.DataFrame) -> str:
        last_date = df.index[-1]
        if isinstance(last_date, pd.Timestamp):
            return last_date.strftime("%Y-%m-%d")
        return str(last_date)

    def _check_stage5_reset(self, df: pd.DataFrame) -> bool:
        if self.stage != 5:
            return False

        if self.stage5_entered_date is None:
            self.stage5_entered_date = self._get_last_date(df)
            return False

        last_date = df.index[-1]
        if isinstance(last_date, pd.Timestamp):
            last_date = last_date.date()
        else:
            last_date = datetime.strptime(str(last_date), "%Y-%m-%d").date()

        entered = datetime.strptime(self.stage5_entered_date, "%Y-%m-%d").date()
        elapsed = (last_date - entered).days

        if elapsed >= STAGE5_RESET_DAYS:
            self.stage = 0
            self.stage5_entered_date = None
            logging.info(f"🔄 Stage 5 → 0 리셋 완료 (경과일: {elapsed}일)")
            return True
        return False


class MarketBottomTracker(MarketStageTracker):
    STAGE_NAMES = {
        0: "초기 상태", 1: "매도세 소진", 2: "재테스트",
        3: "트랩", 4: "추세 전환", 5: "🔥 최종 매수 신호"
    }

    def __init__(self, stage: int = 0, stage5_entered_date: Optional[str] = None, exhaustion_threshold: float = 0.10):
        super().__init__(stage, stage5_entered_date)
        self.exhaustion_threshold = exhaustion_threshold

    def _is_exhaustion(self, df: pd.DataFrame) -> bool:
        last5 = df['close'].tail(5)
        change = last5.iloc[-1] - last5.iloc[0]
        range_ratio = (last5.max() - last5.min()) / last5.mean()
        return bool(change <= 0 and range_ratio <= self.exhaustion_threshold)

    def _is_retest(self, df: pd.DataFrame) -> bool:
        prior_low = df['close'].iloc[:-1].min()
        vol_ma20 = self._vol_ma20(df)
        is_near_low = df['close'].iloc[-1] <= prior_low * 1.045
        is_low_vol = df['volume'].iloc[-1] < vol_ma20.iloc[-1] * 0.85
        return bool(is_near_low and is_low_vol)

    def _is_trap(self, df: pd.DataFrame) -> bool:
        if len(df) < 5: return False
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

        logic = {0: self._is_exhaustion, 1: self._is_retest, 2: self._is_trap, 3: self._is_shift}

        if self.stage in logic and logic[self.stage](clean_df):
            self.stage += 1
            if self.stage == 5:
                self.stage5_entered_date = self._get_last_date(clean_df)
        elif self.stage == 4 and self._is_buy_signal(clean_df):
            self.stage = 5
            self.stage5_entered_date = self._get_last_date(clean_df)

        return self.stage


class MarketTopTracker(MarketStageTracker):
    STAGE_NAMES = {
        0: "초기 상태", 1: "🌡️ 과열", 2: "📉 다이버전스",
        3: "🪤 밴드 트랩", 4: "📊 분산", 5: "🔻 최종 매도 신호"
    }

    def _is_overheat(self, df: pd.DataFrame) -> bool:
        rsi = calculate_rsi(df['close']).dropna()
        if len(rsi) < 6: return False
        recent = rsi.tail(6)
        return bool(recent.iloc[:-1].max() >= 60 and recent.iloc[-1] < 60)

    def _is_dead_cross(self, df: pd.DataFrame) -> bool:
        recent_high = df['close'].rolling(20).max().shift(1)
        made_new_high = (df['close'].tail(5) > recent_high.tail(5)).any()
        macd, signal = calculate_macd(df['close'])
        macd = macd.dropna()
        signal = signal.dropna()
        if len(macd) < 2: return False
        dead_cross = (macd.iloc[-2] >= signal.iloc[-2]) and (macd.iloc[-1] < signal.iloc[-1])
        return bool(made_new_high and dead_cross)

    def _is_band_trap(self, df: pd.DataFrame) -> bool:
        if len(df) < 5: return False
        upper = calculate_bollinger_upper(df['close']).dropna()
        if len(upper) < 6: return False
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

        logic = {0: self._is_overheat, 1: self._is_dead_cross, 2: self._is_band_trap, 3: self._is_distribution}

        if self.stage in logic and logic[self.stage](clean_df):
            self.stage += 1
            if self.stage == 5:
                self.stage5_entered_date = self._get_last_date(clean_df)
        elif self.stage == 4 and self._is_sell_signal(clean_df):
            self.stage = 5
            self.stage5_entered_date = self._get_last_date(clean_df)

        return self.stage


# ====================== 메인 트래커 ======================
class DiscordMarketTracker:
    def __init__(self):
        self.config = self._load_config()
        self.webhook_url: str = self.config.get("DISCORD_WEBHOOK", "")
        self.user_id: str = self.config.get("DISCORD_USER_ID", "")
        self.tickers: list = self.config.get("TICKERS", ["TQQQ", "SOXL"])

        self.bottom_trackers: Dict[str, MarketBottomTracker] = {}
        self.top_trackers: Dict[str, MarketTopTracker] = {}
        self._load_state()

    def _load_config(self) -> dict:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = json.load(f)
        config["DISCORD_WEBHOOK"] = os.environ.get("DISCORD_WEBHOOK") or config.get("DISCORD_WEBHOOK", "")
        config["DISCORD_USER_ID"] = os.environ.get("DISCORD_USER_ID") or config.get("DISCORD_USER_ID", "")
        return config

    def _load_state(self):
        state = {}
        if os.path.exists(STATE_PATH):
            try:
                with open(STATE_PATH, "r", encoding="utf-8") as f:
                    state = json.load(f)
            except Exception as exc:
                logging.warning(f"상태 로드 실패: {exc}")

        for ticker in self.tickers:
            saved = state.get(ticker, {})
            exh_th = 0.16 if ticker in {"SOXL", "SOXX", "TQQQ"} else 0.10
            self.bottom_trackers[ticker] = MarketBottomTracker(
                stage=saved.get("bottom", 0),
                stage5_entered_date=saved.get("bottom_stage5_date"),
                exhaustion_threshold=exh_th
            )
            self.top_trackers[ticker] = MarketTopTracker(
                stage=saved.get("top", 0),
                stage5_entered_date=saved.get("top_stage5_date")
            )

    def _save_state(self):
        state = {
            ticker: {
                "bottom": self.bottom_trackers[ticker].stage,
                "bottom_stage5_date": self.bottom_trackers[ticker].stage5_entered_date,
                "top": self.top_trackers[ticker].stage,
                "top_stage5_date": self.top_trackers[ticker].stage5_entered_date,
            }
            for ticker in self.tickers
        }
        try:
            with open(STATE_PATH, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logging.error(f"상태 저장 실패: {e}")

    def _send_discord(self, message: str):
        if not self.webhook_url:
            logging.info("DISCORD_WEBHOOK이 설정되지 않음")
            return
        try:
            content = f"<@{self.user_id}> {message}" if self.user_id else message
            requests.post(self.webhook_url, json={"content": content}, timeout=10)
        except Exception as exc:
            logging.error(f"Discord 전송 실패: {exc}")

    def get_data(self, ticker: str) -> Optional[pd.DataFrame]:
        try:
            df = yf.download(ticker, period="6mo", interval="1d", progress=False, auto_adjust=True)
            if df is None or df.empty:
                return None

            if isinstance(df, pd.Series):
                df = df.to_frame()

            if isinstance(df.columns, pd.MultiIndex):
                df = df.xs(ticker, level=1, axis=1)

            df = df.copy()
            df.columns = [col.lower() for col in df.columns]
            return df
        except Exception as exc:
            logging.error(f"{ticker} 데이터 다운로드 실패: {exc}")
            return None

    def get_all_data(self) -> Dict[str, pd.DataFrame]:
        try:
            df_multi = yf.download(self.tickers, period="6mo", interval="1d", progress=False, auto_adjust=True)
            if df_multi is None or df_multi.empty:
                return {}

            data_map: Dict[str, pd.DataFrame] = {}
            if isinstance(df_multi.columns, pd.MultiIndex):
                for ticker in self.tickers:
                    if ticker in df_multi.columns.get_level_values(1):
                        ticker_df = df_multi.xs(ticker, level=1, axis=1).copy()
                        ticker_df.columns = [col.lower() for col in ticker_df.columns]
                        data_map[ticker] = ticker_df
            else:
                df = df_multi.copy()
                df.columns = [col.lower() for col in df.columns]
                data_map[self.tickers[0]] = df
            return data_map
        except Exception as exc:
            logging.warning(f"배치 다운로드 실패: {exc}")
            return {}

    def check_leveraged_buy_signal(self):
        for ticker in ["TQQQ", "SOXL"]:
            if ticker not in self.bottom_trackers:
                continue
            df = self.get_data(ticker)
            if df is None or df.empty:
                continue

            stage = self.bottom_trackers[ticker].update(df)

            if stage == 5:
                if ticker == "TQQQ":
                    msg = f"🔥 **TQQQ Stage 5 발생!** → 50% 몰빵 + LOC 20차 분할 추천"
                else:
                    msg = f"🔥 **SOXL Stage 5 발생!** → 30% 몰빵 + LOC 20차 분할 추천 (고변동성 주의)"
                self._send_discord(msg)
                logging.info(f"{ticker} Stage 5 강력 매수 신호!")

    def update_all(self):
        lines = ["📊 **[시장 단계 리포트]**"]

        data_map = self.get_all_data()
        if not data_map:
            data_map = {t: self.get_data(t) for t in self.tickers}

        for ticker, df in data_map.items():
            if df is None or df.empty:
                lines.append(f"• **{ticker}**: 데이터 조회 실패")
                continue

            bottom_stage = self.bottom_trackers[ticker].update(df)
            top_stage = self.top_trackers[ticker].update(df)

            bottom_name = MarketBottomTracker.STAGE_NAMES.get(bottom_stage, "알 수 없음")
            top_name = MarketTopTracker.STAGE_NAMES.get(top_stage, "알 수 없음")

            lines.append(f"• **{ticker}**")
            lines.append(f"   ㄴ 바닥: {bottom_stage}단계 ({bottom_name})")
            lines.append(f"   ㄴ 천장: {top_stage}단계 ({top_name})")

            if bottom_stage == 5:
                lines.append("   **🔥 강력 매수 신호!**")
            if top_stage == 5:
                lines.append("   **🔻 강력 매도 신호!**")

        self._send_discord("\n".join(lines))
        self._save_state()
        logging.info("시장 단계 업데이트 완료")


if __name__ == "__main__":
    try:
        tracker = DiscordMarketTracker()
        tracker.update_all()
        tracker.check_leveraged_buy_signal()
        print("✅ 시스템 실행 완료")
    except Exception as e:
        logging.error(f"치명적 오류: {e}")