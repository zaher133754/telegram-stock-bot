from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from telegram.ext import Application

from analytics import build_auto_notification_report, collect_moex_analysis
from config import Settings
from keyboards import report_actions_keyboard
from user_settings import (
    UserSettings,
    UserSettingsStore,
    calculate_next_streaks,
)
from utils import split_telegram_message


logger = logging.getLogger(__name__)


def start_scheduler(application: Application) -> AsyncIOScheduler:
    settings: Settings = application.bot_data["settings"]
    scheduler = AsyncIOScheduler(timezone=settings.timezone)

    scheduler.add_job(
        run_auto_notifications,
        trigger=IntervalTrigger(
            seconds=settings.scheduler_interval_seconds,
            timezone=settings.timezone,
        ),
        args=[application],
        id="auto_notifications",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )

    scheduler.start()
    logger.info(
        "Auto notification scheduler started: interval=%s seconds",
        settings.scheduler_interval_seconds,
    )
    return scheduler


def stop_scheduler(application: Application) -> None:
    scheduler = application.bot_data.get("scheduler")
    if scheduler is not None:
        scheduler.shutdown(wait=False)


async def run_auto_notifications(application: Application) -> None:
    settings: Settings = application.bot_data["settings"]
    store: UserSettingsStore = application.bot_data["user_settings_store"]

    users = [user for user in store.list_users() if user.auto_notifications_enabled]
    if not users:
        logger.info("No users enabled for auto notifications")
        return

    users_by_timeframe: dict[str, list[UserSettings]] = defaultdict(list)
    for user in users:
        for timeframe in user.notification_timeframes:
            users_by_timeframe[timeframe].append(user)

    for timeframe, timeframe_users in users_by_timeframe.items():
        try:
            result = await asyncio.to_thread(collect_moex_analysis, settings, timeframe)
        except FileNotFoundError:
            logger.exception("Tickers file was not found")
            continue
        except Exception:
            logger.exception("Failed to build auto notification for %s", timeframe)
            continue

        if result.latest_candle_time is None:
            logger.info("No closed candle data for auto notification %s", timeframe)
            continue

        await process_timeframe_notifications(
            application=application,
            store=store,
            settings=settings,
            timeframe=timeframe,
            users=timeframe_users,
            result=result,
        )


async def process_timeframe_notifications(
    *,
    application: Application,
    store: UserSettingsStore,
    settings: Settings,
    timeframe: str,
    users: list[UserSettings],
    result,
) -> None:
    matched_tickers = [item.ticker for item in result.matched_items]

    for user in users:
        current_user = store.get_user(user.user_id)
        if current_user is None:
            continue
        if not current_user.auto_notifications_enabled:
            continue
        if timeframe not in current_user.notification_timeframes:
            continue
        if current_user.last_sent_candle_times.get(timeframe) == result.latest_candle_time:
            logger.info(
                "Auto notification already processed for user_id=%s timeframe=%s candle=%s",
                current_user.user_id,
                timeframe,
                result.latest_candle_time,
            )
            continue

        if not matched_tickers:
            store.record_auto_result(
                user_id=current_user.user_id,
                timeframe=timeframe,
                candle_time=result.latest_candle_time,
                matched_tickers=[],
            )
            logger.info(
                "No matching tickers for user_id=%s timeframe=%s candle=%s",
                current_user.user_id,
                timeframe,
                result.latest_candle_time,
            )
            continue

        next_streaks = calculate_next_streaks(
            current_user.streaks.get(timeframe, {}),
            matched_tickers,
        )
        text = build_auto_notification_report(
            result,
            timezone_name=settings.timezone_name,
            streaks=next_streaks,
        )

        try:
            await send_scheduled_report(
                application=application,
                chat_id=current_user.chat_id,
                text=text,
            )
        except Exception:
            logger.exception(
                "Failed to send auto notification to chat_id=%s timeframe=%s",
                current_user.chat_id,
                timeframe,
            )
            continue

        store.record_auto_result(
            user_id=current_user.user_id,
            timeframe=timeframe,
            candle_time=result.latest_candle_time,
            matched_tickers=matched_tickers,
        )


async def send_scheduled_report(
    *,
    application: Application,
    chat_id: int,
    text: str,
) -> None:
    chunks = split_telegram_message(text)
    for index, chunk in enumerate(chunks):
        reply_markup = report_actions_keyboard() if index == len(chunks) - 1 else None
        await application.bot.send_message(
            chat_id=chat_id,
            text=chunk,
            reply_markup=reply_markup,
        )
