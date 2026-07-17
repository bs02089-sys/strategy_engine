import os
import requests
import pandas as pd
import json

class MarketBottomTracker:
    def __init__(self):
        self.stage = 0 

    def _is_exhaustion(self, df):
        return (df['close'].diff().rolling(5).sum() < 0) & (df['close'].tail(5).nunique() < 3)

    def _is_retest(self, df):
        vol_ma = df['volume'].rolling(20).mean()
        return (df['close'] <= df['close'].min() * 1.02) & (df['volume'] < vol_ma)

    def _is_trap(self, df):
        prev_low = df['close'].shift(1).min()
        return (df['close'].shift(1) < prev_low) & (df['close'] > prev_low)

    def _is_shift(self, df):
        recent_high = df['close'].rolling(20).max().shift(1)
        vol_ma = df['volume'].rolling(20).mean()
        return (df['close'] > recent_high) & (df['volume'] > vol_ma * 1.5)

    def update(self, df):
        if self.stage == 0 and self._is_exhaustion(df):
            self.stage = 1
        elif self.stage == 1 and self._is_retest(df):
            self.stage = 2
        elif self.stage == 2 and self._is_trap(df):
            self.stage = 3
        elif self.stage == 3 and self._is_shift(df):
            self.stage = 4
        return self.stage

class DiscordMarketTracker:
    def __init__(self):
        # 깃허브 Secrets에서 환경변수로 값을 로드
        self.webhook_url = os.environ.get("DISCORD_WEBHOOK")
        self.user_id = os.environ.get("DISCORD_USER_ID")
        
        # 티커는 요청하신 대로 JSON 형태의 문자열로 환경변수에 저장되어 있다고 가정
        tickers_json = os.environ.get("TICKERS", '["SOXL", "TSLA", "IONQ"]')
        self.tickers = json.loads(tickers_json)
        
        self.trackers = {ticker: MarketBottomTracker() for ticker in self.tickers}
        self.last_stages = {ticker: 0 for ticker in self.tickers}

    def send_discord(self, message):
        content = f"<@{self.user_id}> {message}"
        try:
            if self.webhook_url:
                requests.post(self.webhook_url, json={"content": content})
        except Exception as e:
            print(f"전송 오류: {e}")

    def update_all(self, data_map):
        for ticker in self.tickers:
            if ticker in data_map:
                current_stage = self.trackers[ticker].update(data_map[ticker])
                
                if current_stage != self.last_stages[ticker]:
                    self.last_stages[ticker] = current_stage
                    if current_stage == 4:
                        msg = f"🔥 **[긴급] {ticker} 4단계 도달!** 매수 검토 구간입니다."
                    else:
                        msg = f"📢 **[{ticker}] 단계 변경:** {current_stage}단계 진입"
                    self.send_discord(msg)