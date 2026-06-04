from __future__ import annotations

import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from analytics import (
    AnalysisResult,
    CandleComparison,
    build_auto_notification_report,
    build_empty_auto_notification_report,
    build_manual_report,
    format_failures,
    format_period,
)
from moex_client import Candle, MoexClient, candle_key
from user_settings import UserSettingsStore


MOSCOW = ZoneInfo("Europe/Moscow")
TESTS_DIR = Path(__file__).resolve().parent


def moscow_datetime(
    year: int,
    month: int,
    day: int,
    hour: int = 0,
    minute: int = 0,
    second: int = 0,
) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=MOSCOW)


def candle(
    ticker: str,
    begin: datetime,
    *,
    end: datetime | None = None,
    high: float = 100,
    close: float = 101,
    value: float = 1_000_000,
) -> Candle:
    return Candle(
        ticker=ticker,
        begin=begin,
        end=end or begin,
        open=99,
        high=high,
        low=98,
        close=close,
        volume=10,
        value=value,
    )


class MemoryUserSettingsStore(UserSettingsStore):
    def _load(self) -> None:
        self._data = {"users": {}}

    def _save(self) -> None:
        pass


class ClosedCandleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = MoexClient(timezone=MOSCOW)

    def test_10m_candle_closes_only_at_next_boundary(self) -> None:
        previous = candle(
            "SBER",
            moscow_datetime(2026, 6, 4, 16, 0),
            end=moscow_datetime(2026, 6, 4, 16, 9, 59),
        )
        forming = candle(
            "SBER",
            moscow_datetime(2026, 6, 4, 16, 10),
            end=moscow_datetime(2026, 6, 4, 16, 17),
        )

        before_close = self.client._closed_candles(
            [forming, previous],
            "10m",
            now=moscow_datetime(2026, 6, 4, 16, 19, 59),
        )
        at_close = self.client._closed_candles(
            [forming, previous],
            "10m",
            now=moscow_datetime(2026, 6, 4, 16, 20),
        )

        self.assertEqual([item.begin for item in before_close], [previous.begin])
        self.assertEqual([item.begin for item in at_close], [previous.begin, forming.begin])

    def test_intraday_candles_close_at_exact_boundaries(self) -> None:
        minute = candle("SBER", moscow_datetime(2026, 6, 4, 14, 10))
        hour = candle("SBER", moscow_datetime(2026, 6, 4, 14, 0))

        self.assertEqual(
            self.client._closed_candles(
                [minute],
                "1m",
                now=moscow_datetime(2026, 6, 4, 14, 10, 59),
            ),
            [],
        )
        self.assertEqual(
            self.client._closed_candles(
                [minute],
                "1m",
                now=moscow_datetime(2026, 6, 4, 14, 11),
            ),
            [minute],
        )
        self.assertEqual(
            self.client._closed_candles(
                [hour],
                "1h",
                now=moscow_datetime(2026, 6, 4, 14, 59, 59),
            ),
            [],
        )
        self.assertEqual(
            self.client._closed_candles(
                [hour],
                "1h",
                now=moscow_datetime(2026, 6, 4, 15, 0),
            ),
            [hour],
        )

    def test_calendar_candles_close_at_next_period(self) -> None:
        daily = candle("SBER", moscow_datetime(2026, 6, 4))
        weekly = candle("SBER", moscow_datetime(2026, 6, 1))
        monthly = candle("SBER", moscow_datetime(2026, 6, 1))

        self.assertEqual(
            self.client._closed_candles(
                [daily],
                "1d",
                now=moscow_datetime(2026, 6, 4, 23, 59, 59),
            ),
            [],
        )
        self.assertEqual(
            self.client._closed_candles(
                [daily],
                "1d",
                now=moscow_datetime(2026, 6, 5),
            ),
            [daily],
        )
        self.assertEqual(
            self.client._closed_candles(
                [weekly],
                "1w",
                now=moscow_datetime(2026, 6, 7, 23, 59, 59),
            ),
            [],
        )
        self.assertEqual(
            self.client._closed_candles(
                [weekly],
                "1w",
                now=moscow_datetime(2026, 6, 8),
            ),
            [weekly],
        )
        self.assertEqual(
            self.client._closed_candles(
                [monthly],
                "1mo",
                now=moscow_datetime(2026, 6, 30, 23, 59, 59),
            ),
            [],
        )
        self.assertEqual(
            self.client._closed_candles(
                [monthly],
                "1mo",
                now=moscow_datetime(2026, 7, 1),
            ),
            [monthly],
        )

    def test_candle_keys_and_display_periods_use_begin(self) -> None:
        intraday = candle(
            "SBER",
            moscow_datetime(2026, 6, 4, 16, 10),
            end=moscow_datetime(2026, 6, 4, 16, 17),
        )
        hourly = candle("SBER", moscow_datetime(2026, 6, 4, 14, 0))
        weekly = candle("SBER", moscow_datetime(2026, 6, 1))
        monthly = candle("SBER", moscow_datetime(2026, 6, 1))

        self.assertEqual(candle_key(intraday, "10m"), "2026-06-04 16:10")
        self.assertEqual(candle_key(hourly, "1h"), "2026-06-04 14:00")
        self.assertEqual(candle_key(weekly, "1w"), "2026-W23")
        self.assertEqual(candle_key(monthly, "1mo"), "2026-06")
        self.assertEqual(format_period(intraday, "10m"), "16:10–16:19")
        self.assertEqual(format_period(hourly, "1h"), "14:00–14:59")

    def test_closed_candle_sorting_does_not_compare_naive_and_aware_datetimes(self) -> None:
        naive = candle("SBER", datetime(2026, 6, 4, 16, 0))
        aware = candle("SBER", moscow_datetime(2026, 6, 4, 16, 10))

        closed = self.client._closed_candles(
            [aware, naive],
            "10m",
            now=moscow_datetime(2026, 6, 4, 16, 20),
        )

        self.assertEqual([item.begin for item in closed], [naive.begin, aware.begin])


class AnalysisReportTests(unittest.TestCase):
    def setUp(self) -> None:
        previous_begin = moscow_datetime(2026, 6, 4, 16, 0)
        latest_begin = moscow_datetime(2026, 6, 4, 16, 10)
        self.sber = CandleComparison(
            ticker="SBER",
            previous=candle("SBER", previous_begin, high=100, close=99),
            last=candle(
                "SBER",
                latest_begin,
                end=moscow_datetime(2026, 6, 4, 16, 17),
                close=101,
            ),
            percent_change=2,
            matches_condition=True,
        )
        self.lkoh = CandleComparison(
            ticker="LKOH",
            previous=candle("LKOH", previous_begin, high=200, close=199),
            last=candle(
                "LKOH",
                latest_begin,
                end=moscow_datetime(2026, 6, 4, 16, 18),
                high=202,
                close=201,
            ),
            percent_change=1,
            matches_condition=True,
        )
        self.result = AnalysisResult(
            timeframe="10m",
            comparisons=[self.sber, self.lkoh],
            failures=[],
            updated_at=moscow_datetime(2026, 6, 4, 16, 21),
            tickers_count=2,
        )

    def test_same_begin_is_one_period_even_when_end_differs(self) -> None:
        self.assertEqual(self.result.latest_candle_key, "2026-06-04 16:10")
        self.assertEqual(self.result.previous_candle_key, "2026-06-04 16:00")
        self.assertEqual([item.ticker for item in self.result.matched_items], ["SBER", "LKOH"])

    def test_x_status_is_only_in_auto_report(self) -> None:
        auto_report = build_auto_notification_report(
            self.result,
            timezone_name="Europe/Moscow",
            streaks={"SBER": 2, "LKOH": 1},
        )
        manual_report = build_manual_report(
            self.result,
            timezone_name="Europe/Moscow",
        )

        self.assertIn("SBER (X2)", auto_report)
        self.assertNotIn("LKOH (X1)", auto_report)
        self.assertIn("Пробой high:", auto_report)
        self.assertNotIn("(X2)", manual_report)
        self.assertIn("Период последней закрытой свечи: 16:10–16:19 МСК", auto_report)

    def test_failure_message_does_not_expose_technical_errors(self) -> None:
        lines = format_failures(
            [
                ("SBER", "MOEX request failed: timeout details"),
                ("GAZP", "HTTP 500 internal details"),
            ]
        )

        self.assertEqual(
            lines,
            ["⚠️ Часть тикеров временно не удалось проверить: SBER, GAZP."],
        )


class EmptyReportTests(unittest.TestCase):
    def test_higher_timeframes_have_specific_empty_report_formats(self) -> None:
        previous = candle("SBER", moscow_datetime(2026, 5, 1), high=100, close=99)
        latest = candle("SBER", moscow_datetime(2026, 6, 1), high=100, close=99)
        comparison = CandleComparison(
            ticker="SBER",
            previous=previous,
            last=latest,
            percent_change=0,
            matches_condition=False,
        )
        expected_titles = {
            "1h": "Автоуведомление MOEX",
            "1d": "Дневной отчёт MOEX",
            "1w": "Недельный отчёт MOEX",
            "1mo": "Месячный отчёт MOEX",
        }

        for timeframe, title in expected_titles.items():
            with self.subTest(timeframe=timeframe):
                result = AnalysisResult(
                    timeframe=timeframe,
                    comparisons=[comparison],
                    failures=[],
                    updated_at=moscow_datetime(2026, 7, 1, 23, 55),
                    tickers_count=1,
                )

                report = build_empty_auto_notification_report(
                    result,
                    timezone_name="Europe/Moscow",
                )

                self.assertIn(title, report)
                self.assertIn("Нет тикеров", report)
                self.assertIn("Это не инвестиционная рекомендация.", report)
                self.assertNotIn("(X", report)


class UserSettingsTests(unittest.TestCase):
    def test_keys_and_streaks_are_independent_and_idempotent(self) -> None:
        store = MemoryUserSettingsStore(
            TESTS_DIR / "unused-user-settings.json",
            default_notification_timeframes=["1m", "10m", "1h"],
        )
        user = store.ensure_user(chat_id=1, user_id=1)
        self.assertTrue({"1m", "10m", "1h"}.issubset(user.notification_timeframes))

        first = store.record_auto_result(
            user_id=1,
            timeframe="10m",
            candle_key="2026-06-04 12:00",
            previous_candle_key="2026-06-04 11:50",
            matched_tickers=["SBER"],
        )
        duplicate = store.record_auto_result(
            user_id=1,
            timeframe="10m",
            candle_key="2026-06-04 12:00",
            previous_candle_key="2026-06-04 11:50",
            matched_tickers=["SBER"],
        )
        second = store.record_auto_result(
            user_id=1,
            timeframe="10m",
            candle_key="2026-06-04 12:10",
            previous_candle_key="2026-06-04 12:00",
            matched_tickers=["SBER"],
        )
        third = store.record_auto_result(
            user_id=1,
            timeframe="10m",
            candle_key="2026-06-04 12:20",
            previous_candle_key="2026-06-04 12:10",
            matched_tickers=["SBER"],
        )
        fourth = store.record_auto_result(
            user_id=1,
            timeframe="10m",
            candle_key="2026-06-04 12:30",
            previous_candle_key="2026-06-04 12:20",
            matched_tickers=["SBER"],
        )
        fifth = store.record_auto_result(
            user_id=1,
            timeframe="10m",
            candle_key="2026-06-04 12:40",
            previous_candle_key="2026-06-04 12:30",
            matched_tickers=["SBER"],
        )
        reset = store.record_auto_result(
            user_id=1,
            timeframe="10m",
            candle_key="2026-06-04 12:50",
            previous_candle_key="2026-06-04 12:40",
            matched_tickers=[],
            sent=False,
        )
        restarted = store.record_auto_result(
            user_id=1,
            timeframe="10m",
            candle_key="2026-06-04 13:00",
            previous_candle_key="2026-06-04 12:50",
            matched_tickers=["SBER"],
        )
        separate_hour = store.record_auto_result(
            user_id=1,
            timeframe="1h",
            candle_key="2026-06-04 12:00",
            previous_candle_key="2026-06-04 11:00",
            matched_tickers=["SBER"],
        )

        self.assertEqual(first.streaks["10m"], {"SBER": 1})
        self.assertEqual(duplicate.streaks["10m"], {"SBER": 1})
        self.assertEqual(second.streaks["10m"], {"SBER": 2})
        self.assertEqual(third.streaks["10m"], {"SBER": 3})
        self.assertEqual(fourth.streaks["10m"], {"SBER": 4})
        self.assertEqual(fifth.streaks["10m"], {"SBER": 5})
        self.assertEqual(reset.streaks["10m"], {})
        self.assertEqual(restarted.streaks["10m"], {"SBER": 1})
        self.assertEqual(separate_hour.streaks["10m"], {"SBER": 1})
        self.assertEqual(separate_hour.streaks["1h"], {"SBER": 1})
        self.assertEqual(
            separate_hour.last_sent_candle_keys,
            {
                "10m": "2026-06-04 13:00",
                "1h": "2026-06-04 12:00",
            },
        )
        self.assertEqual(
            separate_hour.last_processed_candle_keys,
            separate_hour.last_sent_candle_keys,
        )

        saved_user = store._data["users"]["1"]
        self.assertIn("last_processed_candle_keys", saved_user)
        self.assertIn("last_sent_candle_keys", saved_user)
        self.assertNotIn("last_sent_candle_times", saved_user)

    def test_streak_restarts_when_a_candle_was_skipped(self) -> None:
        store = MemoryUserSettingsStore(TESTS_DIR / "unused-settings.json")
        store.ensure_user(chat_id=1, user_id=1)
        store.record_auto_result(
            user_id=1,
            timeframe="10m",
            candle_key="2026-06-04 12:00",
            previous_candle_key="2026-06-04 11:50",
            matched_tickers=["SBER"],
        )
        result = store.record_auto_result(
            user_id=1,
            timeframe="10m",
            candle_key="2026-06-04 12:20",
            previous_candle_key="2026-06-04 12:10",
            matched_tickers=["SBER"],
        )

        self.assertEqual(result.streaks["10m"], {"SBER": 1})

    def test_legacy_last_sent_times_are_migrated_to_keys(self) -> None:
        store = MemoryUserSettingsStore(TESTS_DIR / "unused-legacy-settings.json")
        store._data["users"]["1"] = {
            "chat_id": 1,
            "user_id": 1,
            "selected_timeframe": "10m",
            "notification_timeframes": ["1m", "10m"],
            "auto_notifications_enabled": True,
            "last_sent_candle_times": {
                "1m": "2026-06-04 14:10",
                "10m": "2026-06-04 16:10",
            },
            "streaks": {},
            "last_reports": {},
        }

        user = store.get_user(1)

        self.assertIsNotNone(user)
        self.assertEqual(
            user.last_sent_candle_keys,
            {
                "1m": "2026-06-04 14:10",
                "10m": "2026-06-04 16:10",
            },
        )
        self.assertEqual(user.last_processed_candle_keys, user.last_sent_candle_keys)
        self.assertNotIn("last_sent_candle_times", store._data["users"]["1"])

    def test_processed_key_is_saved_without_sent_key_for_empty_low_timeframe(self) -> None:
        store = MemoryUserSettingsStore(TESTS_DIR / "unused-processed-settings.json")
        store.ensure_user(chat_id=1, user_id=1)

        user = store.record_auto_result(
            user_id=1,
            timeframe="10m",
            candle_key="2026-06-04 12:00",
            previous_candle_key="2026-06-04 11:50",
            matched_tickers=[],
            sent=False,
        )

        self.assertEqual(
            user.last_processed_candle_keys,
            {"10m": "2026-06-04 12:00"},
        )
        self.assertEqual(user.last_sent_candle_keys, {})


if __name__ == "__main__":
    unittest.main()
