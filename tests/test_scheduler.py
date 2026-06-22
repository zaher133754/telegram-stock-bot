from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

from scheduler import (
    AUTO_ONE_MINUTE_LAST_CHECK_KEY,
    AUTO_NOTIFICATIONS_LOCK_KEY,
    DAILY_EMPTY_REPORT_TEXT,
    _run_auto_notifications,
    check_timeframe_notifications,
    get_expected_candle_key,
    process_timeframe_notifications,
    run_auto_notifications,
    should_check_timeframe,
)


MOSCOW = ZoneInfo("Europe/Moscow")


def schedule_settings():
    return SimpleNamespace(
        timezone=MOSCOW,
        scheduler_interval_seconds=60,
        intraday_check_delay_seconds=30,
        one_minute_check_interval_seconds=180,
        hourly_confirmation_delay_minutes=5,
        hourly_check_window_minutes=20,
        daily_report_time=time(23, 55),
        weekly_report_day=4,
        weekly_report_time=time(23, 55),
        monthly_report_time=time(23, 55),
        send_empty_reports_for_higher_timeframes=True,
        tickers_file=Path("tickers.txt"),
    )


def moscow_datetime(
    year: int,
    month: int,
    day: int,
    hour: int,
    minute: int,
    second: int = 0,
) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=MOSCOW)


class ShouldCheckTimeframeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = schedule_settings()

    def test_intraday_timeframes_only_run_in_due_windows(self) -> None:
        self.assertTrue(
            should_check_timeframe(
                "1m",
                moscow_datetime(2026, 6, 4, 12, 7, 10),
                self.settings,
            )
        )
        self.assertFalse(
            should_check_timeframe(
                "10m",
                moscow_datetime(2026, 6, 4, 12, 10, 10),
                self.settings,
            )
        )
        self.assertTrue(
            should_check_timeframe(
                "10m",
                moscow_datetime(2026, 6, 4, 12, 11, 10),
                self.settings,
            )
        )
        self.assertFalse(
            should_check_timeframe(
                "10m",
                moscow_datetime(2026, 6, 4, 12, 12, 10),
                self.settings,
            )
        )
        self.assertTrue(
            should_check_timeframe(
                "1h",
                moscow_datetime(2026, 6, 4, 13, 1, 10),
                self.settings,
            )
        )
        self.assertTrue(
            should_check_timeframe(
                "1h",
                moscow_datetime(2026, 6, 4, 13, 2, 10),
                self.settings,
            )
        )
        self.assertTrue(
            should_check_timeframe(
                "1h",
                moscow_datetime(2026, 6, 4, 13, 5, 59),
                self.settings,
            )
        )
        self.assertTrue(
            should_check_timeframe(
                "1h",
                moscow_datetime(2026, 6, 4, 13, 6),
                self.settings,
            )
        )
        self.assertTrue(
            should_check_timeframe(
                "1h",
                moscow_datetime(2026, 6, 4, 13, 20, 59),
                self.settings,
            )
        )
        self.assertFalse(
            should_check_timeframe(
                "1h",
                moscow_datetime(2026, 6, 4, 13, 21),
                self.settings,
            )
        )

    def test_daily_weekly_and_monthly_schedules(self) -> None:
        friday = moscow_datetime(2026, 6, 5, 23, 55, 10)
        thursday = moscow_datetime(2026, 6, 4, 23, 55, 10)

        self.assertTrue(should_check_timeframe("1d", thursday, self.settings))
        self.assertTrue(
            should_check_timeframe(
                "1d",
                moscow_datetime(2026, 6, 4, 23, 56, 10),
                self.settings,
            )
        )
        self.assertTrue(should_check_timeframe("1w", friday, self.settings))
        self.assertFalse(should_check_timeframe("1w", thursday, self.settings))
        self.assertTrue(
            should_check_timeframe(
                "1mo",
                moscow_datetime(2026, 6, 30, 23, 55, 10),
                self.settings,
            )
        )
        self.assertFalse(
            should_check_timeframe(
                "1mo",
                moscow_datetime(2026, 7, 1, 23, 55, 10),
                self.settings,
            )
        )
        self.assertFalse(
            should_check_timeframe(
                "1mo",
                moscow_datetime(2026, 6, 15, 23, 55, 10),
                self.settings,
            )
        )


class ExpectedCandleKeyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = schedule_settings()

    def test_intraday_expected_candle_keys(self) -> None:
        self.assertEqual(
            get_expected_candle_key(
                "1m",
                moscow_datetime(2026, 6, 4, 19, 5, 30),
                settings=self.settings,
            ),
            "2026-06-04 19:04",
        )
        self.assertEqual(
            get_expected_candle_key(
                "10m",
                moscow_datetime(2026, 6, 4, 19, 11),
                settings=self.settings,
            ),
            "2026-06-04 19:00",
        )
        self.assertEqual(
            get_expected_candle_key(
                "10m",
                moscow_datetime(2026, 6, 4, 19, 21),
                settings=self.settings,
            ),
            "2026-06-04 19:10",
        )
        self.assertEqual(
            get_expected_candle_key(
                "1h",
                moscow_datetime(2026, 6, 4, 19, 1),
                settings=self.settings,
            ),
            "2026-06-04 18:00",
        )
        self.assertEqual(
            get_expected_candle_key(
                "1h",
                moscow_datetime(2026, 6, 4, 20, 20),
                settings=self.settings,
            ),
            "2026-06-04 19:00",
        )

    def test_calendar_expected_candle_keys(self) -> None:
        self.assertEqual(
            get_expected_candle_key(
                "1d",
                moscow_datetime(2026, 6, 4, 23, 55, 10),
                settings=self.settings,
            ),
            "2026-06-04",
        )
        self.assertEqual(
            get_expected_candle_key(
                "1w",
                moscow_datetime(2026, 6, 5, 23, 55, 10),
                settings=self.settings,
            ),
            "2026-W23",
        )
        self.assertEqual(
            get_expected_candle_key(
                "1mo",
                moscow_datetime(2026, 6, 30, 23, 55, 10),
                settings=self.settings,
            ),
            "2026-06",
        )


class SchedulerFilteringTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_enabled_and_due_timeframes_are_checked(self) -> None:
        settings = schedule_settings()
        user = SimpleNamespace(
            auto_notifications_enabled=True,
            notification_timeframes=["10m", "1h"],
        )
        store = SimpleNamespace(list_users=lambda: [user])
        application = SimpleNamespace(
            bot_data={
                "settings": settings,
                "user_settings_store": store,
            }
        )

        with patch(
            "scheduler.check_timeframe_notifications",
            new_callable=AsyncMock,
        ) as check:
            await _run_auto_notifications(
                application,
                now=moscow_datetime(2026, 6, 4, 12, 31, 10),
            )

        check.assert_awaited_once()
        self.assertEqual(check.await_args.kwargs["timeframe"], "10m")

    async def test_due_timeframes_are_checked_in_priority_order(self) -> None:
        settings = schedule_settings()
        user = SimpleNamespace(
            auto_notifications_enabled=True,
            notification_timeframes=["1m", "10m", "1h"],
        )
        store = SimpleNamespace(list_users=lambda: [user])
        application = SimpleNamespace(
            bot_data={
                "settings": settings,
                "user_settings_store": store,
            }
        )

        with patch(
            "scheduler.check_timeframe_notifications",
            new_callable=AsyncMock,
        ) as check:
            await _run_auto_notifications(
                application,
                now=moscow_datetime(2026, 6, 4, 13, 10, 48),
            )

        self.assertEqual(
            [call.kwargs["timeframe"] for call in check.await_args_list],
            ["1h", "10m", "1m"],
        )

    async def test_weekly_runs_after_daily_on_friday_close(self) -> None:
        settings = schedule_settings()
        user = SimpleNamespace(
            auto_notifications_enabled=True,
            notification_timeframes=["1d", "1w"],
        )
        store = SimpleNamespace(list_users=lambda: [user])
        application = SimpleNamespace(
            bot_data={
                "settings": settings,
                "user_settings_store": store,
            }
        )

        with patch(
            "scheduler.check_timeframe_notifications",
            new_callable=AsyncMock,
        ) as check:
            await _run_auto_notifications(
                application,
                now=moscow_datetime(2026, 6, 5, 23, 55, 10),
            )

        self.assertEqual(
            [call.kwargs["timeframe"] for call in check.await_args_list],
            ["1d", "1w"],
        )

    async def test_monthly_runs_after_daily_on_last_month_day(self) -> None:
        settings = schedule_settings()
        user = SimpleNamespace(
            auto_notifications_enabled=True,
            notification_timeframes=["1d", "1mo"],
        )
        store = SimpleNamespace(list_users=lambda: [user])
        application = SimpleNamespace(
            bot_data={
                "settings": settings,
                "user_settings_store": store,
            }
        )

        with patch(
            "scheduler.check_timeframe_notifications",
            new_callable=AsyncMock,
        ) as check:
            await _run_auto_notifications(
                application,
                now=moscow_datetime(2026, 6, 30, 23, 55, 10),
            )

        self.assertEqual(
            [call.kwargs["timeframe"] for call in check.await_args_list],
            ["1d", "1mo"],
        )

    async def test_one_minute_timeframe_is_throttled(self) -> None:
        settings = schedule_settings()
        user = SimpleNamespace(
            auto_notifications_enabled=True,
            notification_timeframes=["1m"],
        )
        now = moscow_datetime(2026, 6, 4, 13, 10, 48)
        store = SimpleNamespace(list_users=lambda: [user])
        application = SimpleNamespace(
            bot_data={
                "settings": settings,
                "user_settings_store": store,
                AUTO_ONE_MINUTE_LAST_CHECK_KEY: now - timedelta(seconds=60),
            }
        )

        with patch(
            "scheduler.check_timeframe_notifications",
            new_callable=AsyncMock,
        ) as check:
            await _run_auto_notifications(application, now=now)

        check.assert_not_awaited()

    async def test_run_is_skipped_when_previous_run_holds_lock(self) -> None:
        lock = asyncio.Lock()
        await lock.acquire()
        application = SimpleNamespace(
            bot_data={AUTO_NOTIFICATIONS_LOCK_KEY: lock}
        )

        try:
            with self.assertLogs("scheduler", level="INFO") as logs:
                await run_auto_notifications(application)
        finally:
            lock.release()

        self.assertTrue(
            any(
                "Auto notifications skipped: previous run is still running" in line
                for line in logs.output
            )
        )

    async def test_stale_hourly_candle_is_not_processed(self) -> None:
        settings = schedule_settings()
        result = SimpleNamespace(
            latest_candle_key="2026-06-04 17:00",
            moex_requests_count=1,
            matched_items=[],
            debug_last_candles=[],
        )

        with (
            patch("scheduler.collect_moex_analysis", return_value=result),
            patch(
                "scheduler.process_timeframe_notifications",
                new_callable=AsyncMock,
            ) as process,
            self.assertLogs("scheduler", level="INFO") as logs,
        ):
            await check_timeframe_notifications(
                application=SimpleNamespace(bot_data={}),
                store=SimpleNamespace(),
                settings=settings,
                timeframe="1h",
                users=[],
                now=moscow_datetime(2026, 6, 4, 19, 1),
            )

        process.assert_not_awaited()
        self.assertTrue(
            any(
                "Expected 1h candle 18:00 is not available yet. Latest available: 17:00"
                in line
                for line in logs.output
            )
        )

    async def test_hourly_candle_waits_for_confirmation_delay(self) -> None:
        settings = schedule_settings()
        result = SimpleNamespace(
            latest_candle_key="2026-06-04 18:00",
            moex_requests_count=1,
            matched_items=[],
            debug_last_candles=[],
        )
        application = SimpleNamespace(bot_data={})

        with (
            patch("scheduler.collect_moex_analysis", return_value=result),
            patch(
                "scheduler.process_timeframe_notifications",
                new_callable=AsyncMock,
            ) as process,
        ):
            await check_timeframe_notifications(
                application=application,
                store=SimpleNamespace(),
                settings=settings,
                timeframe="1h",
                users=[],
                now=moscow_datetime(2026, 6, 4, 19, 6),
            )
            process.assert_not_awaited()

            await check_timeframe_notifications(
                application=application,
                store=SimpleNamespace(),
                settings=settings,
                timeframe="1h",
                users=[],
                now=moscow_datetime(2026, 6, 4, 19, 11),
            )

        process.assert_awaited_once()

    async def test_daily_uses_latest_available_closed_candle(self) -> None:
        settings = schedule_settings()
        result = SimpleNamespace(
            latest_candle_key="2026-06-04",
            moex_requests_count=1,
            matched_items=[],
        )

        with (
            patch("scheduler.collect_moex_analysis", return_value=result),
            patch(
                "scheduler.process_timeframe_notifications",
                new_callable=AsyncMock,
            ) as process,
        ):
            await check_timeframe_notifications(
                application=SimpleNamespace(bot_data={}),
                store=SimpleNamespace(),
                settings=settings,
                timeframe="1d",
                users=[],
                now=moscow_datetime(2026, 6, 4, 12, 0),
            )

        process.assert_awaited_once()

    async def test_auto_notifications_use_selected_tickers(self) -> None:
        settings = schedule_settings()
        user = SimpleNamespace(
            user_id=1,
            chat_id=2,
            selected_tickers=["SBER"],
            last_processed_candle_keys={},
        )
        store = SimpleNamespace(
            ensure_selected_tickers=Mock(return_value=user),
            get_user=lambda _user_id: user,
        )
        result = SimpleNamespace(
            latest_candle_key="2026-06-04",
            moex_requests_count=1,
            matched_items=[],
        )

        with (
            patch("scheduler.get_available_tickers", return_value=["SBER", "GAZP"]),
            patch("scheduler.collect_moex_analysis", return_value=result) as collect,
            patch(
                "scheduler.process_timeframe_notifications",
                new_callable=AsyncMock,
            ) as process,
        ):
            await check_timeframe_notifications(
                application=SimpleNamespace(bot_data={}),
                store=store,
                settings=settings,
                timeframe="1d",
                users=[user],
                now=moscow_datetime(2026, 6, 4, 12, 0),
            )

        collect.assert_called_once_with(settings, "1d", ["SBER"])
        process.assert_awaited_once()

    async def test_auto_notifications_skip_users_without_selected_tickers(self) -> None:
        settings = schedule_settings()
        user = SimpleNamespace(
            user_id=1,
            chat_id=2,
            selected_tickers=[],
            last_processed_candle_keys={},
        )
        store = SimpleNamespace(ensure_selected_tickers=Mock(return_value=user))

        with (
            patch("scheduler.get_available_tickers", return_value=["SBER"]),
            patch("scheduler.collect_moex_analysis") as collect,
            patch(
                "scheduler.process_timeframe_notifications",
                new_callable=AsyncMock,
            ) as process,
            self.assertLogs("scheduler", level="INFO") as logs,
        ):
            await check_timeframe_notifications(
                application=SimpleNamespace(bot_data={}),
                store=store,
                settings=settings,
                timeframe="1d",
                users=[user],
                now=moscow_datetime(2026, 6, 4, 12, 0),
            )

        collect.assert_not_called()
        process.assert_not_awaited()
        self.assertTrue(
            any("selected_tickers=0" in line for line in logs.output)
        )

    async def test_expected_hourly_candle_is_processed(self) -> None:
        settings = schedule_settings()
        settings.hourly_confirmation_delay_minutes = 0
        result = SimpleNamespace(
            latest_candle_key="2026-06-04 18:00",
            moex_requests_count=1,
            matched_items=[],
            debug_last_candles=[],
        )

        with (
            patch("scheduler.collect_moex_analysis", return_value=result),
            patch(
                "scheduler.process_timeframe_notifications",
                new_callable=AsyncMock,
            ) as process,
            self.assertLogs("scheduler", level="INFO") as logs,
        ):
            await check_timeframe_notifications(
                application=SimpleNamespace(bot_data={}),
                store=SimpleNamespace(),
                settings=settings,
                timeframe="1h",
                users=[],
                now=moscow_datetime(2026, 6, 4, 19, 1),
            )

        process.assert_awaited_once()
        self.assertTrue(
            any("1h notification data ready" in line for line in logs.output)
        )


class EmptyNotificationTests(unittest.IsolatedAsyncioTestCase):
    def notification_user(self, timeframe: str, *, processed: str | None = None):
        processed_keys = {timeframe: processed} if processed else {}
        return SimpleNamespace(
            user_id=1,
            chat_id=2,
            auto_notifications_enabled=True,
            notification_timeframes=[timeframe],
            last_processed_candle_keys=processed_keys,
            last_sent_candle_keys={},
            streaks={},
        )

    def notification_result(self):
        return SimpleNamespace(
            latest_candle_key="2026-06-04 18:00",
            previous_candle_key="2026-06-04 17:00",
            matched_items=[],
        )

    async def test_empty_hour_report_is_sent_and_recorded_as_sent(self) -> None:
        user = self.notification_user("1h")
        store = SimpleNamespace(
            get_user=lambda _user_id: user,
            record_auto_result=Mock(),
        )
        settings = SimpleNamespace(
            send_empty_reports_for_higher_timeframes=True,
            timezone_name="Europe/Moscow",
        )

        with (
            patch(
                "scheduler.build_empty_auto_notification_report",
                return_value="empty report",
            ),
            patch("scheduler.send_scheduled_report", new_callable=AsyncMock) as send,
        ):
            await process_timeframe_notifications(
                application=SimpleNamespace(),
                store=store,
                settings=settings,
                timeframe="1h",
                users=[user],
                result=self.notification_result(),
            )

        send.assert_awaited_once()
        store.record_auto_result.assert_called_once_with(
            user_id=1,
            timeframe="1h",
            candle_key="2026-06-04 18:00",
            previous_candle_key="2026-06-04 17:00",
            matched_tickers=[],
            sent=True,
        )

    async def test_empty_low_timeframe_is_processed_without_send(self) -> None:
        for timeframe in ("1m", "10m"):
            with self.subTest(timeframe=timeframe):
                user = self.notification_user(timeframe)
                store = SimpleNamespace(
                    get_user=lambda _user_id: user,
                    record_auto_result=Mock(),
                )
                settings = SimpleNamespace(
                    send_empty_reports_for_higher_timeframes=True,
                    timezone_name="Europe/Moscow",
                )

                with patch(
                    "scheduler.send_scheduled_report",
                    new_callable=AsyncMock,
                ) as send:
                    await process_timeframe_notifications(
                        application=SimpleNamespace(),
                        store=store,
                        settings=settings,
                        timeframe=timeframe,
                        users=[user],
                        result=self.notification_result(),
                    )

                send.assert_not_awaited()
                self.assertFalse(store.record_auto_result.call_args.kwargs["sent"])

    async def test_all_higher_timeframes_send_empty_reports(self) -> None:
        for timeframe in ("1h", "1d", "1w", "1mo"):
            with self.subTest(timeframe=timeframe):
                user = self.notification_user(timeframe)
                store = SimpleNamespace(
                    get_user=lambda _user_id: user,
                    record_auto_result=Mock(),
                )
                settings = SimpleNamespace(
                    send_empty_reports_for_higher_timeframes=True,
                    timezone_name="Europe/Moscow",
                )

                with (
                    patch(
                        "scheduler.build_empty_auto_notification_report",
                        return_value="empty report",
                    ),
                    patch(
                        "scheduler.send_scheduled_report",
                        new_callable=AsyncMock,
                    ) as send,
                ):
                    await process_timeframe_notifications(
                        application=SimpleNamespace(),
                        store=store,
                        settings=settings,
                        timeframe=timeframe,
                        users=[user],
                        result=self.notification_result(),
                    )

                send.assert_awaited_once()
                self.assertTrue(store.record_auto_result.call_args.kwargs["sent"])

    async def test_empty_daily_report_uses_fixed_text_even_when_disabled(self) -> None:
        user = self.notification_user("1d")
        store = SimpleNamespace(
            get_user=lambda _user_id: user,
            record_auto_result=Mock(),
        )
        settings = SimpleNamespace(
            send_empty_reports_for_higher_timeframes=False,
            timezone_name="Europe/Moscow",
        )
        result = SimpleNamespace(
            latest_candle_key="2026-06-04",
            previous_candle_key="2026-06-03",
            matched_items=[],
        )
        application = SimpleNamespace()

        with (
            patch(
                "scheduler.build_empty_auto_notification_report",
                return_value="empty report",
            ) as build_empty,
            patch("scheduler.send_scheduled_report", new_callable=AsyncMock) as send,
        ):
            await process_timeframe_notifications(
                application=application,
                store=store,
                settings=settings,
                timeframe="1d",
                users=[user],
                result=result,
            )

        build_empty.assert_not_called()
        send.assert_awaited_once_with(
            application=application,
            chat_id=2,
            text=DAILY_EMPTY_REPORT_TEXT,
        )
        store.record_auto_result.assert_called_once_with(
            user_id=1,
            timeframe="1d",
            candle_key="2026-06-04",
            previous_candle_key="2026-06-03",
            matched_tickers=[],
            sent=True,
        )

    async def test_matching_ticker_after_processed_empty_candle_sends_normal_report(self) -> None:
        user = self.notification_user("1h", processed="2026-06-04 18:00")
        store = SimpleNamespace(
            get_user=lambda _user_id: user,
            record_auto_result=Mock(),
        )
        settings = SimpleNamespace(
            send_empty_reports_for_higher_timeframes=True,
            timezone_name="Europe/Moscow",
        )
        result = SimpleNamespace(
            latest_candle_key="2026-06-04 19:00",
            previous_candle_key="2026-06-04 18:00",
            matched_items=[SimpleNamespace(ticker="SBER")],
        )

        with (
            patch("scheduler.build_auto_notification_report", return_value="report"),
            patch("scheduler.send_scheduled_report", new_callable=AsyncMock) as send,
        ):
            await process_timeframe_notifications(
                application=SimpleNamespace(),
                store=store,
                settings=settings,
                timeframe="1h",
                users=[user],
                result=result,
            )

        send.assert_awaited_once()
        store.record_auto_result.assert_called_once_with(
            user_id=1,
            timeframe="1h",
            candle_key="2026-06-04 19:00",
            previous_candle_key="2026-06-04 18:00",
            matched_tickers=["SBER"],
            sent=True,
        )

    async def test_processed_empty_hour_report_is_not_duplicated(self) -> None:
        user = self.notification_user("1h", processed="2026-06-04 18:00")
        store = SimpleNamespace(
            get_user=lambda _user_id: user,
            record_auto_result=Mock(),
        )
        settings = SimpleNamespace(
            send_empty_reports_for_higher_timeframes=True,
            timezone_name="Europe/Moscow",
        )

        with patch("scheduler.send_scheduled_report", new_callable=AsyncMock) as send:
            await process_timeframe_notifications(
                application=SimpleNamespace(),
                store=store,
                settings=settings,
                timeframe="1h",
                users=[user],
                result=self.notification_result(),
            )

        send.assert_not_awaited()
        store.record_auto_result.assert_not_called()

    async def test_hourly_recheck_sends_supplement_for_new_tickers(self) -> None:
        user = SimpleNamespace(
            user_id=1,
            chat_id=2,
            auto_notifications_enabled=True,
            notification_timeframes=["1h"],
            last_processed_candle_keys={"1h": "2026-06-04 18:00"},
            last_sent_candle_keys={"1h": "2026-06-04 18:00"},
            last_auto_report_tickers={"1h": ["SBER"]},
            streaks={"1h": {"SBER": 1}},
        )
        store = SimpleNamespace(
            get_user=lambda _user_id: user,
            record_auto_result=Mock(),
        )
        settings = SimpleNamespace(
            send_empty_reports_for_higher_timeframes=True,
            timezone_name="Europe/Moscow",
        )
        result = SimpleNamespace(
            latest_candle_key="2026-06-04 18:00",
            previous_candle_key="2026-06-04 17:00",
            matched_items=[
                SimpleNamespace(ticker="SBER"),
                SimpleNamespace(ticker="LKOH"),
            ],
        )

        with (
            patch(
                "scheduler.build_hourly_supplement_report",
                return_value="supplement",
            ) as build,
            patch("scheduler.send_scheduled_report", new_callable=AsyncMock) as send,
        ):
            await process_timeframe_notifications(
                application=SimpleNamespace(),
                store=store,
                settings=settings,
                timeframe="1h",
                users=[user],
                result=result,
            )

        send.assert_awaited_once()
        self.assertEqual(build.call_args.kwargs["tickers"], ["LKOH"])
        store.record_auto_result.assert_called_once_with(
            user_id=1,
            timeframe="1h",
            candle_key="2026-06-04 18:00",
            previous_candle_key="2026-06-04 17:00",
            matched_tickers=["SBER", "LKOH"],
            sent=True,
        )

    async def test_empty_hour_report_can_be_disabled_but_is_still_processed(self) -> None:
        user = self.notification_user("1h")
        store = SimpleNamespace(
            get_user=lambda _user_id: user,
            record_auto_result=Mock(),
        )
        settings = SimpleNamespace(
            send_empty_reports_for_higher_timeframes=False,
            timezone_name="Europe/Moscow",
        )

        with (
            patch("scheduler.send_scheduled_report", new_callable=AsyncMock) as send,
            self.assertLogs("scheduler", level="INFO") as logs,
        ):
            await process_timeframe_notifications(
                application=SimpleNamespace(),
                store=store,
                settings=settings,
                timeframe="1h",
                users=[user],
                result=self.notification_result(),
            )

        send.assert_not_awaited()
        self.assertFalse(store.record_auto_result.call_args.kwargs["sent"])
        self.assertTrue(
            any("Empty report skipped by settings" in line for line in logs.output)
        )


if __name__ == "__main__":
    unittest.main()
