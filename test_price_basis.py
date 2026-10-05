#!/usr/bin/env python3
"""가격 기준 회귀 테스트 — 2026-09-22 에 실제로 났던 버그를 고정한다 (표준 라이브러리 unittest).

고정 대상 (실제로 프로덕션에서 잘못된 값을 냈던 문제):
  ① yfinance 일봉 마지막 봉 Close=NaN → 하루 낡은 종가를 '최신 확정 종가'로 사용
     (LOC 매수가 기준이 하루 어긋남) → get_prev_close 의 정규장 분봉 폴백
  ② LOC --signal(load_data) 이 자체 yf.download+dropna 경로라 같은 함정에 빠짐
     (백테스트는 end 고정이라 재현성을 위해 제외해야 한다)

실행:  python3 -m unittest -q test_price_basis      (네트워크 불필요 — yfinance 는 mock)
"""

import contextlib
import io
import unittest
from datetime import date, datetime as real_datetime
from unittest import mock
from zoneinfo import ZoneInfo

import pandas as pd

import LOC_DCA_strategy as loc

NY = ZoneInfo("America/New_York")

# 09-21(월) 일봉 봉은 아직 확정 전(Close=NaN) — 실제 09-22 아침 yfinance 상태
_DAILY_WITH_NAN = pd.DataFrame(
    {"Close": [71.38, 72.64, float("nan")]},
    index=pd.to_datetime(["2026-09-17", "2026-09-18", "2026-09-21"]),
)
_DAILY_OK = pd.DataFrame(
    {"Close": [71.38, 72.64, 78.92]},
    index=pd.to_datetime(["2026-09-17", "2026-09-18", "2026-09-21"]),
)
_INTRADAY = pd.DataFrame(
    {"Close": [78.90, 78.92]},
    index=pd.to_datetime(["2026-09-21 15:58", "2026-09-21 15:59"]),
)


class _FakeTicker:
    """yfinance Ticker 대체 — interval 별 프레임 반환 + 호출 기록 (info 는 준비된 경우에만)."""

    def __init__(self, daily=None, intraday=None, info=None):
        self.daily, self.intraday, self.info_data = daily, intraday, info
        self.intervals: list[str] = []

    def history(self, period=None, interval="1d", **kw):  # noqa: ARG002
        self.intervals.append(interval)
        if interval == "1d":
            if self.daily is None:
                raise AssertionError("일봉 프레임을 준비하지 않았다")
            return self.daily.copy()
        if self.intraday is None:
            raise AssertionError("분봉 폴백이 불려서는 안 되는 케이스다")
        return self.intraday.copy()

    @property
    def info(self):
        if self.info_data is None:
            # info 폴백(previousClose)은 같은 낡은 값을 돌려주므로 여기로 오면 회귀다
            raise AssertionError("info 폴백을 쓰면 안 되는 케이스다")
        return self.info_data


class _FakeDateTime:
    """loc.datetime 대체 — now()/combine() 만 고정 시각으로 제공 (테스트 재현성).

    접근 가능한 속성을 일부러 최소로 둔다 — get_prev_close 가 다른 datetime 기능을
    새로 쓰기 시작하면 AttributeError 로 바로 드러난다.
    """

    fixed_ny = real_datetime(2026, 9, 21, 20, 0, tzinfo=NY)   # 월요일 정규장 마감(16:15 ET) 이후

    @classmethod
    def now(cls, tz=None):
        return cls.fixed_ny.astimezone(tz) if tz is not None else cls.fixed_ny

    @staticmethod
    def combine(d, t, tzinfo=None):
        return real_datetime.combine(d, t, tzinfo=tzinfo)


def _fixed_now(y, m, d, hh, mm):
    """주어진 NY 시각으로 고정한 datetime 대체 클래스 반환."""
    class _At(_FakeDateTime):
        fixed_ny = real_datetime(y, m, d, hh, mm, tzinfo=NY)
    return _At


@contextlib.contextmanager
def _quiet():
    """조회 함수의 진행 로그(stdout)를 삼켜 테스트 출력을 깨끗하게 유지."""
    with contextlib.redirect_stdout(io.StringIO()):
        yield


def _patch_yf(stub):
    return mock.patch.object(loc.yf, "Ticker", lambda *a, **k: stub)  # noqa: ARG005


class PriceFallbackTests(unittest.TestCase):
    """①③ 일봉 미확정 → 정규장 분봉 폴백 (get_prev_close / load_data)."""

    def test_daily_nan_uses_intraday_fallback(self):
        """일봉 마지막 봉 Close=NaN 이면 분봉 종가를 그 세션 확정 종가로 쓴다.

        수정 전: 09-18 $72.64 (하루 낡음) / 수정 후: 09-21 $78.92
        """
        stub = _FakeTicker(daily=_DAILY_WITH_NAN, intraday=_INTRADAY)
        with _patch_yf(stub), mock.patch.object(loc, "datetime", _FakeDateTime), \
                mock.patch.object(loc.time, "sleep", lambda *_: None), _quiet():
            close, date_str = loc.get_prev_close("TQQQ")
        self.assertEqual(close, 78.92)
        self.assertEqual(date_str, "09-21")
        self.assertIn("1m", stub.intervals)   # 분봉 폴백을 실제로 사용

    def test_daily_ok_skips_intraday(self):
        """일봉이 최신이면 분봉을 부르지 않는다 (불필요한 호출·오버헤드 방지)."""
        stub = _FakeTicker(daily=_DAILY_OK)
        with _patch_yf(stub), mock.patch.object(loc, "datetime", _FakeDateTime), _quiet():
            close, date_str = loc.get_prev_close("TQQQ")
        self.assertEqual((close, date_str), (78.92, "09-21"))
        self.assertNotIn("1m", stub.intervals)

    def test_stale_intraday_falls_back_to_info(self):
        """분봉도 기대 세션보다 낡으면(휴장일 오탐 등) info 폴백으로 넘어간다 — 오탐 가격 금지."""
        stub = _FakeTicker(daily=_DAILY_OK, intraday=_INTRADAY, info={"previousClose": 72.64})
        now_0922 = _fixed_now(2026, 9, 22, 20, 0)   # 기대 세션 = 09-22 > 데이터(09-21)
        with _patch_yf(stub), mock.patch.object(loc, "datetime", now_0922), \
                mock.patch.object(loc.time, "sleep", lambda *_: None), _quiet():
            close, date_str = loc.get_prev_close("TQQQ")
        self.assertEqual((close, date_str), (72.64, "N/A"))

    def _download_frame(self):
        return pd.DataFrame({"Close": [71.38, 72.64]},
                            index=pd.to_datetime(["2026-09-17", "2026-09-18"]))

    def test_load_data_appends_fresh_close_in_live_mode(self):
        """② --signal 경로(실시간 모드)도 최신 확정 종가 행을 붙인다.

        수정 전: 기준일 09-18 · 전일 $71.38 · LOC $66.99 / 수정 후: 09-21 · $72.64 · $68.17
        """
        fresh = mock.Mock(return_value=(78.92, date(2026, 9, 21)))
        now_0922 = _fixed_now(2026, 9, 22, 9, 0)
        with mock.patch.object(loc.yf, "download", lambda *a, **k: self._download_frame()), \
                mock.patch.object(loc, "_intraday_last_close", fresh), \
                mock.patch.object(loc, "datetime", now_0922), _quiet():
            df = loc.load_data("TQQQ")
        self.assertEqual(df.index[-1].date(), date(2026, 9, 21))
        self.assertEqual(float(df["Close"].iloc[-1]), 78.92)
        self.assertEqual(float(df["Close"].iloc[-2]), 72.64)   # 전일 종가로 쓰이는 행
        fresh.assert_called_once()

    def test_load_data_backtest_keeps_frame_untouched(self):
        """② 백테스트(end 고정)는 보정하지 않는다 — 재현성(+1317.0%/MDD -81.7%) 유지."""
        frame = pd.DataFrame({"Close": [70.0, 71.0, 72.0]},
                             index=pd.to_datetime(["2026-07-29", "2026-07-30", "2026-07-31"]))
        fresh = mock.Mock(return_value=(78.92, date(2026, 9, 21)))
        with mock.patch.object(loc.yf, "download", lambda *a, **k: frame), \
                mock.patch.object(loc, "_intraday_last_close", fresh), _quiet():
            df = loc.load_data("TQQQ", end=date(2026, 8, 2))
        self.assertEqual(len(df), 3)
        self.assertEqual(df.index[-1].date(), date(2026, 7, 31))
        self.assertEqual(float(df["Close"].iloc[-1]), 72.0)
        fresh.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
