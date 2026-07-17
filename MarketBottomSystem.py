import os
import requests
import pandas as pd
import json
import yfinance as yf

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

    def __init__(self):
        self.stage = 0 

    def _is_exhaustion(self, df):
        res = (df['close'].diff().rolling(5).sum() < 0) & (df['close'].tail(5).nunique() < 3)
        return res.iloc[-1]

    def _is_retest(self, df):
        vol_ma = df['volume'].rolling(20).mean()
        res = (df['close'] <= df['close'].min() * 1.02) & (df['volume'] < vol_ma)
        return res.iloc[-1]

    def _is_trap(self, df):
        prev_low = df['close'].shift(1).min()
        res = (df['close'].shift(1) < prev_low) & (df['close'] > prev_low)
        return res.iloc[-1]

    def _is_shift(self, df):
        recent_high = df['close'].rolling(20).max().shift(1)
        vol_ma = df['volume'].rolling(20).mean()
        res = (df['close'] > recent_high) & (df['volume'] > vol_ma * 1.5)
        return res.iloc[-1]

    def _is_buy_signal(self, df):
        """거래량 및 정배열 조건 검증"""
        vol_ma20 = df['volume'].rolling(20).mean()
        ma5, ma20, ma60 = df['close'].rolling(5).mean(), df['close'].rolling(20).mean(), df['close'].rolling(60).mean()
        
        is_high_volume = df['volume'].iloc[-1] > (vol_ma20.iloc[-1] * 2)
        is_alignment = (ma5.iloc[-1] > ma20.iloc[-1]) and (ma20.iloc[-1] > ma60.iloc[-1])
        return is_high_volume and is_alignment

    def update(self, df):
        # 0~3단계 이동
        logic = {0: self._is_exhaustion, 1: self._is_retest, 2: self._is_trap, 3: self._is_shift}
        if self.stage in logic and logic[self.stage](df):
            self.stage += 1
        # 4단계에서 매수 신호 충족 시 5단계로 이동
        elif self.stage == 4 and self._is_buy_signal(df):
            self.stage = 5
        return self.stage

class DiscordMarketTracker:
    def __init__(self):
        self.webhook_url = os.environ.get("DISCORD_WEBHOOK")
        self.user_id = os.environ.get("DISCORD_USER_ID")
        self.tickers = json.loads(os.environ.get("TICKERS", '["SOXL", "TSLA", "IONQ"]'))
        self.trackers = {ticker: MarketBottomTracker() for ticker in self.tickers}

    def _send_discord(self, message):
        if not self.webhook_url: return
        try:
            requests.post(self.webhook_url, json={"content": f"<@{self.user_id}> {message}"})
        except Exception as e:
            print(f"전송 오류: {e}")

    def update_all(self, data_map):
        report_msg = "📊 **[시장 단계 리포트]**\n"
        for ticker, df in data_map.items():
            current_stage = self.trackers[ticker].update(df)
            stage_name = MarketBottomTracker.STAGE_NAMES.get(current_stage, "알 수 없음")
            report_msg += f"• **{ticker}**: {current_stage}단계 ({stage_name})\n"
        self._send_discord(report_msg)

def get_data(ticker):
    df = yf.download(ticker, period="100d", interval="1d")
    if isinstance(df.columns, pd.MultiIndex): # type: ignore
        df.columns = df.columns.get_level_values(0) # type: ignore
    df.columns = df.columns.str.lower() # type: ignore
    return df

if __name__ == "__main__":
    tracker = DiscordMarketTracker()
    data_map = {t: get_data(t) for t in tracker.tickers}
    tracker.update_all(data_map)
    print("시스템 실행 완료.")