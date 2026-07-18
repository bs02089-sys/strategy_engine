import os
import json
import logging
import requests
import pandas as pd
import yfinance as yf
from dataclasses import dataclass
from typing import Dict, Optional

# ====================== 설정 ======================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "MarketStage_config.json")
STATE_PATH = os.path.join(BASE_DIR, "market_state.json")

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')


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


def calculate_bollinger(close: pd.Series, period: int = 20, num_std: float = 2.0):
    mid = close.rolling(period).mean()
    std = close.rolling(period).std()
    upper = mid + num_std * std
    return upper


# ====================== 트래커 베이스 ======================
@dataclass
class StageInfo:
    stage: int
    name: str


class MarketStageTracker:
    MIN_ROWS = 60

    def __init__(self, stage: int = 0):
        self.stage = stage

    def _prepare_df(self, df: pd.DataFrame) -> Optional[pd.DataFrame]:
        if len(df) < self.MIN_ROWS:
            return None
        df = df.copy()
        df = df.dropna(subset=['close', 'volume'])
        return df


class MarketBottomTracker(MarketStageTracker):
    STAGE_NAMES = {
        0: "초기 상태",
        1: "매도세 소진",
        2: "재테스트",
        3: "트랩",
        4: "추세 전환",
        5: "🔥 최종 매수 신호"
    }

    def _is_exhaustion(self, df: pd.DataFrame) -> bool:
        last5 = df['close'].tail(5)
        change = last5.iloc[-1] - last5.iloc[0]
        return bool(change < 0 and last5.nunique() <= 3)

    def _is_retest(self, df: pd.DataFrame) -> bool:
        prior_low = df['close'].iloc[:-1].min()
        vol_ma20 = df['volume'].shift(1).rolling(20).mean()
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
        vol_ma20 = df['volume'].shift(1).rolling(20).mean()
        breakout = df['close'].iloc[-1] > recent_high.iloc[-1] * 1.005
        high_vol = df['volume'].iloc[-1] > vol_ma20.iloc[-1] * 1.4
        return bool(breakout and high_vol)

    def _is_buy_signal(self, df: pd.DataFrame) -> bool:
        vol_ma20 = df['volume'].shift(1).rolling(20).mean()
        ma5 = df['close'].rolling(5).mean()
        ma20 = df['close'].rolling(20).mean()
        ma60 = df['close'].rolling(60).mean()

        high_vol = df['volume'].iloc[-1] > vol_ma20.iloc[-1] * 1.75
        alignment = (ma5.iloc[-1] > ma20.iloc[-1]) and (ma20.iloc[-1] > ma60.iloc[-1])
        return bool(high_vol and alignment)

    def update(self, df: pd.DataFrame) -> int:
        clean_df = self._prepare_df(df)
        if clean_df is None:
            return self.stage

        logic = {0: self._is_exhaustion, 1: self._is_retest, 2: self._is_trap, 3: self._is_shift}
        
        if self.stage in logic and logic[self.stage](clean_df):
            self.stage += 1
        elif self.stage == 4 and self._is_buy_signal(clean_df):
            self.stage = 5

        return self.stage


class MarketTopTracker(MarketStageTracker):
    STAGE_NAMES = {
        0: "초기 상태",
        1: "🌡️ 과열",
        2: "📉 다이버전스",
        3: "🪤 밴드 트랩",
        4: "📊 분산",
        5: "🔻 최종 매도 신호"
    }

    def _is_overheat(self, df: pd.DataFrame) -> bool:
        rsi = calculate_rsi(df['close']).dropna()
        if len(rsi) < 6:
            return False
        recent = rsi.tail(6)
        touched_70 = recent.iloc[:-1].max() >= 70
        below_70_today = recent.iloc[-1] < 70
        return bool(touched_70 and below_70_today)

    def _is_dead_cross(self, df: pd.DataFrame) -> bool:
        recent_high = df['close'].rolling(20).max().shift(1)
        made_new_high = (df['close'].tail(5) > recent_high.tail(5)).any()

        macd, signal = calculate_macd(df['close'])
        macd = macd.dropna()
        signal = signal.dropna()
        if len(macd) < 2:
            return False

        dead_cross = (macd.iloc[-2] >= signal.iloc[-2]) and (macd.iloc[-1] < signal.iloc[-1])
        return bool(made_new_high and dead_cross)

    def _is_band_trap(self, df: pd.DataFrame) -> bool:
        upper = calculate_bollinger(df['close']).dropna()
        if len(upper) < 6:
            return False

        touched_upper = (df['close'].tail(6).iloc[:-1] > upper.tail(6).iloc[:-1]).any()
        back_inside = df['close'].iloc[-1] < upper.iloc[-1]
        return bool(touched_upper and back_inside)

    def _is_distribution(self, df: pd.DataFrame) -> bool:
        vol_ma20 = df['volume'].shift(1).rolling(20).mean()
        price_change = df['close'].pct_change().iloc[-1]
        high_vol = df['volume'].iloc[-1] > vol_ma20.iloc[-1] * 1.4
        flat_or_down = price_change <= 0.003
        return bool(flat_or_down and high_vol)

    def _is_sell_signal(self, df: pd.DataFrame) -> bool:
        vol_ma20 = df['volume'].shift(1).rolling(20).mean()
        ma5 = df['close'].rolling(5).mean()
        ma20 = df['close'].rolling(20).mean()
        ma60 = df['close'].rolling(60).mean()

        dropping = df['close'].pct_change().iloc[-1] < -0.001
        high_vol = df['volume'].iloc[-1] > vol_ma20.iloc[-1] * 1.75
        bearish_align = (ma5.iloc[-1] < ma20.iloc[-1]) and (ma20.iloc[-1] < ma60.iloc[-1])
        return bool(dropping and high_vol and bearish_align)

    def update(self, df: pd.DataFrame) -> int:
        clean_df = self._prepare_df(df)
        if clean_df is None:
            return self.stage

        logic = {0: self._is_overheat, 1: self._is_dead_cross, 2: self._is_band_trap, 3: self._is_distribution}
        
        if self.stage in logic and logic[self.stage](clean_df):
            self.stage += 1
        elif self.stage == 4 and self._is_sell_signal(clean_df):
            self.stage = 5

        return self.stage


# ====================== 메인 트래커 ======================
class DiscordMarketTracker:
    def __init__(self):
        self.config = self._load_config()
        self.webhook_url = self.config.get("DISCORD_WEBHOOK", "")
        self.user_id = self.config.get("DISCORD_USER_ID", "")
        self.tickers: list[str] = self.config.get("TICKERS", ["SOXL", "TSLA", "IONQ"])

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
        if os.path.exists(STATE_PATH):
            try:
                with open(STATE_PATH, "r", encoding="utf-8") as f:
                    state = json.load(f)
                for ticker in self.tickers:
                    self.bottom_trackers[ticker] = MarketBottomTracker(
                        state.get(ticker, {}).get("bottom", 0)
                    )
                    self.top_trackers[ticker] = MarketTopTracker(
                        state.get(ticker, {}).get("top", 0)
                    )
            except Exception:
                pass

        for ticker in self.tickers:
            if ticker not in self.bottom_trackers:
                self.bottom_trackers[ticker] = MarketBottomTracker()
                self.top_trackers[ticker] = MarketTopTracker()

    def _save_state(self):
        state = {
            ticker: {
                "bottom": self.bottom_trackers[ticker].stage,
                "top": self.top_trackers[ticker].stage,
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
        except Exception as e:
            logging.error(f"Discord 전송 실패: {e}")

    def get_data(self, ticker: str) -> Optional[pd.DataFrame]:
        try:
            df = yf.download(ticker, period="6mo", interval="1d", progress=False, auto_adjust=True)
            if df is None or df.empty:
                return None

            if isinstance(df.columns, pd.MultiIndex):
                df = df.droplevel(1, axis=1)

            df.columns = [col.lower() for col in df.columns]
            return df
        except Exception as e:
            logging.error(f"{ticker} 데이터 다운로드 실패: {e}")
            return None

    def update_all(self):
        report = "📊 **[시장 단계 리포트]**\n"
        data_map = {t: self.get_data(t) for t in self.tickers}
        has_strong_signal = False

        for ticker, df in data_map.items():
            if df is None or df.empty:
                report += f"• **{ticker}**: 데이터 조회 실패\n"
                continue

            bottom_stage = self.bottom_trackers[ticker].update(df)
            top_stage = self.top_trackers[ticker].update(df)

            bottom_name = MarketBottomTracker.STAGE_NAMES.get(bottom_stage)
            top_name = MarketTopTracker.STAGE_NAMES.get(top_stage)

            report += f"• **{ticker}**\n"
            report += f"   ㄴ 바닥: {bottom_stage}단계 ({bottom_name})\n"
            report += f"   ㄴ 천장: {top_stage}단계 ({top_name})\n"

            # 5단계 강력 추천 멘트 추가
            if bottom_stage == 5:
                report += "   **🔥 강력 매수 추천!** (최종 매수 신호 발생)\n"
                has_strong_signal = True
            if top_stage == 5:
                report += "   **🔻 강력 매도 추천!** (최종 매도 신호 발생)\n"
                has_strong_signal = True

        # 전체 요약
        if has_strong_signal:
            report += "\n⚠️ **강력 신호 종목이 있습니다. 주의 깊게 확인하세요!**"

        self._send_discord(report)
        self._save_state()
        logging.info("시장 단계 업데이트 완료")
        

if __name__ == "__main__":
    try:
        tracker = DiscordMarketTracker()
        tracker.update_all()
        print("✅ 시스템 실행 완료")
    except Exception as e:
        logging.error(f"치명적 오류: {e}")