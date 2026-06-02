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

from analytics import (
    TRADING_CONDITION_TEXT,
    build_manual_report,
    build_turnover_report,
    collect_moex_analysis,
)
from config import SUPPORTED_TIMEFRAMES, Settings, load_settings
from keyboards import (
    CALLBACK_CHECK_NOW,
    CALLBACK_HELP,
    CALLBACK_LAST_REPORT,
    CALLBACK_MAIN_MENU,
    CALLBACK_NOTIFICATION_TIMEFRAME_MENU,
    CALLBACK_NOTIFICATION_TIMEFRAME_PREFIX,
    CALLBACK_NOTIFICATIONS,
    CALLBACK_RESTART,
    CALLBACK_SETTINGS,
    CALLBACK_START_PANEL,
    CALLBACK_TICKERS,
    CALLBACK_TIMEFRAME_MENU,
    CALLBACK_TIMEFRAME_PREFIX,
    CALLBACK_TOGGLE_NOTIFICATIONS,
    CALLBACK_VOLUMES,
    after_timeframe_keyboard,
    main_menu_keyboard,
    notification_timeframe_keyboard,
    notifications_keyboard,
    report_actions_keyboard,
    timeframe_keyboard,
)
from scheduler import start_scheduler, stop_scheduler
from user_settings import BASE_NOTIFICATION_TIMEFRAMES, UserSettings, UserSettingsStore
from utils import (
    enabled_label,
    load_tickers,
    split_telegram_message,
    timeframe_list_label,
    timeframe_label,
)


logger = logging.getLogger(__name__)
ACCESS_DENIED_TEXT = "⛔ У вас нет доступа к этому боту."

AUTO_CANDLE_PHRASES = {
    "1m": "минутной",
    "10m": "10-минутной",
    "1h": "часовой",
    "1d": "дневной",
    "1w": "недельной",
    "1mo": "месячной",
}


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
        f"Таймфрейм ручной проверки: {timeframe_label(user_settings.timeframe)}\n"
        "Логика отбора: close последней свечи > high предыдущей свечи.\n\n"
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
    await send_manual_report(update, context, user_settings=user_settings)


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
    elif data == CALLBACK_NOTIFICATION_TIMEFRAME_MENU:
        await send_notification_timeframe_menu(
            update,
            context,
            user_settings=user_settings,
        )
    elif data.startswith(CALLBACK_NOTIFICATION_TIMEFRAME_PREFIX):
        await set_notification_timeframe(update, context, data)
    elif data == CALLBACK_TOGGLE_NOTIFICATIONS:
        await toggle_auto_notifications(update, context)
    elif data == CALLBACK_CHECK_NOW:
        await send_manual_report(update, context, user_settings=user_settings)
    elif data == CALLBACK_LAST_REPORT:
        await send_last_report(update, context, user_settings=user_settings)
    elif data == CALLBACK_VOLUMES:
        await send_turnover_report(update, context, user_settings=user_settings)
    elif data == CALLBACK_TICKERS:
        await send_tickers(update, context)
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
        f"Таймфрейм ручной проверки: {timeframe_label(user_settings.timeframe)}\n"
        f"Таймфреймы автоуведомлений: {timeframe_list_label(user_settings.notification_timeframes)}\n"
        f"Уведомления: {enabled_label(user_settings.auto_notifications_enabled)}\n"
        f"Количество тикеров: {tickers_count}\n"
        "Источник данных: MOEX ISS API\n"
        f"Логика отбора: {TRADING_CONDITION_TEXT}"
    )
    await send_text(update, context, text, reply_markup=main_menu_keyboard())


async def send_main_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_settings: UserSettings,
) -> None:
    text = (
        "Главное меню\n\n"
        f"Таймфрейм ручной проверки: {timeframe_label(user_settings.timeframe)}\n"
        f"Таймфреймы автоуведомлений: {timeframe_list_label(user_settings.notification_timeframes)}\n"
        f"Уведомления: {enabled_label(user_settings.auto_notifications_enabled)}"
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
        f"Таймфрейм ручной проверки: {timeframe_label(user_settings.timeframe)}\n"
        f"Таймфреймы автоуведомлений: {timeframe_list_label(user_settings.notification_timeframes)}\n"
        f"Уведомления: {enabled_label(user_settings.auto_notifications_enabled)}"
    )
    await send_text(update, context, text, reply_markup=main_menu_keyboard())


async def send_timeframe_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await send_text(
        update,
        context,
        "⏱ Выберите таймфрейм ручной проверки:",
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
        f"✅ Таймфрейм ручной проверки выбран: {timeframe_label(timeframe)}\n\n"
        f"Теперь бот будет проверять условие: {TRADING_CONDITION_TEXT}."
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
        f"Таймфреймы автоуведомлений: "
        f"{timeframe_list_label(user_settings.notification_timeframes)}\n"
        f"Статус: {enabled_label(user_settings.auto_notifications_enabled)}\n\n"
        "1 день, 1 неделя и 1 месяц включены в базовом наборе. "
        "Короткие таймфреймы можно добавить отдельно."
    )
    await send_text(update, context, text, reply_markup=notifications_keyboard())


async def send_notification_timeframe_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    user_settings: UserSettings,
) -> None:
    await send_text(
        update,
        context,
        "⏱ Выберите таймфрейм уведомлений:\n\n"
        "✅ отмечены уже включённые. 1 день, 1 неделя и 1 месяц включены по умолчанию.",
        reply_markup=notification_timeframe_keyboard(user_settings.notification_timeframes),
    )


async def set_notification_timeframe(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    callback_data: str,
) -> None:
    timeframe = callback_data.removeprefix(CALLBACK_NOTIFICATION_TIMEFRAME_PREFIX)
    if timeframe not in SUPPORTED_TIMEFRAMES:
        await send_text(
            update,
            context,
            "Этот таймфрейм не поддерживается.",
            reply_markup=notification_timeframe_keyboard(),
        )
        return

    store = get_user_settings_store(context)
    user = update.effective_user
    if user is None:
        return
    user_settings = store.toggle_notification_timeframe(user.id, timeframe)
    candle_phrase = AUTO_CANDLE_PHRASES.get(timeframe, timeframe_label(timeframe))
    if timeframe in BASE_NOTIFICATION_TIMEFRAMES:
        text = (
            f"✅ {timeframe_label(timeframe)} уже включён по умолчанию.\n\n"
            "Дневные, недельные и месячные уведомления остаются включёнными "
            "в базовом наборе."
        )
    elif timeframe in user_settings.notification_timeframes:
        text = (
            f"✅ Таймфрейм уведомлений добавлен: {timeframe_label(timeframe)}\n\n"
            f"Теперь бот будет отправлять уведомления после закрытия каждой новой "
            f"{candle_phrase} свечи, если найдёт тикеры, у которых close последней "
            "свечи выше high предыдущей свечи.\n\n"
            "Базовые уведомления за 1 день, 1 неделю и 1 месяц остаются включёнными."
        )
    else:
        text = (
            f"☑️ Таймфрейм уведомлений отключён: {timeframe_label(timeframe)}\n\n"
            "Базовые уведомления за 1 день, 1 неделю и 1 месяц остаются включёнными."
        )
    await send_text(
        update,
        context,
        text,
        reply_markup=notification_timeframe_keyboard(user_settings.notification_timeframes),
    )


async def toggle_auto_notifications(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    store = get_user_settings_store(context)
    user = update.effective_user
    if user is None:
        return

    user_settings = store.toggle_auto_notifications(user.id)
    await send_notifications_menu(update, context, user_settings=user_settings)


async def send_manual_report(
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
        text = build_manual_report(result, timezone_name=settings.timezone_name)

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
            "Пока нет сохранённого последнего отчёта. Нажмите «Проверить сейчас».",
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
    text = (
        "⚙️ Настройки\n\n"
        f"Таймфрейм ручной проверки: {timeframe_label(user_settings.timeframe)}\n"
        f"Таймфреймы автоуведомлений: {timeframe_list_label(user_settings.notification_timeframes)}\n"
        f"Уведомления: {enabled_label(user_settings.auto_notifications_enabled)}\n"
        "Источник данных: MOEX\n"
        f"Количество тикеров: {count_tickers(settings)}\n"
        f"Timezone: {settings.timezone_name}\n"
        f"chat_id: {user_settings.chat_id}\n"
        f"user_id: {user_settings.user_id}\n"
        f"Логика отбора: {TRADING_CONDITION_TEXT}"
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
        "и проверяет условие: close последней свечи > high предыдущей свечи. "
        "Простыми словами: акция закрылась выше максимума предыдущей свечи.\n\n"
        "Текущая незакрытая свеча не используется. Для 1m и 10m бот берёт готовые "
        "свечи MOEX ISS без ручной агрегации.\n\n"
        "Автоуведомления за 1 день, 1 неделю и 1 месяц включены в базовом наборе. "
        "В разделе уведомлений можно дополнительно включить 1m, 10m или 1h; "
        "например, 10m будет работать вместе с 1d, 1w и 1mo. Бот проверяет новую "
        "закрытую свечу примерно раз в минуту и не отправляет пустые автоотчёты, "
        "если подходящих тикеров нет.\n\n"
        "Статусы X2, X3, X4 показываются только в автоуведомлениях. X2 означает, "
        "что тикер второй раз подряд попал в автоотчёт на выбранном таймфрейме. "
        "Если тикер перестал попадать, счётчик сбрасывается.\n\n"
        "В отчётах не показывается volume. Если MOEX вернул поле value, бот "
        "показывает оборот в рублях.\n\n"
        "Кнопки:\n"
        "▶️ Старт — показать текущие настройки.\n"
        "🔄 Рестарт — сбросить настройки без удаления истории Telegram.\n"
        "🔍 Проверить сейчас — вручную построить отчёт.\n"
        "⏱ Таймфрейм — выбрать период свечи для ручной проверки.\n"
        "🔔 Уведомления — добавить короткие таймфреймы к базовым автоуведомлениям.\n"
        "📄 Последний отчёт — показать последний сохранённый ручной отчёт.\n"
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
        default_notification_timeframes=settings.default_notification_timeframes,
        default_auto_notifications=settings.auto_notifications,
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
