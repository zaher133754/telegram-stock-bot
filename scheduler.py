from __future__ import annotations

import asyncio
import calendar
import logging
import time
from collections import defaultdict
from datetime import date, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from telegram.ext import Application

from analytics import (
    build_auto_notification_report,
    build_empty_auto_notification_report,
    build_hourly_supplement_report,
    collect_moex_analysis,
)
from config import SUPPORTED_TIMEFRAMES, Settings
from instruments import get_available_tickers
from keyboards import report_actions_keyboard
from moex_client import candle_key
from user_settings import (
    UserSettings,
    UserSettingsStore,
    calculate_streaks_for_new_candle,
    merge_ticker_lists,
    normalize_ticker_list,
)
from utils import split_telegram_message


logger = logging.getLogger(__name__)
AUTO_NOTIFICATIONS_TASK_KEY = "auto_notifications_task"
AUTO_NOTIFICATIONS_LOCK_KEY = "auto_notifications_lock"
AUTO_HOURLY_FIRST_SEEN_KEY = "auto_hourly_first_seen"
AUTO_ONE_MINUTE_LAST_CHECK_KEY = "auto_one_minute_last_check"
AUTO_TIMEFRAME_PRIORITY = ("1h", "10m", "1m", "1d", "1w", "1mo")
EMPTY_REPORT_TIMEFRAMES = frozenset({"1h", "1d", "1w", "1mo"})
DAILY_EMPTY_REPORT_TEXT = "По дневному таймфрейму сигналов нет"
DEFAULT_REPORT_TIME = datetime(2000, 1, 1, 23, 55).time()


def get_expected_candle_key(
    timeframe: str,
    now_msk: datetime,
    *,
    settings: Settings | None = None,
) -> str:
    current_time = (
        _as_settings_timezone(now_msk, settings)
        if settings is not None
        else now_msk
    )

    if timeframe == "1m":
        expected_begin = current_time.replace(second=0, microsecond=0) - timedelta(
            minutes=1
        )
        return expected_begin.strftime("%Y-%m-%d %H:%M")

    if timeframe == "10m":
        boundary_minute = current_time.minute // 10 * 10
        boundary = current_time.replace(
            minute=boundary_minute,
            second=0,
            microsecond=0,
        )
        expected_begin = boundary - timedelta(minutes=10)
        return expected_begin.strftime("%Y-%m-%d %H:%M")

    if timeframe == "1h":
        boundary = current_time.replace(minute=0, second=0, microsecond=0)
        expected_begin = boundary - timedelta(hours=1)
        return expected_begin.strftime("%Y-%m-%d %H:%M")

    if timeframe == "1d":
        report_time = getattr(settings, "daily_report_time", DEFAULT_REPORT_TIME)
        expected_day = current_time.date()
        if current_time.time() < report_time:
            expected_day -= timedelta(days=1)
        return _previous_trading_day(expected_day).isoformat()

    if timeframe == "1w":
        report_day = int(getattr(settings, "weekly_report_day", 4))
        report_time = getattr(settings, "weekly_report_time", DEFAULT_REPORT_TIME)
        week_start = current_time.date() - timedelta(days=current_time.weekday())
        report_date = week_start + timedelta(days=report_day)
        if (
            current_time.date() < report_date
            or current_time.date() == report_date
            and current_time.time() < report_time
        ):
            week_start -= timedelta(days=7)
        iso_year, iso_week, _ = week_start.isocalendar()
        return f"{iso_year}-W{iso_week:02d}"

    if timeframe == "1mo":
        report_time = getattr(settings, "monthly_report_time", DEFAULT_REPORT_TIME)
        last_day = calendar.monthrange(current_time.year, current_time.month)[1]
        if current_time.day == last_day and current_time.time() >= report_time:
            return f"{current_time.year}-{current_time.month:02d}"

        first_day = date(current_time.year, current_time.month, 1)
        previous_month = first_day - timedelta(days=1)
        return f"{previous_month.year}-{previous_month.month:02d}"

    raise ValueError(f"Unsupported timeframe: {timeframe}")


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
        timeframe
        for timeframe in AUTO_TIMEFRAME_PRIORITY
        if timeframe in SUPPORTED_TIMEFRAMES and timeframe in users_by_timeframe
    ]
    logger.info("Enabled timeframes: %s", ", ".join(enabled_timeframes))

    due_timeframes: list[str] = []
    for timeframe in enabled_timeframes:
        reason = timeframe_skip_reason(timeframe, current_time, settings)
        if reason is None and timeframe == "1m":
            reason = one_minute_throttle_skip_reason(application, current_time, settings)
        if reason is not None:
            logger.info("Skipping timeframe: %s, reason: %s", timeframe, reason)
            continue
        due_timeframes.append(timeframe)

    if not due_timeframes:
        logger.info("No timeframes are due for checking")
        return

    for timeframe in due_timeframes:
        await check_timeframe_notifications(
            application=application,
            store=store,
            settings=settings,
            timeframe=timeframe,
            users=users_by_timeframe[timeframe],
            now=current_time,
        )
        if timeframe == "1m":
            application.bot_data[AUTO_ONE_MINUTE_LAST_CHECK_KEY] = current_time


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
    current_time = _as_settings_timezone(
        now or datetime.now(settings.timezone),
        settings,
    )
    expected_candle_key = get_expected_candle_key(
        timeframe,
        current_time,
        settings=settings,
    )
    logger.info("Checking timeframe: %s", timeframe)
    try:
        if timeframe == "1h":
            logger.info(
                "1h notification check: now_msk=%s expected_candle_key=%s "
                "empty_report_enabled=%s",
                current_time.isoformat(sep=" ", timespec="seconds"),
                expected_candle_key,
                settings.send_empty_reports_for_higher_timeframes,
            )

            pending_reason = hourly_confirmation_pending_reason(
                application,
                expected_candle_key=expected_candle_key,
                now=current_time,
                settings=settings,
            )
            if pending_reason is not None:
                logger.info("Skipping timeframe: 1h, reason: %s", pending_reason)
                return

        analysis_groups = build_selected_ticker_groups(
            store=store,
            users=users,
            settings=settings,
            timeframe=timeframe,
        )
        active_users = [
            user
            for selected_tickers, group_users in analysis_groups
            if selected_tickers is not None
            for user in group_users
        ]

        if users and not active_users:
            logger.info(
                "Auto notification skipped: timeframe=%s no users with selected tickers",
                timeframe,
            )
            return

        processed_check_users = active_users if users else users
        if timeframe in {"1m", "10m"} and all_users_processed_candle(
            processed_check_users,
            timeframe=timeframe,
            candle_key=expected_candle_key,
        ):
            logger.info(
                "Auto notification already processed by all users: timeframe=%s candle=%s",
                timeframe,
                expected_candle_key,
            )
            return

        for selected_tickers, group_users in analysis_groups:
            logger.info(
                "Auto notification selected tickers: timeframe=%s users=%s tickers=%s",
                timeframe,
                len(group_users),
                len(selected_tickers) if selected_tickers is not None else "default",
            )
            result = await asyncio.to_thread(
                collect_moex_analysis,
                settings,
                timeframe,
                selected_tickers,
            )
            requests_count += result.moex_requests_count

            latest_available_candle_key = result.latest_candle_key
            if latest_available_candle_key is None:
                logger.info("No closed candle data for auto notification %s", timeframe)
                if timeframe == "1h":
                    log_hourly_auto_debug(
                        now_msk=current_time,
                        expected_candle_key=expected_candle_key,
                        latest_available_candle_key=None,
                        result=result,
                        sent=False,
                        reason="no closed candle data",
                    )
                continue

            if timeframe == "1h":
                logger.info(
                    "1h notification data ready: candle_key=%s matched_tickers=%s",
                    latest_available_candle_key,
                    len(result.matched_items),
                )

            if timeframe != "1d" and latest_available_candle_key < expected_candle_key:
                logger.info(
                    "Expected %s candle %s is not available yet. Latest available: %s",
                    timeframe,
                    short_candle_key_for_log(expected_candle_key, timeframe),
                    short_candle_key_for_log(latest_available_candle_key, timeframe),
                )
                if timeframe == "1h":
                    log_hourly_auto_debug(
                        now_msk=current_time,
                        expected_candle_key=expected_candle_key,
                        latest_available_candle_key=latest_available_candle_key,
                        result=result,
                        sent=False,
                        reason="expected candle not available yet",
                    )
                continue

            if timeframe != "1d" and latest_available_candle_key > expected_candle_key:
                logger.warning(
                    "Latest available %s candle %s is newer than expected %s. "
                    "Auto notification skipped.",
                    timeframe,
                    latest_available_candle_key,
                    expected_candle_key,
                )
                if timeframe == "1h":
                    log_hourly_auto_debug(
                        now_msk=current_time,
                        expected_candle_key=expected_candle_key,
                        latest_available_candle_key=latest_available_candle_key,
                        result=result,
                        sent=False,
                        reason="latest available candle is newer than expected",
                    )
                continue

            if timeframe == "1h" and not mark_hourly_candle_seen_or_ready(
                application,
                candle_key_value=latest_available_candle_key,
                now=current_time,
                settings=settings,
            ):
                log_hourly_auto_debug(
                    now_msk=current_time,
                    expected_candle_key=expected_candle_key,
                    latest_available_candle_key=latest_available_candle_key,
                    result=result,
                    sent=False,
                    reason="hourly confirmation delay is not elapsed",
                )
                continue

            sent = await process_timeframe_notifications(
                application=application,
                store=store,
                settings=settings,
                timeframe=timeframe,
                users=group_users,
                result=result,
            )
            if timeframe == "1h":
                log_hourly_auto_debug(
                    now_msk=current_time,
                    expected_candle_key=expected_candle_key,
                    latest_available_candle_key=latest_available_candle_key,
                    result=result,
                    sent=sent,
                    reason=None if sent else "no message sent",
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


def build_selected_ticker_groups(
    *,
    store: UserSettingsStore,
    users: list[UserSettings],
    settings: Settings,
    timeframe: str,
) -> list[tuple[list[str] | None, list[UserSettings]]]:
    if not users:
        return [(None, users)]

    all_tickers = get_available_tickers(settings.tickers_file, allow_missing=True)
    grouped_users: dict[tuple[str, ...], list[UserSettings]] = defaultdict(list)
    for user in users:
        current_user = store.ensure_selected_tickers(user.user_id, all_tickers)
        selected_tickers = current_user.selected_tickers
        if not selected_tickers:
            logger.info(
                "Auto notification user skipped: user_id=%s timeframe=%s selected_tickers=0",
                current_user.user_id,
                timeframe,
            )
            continue
        grouped_users[tuple(selected_tickers)].append(current_user)

    return [
        (list(selected_tickers), group_users)
        for selected_tickers, group_users in grouped_users.items()
    ]


def should_check_timeframe(
    timeframe: str,
    now: datetime,
    config: Settings,
) -> bool:
    return timeframe_skip_reason(timeframe, now, config) is None


def one_minute_throttle_skip_reason(
    application: Application,
    now: datetime,
    settings: Settings,
) -> str | None:
    interval_seconds = max(
        60,
        int(getattr(settings, "one_minute_check_interval_seconds", 180)),
    )
    last_check = application.bot_data.get(AUTO_ONE_MINUTE_LAST_CHECK_KEY)
    if not isinstance(last_check, datetime):
        return None

    current_time = _as_settings_timezone(now, settings)
    previous_time = _as_settings_timezone(last_check, settings)
    elapsed_seconds = (current_time - previous_time).total_seconds()
    if elapsed_seconds < interval_seconds:
        remaining_seconds = int(interval_seconds - elapsed_seconds)
        return f"1m throttled for {remaining_seconds} more seconds"
    return None


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
        window_minutes = max(
            1,
            min(59, int(getattr(config, "hourly_check_window_minutes", 20))),
        )
        if 1 <= current_time.minute <= window_minutes:
            return None
        return f"not within first {window_minutes} minutes after hour boundary"

    if timeframe == "1d":
        return None

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


def all_users_processed_candle(
    users: list[UserSettings],
    *,
    timeframe: str,
    candle_key: str,
) -> bool:
    return bool(users) and all(
        user.last_processed_candle_keys.get(timeframe) == candle_key
        for user in users
    )


def hourly_confirmation_pending_reason(
    application: Application,
    *,
    expected_candle_key: str,
    now: datetime,
    settings: Settings,
) -> str | None:
    first_seen_by_candle = get_hourly_first_seen_state(application, settings, now)
    first_seen = first_seen_by_candle.get(expected_candle_key)
    if first_seen is None:
        return None

    delay = hourly_confirmation_delay(settings)
    if delay.total_seconds() <= 0:
        return None

    current_time = _as_settings_timezone(now, settings)
    elapsed = current_time - first_seen
    if elapsed >= delay:
        return None

    remaining_seconds = int((delay - elapsed).total_seconds())
    return f"hourly confirmation delay pending for {remaining_seconds} more seconds"


def mark_hourly_candle_seen_or_ready(
    application: Application,
    *,
    candle_key_value: str,
    now: datetime,
    settings: Settings,
) -> bool:
    first_seen_by_candle = get_hourly_first_seen_state(application, settings, now)
    current_time = _as_settings_timezone(now, settings)
    first_seen = first_seen_by_candle.setdefault(candle_key_value, current_time)
    delay = hourly_confirmation_delay(settings)
    if current_time - first_seen < delay:
        logger.info(
            "1h candle confirmation delay: candle=%s first_seen=%s ready_at=%s",
            candle_key_value,
            first_seen.isoformat(sep=" ", timespec="seconds"),
            (first_seen + delay).isoformat(sep=" ", timespec="seconds"),
        )
        return False
    return True


def get_hourly_first_seen_state(
    application: Application,
    settings: Settings,
    now: datetime,
) -> dict[str, datetime]:
    raw_state = application.bot_data.setdefault(AUTO_HOURLY_FIRST_SEEN_KEY, {})
    if not isinstance(raw_state, dict):
        raw_state = {}
        application.bot_data[AUTO_HOURLY_FIRST_SEEN_KEY] = raw_state

    current_time = _as_settings_timezone(now, settings)
    stale_before = current_time - timedelta(hours=6)
    normalized: dict[str, datetime] = {}
    for key, value in raw_state.items():
        if not isinstance(value, datetime):
            continue
        seen_at = _as_settings_timezone(value, settings)
        if seen_at >= stale_before:
            normalized[str(key)] = seen_at

    application.bot_data[AUTO_HOURLY_FIRST_SEEN_KEY] = normalized
    return normalized


def hourly_confirmation_delay(settings: Settings) -> timedelta:
    minutes = max(
        0,
        min(10, int(getattr(settings, "hourly_confirmation_delay_minutes", 5))),
    )
    return timedelta(minutes=minutes)


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


def _previous_trading_day(value: date) -> date:
    while value.weekday() >= 5:
        value -= timedelta(days=1)
    return value


def _as_settings_timezone(value: datetime, settings: Settings) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=settings.timezone)
    return value.astimezone(settings.timezone)


def log_hourly_auto_debug(
    *,
    now_msk: datetime,
    expected_candle_key: str,
    latest_available_candle_key: str | None,
    result,
    sent: bool,
    reason: str | None,
) -> None:
    reference = getattr(result, "reference_comparison", None)
    selected_last = getattr(reference, "last", None) if reference is not None else None
    selected_previous = (
        getattr(reference, "previous", None) if reference is not None else None
    )
    result_label = "sent" if sent else "skip"
    logger.info(
        "1h debug:\n"
        "now_msk=%s\n"
        "expected_candle_key=%s\n"
        "latest_available_candle_key=%s\n"
        "last_hourly_candles=%s\n"
        "selected_last_closed=%s\n"
        "selected_previous_closed=%s\n"
        "result=%s\n"
        "reason=%s",
        now_msk.isoformat(sep=" ", timespec="seconds"),
        expected_candle_key,
        latest_available_candle_key,
        format_debug_candles(getattr(result, "debug_last_candles", [])),
        candle_key(selected_last, "1h") if selected_last is not None else None,
        candle_key(selected_previous, "1h") if selected_previous is not None else None,
        result_label,
        reason,
    )


def format_debug_candles(candles) -> str:
    if not candles:
        return "[]"
    return "[" + ", ".join(candle_key(candle, "1h") for candle in candles[-5:]) + "]"


def short_candle_key_for_log(candle_key_value: str | None, timeframe: str) -> str | None:
    if candle_key_value is None:
        return None
    if timeframe in {"1m", "10m", "1h"}:
        return candle_key_value[-5:]
    return candle_key_value


async def process_timeframe_notifications(
    *,
    application: Application,
    store: UserSettingsStore,
    settings: Settings,
    timeframe: str,
    users: list[UserSettings],
    result,
) -> bool:
    latest_candle_key = result.latest_candle_key
    if latest_candle_key is None:
        return False

    matched_tickers = normalize_ticker_list([item.ticker for item in result.matched_items])
    sent_any = False

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
            if timeframe == "1h":
                sent = await process_hourly_recheck(
                    application=application,
                    store=store,
                    settings=settings,
                    user=current_user,
                    result=result,
                    latest_candle_key=latest_candle_key,
                    matched_tickers=matched_tickers,
                )
                sent_any = sent_any or sent
                continue

            logger.info(
                "Auto notification already processed for user_id=%s timeframe=%s candle=%s",
                current_user.user_id,
                timeframe,
                latest_candle_key,
            )
            continue

        if not matched_tickers:
            send_empty_report = timeframe == "1d" or (
                settings.send_empty_reports_for_higher_timeframes
                and timeframe in EMPTY_REPORT_TIMEFRAMES
            )
            if send_empty_report:
                if timeframe == "1d":
                    text = DAILY_EMPTY_REPORT_TEXT
                else:
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
                sent_any = True

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
        sent_any = True

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

    return sent_any


async def process_hourly_recheck(
    *,
    application: Application,
    store: UserSettingsStore,
    settings: Settings,
    user: UserSettings,
    result,
    latest_candle_key: str,
    matched_tickers: list[str],
) -> bool:
    has_ticker_snapshot = has_auto_report_ticker_snapshot(
        user,
        timeframe="1h",
        candle_key=latest_candle_key,
    )
    known_tickers = known_auto_report_tickers(
        user,
        timeframe="1h",
        candle_key=latest_candle_key,
    )
    new_tickers = [
        ticker
        for ticker in matched_tickers
        if ticker not in set(known_tickers)
    ]
    sent_for_candle = user.last_sent_candle_keys.get("1h") == latest_candle_key

    if sent_for_candle:
        if not has_ticker_snapshot:
            store.record_auto_result(
                user_id=user.user_id,
                timeframe="1h",
                candle_key=latest_candle_key,
                previous_candle_key=result.previous_candle_key,
                matched_tickers=matched_tickers,
                sent=True,
            )
            logger.info(
                "Hourly ticker snapshot initialized without supplement: "
                "user_id=%s candle=%s matched_tickers=%s",
                user.user_id,
                latest_candle_key,
                len(matched_tickers),
            )
            return False

        if not new_tickers:
            logger.info(
                "Hourly auto notification already sent with no new tickers: "
                "user_id=%s candle=%s",
                user.user_id,
                latest_candle_key,
            )
            return False

        next_streaks = calculate_streaks_for_same_candle(
            user,
            timeframe="1h",
            matched_tickers=merge_ticker_lists(known_tickers, matched_tickers),
        )
        text = build_hourly_supplement_report(
            result,
            timezone_name=settings.timezone_name,
            tickers=new_tickers,
            streaks=next_streaks,
        )
        try:
            await send_scheduled_report(
                application=application,
                chat_id=user.chat_id,
                text=text,
            )
        except Exception:
            logger.exception(
                "Failed to send hourly supplement to chat_id=%s",
                user.chat_id,
            )
            return False

        store.record_auto_result(
            user_id=user.user_id,
            timeframe="1h",
            candle_key=latest_candle_key,
            previous_candle_key=result.previous_candle_key,
            matched_tickers=merge_ticker_lists(known_tickers, matched_tickers),
            sent=True,
        )
        logger.info(
            "Hourly supplement sent: user_id=%s candle=%s new_tickers=%s",
            user.user_id,
            latest_candle_key,
            len(new_tickers),
        )
        return True

    if not matched_tickers:
        logger.info(
            "Hourly auto notification already processed without sent report: "
            "user_id=%s candle=%s matched_tickers=0",
            user.user_id,
            latest_candle_key,
        )
        return False

    next_streaks = calculate_streaks_for_same_candle(
        user,
        timeframe="1h",
        matched_tickers=matched_tickers,
    )
    text = build_auto_notification_report(
        result,
        timezone_name=settings.timezone_name,
        streaks=next_streaks,
    )
    try:
        await send_scheduled_report(
            application=application,
            chat_id=user.chat_id,
            text=text,
        )
    except Exception:
        logger.exception(
            "Failed to send delayed hourly auto notification to chat_id=%s",
            user.chat_id,
        )
        return False

    store.record_auto_result(
        user_id=user.user_id,
        timeframe="1h",
        candle_key=latest_candle_key,
        previous_candle_key=result.previous_candle_key,
        matched_tickers=matched_tickers,
        sent=True,
    )
    logger.info(
        "Delayed hourly auto notification sent: user_id=%s candle=%s matched_tickers=%s",
        user.user_id,
        latest_candle_key,
        len(matched_tickers),
    )
    return True


def known_auto_report_tickers(
    user: UserSettings,
    *,
    timeframe: str,
    candle_key: str,
) -> list[str]:
    if user.last_processed_candle_keys.get(timeframe) != candle_key:
        return []
    raw_tickers = getattr(user, "last_auto_report_tickers", {})
    if not isinstance(raw_tickers, dict):
        return []
    return normalize_ticker_list(raw_tickers.get(timeframe, []))


def has_auto_report_ticker_snapshot(
    user: UserSettings,
    *,
    timeframe: str,
    candle_key: str,
) -> bool:
    if user.last_processed_candle_keys.get(timeframe) != candle_key:
        return False
    raw_tickers = getattr(user, "last_auto_report_tickers", {})
    return isinstance(raw_tickers, dict) and timeframe in raw_tickers


def calculate_streaks_for_same_candle(
    user: UserSettings,
    *,
    timeframe: str,
    matched_tickers: list[str],
) -> dict[str, int]:
    current_streaks = dict(user.streaks.get(timeframe, {}))
    for ticker in normalize_ticker_list(matched_tickers):
        current_streaks.setdefault(ticker, 1)
    return current_streaks


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
