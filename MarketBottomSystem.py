import os
import requests
import pandas as pd
import json
import yfinance as yf

class MarketBottomTracker:
    """개별 티커의 시장 단계(Stage)를 추적하는 엔진"""
    def __init__(self):
        self.stage = 0 

    def _is_exhaustion(self, df):
        # 5일 이동합이 음수이고 최근 5일간 종가 변화가 적을 때
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

    def update(self, df):
        """현재 stage에 따라 다음 단계로 이동 가능한지 판별"""
        logic = {
            0: self._is_exhaustion,
            1: self._is_retest,
            2: self._is_trap,
            3: self._is_shift
        }
        
        if self.stage in logic and logic[self.stage](df):
            self.stage += 1
        return self.stage

class DiscordMarketTracker:
    """데이터 수집 및 디스코드 알림 관리"""
    def __init__(self):
        self.webhook_url = os.environ.get("DISCORD_WEBHOOK")
        self.user_id = os.environ.get("DISCORD_USER_ID")
        self.tickers = json.loads(os.environ.get("TICKERS", '["SOXL", "TSLA", "IONQ"]'))
        
        self.trackers = {ticker: MarketBottomTracker() for ticker in self.tickers}
        self.last_stages = {ticker: 0 for ticker in self.tickers}

    def _send_discord(self, message):
        if not self.webhook_url: return
        try:
            requests.post(self.webhook_url, json={"content": f"<@{self.user_id}> {message}"})
        except Exception as e:
            print(f"전송 오류: {e}")

    def update_all(self, data_map):
        # 1. 메시지 리스트 생성
        report_msg = "📊 **[현재 시장 단계 리포트]**\n"
        
        for ticker, df in data_map.items():
            # 현재 단계 업데이트
            current_stage = self.trackers[ticker].update(df)
            
            # 단계별 메시지 설정
            if current_stage == 4:
                report_msg += f"🔥 **{ticker}**: 4단계 (매수 검토 구간!)\n"
            else:
                report_msg += f"• **{ticker}**: {current_stage}단계\n"
        
        # 2. 메시지 전송 (한 번에 모아서 전송)
        self._send_discord(report_msg)

def get_data(ticker):
    """yfinance 데이터 수집 및 전처리"""
    df = yf.download(ticker, period="100d", interval="1d")
    # MultiIndex 대응 및 소문자 변환
    if isinstance(df.columns, pd.MultiIndex): # type: ignore
        df.columns = df.columns.get_level_values(0) # type: ignore
    df.columns = df.columns.str.lower() # type: ignore
    return df

if __name__ == "__main__":
    tracker = DiscordMarketTracker()
    data_map = {}
    
    for ticker in tracker.tickers:
        try:
            print(f"데이터 수집 중: {ticker}")
            data_map[ticker] = get_data(ticker)
        except Exception as e:
            print(f"{ticker} 데이터 수집 실패: {e}")
            
    tracker.update_all(data_map)
    print("시장 모니터링 시스템 실행 완료.")