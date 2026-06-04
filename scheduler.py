from __future__ import annotations

import asyncio
import calendar
import logging
import time
from collections import defaultdict
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from telegram.ext import Application

from analytics import (
    build_auto_notification_report,
    build_empty_auto_notification_report,
    collect_moex_analysis,
)
from config import SUPPORTED_TIMEFRAMES, Settings
from keyboards import report_actions_keyboard
from user_settings import (
    UserSettings,
    UserSettingsStore,
    calculate_streaks_for_new_candle,
)
from utils import split_telegram_message


logger = logging.getLogger(__name__)
AUTO_NOTIFICATIONS_TASK_KEY = "auto_notifications_task"
AUTO_NOTIFICATIONS_LOCK_KEY = "auto_notifications_lock"
EMPTY_REPORT_TIMEFRAMES = frozenset({"1h", "1d", "1w", "1mo"})


def start_scheduler(application: Application) -> AsyncIOScheduler:
    settings: Settings = application.bot_data["settings"]
    scheduler = AsyncIOScheduler(timezone=settings.timezone)

    scheduler.add_job(
        trigger_auto_notifications,
        trigger=IntervalTrigger(
            seconds=settings.scheduler_interval_seconds,
            timezone=settings.timezone,
        ),
        args=[application],
        id="auto_notifications",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=settings.scheduler_interval_seconds,
        replace_existing=True,
    )

    scheduler.start()
    logger.info(
        "Auto notification scheduler started: interval=%s seconds",
        settings.scheduler_interval_seconds,
    )
    return scheduler


async def stop_scheduler(application: Application) -> None:
    scheduler = application.bot_data.get("scheduler")
    if scheduler is not None and scheduler.running:
        scheduler.shutdown(wait=False)

    task = application.bot_data.get(AUTO_NOTIFICATIONS_TASK_KEY)
    if isinstance(task, asyncio.Task) and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            logger.info("Auto notification background task cancelled")


async def trigger_auto_notifications(application: Application) -> None:
    current_task = application.bot_data.get(AUTO_NOTIFICATIONS_TASK_KEY)
    if isinstance(current_task, asyncio.Task) and not current_task.done():
        logger.info("Auto notifications skipped: previous run is still running")
        return

    task = asyncio.create_task(
        run_auto_notifications(application),
        name="auto_notifications",
    )
    application.bot_data[AUTO_NOTIFICATIONS_TASK_KEY] = task
    task.add_done_callback(log_auto_notification_task_result)


def log_auto_notification_task_result(task: asyncio.Task) -> None:
    if task.cancelled():
        return

    error = task.exception()
    if error is not None:
        logger.error(
            "Auto notification background task failed",
            exc_info=(type(error), error, error.__traceback__),
        )


async def run_auto_notifications(
    application: Application,
    *,
    now: datetime | None = None,
) -> None:
    lock = application.bot_data.setdefault(
        AUTO_NOTIFICATIONS_LOCK_KEY,
        asyncio.Lock(),
    )
    if lock.locked():
        logger.info("Auto notifications skipped: previous run is still running")
        return

    started_at = time.perf_counter()
    logger.info("Auto notifications started")
    try:
        async with lock:
            await _run_auto_notifications(application, now=now)
    finally:
        logger.info(
            "Auto notifications finished in %.1f sec",
            time.perf_counter() - started_at,
        )


async def _run_auto_notifications(
    application: Application,
    *,
    now: datetime | None = None,
) -> None:
    settings: Settings = application.bot_data["settings"]
    store: UserSettingsStore = application.bot_data["user_settings_store"]
    current_time = _as_settings_timezone(now or datetime.now(settings.timezone), settings)

    users = [user for user in store.list_users() if user.auto_notifications_enabled]
    if not users:
        logger.info("No users enabled for auto notifications")
        return

    users_by_timeframe: dict[str, list[UserSettings]] = defaultdict(list)
    for user in users:
        for timeframe in user.notification_timeframes:
            users_by_timeframe[timeframe].append(user)

    enabled_timeframes = [
        timeframe for timeframe in SUPPORTED_TIMEFRAMES if timeframe in users_by_timeframe
    ]
    logger.info("Enabled timeframes: %s", ", ".join(enabled_timeframes))

    due_timeframes: list[str] = []
    for timeframe in enabled_timeframes:
        reason = timeframe_skip_reason(timeframe, current_time, settings)
        if reason is not None:
            logger.info("Skipping timeframe: %s, reason: %s", timeframe, reason)
            continue
        due_timeframes.append(timeframe)

    if not due_timeframes:
        logger.info("No timeframes are due for checking")
        return

    await asyncio.gather(
        *(
            check_timeframe_notifications(
                application=application,
                store=store,
                settings=settings,
                timeframe=timeframe,
                users=users_by_timeframe[timeframe],
                now=current_time,
            )
            for timeframe in due_timeframes
        )
    )


async def check_timeframe_notifications(
    *,
    application: Application,
    store: UserSettingsStore,
    settings: Settings,
    timeframe: str,
    users: list[UserSettings],
    now: datetime | None = None,
) -> None:
    started_at = time.perf_counter()
    requests_count = 0
    logger.info("Checking timeframe: %s", timeframe)
    try:
        if timeframe == "1h":
            current_time = _as_settings_timezone(
                now or datetime.now(settings.timezone),
                settings,
            )
            logger.info(
                "1h notification check: now_msk=%s empty_report_enabled=%s",
                current_time.isoformat(sep=" ", timespec="seconds"),
                settings.send_empty_reports_for_higher_timeframes,
            )

        result = await asyncio.to_thread(collect_moex_analysis, settings, timeframe)
        requests_count = result.moex_requests_count

        if result.latest_candle_key is None:
            logger.info("No closed candle data for auto notification %s", timeframe)
            return

        if timeframe == "1h":
            logger.info(
                "1h notification data ready: candle_key=%s matched_tickers=%s",
                result.latest_candle_key,
                len(result.matched_items),
            )

        await process_timeframe_notifications(
            application=application,
            store=store,
            settings=settings,
            timeframe=timeframe,
            users=users,
            result=result,
        )
    except FileNotFoundError:
        logger.exception("Tickers file was not found")
    except Exception:
        logger.exception("Failed to build auto notification for %s", timeframe)
    finally:
        logger.info(
            "Timeframe check finished: timeframe=%s duration=%.1f sec moex_requests=%s",
            timeframe,
            time.perf_counter() - started_at,
            requests_count,
        )


def should_check_timeframe(
    timeframe: str,
    now: datetime,
    config: Settings,
) -> bool:
    return timeframe_skip_reason(timeframe, now, config) is None


def timeframe_skip_reason(
    timeframe: str,
    now: datetime,
    config: Settings,
) -> str | None:
    current_time = _as_settings_timezone(now, config)
    interval_seconds = max(1, config.scheduler_interval_seconds)

    if timeframe == "1m":
        return None

    if timeframe == "10m":
        seconds_since_boundary = current_time.minute % 10 * 60 + current_time.second
        if _is_in_delay_window(
            seconds_since_boundary,
            delay_seconds=config.intraday_check_delay_seconds,
            interval_seconds=interval_seconds,
        ):
            return None
        return "not 10-minute candle boundary"

    if timeframe == "1h":
        if 1 <= current_time.minute <= 5:
            return None
        return "not within first five minutes after hour boundary"

    if timeframe == "1d":
        if _is_in_scheduled_time_window(
            current_time,
            config.daily_report_time,
            interval_seconds,
        ):
            return None
        return "not daily report time"

    if timeframe == "1w":
        if current_time.weekday() != config.weekly_report_day:
            return "not weekly report day"
        if _is_in_scheduled_time_window(
            current_time,
            config.weekly_report_time,
            interval_seconds,
        ):
            return None
        return "not weekly report time"

    if timeframe == "1mo":
        if not _is_month_end_check_day(current_time):
            return "not month-end check day"
        if _is_in_scheduled_time_window(
            current_time,
            config.monthly_report_time,
            interval_seconds,
        ):
            return None
        return "not monthly report time"

    return "unsupported timeframe"


def _is_in_delay_window(
    seconds_since_boundary: int,
    *,
    delay_seconds: int,
    interval_seconds: int,
) -> bool:
    return delay_seconds <= seconds_since_boundary < delay_seconds + interval_seconds


def _is_in_scheduled_time_window(
    now: datetime,
    scheduled_time,
    interval_seconds: int,
) -> bool:
    scheduled_at = datetime.combine(
        now.date(),
        scheduled_time,
        tzinfo=now.tzinfo,
    )
    elapsed_seconds = (now - scheduled_at).total_seconds()
    return 0 <= elapsed_seconds < interval_seconds


def _is_month_end_check_day(now: datetime) -> bool:
    last_day = calendar.monthrange(now.year, now.month)[1]
    return now.day >= last_day - 3 or now.day <= 3


def _as_settings_timezone(value: datetime, settings: Settings) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=settings.timezone)
    return value.astimezone(settings.timezone)


async def process_timeframe_notifications(
    *,
    application: Application,
    store: UserSettingsStore,
    settings: Settings,
    timeframe: str,
    users: list[UserSettings],
    result,
) -> None:
    latest_candle_key = result.latest_candle_key
    if latest_candle_key is None:
        return

    matched_tickers = [item.ticker for item in result.matched_items]

    for user in users:
        current_user = store.get_user(user.user_id)
        if current_user is None:
            continue
        if not current_user.auto_notifications_enabled:
            continue
        if timeframe not in current_user.notification_timeframes:
            continue
        last_processed_candle_key = current_user.last_processed_candle_keys.get(timeframe)
        if timeframe == "1h":
            logger.info(
                "1h notification decision: user_id=%s candle_key=%s "
                "last_processed_candle_key=%s last_sent_candle_key=%s "
                "matched_tickers=%s empty_report_enabled=%s",
                current_user.user_id,
                latest_candle_key,
                last_processed_candle_key,
                current_user.last_sent_candle_keys.get(timeframe),
                len(matched_tickers),
                settings.send_empty_reports_for_higher_timeframes,
            )
        if last_processed_candle_key == latest_candle_key:
            logger.info(
                "Auto notification already processed for user_id=%s timeframe=%s candle=%s",
                current_user.user_id,
                timeframe,
                latest_candle_key,
            )
            continue

        if not matched_tickers:
            send_empty_report = (
                settings.send_empty_reports_for_higher_timeframes
                and timeframe in EMPTY_REPORT_TIMEFRAMES
            )
            if send_empty_report:
                text = build_empty_auto_notification_report(
                    result,
                    timezone_name=settings.timezone_name,
                )
                try:
                    await send_scheduled_report(
                        application=application,
                        chat_id=current_user.chat_id,
                        text=text,
                    )
                except Exception:
                    logger.exception(
                        "Failed to send empty auto notification to chat_id=%s timeframe=%s",
                        current_user.chat_id,
                        timeframe,
                    )
                    continue

            store.record_auto_result(
                user_id=current_user.user_id,
                timeframe=timeframe,
                candle_key=latest_candle_key,
                previous_candle_key=result.previous_candle_key,
                matched_tickers=[],
                sent=send_empty_report,
            )
            if send_empty_report:
                logger.info(
                    "No matching tickers for timeframe=%s candle=%s. Empty report sent.",
                    timeframe,
                    latest_candle_key,
                )
            elif timeframe in EMPTY_REPORT_TIMEFRAMES:
                logger.info(
                    "No matching tickers for timeframe=%s candle=%s. "
                    "Empty report skipped by settings.",
                    timeframe,
                    latest_candle_key,
                )
            else:
                logger.info(
                    "No matching tickers for timeframe=%s candle=%s. "
                    "Empty report skipped for low timeframe.",
                    timeframe,
                    latest_candle_key,
                )
            continue

        next_streaks = calculate_streaks_for_new_candle(
            current_user.streaks.get(timeframe, {}),
            matched_tickers,
            last_processed_candle_key=last_processed_candle_key,
            previous_candle_key=result.previous_candle_key,
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
            candle_key=latest_candle_key,
            previous_candle_key=result.previous_candle_key,
            matched_tickers=matched_tickers,
            sent=True,
        )
        logger.info(
            "Auto notification sent: user_id=%s timeframe=%s candle=%s "
            "matched_tickers=%s",
            current_user.user_id,
            timeframe,
            latest_candle_key,
            len(matched_tickers),
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
