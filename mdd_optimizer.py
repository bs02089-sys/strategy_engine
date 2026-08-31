import os
import json
import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime
from typing import Dict, Tuple, List, Optional


class MDDOptimizer:
    def __init__(self, config_path: str = "mdd_config.json"):
        self.config_path = config_path
        self.config = self._load_config()
        self.tickers = list(self.config.get("tickers", {}).keys())
        self.default_levels = self.config.get("default_levels", {
            "strong_bull": [-15, -25, -40],
            "normal": [-20, -32, -48],
            "deep_correction": [-25, -38, -55],
            "extreme": [-30, -45, -60]
        })
        self.hard_limits = self.config.get("hard_limits", {})
        self.default_hard_limit = self.config.get("default_hard_limit", -70.0)

    def _load_config(self) -> dict:
        if not os.path.exists(self.config_path):
            raise FileNotFoundError(f"설정 파일이 없습니다: {self.config_path}")
        with open(self.config_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def get_data(self, ticker: str, period: str = "max", retries: int = 3, delay: float = 3.0) -> Optional[pd.DataFrame]:
        import time
        for attempt in range(1, retries + 1):
            try:
                df = yf.download(ticker, period=period, interval="1d", progress=False, auto_adjust=True)
                if df is None or df.empty:
                    raise ValueError("빈 데이터 반환됨 (rate limit 가능성)")
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                df.columns = [str(col).lower() for col in df.columns]
<<<<<<< HEAD
                # 아직 장 마감 전인 오늘 날짜 등, close가 비어있는 트레일링 행 제거
                df = df.dropna(subset=["close"])
=======
                # 아직 장 마감 전인 오늘 날짜 등, close/high가 비어있는 트레일링 행 제거
                df = df.dropna(subset=["close", "high"])
>>>>>>> 41f4012 (수정)
                if df.empty:
                    raise ValueError("유효한 종가 데이터 없음")
                return df
            except Exception as e:
                print(f"⚠️ {ticker} 시도 {attempt}/{retries} 실패: {e}")
                if attempt < retries:
                    time.sleep(delay)
        print(f"❌ {ticker} 데이터 다운로드 최종 실패")
        return None

    def calculate_indicators(self, df: pd.DataFrame) -> Dict:
        close = df["close"]
        high = df["high"]

        # ATH는 종가가 아니라 일중 고가(High) 기준 — 52주 최고가/실제 ATH와 일치시킴
        ath = high.cummax().iloc[-1]
        current_price = close.iloc[-1]
        current_dd = (current_price / ath - 1) * 100

        returns = close.pct_change().dropna()
        volatility = returns.tail(20).std() * np.sqrt(252)

        ma5 = close.rolling(5).mean().iloc[-1]
        ma20 = close.rolling(20).mean().iloc[-1]
        ma60 = close.rolling(60).mean().iloc[-1]

        trend_score = 0
        if ma5 > ma20:
            trend_score += 1
        if ma20 > ma60:
            trend_score += 1
        if current_price > ma20:
            trend_score += 1

        return {
            "current_price": float(current_price),
            "ath": float(ath),
            "current_dd": float(current_dd),
            "volatility": float(volatility),
            "trend_score": int(trend_score)
        }

    def detect_regime(self, indicators: Dict) -> str:
        dd = indicators["current_dd"]
        vol = indicators["volatility"]
        trend = indicators["trend_score"]

        if dd > -12 and vol < 0.28 and trend >= 2:
            return "strong_bull"
        elif dd > -28 and vol < 0.40:
            return "normal"
        elif dd > -45:
            return "deep_correction"
        else:
            return "extreme"

    def get_levels_for_ticker(self, ticker: str, regime: str) -> Tuple[float, float, float]:
        ticker_config = self.config.get("tickers", {}).get(ticker, {})
        levels = ticker_config.get(regime) or self.default_levels.get(regime)
        return tuple(float(x) for x in levels)

    def get_hard_limit(self, ticker: str) -> float:
        return float(self.hard_limits.get(ticker, self.default_hard_limit))

    def adjust_levels(self, ticker: str, levels: Tuple[float, float, float], current_dd: float) -> Tuple[float, float, float]:
        hard_limit = self.get_hard_limit(ticker)
        adjusted = list(levels)

        if current_dd <= adjusted[0] - 5:
            adjusted[0] = current_dd - 4
            adjusted[1] = current_dd - 12
            adjusted[2] = current_dd - 22

        for i in range(3):
            if current_dd <= adjusted[i] + 3:
                new_level = min(adjusted[i], current_dd - 3)
                adjusted[i] = max(new_level, adjusted[i] - 5)

        adjusted = [max(level, hard_limit) for level in adjusted]

        for i in range(1, 3):
            if adjusted[i] > adjusted[i-1] - 8:
                adjusted[i] = adjusted[i-1] - 10
            if adjusted[i] < adjusted[i-1] - 25:
                adjusted[i] = adjusted[i-1] - 20

        adjusted = [max(level, hard_limit) for level in adjusted]

        for i in range(1, 3):
            if adjusted[i-1] > hard_limit and adjusted[i] >= adjusted[i-1]:
                adjusted[i] = max(adjusted[i-1] - 1, hard_limit)

        adjusted = [round(level, 1) for level in adjusted]
        return tuple(adjusted)

    def generate_signals(self, levels: Tuple, indicators: Dict, tolerance: float = 4.0) -> List[Dict]:
        current_dd = indicators["current_dd"]
        ath = indicators["ath"]
        signals = []

        for i, level in enumerate(levels):
            if level >= current_dd >= (level - tolerance):
                target_price = ath * (1 + level / 100)
                signals.append({
                    "차수": i + 1,
                    "목표_MDD": level,
                    "목표_가격": round(target_price, 2),
                    "현재_DD": round(current_dd, 2),
                    "비중": "33.3%"
                })

        return signals

    def get_live_price(self, ticker: str) -> Optional[float]:
        """
        일봉 종가는 마지막 완결된 거래일 기준이라 장중에는 실제 시세와
        괴리가 생길 수 있음. 가능하면 실시간(지연) 시세로 덮어씀.
        실패하면 None을 반환해 일봉 종가를 그대로 사용하게 함.
        """
        try:
            fast_info = yf.Ticker(ticker).fast_info
            price = fast_info.get("lastPrice") or fast_info.get("last_price")
            return float(price) if price else None
        except Exception as e:
            print(f"⚠️ {ticker} 실시간 시세 조회 실패, 일봉 종가로 대체: {e}")
            return None

    def analyze(self, ticker: str) -> Dict:
        df = self.get_data(ticker)
        if df is None or len(df) < 60:
            return {"error": f"{ticker} 데이터 부족"}

        try:
            indicators = self.calculate_indicators(df)

            # 가능하면 현재가를 실시간 시세로 교체하고 DD도 재계산
            live_price = self.get_live_price(ticker)
            if live_price is not None:
                indicators["current_price"] = live_price
                indicators["current_dd"] = (live_price / indicators["ath"] - 1) * 100

            regime = self.detect_regime(indicators)
            base_levels = self.get_levels_for_ticker(ticker, regime)
            levels = self.adjust_levels(ticker, base_levels, indicators["current_dd"])
            signals = self.generate_signals(levels, indicators)
        except Exception as e:
            return {"error": f"{ticker} 지표/레벨 계산 실패: {e}"}

        return {
            "ticker": ticker,
            "regime": regime,
            "levels": levels,
            "indicators": {
                "현재가": round(indicators["current_price"], 2),
                "ATH": round(indicators["ath"], 2),
                "현재_DD": f"{indicators['current_dd']:.2f}%",
                "변동성": f"{indicators['volatility']*100:.1f}%",
                "추세점수": indicators["trend_score"]
            },
            "signals": signals
        }

    def run(self):
        print("=" * 65)
        print(f"📊 장세 기반 MDD 3차 분할 최적화 시스템")
        print(f"실행 시간: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"분석 종목: {', '.join(self.tickers)}")
        print("=" * 65)

        results = {}
        for ticker in self.tickers:
            result = self.analyze(ticker)
            results[ticker] = result

            if "error" in result:
                print(f"\n❌ {ticker}: {result['error']}")
                continue

            print(f"\n🔹 {ticker}")
            print(f"   장세      : {result['regime']}")
            print(f"   최적 MDD  : {result['levels'][0]}% / {result['levels'][1]}% / {result['levels'][2]}%")
            print(f"   현재가    : ${result['indicators']['현재가']}")
            print(f"   ATH       : ${result['indicators']['ATH']}")
            print(f"   현재 DD   : {result['indicators']['현재_DD']}")
            print(f"   변동성    : {result['indicators']['변동성']}")

            if result["signals"]:
                print("   🔥 매수 신호:")
                for sig in result["signals"]:
                    print(f"      → {sig['차수']}차 | MDD {sig['목표_MDD']}% | 목표가 ${sig['목표_가격']}")
            else:
                print("   매수 신호 없음")

        print("\n" + "=" * 65)
        return results


if __name__ == "__main__":
    optimizer = MDDOptimizer(config_path="mdd_config.json")
    optimizer.run()