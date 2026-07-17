import os
import json
import requests
import pandas as pd
import yfinance as yf

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "Marketbottom_config.json")
STATE_PATH = os.path.join(BASE_DIR, "market_bottom_state.json")


class MarketBottomTracker:
    """개별 티커의 시장 단계(Stage)를 추적하는 엔진"""
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
        # 매도 압력이 소진된 상태인지 확인
        last5_diff_sum = df['close'].diff().tail(5).sum()
        last5_nunique = df['close'].tail(5).nunique()
        return bool(last5_diff_sum < 0 and last5_nunique < 3)

    def _is_retest(self, df):
        # '오늘'을 제외한 구간의 저점을 기준으로 재접근(재테스트) 여부 확인
        # (오늘 종가를 포함해 최소값을 구하면, 오늘이 신저점을 찍는 경우에도
        #  항상 "재테스트"로 오판되는 문제가 있었음)
        prior_low = df['close'].iloc[:-1].min()
        vol_ma20 = df['volume'].shift(1).rolling(20).mean()  # 오늘 거래량은 평균에서 제외
        is_near_low = df['close'].iloc[-1] <= prior_low * 1.02
        is_low_volume = df['volume'].iloc[-1] < vol_ma20.iloc[-1]
        return bool(is_near_low and is_low_volume)

    def _is_trap(self, df):
        # 원본 코드의 버그: prev_low = df['close'].shift(1).min() 은
        # '어제'까지 포함한 최소값이라, "어제 종가 < prev_low" 조건이
        # 수학적으로 항상 거짓이 되어 트랩 단계로 절대 못 넘어가는 문제가 있었음.
        # -> 트랩이 발생하기 '이전'까지의 저점을 기준점으로 삼도록 수정.
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
            # 데이터가 충분치 않으면(ma60 계산 불가) 상태를 그대로 유지
            return self.stage

        logic = {0: self._is_exhaustion, 1: self._is_retest, 2: self._is_trap, 3: self._is_shift}
        if self.stage in logic and logic[self.stage](df):
            self.stage += 1
        elif self.stage == 4 and self._is_buy_signal(df):
            self.stage = 5
        return self.stage


class DiscordMarketTracker:
    def __init__(self):
        self.config = self._load_config()
        self.webhook_url = self.config.get("DISCORD_WEBHOOK", "")
        self.user_id = self.config.get("DISCORD_USER_ID", "")
        self.tickers = self.config.get("TICKERS", ["SOXL", "TSLA", "IONQ"])

        # 원본 코드의 치명적 버그: 매 실행(GitHub Actions cron)마다
        # MarketBottomTracker()를 새로 만들어 stage가 항상 0으로 초기화되었음.
        # -> 상태를 파일로 저장하고, 다음 실행 때 불러와서 이어서 판단하도록 수정.
        saved_state = self._load_state()
        self.trackers = {
            ticker: MarketBottomTracker(stage=saved_state.get(ticker, 0))
            for ticker in self.tickers
        }

    @staticmethod
    def _load_config():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = json.load(f)
        # 시크릿 값은 환경 변수(GitHub Secrets)가 있으면 그것을 우선 사용하고,
        # 없으면 config 파일 값을 사용 (로컬 테스트 편의를 위함)
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
        state = {ticker: tracker.stage for ticker, tracker in self.trackers.items()}
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
            current_stage = self.trackers[ticker].update(df)
            stage_name = MarketBottomTracker.STAGE_NAMES.get(current_stage, "알 수 없음")
            report_msg += f"• **{ticker}**: {current_stage}단계 ({stage_name})\n"
        self._send_discord(report_msg)
        self._save_state()  # 다음 실행을 위해 stage 저장 (이게 없으면 상태가 계속 리셋됨)


def get_data(ticker):
    try:
        # 100d(달력일 기준)는 공휴일/주말을 감안하면 거래일 60~70일 정도라
        # ma60 계산이 불안정할 수 있어 6mo로 여유를 둠
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
