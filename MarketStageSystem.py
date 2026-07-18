import os
import json
import requests
import pandas as pd
import yfinance as yf

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "MarketStage_config.json")
STATE_PATH = os.path.join(BASE_DIR, "market_state.json")


# ---------------------------------------------------------------------------
# 기술적 지표 계산 (RSI, MACD, 볼린저 밴드)
# 바닥/천장 트래커가 공통으로 사용
# ---------------------------------------------------------------------------
def calculate_rsi(close, period=14):
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calculate_macd(close, fast=12, slow=26, signal=9):
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line, signal_line


def calculate_bollinger(close, period=20, num_std=2):
    mid = close.rolling(period).mean()
    std = close.rolling(period).std()
    upper = mid + num_std * std
    lower = mid - num_std * std
    return upper, mid, lower


class MarketBottomTracker:
    """개별 티커의 바닥 형성 단계(Stage)를 추적하는 엔진"""
    STAGE_NAMES = {
        0: "초기 상태",
        1: "매도세 소진",
        2: "재테스트",
        3: "트랩",
        4: "추세 전환",
        5: "🔥 최종 매수 신호(거래량/정배열 확인)"
    }

    MIN_ROWS = 60  # ma60 계산에 필요한 최소 데이터 수

    def __init__(self, stage=0):
        self.stage = stage

    def _is_exhaustion(self, df):
        # 최근 5일간 종가가 하락 추세이면서, 종가 종류가 거의 바뀌지 않을 정도로
        # 매도 압력이 소진된 상태인지 확인 (하락폭/값-종류 모두 '최근 5일' 기준으로 통일)
        last5_change = df['close'].iloc[-1] - df['close'].iloc[-5]
        last5_nunique = df['close'].tail(5).nunique()
        return bool(last5_change < 0 and last5_nunique < 3)

    def _is_retest(self, df):
        # '오늘'을 제외한 구간의 저점을 기준으로 재접근(재테스트) 여부 확인
        prior_low = df['close'].iloc[:-1].min()
        vol_ma20 = df['volume'].shift(1).rolling(20).mean()  # 오늘 거래량은 평균에서 제외
        is_near_low = df['close'].iloc[-1] <= prior_low * 1.02
        is_low_volume = df['volume'].iloc[-1] < vol_ma20.iloc[-1]
        return bool(is_near_low and is_low_volume)

    def _is_trap(self, df):
        # 트랩이 발생하기 '이전'까지의 저점을 기준점으로 삼음
        if len(df) < 3:
            return False
        prev_low = df['close'].iloc[:-2].min()
        yesterday_broke_low = df['close'].iloc[-2] < prev_low
        today_recovered = df['close'].iloc[-1] > prev_low
        return bool(yesterday_broke_low and today_recovered)

    def _is_shift(self, df):
        recent_high = df['close'].rolling(20).max().shift(1)  # 오늘 제외 20일 고점
        vol_ma20 = df['volume'].shift(1).rolling(20).mean()   # 오늘 거래량 제외
        is_breakout = df['close'].iloc[-1] > recent_high.iloc[-1]
        is_high_volume = df['volume'].iloc[-1] > vol_ma20.iloc[-1] * 1.5
        return bool(is_breakout and is_high_volume)

    def _is_buy_signal(self, df):
        """거래량 및 정배열 조건 검증"""
        vol_ma20 = df['volume'].shift(1).rolling(20).mean()  # 오늘 거래량은 평균에서 제외
        ma5 = df['close'].rolling(5).mean()
        ma20 = df['close'].rolling(20).mean()
        ma60 = df['close'].rolling(60).mean()

        is_high_volume = df['volume'].iloc[-1] > (vol_ma20.iloc[-1] * 2)
        is_alignment = (ma5.iloc[-1] > ma20.iloc[-1]) and (ma20.iloc[-1] > ma60.iloc[-1])
        return bool(is_high_volume and is_alignment)

    def update(self, df):
        if len(df) < self.MIN_ROWS:
            return self.stage  # 데이터 부족 시 상태 유지

        logic = {0: self._is_exhaustion, 1: self._is_retest, 2: self._is_trap, 3: self._is_shift}
        if self.stage in logic and logic[self.stage](df):
            self.stage += 1
        elif self.stage == 4 and self._is_buy_signal(df):
            self.stage = 5
        return self.stage


class MarketTopTracker:
    """개별 티커의 천장(고점) 형성 단계(Stage)를 추적하는 엔진

    바닥 트래커와 대칭되는 5단계 구조로, 사용자가 지정한 3가지 기술적
    지표(RSI 70 재하락, 신고가 후 MACD 데드크로스, 볼린저 상단 재진입)를
    거래량 패턴(과열/분산/거래량 마름)과 결합해 단계별로 확인한다.
    """
    STAGE_NAMES = {
        0: "초기 상태",
        1: "🌡️ 과열(RSI 70 상향 돌파 후 재하락)",
        2: "📉 다이버전스(신고가 + MACD 데드크로스)",
        3: "🪤 밴드 트랩(볼린저 상단 돌파 후 재진입)",
        4: "📊 분산(거래량 분산일 확인)",
        5: "🔻 최종 매도 신호(거래량/역배열 확인)"
    }

    MIN_ROWS = 60  # ma60, MACD(26,9) 안정화에 필요한 최소 데이터 수

    def __init__(self, stage=0):
        self.stage = stage

    def _is_overheat(self, df):
        # 최근 5일(오늘 포함) 내에 RSI가 70 이상을 찍은 적이 있고,
        # 오늘은 70 아래로 내려온 경우 -> 과열 후 냉각 시작
        rsi = calculate_rsi(df['close'])
        recent_rsi = rsi.tail(6)
        touched_70_before_today = recent_rsi.iloc[:-1].max() >= 70
        today_below_70 = recent_rsi.iloc[-1] < 70
        return bool(touched_70_before_today and today_below_70)

    def _is_dead_cross(self, df):
        # 최근 5일 내 신고가를 경신했고, 오늘 MACD선이 시그널선을
        # 위에서 아래로 교차(데드크로스)하는 경우
        recent_high_before = df['close'].rolling(20).max().shift(1)
        made_new_high_recently = bool(
            (df['close'].tail(5) > recent_high_before.tail(5)).any()
        )
        macd_line, signal_line = calculate_macd(df['close'])
        crossed_down_today = (macd_line.iloc[-2] >= signal_line.iloc[-2]) and \
                              (macd_line.iloc[-1] < signal_line.iloc[-1])
        return bool(made_new_high_recently and crossed_down_today)

    def _is_band_trap(self, df):
        # 최근 5일 내 볼린저 상단을 돌파한 적이 있고, 오늘은 밴드 안으로
        # 재진입한 경우 -> 상단 돌파가 속임수(가짜 돌파)였을 가능성
        upper, _, _ = calculate_bollinger(df['close'])
        touched_upper_before_today = bool(
            (df['close'].tail(6).iloc[:-1] > upper.tail(6).iloc[:-1]).any()
        )
        today_back_inside = df['close'].iloc[-1] < upper.iloc[-1]
        return bool(touched_upper_before_today and today_back_inside)

    def _is_distribution(self, df):
        # 분산일(distribution day): 주가는 거의 오르지 못했는데(또는 하락)
        # 거래량은 평소보다 훨씬 크게 실린 경우 -> 큰손이 조용히 매도 중
        vol_ma20 = df['volume'].shift(1).rolling(20).mean()  # 오늘 거래량 제외
        price_change = df['close'].pct_change().iloc[-1]
        is_flat_or_down = price_change <= 0.002
        is_heavy_volume = df['volume'].iloc[-1] > vol_ma20.iloc[-1] * 1.5
        return bool(is_flat_or_down and is_heavy_volume)

    def _is_sell_signal(self, df):
        """거래량 및 역배열 조건 검증 (바닥의 _is_buy_signal과 대칭)"""
        vol_ma20 = df['volume'].shift(1).rolling(20).mean()  # 오늘 거래량 제외
        ma5 = df['close'].rolling(5).mean()
        ma20 = df['close'].rolling(20).mean()
        ma60 = df['close'].rolling(60).mean()

        price_dropping = df['close'].pct_change().iloc[-1] < 0
        is_high_volume = df['volume'].iloc[-1] > (vol_ma20.iloc[-1] * 2)
        is_bearish_alignment = (ma5.iloc[-1] < ma20.iloc[-1]) and (ma20.iloc[-1] < ma60.iloc[-1])
        return bool(price_dropping and is_high_volume and is_bearish_alignment)

    def update(self, df):
        if len(df) < self.MIN_ROWS:
            return self.stage  # 데이터 부족 시 상태 유지

        logic = {0: self._is_overheat, 1: self._is_dead_cross, 2: self._is_band_trap, 3: self._is_distribution}
        if self.stage in logic and logic[self.stage](df):
            self.stage += 1
        elif self.stage == 4 and self._is_sell_signal(df):
            self.stage = 5
        return self.stage


class DiscordMarketTracker:
    def __init__(self):
        self.config = self._load_config()
        self.webhook_url = self.config.get("DISCORD_WEBHOOK", "")
        self.user_id = self.config.get("DISCORD_USER_ID", "")
        self.tickers = self.config.get("TICKERS", ["SOXL", "TSLA", "IONQ"])

        # 상태를 파일로 저장/로딩 -> 매 실행마다 stage가 0으로 리셋되는 것을 방지
        # state 포맷: {"SOXL": {"bottom": 2, "top": 0}, ...}
        saved_state = self._load_state()
        self.bottom_trackers = {
            ticker: MarketBottomTracker(stage=saved_state.get(ticker, {}).get("bottom", 0))
            for ticker in self.tickers
        }
        self.top_trackers = {
            ticker: MarketTopTracker(stage=saved_state.get(ticker, {}).get("top", 0))
            for ticker in self.tickers
        }

    @staticmethod
    def _load_config():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = json.load(f)
        config["DISCORD_WEBHOOK"] = os.environ.get("DISCORD_WEBHOOK") or config.get("DISCORD_WEBHOOK", "")
        config["DISCORD_USER_ID"] = os.environ.get("DISCORD_USER_ID") or config.get("DISCORD_USER_ID", "")
        return config

    @staticmethod
    def _load_state():
        if os.path.exists(STATE_PATH):
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}

    def _save_state(self):
        state = {
            ticker: {
                "bottom": self.bottom_trackers[ticker].stage,
                "top": self.top_trackers[ticker].stage,
            }
            for ticker in self.tickers
        }
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)

    def _send_discord(self, message):
        if not self.webhook_url:
            print("DISCORD_WEBHOOK이 설정되지 않아 메시지를 전송하지 않습니다.")
            return
        try:
            requests.post(self.webhook_url, json={"content": f"<@{self.user_id}> {message}"}, timeout=10)
        except Exception as e:
            print(f"전송 오류: {e}")

    def update_all(self, data_map):
        report_msg = "📊 **[시장 단계 리포트]**\n"
        for ticker, df in data_map.items():
            if df is None or df.empty:
                report_msg += f"• **{ticker}**: 데이터 조회 실패\n"
                continue

            bottom_stage = self.bottom_trackers[ticker].update(df)
            top_stage = self.top_trackers[ticker].update(df)
            bottom_name = MarketBottomTracker.STAGE_NAMES.get(bottom_stage, "알 수 없음")
            top_name = MarketTopTracker.STAGE_NAMES.get(top_stage, "알 수 없음")

            report_msg += (
                f"• **{ticker}**\n"
                f"   ㄴ 바닥: {bottom_stage}단계 ({bottom_name})\n"
                f"   ㄴ 천장: {top_stage}단계 ({top_name})\n"
            )
        self._send_discord(report_msg)
        self._save_state()


def get_data(ticker):
    try:
        # 100d(달력일 기준)는 공휴일/주말을 감안하면 거래일 60~70일 정도라
        # ma60/MACD 계산이 불안정할 수 있어 6mo로 여유를 둠
        df = yf.download(ticker, period="6mo", interval="1d", progress=False)
    except Exception as e:
        print(f"{ticker} 데이터 조회 오류: {e}")
        return None
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):  # type: ignore
        df.columns = df.columns.get_level_values(0)  # type: ignore
    df.columns = df.columns.str.lower()  # type: ignore
    return df


if __name__ == "__main__":
    tracker = DiscordMarketTracker()
    data_map = {t: get_data(t) for t in tracker.tickers}
    tracker.update_all(data_map)
    print("시스템 실행 완료.")
