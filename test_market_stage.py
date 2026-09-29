#!/usr/bin/env python3
"""시장 단계 트래커 규칙 회귀 테스트 — 2026-09-29 재설계를 고정한다 (표준 라이브러리 unittest).

왜 필요한가 (재설계 전 실제 문제):
  ① 하단 1단계 조건이 '최근 5봉 순하락 + 좁은 변동폭'이라 발동률 37% — 사실상 기본 상태
  ② 단계는 오르기만 하고 만료가 5단계 30일뿐이라, 한 번 켜지면 영구 고정
     (하단 1단계 이상 97%, 상단 1단계 이상 92%, 상단 4단계는 평균 8~15개월·최장 2.4년 체류)
  ③ 그래서 리포트가 '바닥 1단계 + 천장 1단계'처럼 모순되거나 낡은 라벨을 보여줬다

고정 대상:
  · 하단 1단계 = 6개월 고점 대비 낙폭 <= -10% AND RSI(14) < 35   (_is_exhaustion)
  · 구 조건이 True 였던 '조용한 하락'은 이제 False — 같은 버그가 조용히 되돌아오는 것을 차단
  · 만료는 '현재 단계 진입일(stage_entered_date) 기준 N봉 무진전' — 하단 1~2단계 30봉, 상단 4단계 120봉
    (하단 3단계·상단 5단계는 만료 제외 — 4단계는 20일 고점 돌파를 오래 기다려야 해서 래더가 죽는다)
  · 진입일(bottom/top_stage_entered_date)이 상태 파일에 저장·복원된다 — 저장 안 되면 만료가 영영 안 걸린다

실행:  python3 -m unittest -q test_market_stage      (네트워크 불필요 — yfinance 미사용)
"""

import json
import os
import tempfile
import unittest
from unittest import mock

import pandas as pd

import MarketStageSystem as ms


def _make_df(closes, start="2025-01-01", volume=1_000_000):
    idx = pd.date_range(start, periods=len(closes), freq="D")
    return pd.DataFrame({"close": closes, "volume": [volume] * len(closes)}, index=idx)


def _d(df, i):
    return df.index[i].strftime("%Y-%m-%d")


# 단조 하락 120봉(120→1) → 낙폭 -99%, RSI 0 (avg_gain=0)
_DEEP_DECLINE = _make_df(list(range(120, 0, -1)))

# 하락 후 반등 → 낙폭 -17% 유지 + RSI 는 35 위 (2026-09-28 TQQQ 실측과 같은 모양)
_DECLINE_THEN_RALLY = _make_df(list(range(120, 70, -1)) + list(range(70, 101)))

# 구 조건(5봉 순하락 + 좁은 변동폭)이 True 였던 '조용한 하락' — 낙폭은 얕다
_QUIET_DRIFT = _make_df([100 + (1.5 if i % 2 else -1.5) for i in range(115)] + [100, 99.9, 99.8, 99.7, 99.6])

# 단조 상승 200봉 — 만료 테스트용 (하단 조건·매도 신호 모두 False 가 되도록 거래량은 평탄)
# 200봉: 상단 4단계 만료(120봉)를 검증하려면 그보다 긴 이력이 필요하다
_RISING = _make_df([100 + i * 0.5 for i in range(200)])


def _old_rule_true(df, threshold):
    """재설계 전 _is_exhaustion 구현 — 회귀 확인용."""
    last5 = df['close'].tail(5)
    change = last5.iloc[-1] - last5.iloc[0]
    range_ratio = (last5.max() - last5.min()) / last5.mean()
    return bool(change <= 0 and range_ratio <= threshold)


class BottomStage1RuleTest(unittest.TestCase):
    def test_deep_drawdown_and_oversold_triggers(self):
        tr = ms.MarketBottomTracker(stage=0)
        self.assertTrue(tr._is_exhaustion(_DEEP_DECLINE))
        self.assertEqual(tr.update(_DEEP_DECLINE), 1)
        self.assertEqual(tr.stage_entered_date, _d(_DEEP_DECLINE, -1))

    def test_drawdown_without_oversold_does_not_trigger(self):
        """2026-09-28 TQQQ 실측 모양: 낙폭 -11%지만 RSI 57 — 발동하면 안 된다."""
        tr = ms.MarketBottomTracker(stage=0)
        close = _DECLINE_THEN_RALLY['close']
        self.assertLessEqual(close.iloc[-1] / close.max() - 1, -ms.BOTTOM_DRAWDOWN_PCT)
        self.assertGreater(ms.calculate_rsi(close).dropna().iloc[-1], ms.BOTTOM_RSI_MAX)
        self.assertFalse(tr._is_exhaustion(_DECLINE_THEN_RALLY))
        self.assertEqual(tr.update(_DECLINE_THEN_RALLY), 0)

    def test_quiet_drift_no_longer_triggers(self):
        """구 조건은 True, 현행은 False — '5봉 순하락 + 좁은 변동폭'으로 되돌아가면 실패한다."""
        self.assertTrue(_old_rule_true(_QUIET_DRIFT, 0.16), "픽스처가 구 조건을 만족하지 않음")
        self.assertFalse(ms.MarketBottomTracker(stage=0)._is_exhaustion(_QUIET_DRIFT))

    def test_oversold_without_drawdown_does_not_trigger(self):
        """RSI 만 낮고 6개월 고점 근처면 바닥 국면이 아니다."""
        closes = [100 + (0.5 if i % 2 else -0.5) for i in range(118)]  # 고점 근처 횡보
        closes[-5:] = [100.0, 99.6, 99.2, 98.8, 98.4]
        df = _make_df(closes)
        close = df['close']
        self.assertGreater(close.iloc[-1] / close.max() - 1, -ms.BOTTOM_DRAWDOWN_PCT)
        self.assertFalse(ms.MarketBottomTracker(stage=0)._is_exhaustion(df))


class ExpiryTest(unittest.TestCase):
    """만료 = '현재 단계 진입 후 N봉 무진전' (하단 1~2단계 30봉 / 상단 4단계 120봉)."""

    def test_bottom_expires_after_threshold_bars(self):
        tr = ms.MarketBottomTracker(stage=1, stage_entered_date=_d(_RISING, -1 - ms.BOTTOM_EXPIRY_BARS))
        self.assertEqual(tr.update(_RISING), 0)
        self.assertIsNone(tr.stage_entered_date)

    def test_bottom_alive_just_before_threshold(self):
        entry = _d(_RISING, -ms.BOTTOM_EXPIRY_BARS)
        tr = ms.MarketBottomTracker(stage=1, stage_entered_date=entry)
        self.assertEqual(tr.update(_RISING), 1)
        self.assertEqual(tr.stage_entered_date, entry)

    def test_bottom_clock_resets_on_advance(self):
        """1단계에서 오래 머물렀어도 2단계로 올라간 뒤면 30봉을 새로 센다 (무진전 = 진전 없음)."""
        tr = ms.MarketBottomTracker(stage=2, stage_entered_date=_d(_RISING, -ms.BOTTOM_EXPIRY_BARS))
        self.assertEqual(tr.update(_RISING), 2)

    def test_bottom_stage3_is_never_expired(self):
        """3단계는 4단계(20일 고점 돌파)를 기다려야 하므로 만료 제외 — 걸면 래더가 완주 못 한다."""
        tr = ms.MarketBottomTracker(stage=3, stage_entered_date=_d(_RISING, 0))
        self.assertEqual(tr.update(_RISING), 3)

    def test_legacy_state_without_entry_date_starts_clock_now(self):
        """날짜 없는 구버전 상태 파일 → 즉시 리셋하지 않고 지금부터 기산한다."""
        tr = ms.MarketBottomTracker(stage=1)
        self.assertEqual(tr.update(_RISING), 1)
        self.assertEqual(tr.stage_entered_date, _d(_RISING, -1))

    def test_top_stage4_expires_after_threshold_bars(self):
        tr = ms.MarketTopTracker(stage=4, stage_entered_date=_d(_RISING, -1 - ms.TOP_EXPIRY_BARS))
        self.assertEqual(tr.update(_RISING), 0)
        self.assertIsNone(tr.stage_entered_date)

    def test_top_stage4_alive_just_before_threshold(self):
        entry = _d(_RISING, -ms.TOP_EXPIRY_BARS)
        tr = ms.MarketTopTracker(stage=4, stage_entered_date=entry)
        self.assertEqual(tr.update(_RISING), 4)
        self.assertEqual(tr.stage_entered_date, entry)

    def test_top_stage5_is_expired_only_by_its_own_reset(self):
        """5단계는 30일 리셋(STAGE5_RESET_DAYS)만 적용 — 4단계 만료 규칙에 걸리면 안 된다."""
        tr = ms.MarketTopTracker(
            stage=5,
            stage_entered_date=_d(_RISING, 0),
            stage5_entered_date=_d(_RISING, -1),
        )
        self.assertEqual(tr.update(_RISING), 5)

    def test_top_stage3_is_not_expired(self):
        tr = ms.MarketTopTracker(stage=3, stage_entered_date=_d(_RISING, 0))
        self.assertEqual(tr.update(_RISING), 3)


class StateSchemaTest(unittest.TestCase):
    def test_entry_date_round_trip(self):
        """진입일이 저장되지 않으면 만료가 영영 걸리지 않는다."""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"TQQQ": {"bottom": 1, "bottom_stage_entered_date": "2026-09-01",
                                    "top": 4, "top_stage_entered_date": "2026-09-02"}}, f)
            with mock.patch.object(ms, "STATE_PATH", path):
                tracker = ms.DiscordMarketTracker()
                self.assertEqual(tracker.bottom_trackers["TQQQ"].stage_entered_date, "2026-09-01")
                self.assertEqual(tracker.top_trackers["TQQQ"].stage_entered_date, "2026-09-02")
                tracker._save_state()
            with open(path, encoding="utf-8") as f:
                saved = json.load(f)["TQQQ"]
            self.assertEqual(saved["bottom_stage_entered_date"], "2026-09-01")
            self.assertEqual(saved["top_stage_entered_date"], "2026-09-02")


if __name__ == "__main__":
    unittest.main()
