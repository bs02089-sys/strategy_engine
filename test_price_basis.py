#!/usr/bin/env python3
"""가격 기준 회귀 테스트 — 2026-09-22 에 실제로 났던 버그를 고정한다 (표준 라이브러리 unittest).

고정 대상 (각 항목이 실제로 프로덕션에서 잘못된 값을 냈던 문제):
  ① yfinance 일봉 마지막 봉 Close=NaN → 하루 낡은 종가를 '최신 확정 종가'로 사용
     (스윙 래더·LOC 매수가 기준이 하루 어긋남) → get_prev_close 의 정규장 분봉 폴백
  ② get_prior_close 가 dropna 로 as_of 세션 행을 지워 '전일 종가'가 한 세션 더 밀림
  ③ LOC --signal(load_data) 이 자체 yf.download+dropna 경로라 같은 함정에 빠짐
     (백테스트는 end 고정이라 재현성을 위해 제외해야 한다)
  ④ 라이브 표시 중 하락률·전일 종가 비교 기준이 섞여 같은 화면이 모순 → _display_dd / 라이브 오버레이
  ⑤ 대시보드 '종가 기준' 옆에 생성 시각(종가 날짜 아님)이 찍힘 → _close_date / 헤더
  ⑥ 알림 판정·메시지는 라이브 값에 흔들리지 않고 확정 종가 기준 → detect_alerts

실행:  python3 -m unittest -q test_price_basis      (네트워크 불필요 — yfinance 는 mock)
"""

import contextlib
import copy
import io
import unittest
from datetime import date, datetime as real_datetime
from unittest import mock
from zoneinfo import ZoneInfo

import pandas as pd

import LOC_DCA_strategy as loc
import swing_alerter as sa

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

    def test_prior_close_skips_nan_session_row(self):
        """② as_of 세션 행(Close=NaN)을 지우지 않고, 그 '앞' 유효 종가를 전일 종가로 쓴다.

        수정 전: 09-21 기준 $71.38(09-17) 로 한 세션 더 밀렸다.
        """
        stub = _FakeTicker(daily=_DAILY_WITH_NAN)
        with _patch_yf(stub), mock.patch.object(loc.time, "sleep", lambda *_: None), _quiet():
            self.assertEqual(sa.get_prior_close("TQQQ", "09-21"), (72.64, "09-18"))
            self.assertEqual(sa.get_prior_close("TQQQ", "09-18"), (71.38, "09-17"))
            self.assertEqual(sa.get_prior_close("TQQQ", "N/A"), (None, None))   # 방어

    def _download_frame(self):
        return pd.DataFrame({"Close": [71.38, 72.64]},
                            index=pd.to_datetime(["2026-09-17", "2026-09-18"]))

    def test_load_data_appends_fresh_close_in_live_mode(self):
        """③ --signal 경로(실시간 모드)도 최신 확정 종가 행을 붙인다.

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
        """③ 백테스트(end 고정)는 보정하지 않는다 — 재현성(+1317.0%/MDD -81.7%) 유지."""
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


class DisplayBasisTests(unittest.TestCase):
    """④ 라이브 표시 기준 통일 (_display_dd / 라이브 오버레이)."""

    @staticmethod
    def _status(close, close_date, live_price):
        """compute_ticker 를 mock 가격으로 돌려 상태 dict 생성 (네트워크 없음)."""
        cfg = copy.deepcopy(sa.DEFAULT_CFG)
        pos: dict = {}
        with mock.patch.object(sa, "get_prev_close", lambda t: (close, close_date)), \
                mock.patch.object(sa, "get_all_time_high", lambda t: (88.09, "2026-06-03")), \
                mock.patch.object(sa, "get_prior_close", lambda t, a: (71.38, "09-17")), \
                mock.patch.object(sa, "_get_live_price", lambda t: live_price):
            return sa.compute_ticker("TQQQ", pos, cfg, live=live_price is not None), cfg, pos

    def test_display_dd_prefers_live_only_when_live(self):
        """④ 표시 하락률은 라이브일 때만 라이브 값 (판정용 dd_pct 는 그대로)."""
        self.assertEqual(sa._display_dd({"dd_pct": -17.54}), -17.54)
        self.assertEqual(
            sa._display_dd({"dd_pct": -17.54, "live": True, "live_dd_pct": -13.33}), -13.33)

    def test_close_date_filters_non_session_values(self):
        """⑤ '종가 기준' 옆에는 MM-DD 세션 날짜만 — 'N/A'(info 폴백)·시각 문자열 배제."""
        self.assertEqual(sa._close_date("09-21"), "09-21")
        self.assertIsNone(sa._close_date("N/A"))
        self.assertIsNone(sa._close_date("09-21 10:12"))
        self.assertIsNone(sa._close_date(None))

    def test_live_overlay_rebases_prior_close_and_keeps_judgement_basis(self):
        """④ 라이브 전일 종가 = 직전 세션(확정 종가)이고, 판정은 종가 기준으로 남는다.

        수정 전(09-21 라이브 $76.35): 전일 종가 $71.38(09-17) ▲7.0% · 판정 하락 -17.5% 로
        같은 화면이 서로 모순 → 수정 후 $72.64(09-18) ▲5.1% · 표시 -13.3% / 판정 -17.5%.
        """
        st, _, _ = self._status(close=72.64, close_date="09-18", live_price=76.35)
        self.assertTrue(st["live"])
        self.assertEqual(st["price"], 76.35)
        self.assertEqual(st["close_price"], 72.64)
        self.assertEqual(st["close_as_of"], "09-18")           # 확정 종가 날짜 보존
        self.assertEqual(st["prior_close"], 72.64)             # 전일 종가 = 직전 세션
        self.assertEqual(st["prior_close_date"], "09-18")
        self.assertAlmostEqual(st["day_change_pct"], (76.35 - 72.64) / 72.64 * 100)
        self.assertAlmostEqual(st["dd_pct"], (72.64 - 88.09) / 88.09 * 100)        # 판정(종가)
        self.assertAlmostEqual(sa._display_dd(st), (76.35 - 88.09) / 88.09 * 100)   # 표시(라이브)
        self.assertGreater(sa._display_dd(st), st["dd_pct"])   # 라이브가 덜 하락 — 섞이면 모순
        # 래더 도달 판정은 종가(72.64 ≤ 74.88) 기준 — 라이브 76.35 였다면 False 가 된다
        self.assertTrue(next(l for l in st["ladder"] if l["pct"] == 15)["hit"])
        # 다음 구간 남은 %p 도 표시 기준(라이브)으로 계산한다 — 종가 기준이면 2.5%p 로 모순
        nxt_pct = next(l["pct"] for l in st["ladder"] if not l["hit"] and not l["bought"])
        summary = sa._ladder_summary(st)
        self.assertIn(f"{nxt_pct - abs(sa._display_dd(st)):.1f}%p", summary)
        self.assertNotIn(f"{nxt_pct - abs(st['dd_pct']):.1f}%p", summary)

    def test_dashboard_header_shows_close_session_date(self):
        """⑤ 헤더: 업데이트는 KST, '종가 기준' 옆은 표시 가격의 종가 날짜(생성 시각 아님)."""
        st_close, cfg, _ = self._status(close=72.64, close_date="09-18", live_price=None)
        html = sa.render_dashboard([st_close], cfg, updated_at="2026-09-22 09:20 KST")
        self.assertIn("업데이트 2026-09-22 09:20 KST", html)
        self.assertIn("종가 기준 09-18 (미국 ET)", html)

        st_live, cfg, _ = self._status(close=72.64, close_date="09-18", live_price=76.35)
        html_live = sa.render_dashboard([st_live], cfg, updated_at="2026-09-22 09:20 KST")
        self.assertIn("실시간(15분 지연) 기준", html_live)
        self.assertIn("전일 종가 $72.64 (09-18)", html_live)   # 라이브 세션 직전 세션 + 날짜

        st_na, cfg, _ = self._status(close=72.64, close_date="N/A", live_price=None)
        self.assertIn("날짜 미확인", sa.render_dashboard([st_na], cfg, updated_at="x KST"))


class AlertJudgementTests(unittest.TestCase):
    """⑥ 알림 판정·메시지는 라이브 값에 흔들리지 않고 확정 종가 기준 (AGENTS 계약)."""

    def test_alerts_judge_on_confirmed_close_not_live_price(self):
        cfg = copy.deepcopy(sa.DEFAULT_CFG)
        pos: dict = {}
        with mock.patch.object(sa, "get_prev_close", lambda t: (74.00, "09-18")), \
                mock.patch.object(sa, "get_all_time_high", lambda t: (88.09, "2026-06-03")), \
                mock.patch.object(sa, "get_prior_close", lambda t, a: (71.38, "09-17")), \
                mock.patch.object(sa, "_get_live_price", lambda t: 76.35):
            st = sa.compute_ticker("TQQQ", pos, cfg, live=True)

        msgs, zone_msgs = sa.detect_alerts(st, pos, cfg)

        # 종가 74.00 은 -15% 구간(74.88) 이하 → 도달. 라이브 76.35 로 판정하면 알림이 사라진다.
        # 새 사이클 스냅샷이므로 -5/-10 구간도 함께 '도달'로 기록된다 (문서화된 동작).
        self.assertEqual(pos["ZONE_ALERTS"]["hit"], [5, 10, 15])
        self.assertEqual(pos["ZONE_ALERTS"]["imminent"], [20])
        hit_msg = next(m for m in msgs if "-15% 매수 구간 도달" in m)
        self.assertIn("$74.00", hit_msg)          # 메시지의 현재가도 종가 기준 (라이브 아님)
        self.assertIn("매수 구간 임박", "\n".join(msgs))
        # 매수 구간 푸시 전용 메시지 = 도달 + 임박 (Discord 전체 메시지와 별개 집합)
        self.assertEqual(len(zone_msgs),
                         len(pos["ZONE_ALERTS"]["hit"]) + len(pos["ZONE_ALERTS"]["imminent"]))
        self.assertEqual(pos["ATH_CYCLE_BASE"], 88.09)   # 첫 실행 — 재설정 기준 기록


if __name__ == "__main__":
    unittest.main(verbosity=2)
