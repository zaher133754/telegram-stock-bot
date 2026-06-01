from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from config import Settings, load_settings
from moex_client import (
    DailyCloseComparison,
    CurrentQuote,
    MarketDataClient,
    MarketDataError,
    MoexClient,
)


CHECK_NOW_CALLBACK = "check_now"
logger = logging.getLogger(__name__)


class SecretRedactingFilter(logging.Filter):
    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self.secrets = [secret for secret in secrets if secret]

    def filter(self, record: logging.LogRecord) -> bool:
        for secret in self.secrets:
            if isinstance(record.msg, str):
                record.msg = record.msg.replace(secret, "<redacted>")
            if record.args:
                record.args = tuple(
                    arg.replace(secret, "<redacted>") if isinstance(arg, str) else arg
                    for arg in record.args
                )
        return True


def setup_logging(log_file: Path, *, token: str) -> None:
    redacting_filter = SecretRedactingFilter([token])
    console_handler = logging.StreamHandler()
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    console_handler.addFilter(redacting_filter)
    file_handler.addFilter(redacting_filter)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        handlers=[console_handler, file_handler],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def check_now_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Проверить сейчас", callback_data=CHECK_NOW_CALLBACK)]]
    )


def is_user_allowed(update: Update, settings: Settings) -> bool:
    if settings.allowed_user_id is None:
        return True
    user = update.effective_user
    return user is not None and user.id == settings.allowed_user_id


async def deny_access(update: Update) -> None:
    if update.callback_query:
        await update.callback_query.answer("Доступ запрещен", show_alert=True)
        return
    if update.effective_message:
        await update.effective_message.reply_text("Доступ запрещен.")


def get_settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.application.bot_data["settings"]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings(context)
    if not is_user_allowed(update, settings):
        await deny_access(update)
        return

    chat = update.effective_chat
    if chat is None or update.effective_message is None:
        return

    saved_note = ""
    if settings.telegram_chat_id is None:
        settings.chat_id_file.write_text(str(chat.id), encoding="utf-8")
        saved_note = (
            "\n\nЯ сохранил этот chat_id в chat_id.txt. "
            "Также его можно вставить в TELEGRAM_CHAT_ID в файле .env."
        )

    text = (
        "Привет. Я буду отслеживать акции MOEX из tickers.txt.\n\n"
        f"Ваш chat_id: {chat.id}"
        f"{saved_note}\n\n"
        "Нажмите кнопку ниже, чтобы проверить акции сейчас."
    )
    await update.effective_message.reply_text(text, reply_markup=check_now_keyboard())


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings(context)
    if not is_user_allowed(update, settings):
        await deny_access(update)
        return

    text = (
        "Команды:\n"
        "/start - показать кнопку и ваш chat_id\n"
        "/report - вручную запустить отчет по закрытию дня\n"
        "/help - показать справку"
    )
    if update.effective_message:
        await update.effective_message.reply_text(text, reply_markup=check_now_keyboard())


async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings(context)
    if not is_user_allowed(update, settings):
        await deny_access(update)
        return

    if update.effective_message is None:
        return

    status_message = await update.effective_message.reply_text("Готовлю отчет...")
    text = await asyncio.to_thread(build_daily_report, settings)
    await status_message.edit_text(text, reply_markup=check_now_keyboard())


async def check_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings(context)
    if not is_user_allowed(update, settings):
        await deny_access(update)
        return

    query = update.callback_query
    if query is None:
        return

    await query.answer()
    await query.edit_message_text("Проверяю данные...")

    text = await asyncio.to_thread(build_current_report, settings)
    await query.edit_message_text(text, reply_markup=check_now_keyboard())


async def scheduled_daily_report(application: Application) -> None:
    settings: Settings = application.bot_data["settings"]
    chat_id = resolve_report_chat_id(settings)
    if chat_id is None:
        logger.warning(
            "No TELEGRAM_CHAT_ID or chat_id.txt found. Scheduled report was skipped."
        )
        return

    try:
        text = await asyncio.to_thread(build_daily_report, settings)
        await application.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=check_now_keyboard(),
        )
    except Exception:
        logger.exception("Failed to send scheduled daily report")


async def post_init(application: Application) -> None:
    settings: Settings = application.bot_data["settings"]
    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    trigger = CronTrigger(
        hour=settings.report_hour,
        minute=settings.report_minute,
        timezone=settings.timezone,
    )
    scheduler.add_job(
        scheduled_daily_report,
        trigger=trigger,
        args=[application],
        id="daily_report",
        replace_existing=True,
    )
    scheduler.start()
    application.bot_data["scheduler"] = scheduler
    logger.info(
        "Daily report scheduled at %s %s",
        settings.report_time,
        settings.timezone_name,
    )


async def post_shutdown(application: Application) -> None:
    scheduler = application.bot_data.get("scheduler")
    if scheduler is not None:
        scheduler.shutdown(wait=False)


def load_tickers(path: Path) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(f"Tickers file not found: {path}")

    tickers: list[str] = []
    seen: set[str] = set()
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip().upper()
        if not line or line in seen:
            continue
        seen.add(line)
        tickers.append(line)
    return tickers


def resolve_report_chat_id(settings: Settings) -> int | None:
    if settings.telegram_chat_id is not None:
        return settings.telegram_chat_id
    if not settings.chat_id_file.exists():
        return None
    value = settings.chat_id_file.read_text(encoding="utf-8").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        logger.error("Invalid chat_id.txt value: %s", value)
        return None


def create_market_data_client(settings: Settings) -> MarketDataClient:
    return MoexClient(board=settings.moex_board, timeout=settings.moex_timeout_seconds)


def build_current_report(settings: Settings) -> str:
    tickers = load_tickers(settings.tickers_file)
    if not tickers:
        return "В tickers.txt нет тикеров для проверки."

    client = create_market_data_client(settings)
    matched: list[CurrentQuote] = []
    failed: list[tuple[str, str]] = []

    for ticker in tickers:
        try:
            quote = client.get_current_quote(ticker)
        except MarketDataError as exc:
            logger.warning("Failed to get current data for %s: %s", ticker, exc)
            failed.append((ticker, str(exc)))
            continue
        except Exception as exc:
            logger.exception("Unexpected current data error for %s", ticker)
            failed.append((ticker, str(exc)))
            continue

        if quote.current_price > quote.previous_close:
            matched.append(quote)

    now = datetime.now(settings.timezone)
    lines: list[str] = ["Акции сейчас выше предыдущего закрытия", ""]
    if matched:
        for index, quote in enumerate(matched, start=1):
            percent = percent_change(quote.current_price, quote.previous_close)
            lines.extend(
                [
                    (
                        f"{index}. {quote.ticker} - {format_price(quote.current_price)} ₽ "
                        f"/ {format_percent(percent)}"
                    ),
                    f"   Сейчас: {format_price(quote.current_price)} ₽",
                    f"   Пред. закрытие: {format_price(quote.previous_close)} ₽",
                    "",
                ]
            )
    else:
        lines.append("Сейчас нет акций выше предыдущего закрытия")
        lines.append("")

    if failed:
        lines.extend(format_failures(failed))
        lines.append("")

    lines.append(f"Обновлено: {now:%H:%M} {timezone_label(settings)}")
    return "\n".join(lines).strip()


def build_daily_report(settings: Settings) -> str:
    tickers = load_tickers(settings.tickers_file)
    if not tickers:
        return "В tickers.txt нет тикеров для проверки."

    client = create_market_data_client(settings)
    matched: list[DailyCloseComparison] = []
    failed: list[tuple[str, str]] = []
    trade_dates: set = set()

    for ticker in tickers:
        try:
            comparison = client.get_daily_close_comparison(ticker)
        except MarketDataError as exc:
            logger.warning("Failed to get daily data for %s: %s", ticker, exc)
            failed.append((ticker, str(exc)))
            continue
        except Exception as exc:
            logger.exception("Unexpected daily data error for %s", ticker)
            failed.append((ticker, str(exc)))
            continue

        trade_dates.add(comparison.trade_date)
        if comparison.close > comparison.previous_close:
            matched.append(comparison)

    report_date = max(trade_dates) if trade_dates else datetime.now(settings.timezone).date()
    lines: list[str] = ["Акции, закрывшиеся выше предыдущего дня", ""]

    if matched:
        for index, item in enumerate(matched, start=1):
            percent = percent_change(item.close, item.previous_close)
            lines.append(
                f"{index}. {item.ticker} - {format_price(item.close)} ₽ / {format_percent(percent)}"
            )
    else:
        if trade_dates:
            lines.append("Нет акций, закрывшихся выше предыдущего торгового дня.")
        else:
            lines.append("Нет данных по закрытым торговым дням. Возможно, биржа не торговалась.")

    lines.append("")
    if failed:
        lines.extend(format_failures(failed))
        lines.append("")

    lines.append(f"Дата: {report_date:%d.%m.%Y}")
    lines.append("Это не инвестиционная рекомендация.")
    return "\n".join(lines).strip()


def percent_change(current_price: float, previous_close: float) -> float:
    return (current_price - previous_close) / previous_close * 100


def format_price(value: float) -> str:
    if value >= 1000 and value.is_integer():
        return f"{value:.0f}"
    return f"{value:.2f}"


def format_percent(value: float) -> str:
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.2f}%"


def format_failures(failed: list[tuple[str, str]]) -> list[str]:
    lines = ["Не удалось получить данные:"]
    for ticker, error in failed:
        lines.append(f"- {ticker}: {error}")
    return lines


def timezone_label(settings: Settings) -> str:
    if settings.timezone_name == "Europe/Moscow":
        return "МСК"
    return settings.timezone_name


def main() -> None:
    settings = load_settings()
    setup_logging(settings.log_file, token=settings.telegram_bot_token)

    application = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.bot_data["settings"] = settings
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("report", report_command))
    application.add_handler(CallbackQueryHandler(check_now, pattern=f"^{CHECK_NOW_CALLBACK}$"))

    logger.info("Bot is starting")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
