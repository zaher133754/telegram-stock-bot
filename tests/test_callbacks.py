from __future__ import annotations

import asyncio
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bot import EMPTY_SELECTED_TICKERS_TEXT, handle_callback, send_manual_report
from telegram.error import TelegramError
from keyboards import (
    CALLBACK_AI_ANALYSIS,
    CALLBACK_AI_CHART_PREFIX,
    CALLBACK_AI_REFRESH_PREFIX,
    CALLBACK_AI_TICKER_PREFIX,
    CALLBACK_NOTIFICATIONS,
    CALLBACK_SETTINGS,
    CALLBACK_TICKERS,
    MAIN_MENU,
    REFRESH,
    TIMEFRAME_MENU,
    after_timeframe_keyboard,
    ai_analysis_actions_keyboard,
    build_ai_tickers_keyboard,
    build_tickers_keyboard,
    main_menu_keyboard,
    main_menu_only_keyboard,
    normalize_callback_data,
    notification_timeframe_keyboard,
    notifications_keyboard,
    report_actions_keyboard,
    timeframe_keyboard,
)
from scheduler import AUTO_NOTIFICATIONS_TASK_KEY, trigger_auto_notifications


def callback_values(markup) -> list[str]:
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
    ]


def callback_update(data: str):
    query = SimpleNamespace(
        data=data,
        answer=AsyncMock(),
        message=SimpleNamespace(reply_text=AsyncMock()),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=123),
    )
    context = SimpleNamespace(
        application=SimpleNamespace(
            bot_data={"settings": SimpleNamespace(allowed_user_id=None)}
        )
    )
    return update, context, query


class KeyboardCallbackTests(unittest.TestCase):
    def test_required_callback_constants_have_unified_values(self) -> None:
        self.assertEqual(MAIN_MENU, "main_menu")
        self.assertEqual(REFRESH, "refresh")
        self.assertEqual(TIMEFRAME_MENU, "timeframe_menu")

    def test_required_buttons_have_callback_data_and_handlers(self) -> None:
        main_callbacks = callback_values(main_menu_keyboard())
        report_callbacks = callback_values(report_actions_keyboard())
        notification_callbacks = callback_values(notifications_keyboard())

        self.assertIn(REFRESH, main_callbacks)
        self.assertIn(TIMEFRAME_MENU, main_callbacks)
        self.assertIn(CALLBACK_NOTIFICATIONS, main_callbacks)
        self.assertIn(CALLBACK_TICKERS, main_callbacks)
        self.assertIn(CALLBACK_AI_ANALYSIS, main_callbacks)
        self.assertIn(CALLBACK_SETTINGS, main_callbacks)
        self.assertEqual(report_callbacks, [REFRESH, TIMEFRAME_MENU, MAIN_MENU])
        self.assertIn(REFRESH, notification_callbacks)
        self.assertIn(MAIN_MENU, notification_callbacks)

        for markup in (
            main_menu_keyboard(),
            timeframe_keyboard(),
            notification_timeframe_keyboard(["1m", "10m"]),
            after_timeframe_keyboard(),
            notifications_keyboard(),
            report_actions_keyboard(),
            build_ai_tickers_keyboard(["SBER", "GAZP"]),
            ai_analysis_actions_keyboard("SBER"),
            main_menu_only_keyboard(),
        ):
            self.assertTrue(all(callback_values(markup)))

    def test_legacy_callbacks_are_normalized(self) -> None:
        self.assertEqual(normalize_callback_data("menu:main"), MAIN_MENU)
        self.assertEqual(normalize_callback_data("menu:tickers"), CALLBACK_TICKERS)
        self.assertEqual(normalize_callback_data("report:check"), REFRESH)
        self.assertEqual(normalize_callback_data("menu:timeframe"), TIMEFRAME_MENU)

    def test_tickers_keyboard_is_paginated(self) -> None:
        tickers = [f"T{i:02d}" for i in range(25)]
        markup = build_tickers_keyboard(["T00", "T21"], tickers, page=1)
        callbacks = callback_values(markup)
        labels = [
            button.text
            for row in markup.inline_keyboard
            for button in row
        ]

        self.assertIn("ticker_toggle:T20", callbacks)
        self.assertIn("ticker_toggle:T24", callbacks)
        self.assertNotIn("ticker_toggle:T00", callbacks)
        self.assertIn("✅ T21", labels)
        self.assertIn("❌ T20", labels)
        self.assertIn("tickers_page:0", callbacks)
        self.assertIn("tickers_all", callbacks)
        self.assertIn("tickers_none", callbacks)
        self.assertIn("tickers_save", callbacks)

    def test_ai_tickers_keyboard_and_actions(self) -> None:
        markup = build_ai_tickers_keyboard(["SBER", "GAZP", "LKOH"], page=0)
        callbacks = callback_values(markup)

        self.assertIn(f"{CALLBACK_AI_TICKER_PREFIX}SBER", callbacks)
        self.assertIn("ai:page:0", callbacks)
        self.assertIn(MAIN_MENU, callbacks)

        actions = callback_values(ai_analysis_actions_keyboard("SBER"))
        self.assertEqual(
            actions,
            [
                f"{CALLBACK_AI_REFRESH_PREFIX}SBER",
                f"{CALLBACK_AI_CHART_PREFIX}SBER",
                MAIN_MENU,
            ],
        )


class CallbackHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_main_menu_and_legacy_main_menu_are_answered_and_handled(self) -> None:
        for data in (MAIN_MENU, "menu:main"):
            with self.subTest(data=data):
                update, context, query = callback_update(data)
                user_settings = object()

                with (
                    patch("bot.ensure_user_settings", return_value=user_settings),
                    patch("bot.send_main_menu", new_callable=AsyncMock) as send_main_menu,
                ):
                    await handle_callback(update, context)

                query.answer.assert_awaited_once_with()
                send_main_menu.assert_awaited_once_with(update, context, user_settings)

    async def test_required_callbacks_are_answered_and_routed(self) -> None:
        routes = (
            (REFRESH, "bot.send_manual_report"),
            (TIMEFRAME_MENU, "bot.send_timeframe_menu"),
            (CALLBACK_NOTIFICATIONS, "bot.send_notifications_menu"),
            (CALLBACK_TICKERS, "bot.send_tickers_menu"),
            (CALLBACK_AI_ANALYSIS, "bot.send_ai_tickers_menu"),
            (CALLBACK_SETTINGS, "bot.send_settings"),
        )

        for data, handler_path in routes:
            with self.subTest(data=data):
                update, context, query = callback_update(data)

                with (
                    patch("bot.ensure_user_settings", return_value=object()),
                    patch(handler_path, new_callable=AsyncMock) as handler,
                ):
                    await handle_callback(update, context)

                query.answer.assert_awaited_once_with()
                handler.assert_awaited_once()

    async def test_ai_ticker_callbacks_are_routed(self) -> None:
        routes = (
            (f"{CALLBACK_AI_TICKER_PREFIX}SBER", False),
            (f"{CALLBACK_AI_REFRESH_PREFIX}SBER", False),
            (f"{CALLBACK_AI_CHART_PREFIX}SBER", True),
        )

        for data, chart_only in routes:
            with self.subTest(data=data):
                update, context, query = callback_update(data)

                with (
                    patch("bot.ensure_user_settings", return_value=object()),
                    patch("bot.send_ai_analysis_for_ticker", new_callable=AsyncMock) as handler,
                ):
                    await handle_callback(update, context)

                query.answer.assert_awaited_once_with()
                if chart_only:
                    handler.assert_awaited_once_with(
                        update,
                        context,
                        "SBER",
                        chart_only=True,
                    )
                else:
                    handler.assert_awaited_once_with(update, context, "SBER")

    async def test_unknown_callback_answers_and_shows_main_menu_button(self) -> None:
        update, context, query = callback_update("unknown_callback")

        with (
            patch("bot.ensure_user_settings", return_value=object()),
            patch("bot.send_text", new_callable=AsyncMock) as send_text,
        ):
            await handle_callback(update, context)

        query.answer.assert_awaited_once_with()
        send_text.assert_awaited_once()
        args, kwargs = send_text.await_args
        self.assertEqual(args[2], "Неизвестная команда. Откройте главное меню.")
        self.assertEqual(callback_values(kwargs["reply_markup"]), [MAIN_MENU])

    async def test_callback_action_continues_when_answer_fails(self) -> None:
        update, context, query = callback_update(MAIN_MENU)
        query.answer.side_effect = TelegramError("answer failed")

        with (
            patch("bot.ensure_user_settings", return_value=object()),
            patch("bot.send_main_menu", new_callable=AsyncMock) as send_main_menu,
        ):
            with self.assertLogs("bot", level="ERROR"):
                await handle_callback(update, context)

        query.answer.assert_awaited_once_with()
        send_main_menu.assert_awaited_once()


class ManualReportTickerSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_manual_report_warns_when_no_tickers_selected(self) -> None:
        update = SimpleNamespace()
        context = SimpleNamespace()
        user_settings = SimpleNamespace(user_id=1, timeframe="1d", selected_tickers=[])
        store = SimpleNamespace(ensure_selected_tickers=Mock(return_value=user_settings))

        with (
            patch("bot.get_settings", return_value=SimpleNamespace()),
            patch("bot.get_user_settings_store", return_value=store),
            patch("bot.get_all_tickers", return_value=["SBER"]),
            patch("bot.send_text", new_callable=AsyncMock) as send_text,
        ):
            await send_manual_report(update, context, user_settings=user_settings)

        send_text.assert_awaited_once()
        self.assertEqual(send_text.await_args.args[2], EMPTY_SELECTED_TICKERS_TEXT)

    async def test_manual_report_uses_selected_tickers(self) -> None:
        update = SimpleNamespace()
        context = SimpleNamespace()
        settings = SimpleNamespace(timezone_name="Europe/Moscow")
        user_settings = SimpleNamespace(
            user_id=1,
            timeframe="1d",
            selected_tickers=["SBER"],
        )
        store = SimpleNamespace(
            ensure_selected_tickers=Mock(return_value=user_settings),
            save_last_report=Mock(),
        )
        result = SimpleNamespace(
            tickers_count=0,
            latest_candle_key=None,
            updated_at=datetime(2026, 6, 4, 12, 0),
        )

        with (
            patch("bot.get_settings", return_value=settings),
            patch("bot.get_user_settings_store", return_value=store),
            patch("bot.get_all_tickers", return_value=["SBER", "GAZP"]),
            patch("bot.collect_moex_analysis", return_value=result) as collect,
            patch("bot.send_text", new_callable=AsyncMock),
        ):
            await send_manual_report(update, context, user_settings=user_settings)

        collect.assert_called_once_with(settings, "1d", ["SBER"])


class SchedulerTriggerTests(unittest.IsolatedAsyncioTestCase):
    async def test_trigger_does_not_start_overlapping_notification_passes(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        application = SimpleNamespace(bot_data={})

        async def long_running_pass(_application) -> None:
            started.set()
            await release.wait()

        with patch("scheduler.run_auto_notifications", side_effect=long_running_pass) as run:
            await trigger_auto_notifications(application)
            await started.wait()
            first_task = application.bot_data[AUTO_NOTIFICATIONS_TASK_KEY]

            await trigger_auto_notifications(application)
            self.assertIs(application.bot_data[AUTO_NOTIFICATIONS_TASK_KEY], first_task)
            self.assertEqual(run.call_count, 1)

            release.set()
            await first_task


if __name__ == "__main__":
    unittest.main()
