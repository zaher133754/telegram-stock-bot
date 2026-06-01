from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram.ext import Application

from analytics import (
    build_daily_report,
    build_monthly_report,
    build_weekly_report,
    collect_moex_analysis,
)
from config import Settings
from keyboards import report_actions_keyboard
from user_settings import ReportType, UserSettings, UserSettingsStore
from utils import split_telegram_message


logger = logging.getLogger(__name__)

WEEKDAY_CRON = {
    "MONDAY": "mon",
    "TUESDAY": "tue",
    "WEDNESDAY": "wed",
    "THURSDAY": "thu",
    "FRIDAY": "fri",
    "SATURDAY": "sat",
    "SUNDAY": "sun",
}


@dataclass(frozen=True)
class AutoReportSpec:
    report_type: ReportType
    timeframe: str
    title: str


AUTO_REPORTS = {
    "daily": AutoReportSpec("daily", "1d", "daily"),
    "weekly": AutoReportSpec("weekly", "1w", "weekly"),
    "monthly": AutoReportSpec("monthly", "1mo", "monthly"),
}


def start_scheduler(application: Application) -> AsyncIOScheduler:
    settings: Settings = application.bot_data["settings"]
    scheduler = AsyncIOScheduler(timezone=settings.timezone)

    daily_hour, daily_minute = split_report_time(settings.daily_report_time)
    scheduler.add_job(
        run_auto_report,
        trigger=CronTrigger(
            hour=daily_hour,
            minute=daily_minute,
            timezone=settings.timezone,
        ),
        args=[application, AUTO_REPORTS["daily"]],
        id="daily_report",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )

    weekly_hour, weekly_minute = split_report_time(settings.weekly_report_time)
    scheduler.add_job(
        run_auto_report,
        trigger=CronTrigger(
            day_of_week=WEEKDAY_CRON[settings.weekly_report_day],
            hour=weekly_hour,
            minute=weekly_minute,
            timezone=settings.timezone,
        ),
        args=[application, AUTO_REPORTS["weekly"]],
        id="weekly_report",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )

    monthly_hour, monthly_minute = split_report_time(settings.monthly_report_time)
    scheduler.add_job(
        run_auto_report,
        trigger=CronTrigger(
            hour=monthly_hour,
            minute=monthly_minute,
            timezone=settings.timezone,
        ),
        args=[application, AUTO_REPORTS["monthly"]],
        id="monthly_report",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )

    scheduler.start()
    logger.info(
        "Auto report scheduler started: daily=%s, weekly=%s %s, monthly=%s",
        settings.daily_report_time,
        settings.weekly_report_day,
        settings.weekly_report_time,
        settings.monthly_report_time,
    )
    return scheduler


def stop_scheduler(application: Application) -> None:
    scheduler = application.bot_data.get("scheduler")
    if scheduler is not None:
        scheduler.shutdown(wait=False)


async def run_auto_report(application: Application, spec: AutoReportSpec) -> None:
    settings: Settings = application.bot_data["settings"]
    store: UserSettingsStore = application.bot_data["user_settings_store"]

    users = [user for user in store.list_users() if is_report_enabled(user, spec.report_type)]
    if not users:
        logger.info("No users enabled for %s auto report", spec.report_type)
        return

    try:
        result = await asyncio.to_thread(collect_moex_analysis, settings, spec.timeframe)
    except FileNotFoundError:
        logger.exception("Tickers file was not found")
        return
    except Exception:
        logger.exception("Failed to build %s auto report", spec.report_type)
        return

    if result.latest_candle_time is None:
        logger.info("No closed candle data for %s auto report", spec.report_type)
        return

    text = build_auto_report_text(spec.report_type, result)
    for user in users:
        current_user = store.get_user(user.user_id)
        if current_user is None or not is_report_enabled(current_user, spec.report_type):
            continue
        if get_last_sent_candle(current_user, spec.report_type) == result.latest_candle_time:
            logger.info(
                "%s auto report already sent to user_id=%s for candle=%s",
                spec.report_type,
                current_user.user_id,
                result.latest_candle_time,
            )
            continue

        try:
            await send_scheduled_report(
                application=application,
                chat_id=current_user.chat_id,
                text=text,
            )
        except Exception:
            logger.exception(
                "Failed to send %s report to chat_id=%s",
                spec.report_type,
                current_user.chat_id,
            )
            continue

        store.record_auto_report(
            user_id=current_user.user_id,
            report_type=spec.report_type,
            candle_time=result.latest_candle_time,
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


def build_auto_report_text(report_type: ReportType, result) -> str:
    if report_type == "daily":
        return build_daily_report(result)
    if report_type == "weekly":
        return build_weekly_report(result)
    return build_monthly_report(result)


def is_report_enabled(user: UserSettings, report_type: ReportType) -> bool:
    if report_type == "daily":
        return user.auto_daily_report
    if report_type == "weekly":
        return user.auto_weekly_report
    return user.auto_monthly_report


def get_last_sent_candle(user: UserSettings, report_type: ReportType) -> str | None:
    if report_type == "daily":
        return user.last_sent_daily_candle
    if report_type == "weekly":
        return user.last_sent_weekly_candle
    return user.last_sent_monthly_candle


def split_report_time(value: str) -> tuple[int, int]:
    hour, minute = value.split(":", 1)
    return int(hour), int(minute)
