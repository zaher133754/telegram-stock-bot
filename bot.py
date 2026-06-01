from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from telegram import InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from analytics import build_growth_report, build_turnover_report, collect_moex_analysis
from config import SUPPORTED_TIMEFRAMES, Settings, load_settings
from keyboards import (
    CALLBACK_CHECK_NOW,
    CALLBACK_HELP,
    CALLBACK_LAST_REPORT,
    CALLBACK_MAIN_MENU,
    CALLBACK_NOTIFICATIONS,
    CALLBACK_RESTART,
    CALLBACK_SETTINGS,
    CALLBACK_START_PANEL,
    CALLBACK_TICKERS,
    CALLBACK_TIMEFRAME_MENU,
    CALLBACK_TIMEFRAME_PREFIX,
    CALLBACK_TOGGLE_DAILY_REPORT,
    CALLBACK_TOGGLE_MONTHLY_REPORT,
    CALLBACK_TOGGLE_WEEKLY_REPORT,
    CALLBACK_VOLUMES,
    after_timeframe_keyboard,
    main_menu_keyboard,
    notifications_keyboard,
    report_actions_keyboard,
    timeframe_keyboard,
)
from scheduler import start_scheduler, stop_scheduler
from user_settings import UserSettings, UserSettingsStore
from utils import (
    enabled_short_label,
    load_tickers,
    split_telegram_message,
    timeframe_label,
)


logger = logging.getLogger(__name__)
ACCESS_DENIED_TEXT = "⛔ У вас нет доступа к этому боту."


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
    log_file.parent.mkdir(parents=True, exist_ok=True)
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


def get_settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.application.bot_data["settings"]


def get_user_settings_store(context: ContextTypes.DEFAULT_TYPE) -> UserSettingsStore:
    return context.application.bot_data["user_settings_store"]


def is_user_allowed(update: Update, settings: Settings) -> bool:
    if settings.allowed_user_id is None:
        return True
    user = update.effective_user
    return user is not None and user.id == settings.allowed_user_id


async def deny_access(update: Update) -> None:
    if update.callback_query:
        await update.callback_query.answer("Доступ запрещён", show_alert=True)
        if update.callback_query.message:
            await update.callback_query.message.reply_text(ACCESS_DENIED_TEXT)
        return
    if update.effective_message:
        await update.effective_message.reply_text(ACCESS_DENIED_TEXT)


def ensure_user_settings(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> UserSettings:
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or user is None:
        raise RuntimeError("Cannot resolve chat_id or user_id from Telegram update")
    store = get_user_settings_store(context)
    return store.ensure_user(chat_id=chat.id, user_id=user.id)


async def send_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    chunks = split_telegram_message(text)
    for index, chunk in enumerate(chunks):
        markup = reply_markup if index == len(chunks) - 1 else None
        message = update.effective_message
        if message is not None:
            await message.reply_text(chunk, reply_markup=markup)
            continue

        chat = update.effective_chat
        if chat is None:
            return
        await context.bot.send_message(chat_id=chat.id, text=chunk, reply_markup=markup)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings(context)
    if not is_user_allowed(update, settings):
        await deny_access(update)
        return

    user_settings = ensure_user_settings(update, context)
    text = (
        "Привет. Я отслеживаю российские акции MOEX по закрытым свечам.\n\n"
        f"user_id: {user_settings.user_id}\n"
        f"chat_id: {user_settings.chat_id}\n"
        f"Текущий таймфрейм: {timeframe_label(user_settings.timeframe)}\n\n"
        "Нажмите кнопку в главном меню, чтобы проверить акции сейчас или изменить настройки."
    )
    await send_text(update, context, text, reply_markup=main_menu_keyboard())


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings(context)
    if not is_user_allowed(update, settings):
        await deny_access(update)
        return
    ensure_user_settings(update, context)
    await send_help(update, context)


async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings(context)
    if not is_user_allowed(update, settings):
        await deny_access(update)
        return

    user_settings = ensure_user_settings(update, context)
    await send_growth_report(update, context, user_settings=user_settings)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings(context)
    if not is_user_allowed(update, settings):
        await deny_access(update)
        return

    query = update.callback_query
    if query is None:
        return
    await query.answer()

    user_settings = ensure_user_settings(update, context)
    data = query.data or ""

    if data == CALLBACK_START_PANEL:
        await send_start_panel(update, context, user_settings)
    elif data == CALLBACK_MAIN_MENU:
        await send_main_menu(update, context, user_settings)
    elif data == CALLBACK_RESTART:
        await restart_user_settings(update, context)
    elif data == CALLBACK_TIMEFRAME_MENU:
        await send_timeframe_menu(update, context)
    elif data.startswith(CALLBACK_TIMEFRAME_PREFIX):
        await set_timeframe(update, context, data)
    elif data == CALLBACK_NOTIFICATIONS:
        await send_notifications_menu(update, context, user_settings=user_settings)
    elif data == CALLBACK_TOGGLE_DAILY_REPORT:
        await toggle_report_notification(update, context, report_type="daily")
    elif data == CALLBACK_TOGGLE_WEEKLY_REPORT:
        await toggle_report_notification(update, context, report_type="weekly")
    elif data == CALLBACK_TOGGLE_MONTHLY_REPORT:
        await toggle_report_notification(update, context, report_type="monthly")
    elif data == CALLBACK_CHECK_NOW:
        await send_growth_report(update, context, user_settings=user_settings)
    elif data == CALLBACK_LAST_REPORT:
        await send_last_report(update, context, user_settings=user_settings)
    elif data == CALLBACK_VOLUMES:
        await send_turnover_report(update, context, user_settings=user_settings)
    elif data == CALLBACK_TICKERS:
        await send_tickers(update, context, user_settings=user_settings)
    elif data == CALLBACK_SETTINGS:
        await send_settings(update, context, user_settings=user_settings)
    elif data == CALLBACK_HELP:
        await send_help(update, context)
    else:
        await send_text(
            update,
            context,
            "Неизвестная кнопка. Вернитесь в главное меню.",
            reply_markup=main_menu_keyboard(),
        )


async def send_start_panel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_settings: UserSettings,
) -> None:
    settings = get_settings(context)
    tickers_count = count_tickers(settings)
    text = (
        "▶️ Старт\n\n"
        "Текущие настройки:\n"
        f"Таймфрейм: {timeframe_label(user_settings.timeframe)}\n"
        f"Дневной отчёт: {enabled_short_label(user_settings.auto_daily_report)}\n"
        f"Недельный отчёт: {enabled_short_label(user_settings.auto_weekly_report)}\n"
        f"Месячный отчёт: {enabled_short_label(user_settings.auto_monthly_report)}\n"
        f"Количество тикеров: {tickers_count}\n"
        "Источник данных: MOEX ISS API"
    )
    await send_text(update, context, text, reply_markup=main_menu_keyboard())


async def send_main_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_settings: UserSettings,
) -> None:
    text = (
        "Главное меню\n\n"
        f"Таймфрейм: {timeframe_label(user_settings.timeframe)}\n"
        f"Дневной отчёт: {enabled_short_label(user_settings.auto_daily_report)}\n"
        f"Недельный отчёт: {enabled_short_label(user_settings.auto_weekly_report)}\n"
        f"Месячный отчёт: {enabled_short_label(user_settings.auto_monthly_report)}"
    )
    await send_text(update, context, text, reply_markup=main_menu_keyboard())


async def restart_user_settings(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    store = get_user_settings_store(context)
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or user is None:
        return

    user_settings = store.reset_user(user_id=user.id, chat_id=chat.id)
    text = (
        "✅ Настройки сброшены. История диалога сохранена.\n\n"
        f"Таймфрейм: {timeframe_label(user_settings.timeframe)}\n"
        f"Дневной отчёт: {enabled_short_label(user_settings.auto_daily_report)}\n"
        f"Недельный отчёт: {enabled_short_label(user_settings.auto_weekly_report)}\n"
        f"Месячный отчёт: {enabled_short_label(user_settings.auto_monthly_report)}"
    )
    await send_text(update, context, text, reply_markup=main_menu_keyboard())


async def send_timeframe_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await send_text(
        update,
        context,
        "⏱ Выберите таймфрейм:",
        reply_markup=timeframe_keyboard(),
    )


async def set_timeframe(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    callback_data: str,
) -> None:
    timeframe = callback_data.removeprefix(CALLBACK_TIMEFRAME_PREFIX)
    if timeframe not in SUPPORTED_TIMEFRAMES:
        await send_text(
            update,
            context,
            "Этот таймфрейм не поддерживается.",
            reply_markup=timeframe_keyboard(),
        )
        return

    store = get_user_settings_store(context)
    user = update.effective_user
    if user is None:
        return
    store.set_timeframe(user.id, timeframe)
    text = (
        f"✅ Таймфрейм выбран: {timeframe_label(timeframe)}\n\n"
        "Теперь бот будет сравнивать закрытие последней и предпоследней "
        "закрытой свечи на этом таймфрейме."
    )
    await send_text(update, context, text, reply_markup=after_timeframe_keyboard())


async def send_notifications_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    user_settings: UserSettings,
) -> None:
    text = (
        "🔔 Уведомления\n\n"
        f"Дневной отчёт: {enabled_short_label(user_settings.auto_daily_report)}\n"
        f"Недельный отчёт: {enabled_short_label(user_settings.auto_weekly_report)}\n"
        f"Месячный отчёт: {enabled_short_label(user_settings.auto_monthly_report)}"
    )
    await send_text(update, context, text, reply_markup=notifications_keyboard())


async def toggle_report_notification(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    report_type: str,
) -> None:
    store = get_user_settings_store(context)
    user = update.effective_user
    if user is None:
        return

    if report_type == "daily":
        user_settings = store.toggle_daily_report(user.id)
        name = "Дневной отчёт"
        enabled = user_settings.auto_daily_report
    elif report_type == "weekly":
        user_settings = store.toggle_weekly_report(user.id)
        name = "Недельный отчёт"
        enabled = user_settings.auto_weekly_report
    else:
        user_settings = store.toggle_monthly_report(user.id)
        name = "Месячный отчёт"
        enabled = user_settings.auto_monthly_report

    await send_text(
        update,
        context,
        (
            f"🔔 {name}: {enabled_short_label(enabled)}.\n\n"
            "Уведомления\n\n"
            f"Дневной отчёт: {enabled_short_label(user_settings.auto_daily_report)}\n"
            f"Недельный отчёт: {enabled_short_label(user_settings.auto_weekly_report)}\n"
            f"Месячный отчёт: {enabled_short_label(user_settings.auto_monthly_report)}"
        ),
        reply_markup=notifications_keyboard(),
    )


async def send_growth_report(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    user_settings: UserSettings,
) -> None:
    settings = get_settings(context)
    store = get_user_settings_store(context)
    await send_text(update, context, "Готовлю отчёт по закрытым свечам...")

    try:
        result = await asyncio.to_thread(
            collect_moex_analysis,
            settings,
            user_settings.timeframe,
        )
    except FileNotFoundError:
        logger.exception("Tickers file was not found")
        await send_text(
            update,
            context,
            "Файл tickers.txt не найден. Создайте его и добавьте тикеры MOEX.",
            reply_markup=main_menu_keyboard(),
        )
        return
    except Exception:
        logger.exception("Failed to build manual report")
        await send_text(
            update,
            context,
            "Не удалось подготовить отчёт. Подробности записаны в bot.log.",
            reply_markup=main_menu_keyboard(),
        )
        return

    if result.tickers_count == 0:
        text = "В tickers.txt нет тикеров для проверки."
    else:
        text = build_growth_report(result, timezone_name=settings.timezone_name)

    store.save_last_report(
        user_id=user_settings.user_id,
        timeframe=user_settings.timeframe,
        text=text,
        candle_time=result.latest_candle_time,
        created_at=result.updated_at.isoformat(),
    )

    await send_text(update, context, text, reply_markup=report_actions_keyboard())


async def send_last_report(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    user_settings: UserSettings,
) -> None:
    store = get_user_settings_store(context)
    report = store.get_last_report(user_settings.user_id, user_settings.timeframe)
    if report is None:
        await send_text(
            update,
            context,
            "Пока нет сохранённого последнего отчёта. Нажмите ‘Проверить сейчас’.",
            reply_markup=report_actions_keyboard(),
        )
        return

    await send_text(update, context, report.text, reply_markup=report_actions_keyboard())


async def send_turnover_report(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    user_settings: UserSettings,
) -> None:
    settings = get_settings(context)
    await send_text(update, context, "Готовлю отчёт по обороту...")

    try:
        result = await asyncio.to_thread(
            collect_moex_analysis,
            settings,
            user_settings.timeframe,
        )
    except FileNotFoundError:
        logger.exception("Tickers file was not found")
        await send_text(
            update,
            context,
            "Файл tickers.txt не найден. Создайте его и добавьте тикеры MOEX.",
            reply_markup=main_menu_keyboard(),
        )
        return
    except Exception:
        logger.exception("Failed to build turnover report")
        await send_text(
            update,
            context,
            "Не удалось подготовить отчёт по обороту. Подробности записаны в bot.log.",
            reply_markup=main_menu_keyboard(),
        )
        return

    if result.tickers_count == 0:
        text = "В tickers.txt нет тикеров для проверки."
    else:
        text = build_turnover_report(result, timezone_name=settings.timezone_name)
    await send_text(update, context, text, reply_markup=report_actions_keyboard())


async def send_tickers(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    user_settings: UserSettings,
) -> None:
    settings = get_settings(context)
    try:
        tickers = load_tickers(settings.tickers_file)
    except FileNotFoundError:
        logger.exception("Tickers file was not found")
        await send_text(
            update,
            context,
            "Файл tickers.txt не найден.",
            reply_markup=main_menu_keyboard(),
        )
        return

    tickers_text = "\n".join(tickers) if tickers else "Список пуст."
    text = (
        "📋 Мои тикеры\n\n"
        f"Количество тикеров: {len(tickers)}\n\n"
        f"{tickers_text}\n\n"
        "Список можно изменить в файле tickers.txt."
    )
    await send_text(update, context, text, reply_markup=main_menu_keyboard())


async def send_settings(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    user_settings: UserSettings,
) -> None:
    settings = get_settings(context)
    allowed = (
        str(settings.allowed_user_id)
        if settings.allowed_user_id is not None
        else "не задан"
    )
    text = (
        "⚙️ Настройки\n\n"
        f"Таймфрейм для ручной проверки: {timeframe_label(user_settings.timeframe)}\n"
        "Источник данных: MOEX\n"
        f"Количество тикеров: {count_tickers(settings)}\n"
        f"Дневной отчёт: {enabled_short_label(user_settings.auto_daily_report)}\n"
        f"Недельный отчёт: {enabled_short_label(user_settings.auto_weekly_report)}\n"
        f"Месячный отчёт: {enabled_short_label(user_settings.auto_monthly_report)}\n"
        f"Время дневного отчёта: {settings.daily_report_time}\n"
        f"Время недельного отчёта: {settings.weekly_report_day} {settings.weekly_report_time}\n"
        f"Время месячного отчёта: {settings.monthly_report_time}\n"
        f"Timezone: {settings.timezone_name}\n"
        f"chat_id: {user_settings.chat_id}\n"
        f"allowed user: {allowed}"
    )
    await send_text(update, context, text, reply_markup=main_menu_keyboard())


async def send_help(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    text = (
        "❓ Помощь\n\n"
        "Бот отслеживает акции MOEX из tickers.txt через MOEX ISS API.\n\n"
        "Таймфрейм — период одной свечи: 1 минута, 10 минут, 1 час, 1 день, "
        "1 неделя или 1 месяц.\n\n"
        "При проверке бот берёт две последние закрытые свечи выбранного таймфрейма "
        "и сравнивает их close. В отчёт попадает тикер, если close последней "
        "закрытой свечи выше close предыдущей закрытой свечи. Объёмы не показываются; "
        "если MOEX вернул поле value, бот показывает оборот в рублях.\n\n"
        "Автоуведомления по умолчанию отправляются только для дневного, недельного "
        "и месячного отчёта. Таймфреймы 1m, 10m и 1h используются только для ручной проверки.\n\n"
        "Кнопки:\n"
        "▶️ Старт — показать текущие настройки.\n"
        "🔄 Рестарт — сбросить настройки без удаления истории Telegram.\n"
        "🔍 Проверить сейчас — вручную построить отчёт.\n"
        "⏱ Таймфрейм — выбрать период свечи.\n"
        "🔔 Уведомления — включить или выключить дневной, недельный и месячный автоотчёты.\n"
        "📄 Последний отчёт — показать последний сохранённый отчёт.\n"
        "💰 Оборот — показать топ-10 тикеров по обороту последней свечи.\n"
        "📋 Мои тикеры — показать список из tickers.txt.\n"
        "⚙️ Настройки — показать параметры бота."
    )
    await send_text(update, context, text, reply_markup=main_menu_keyboard())


def count_tickers(settings: Settings) -> int:
    try:
        return len(load_tickers(settings.tickers_file))
    except FileNotFoundError:
        return 0


async def post_init(application: Application) -> None:
    settings: Settings = application.bot_data["settings"]
    store: UserSettingsStore = application.bot_data["user_settings_store"]

    if settings.telegram_chat_id is not None:
        bootstrap_user_id = settings.allowed_user_id or settings.telegram_chat_id
        store.bootstrap_user(
            chat_id=settings.telegram_chat_id,
            user_id=bootstrap_user_id,
        )

    application.bot_data["scheduler"] = start_scheduler(application)


async def post_shutdown(application: Application) -> None:
    stop_scheduler(application)


def main() -> None:
    settings = load_settings()
    setup_logging(settings.log_file, token=settings.telegram_bot_token)

    user_settings_store = UserSettingsStore(
        settings.user_settings_path,
        default_timeframe=settings.default_timeframe,
        default_auto_daily_report=settings.auto_daily_report,
        default_auto_weekly_report=settings.auto_weekly_report,
        default_auto_monthly_report=settings.auto_monthly_report,
    )

    application = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.bot_data["settings"] = settings
    application.bot_data["user_settings_store"] = user_settings_store

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("report", report_command))
    application.add_handler(CallbackQueryHandler(handle_callback))

    logger.info("Bot is starting")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
