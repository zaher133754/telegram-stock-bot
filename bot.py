from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from telegram import InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from analytics import (
    TRADING_CONDITION_TEXT,
    build_manual_report,
    build_turnover_report,
    collect_market_analysis,
    format_candle_key_period,
    strip_candle_key_source,
)
from config import INTRADAY_TIMEFRAME_MINUTES, SUPPORTED_TIMEFRAMES, Settings, load_settings
from instruments import get_available_tickers
from keyboards import (
    CALLBACK_HELP,
    CALLBACK_LAST_REPORT,
    CALLBACK_NOTIFICATION_TIMEFRAME_MENU,
    CALLBACK_NOTIFICATION_TIMEFRAME_PREFIX,
    CALLBACK_NOTIFICATIONS,
    CALLBACK_RESTART,
    CALLBACK_SETTINGS,
    CALLBACK_START_PANEL,
    CALLBACK_TICKER_TOGGLE_PREFIX,
    CALLBACK_TICKERS,
    CALLBACK_TICKERS_ALL,
    CALLBACK_TICKERS_NONE,
    CALLBACK_TICKERS_PAGE_PREFIX,
    CALLBACK_TICKERS_SAVE,
    CALLBACK_TIMEFRAME_PREFIX,
    CALLBACK_TOGGLE_NOTIFICATIONS,
    CALLBACK_VOLUMES,
    MAIN_MENU,
    REFRESH,
    TIMEFRAME_MENU,
    after_timeframe_keyboard,
    build_tickers_keyboard,
    main_menu_keyboard,
    main_menu_only_keyboard,
    notification_timeframe_keyboard,
    normalize_callback_data,
    notifications_keyboard,
    report_actions_keyboard,
    timeframe_keyboard,
)
from scheduler import get_expected_candle_key, start_scheduler, stop_scheduler
from user_settings import BASE_NOTIFICATION_TIMEFRAMES, UserSettings, UserSettingsStore
from utils import (
    enabled_label,
    split_telegram_message,
    timeframe_list_label,
    timeframe_label,
    timezone_label,
)


logger = logging.getLogger(__name__)
ACCESS_DENIED_TEXT = "⛔ У вас нет доступа к этому боту."
TELEGRAM_CONNECTION_POOL_SIZE = 32
TELEGRAM_GET_UPDATES_POOL_SIZE = 2
TELEGRAM_POOL_TIMEOUT_SECONDS = 5
TELEGRAM_CONCURRENT_UPDATES = 16
TICKERS_PAGE_SIZE = 20
TICKERS_MENU_PAGE_STATE_KEY = "tickers_menu_pages"
MANUAL_REPORT_LOCKS_KEY = "manual_report_locks"
EMPTY_SELECTED_TICKERS_TEXT = (
    "У вас не выбрано ни одного тикера. Откройте Мои тикеры и выберите акции для отслеживания."
)

AUTO_CANDLE_PHRASES = {
    "1m": "минутной",
    "2m": "2-минутной",
    "3m": "3-минутной",
    "5m": "5-минутной",
    "10m": "10-минутной",
    "15m": "15-минутной",
    "30m": "30-минутной",
    "1h": "часовой",
    "2h": "2-часовой",
    "4h": "4-часовой",
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


def setup_logging(
    log_file: Path,
    *,
    token: str,
    extra_secrets: list[str] | None = None,
) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    redacting_filter = SecretRedactingFilter([token, *(extra_secrets or [])])
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


def get_manual_report_lock(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> asyncio.Lock:
    locks = context.application.bot_data.setdefault(MANUAL_REPORT_LOCKS_KEY, {})
    lock = locks.get(user_id)
    if not isinstance(lock, asyncio.Lock):
        lock = asyncio.Lock()
        locks[user_id] = lock
    return lock


def get_all_tickers(context: ContextTypes.DEFAULT_TYPE, *, allow_missing: bool = True) -> list[str]:
    settings = get_settings(context)
    try:
        return get_available_tickers(settings.tickers_file, allow_missing=allow_missing)
    except FileNotFoundError:
        logger.exception("Tickers file was not found")
        return []


def configured_data_source_label(settings: Settings) -> str:
    token_present = bool(str(getattr(settings, "tinkoff_invest_token", "") or "").strip())
    if getattr(settings, "use_tinvest_as_primary", True) and token_present:
        if getattr(settings, "use_moex_fallback", True):
            return "T-Invest API, fallback MOEX ISS API"
        return "T-Invest API"
    if getattr(settings, "use_moex_fallback", True):
        return "MOEX ISS API"
    return "T-Invest API недоступен"


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
    store.ensure_user(chat_id=chat.id, user_id=user.id)
    return store.ensure_selected_tickers(user.id, get_all_tickers(context))


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


def normalize_reply_button_text(text: str) -> str:
    value = " ".join(text.strip().split()).casefold()
    for marker in ("🔄", "🔍", "⏱", "⬅️", "⬅", "️"):
        value = value.replace(marker, "")
    return " ".join(value.strip().split())


async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    text = str(message.text or "") if message is not None else ""
    user_id = update.effective_user.id if update.effective_user is not None else None
    logger.info("Reply button received: user_id=%s text=%s", user_id, text)

    settings = get_settings(context)
    if not is_user_allowed(update, settings):
        await deny_access(update)
        return

    user_settings = ensure_user_settings(update, context)
    command = normalize_reply_button_text(text)

    if command in {"обновить", "проверить сейчас"}:
        await send_manual_report(update, context, user_settings=user_settings)
    elif command == "таймфрейм":
        await send_timeframe_menu(update, context)
    elif command in {"главное меню", "меню"}:
        await send_main_menu(update, context, user_settings)
    else:
        logger.info("Unknown text command: user_id=%s text=%s", user_id, text)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return

    raw_data = str(query.data or "")
    user_id = update.effective_user.id if update.effective_user is not None else None
    logger.info("Callback received: user_id=%s data=%s", user_id, raw_data)

    try:
        await query.answer()
    except TelegramError:
        logger.exception(
            "Failed to answer callback: user_id=%s data=%s",
            user_id,
            raw_data,
        )

    settings = get_settings(context)
    if not is_user_allowed(update, settings):
        if query.message is not None:
            await query.message.reply_text(ACCESS_DENIED_TEXT)
        return

    user_settings = ensure_user_settings(update, context)
    data = normalize_callback_data(raw_data)

    if data == CALLBACK_START_PANEL:
        await send_start_panel(update, context, user_settings)
    elif data == MAIN_MENU:
        await send_main_menu(update, context, user_settings)
    elif data == CALLBACK_RESTART:
        await restart_user_settings(update, context)
    elif data == TIMEFRAME_MENU:
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
    elif data == REFRESH:
        await send_manual_report(update, context, user_settings=user_settings)
    elif data == CALLBACK_LAST_REPORT:
        await send_last_report(update, context, user_settings=user_settings)
    elif data == CALLBACK_VOLUMES:
        await send_turnover_report(update, context, user_settings=user_settings)
    elif data == CALLBACK_TICKERS:
        await send_tickers_menu(update, context, page=0)
    elif data.startswith(CALLBACK_TICKERS_PAGE_PREFIX):
        await send_tickers_menu(
            update,
            context,
            page=parse_tickers_page(data),
        )
    elif data.startswith(CALLBACK_TICKER_TOGGLE_PREFIX):
        await toggle_ticker_selection(update, context, data)
    elif data == CALLBACK_TICKERS_ALL:
        await select_all_tickers(update, context)
    elif data == CALLBACK_TICKERS_NONE:
        await clear_ticker_selection(update, context)
    elif data == CALLBACK_TICKERS_SAVE:
        await save_ticker_selection(update, context)
    elif data == CALLBACK_SETTINGS:
        await send_settings(update, context, user_settings=user_settings)
    elif data == CALLBACK_HELP:
        await send_help(update, context)
    else:
        await send_text(
            update,
            context,
            "Неизвестная команда. Откройте главное меню.",
            reply_markup=main_menu_only_keyboard(),
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
        f"Источник данных: {configured_data_source_label(settings)}\n"
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

    all_tickers = get_all_tickers(context)
    user_settings = store.reset_user(
        user_id=user.id,
        chat_id=chat.id,
        all_tickers=all_tickers,
    )
    logger.info(
        "User settings restarted: user_id=%s selected_tickers=%s",
        user.id,
        len(user_settings.selected_tickers),
    )
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
    current_user_settings = store.get_user(user.id)
    old_notification_timeframes = (
        list(current_user_settings.notification_timeframes)
        if current_user_settings is not None
        else []
    )
    user_settings = store.toggle_notification_timeframe(user.id, timeframe)
    logger.info(
        "Notification timeframe toggled: user_id=%s timeframe=%s old_notification_timeframes=%s new_notification_timeframes=%s",
        user.id,
        timeframe,
        old_notification_timeframes,
        user_settings.notification_timeframes,
    )
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
    lock = get_manual_report_lock(context, user_settings.user_id)
    if lock.locked():
        logger.info(
            "Manual report skipped: user_id=%s reason=already_running",
            user_settings.user_id,
        )
        await send_text(
            update,
            context,
            "Проверка уже выполняется, подождите немного.",
            reply_markup=report_actions_keyboard(),
        )
        return

    async with lock:
        await send_manual_report_unlocked(
            update,
            context,
            user_settings=user_settings,
        )


async def send_manual_report_unlocked(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    user_settings: UserSettings,
) -> None:
    settings = get_settings(context)
    store = get_user_settings_store(context)
    user_settings = store.ensure_selected_tickers(
        user_settings.user_id,
        get_all_tickers(context),
    )
    selected_tickers = user_settings.selected_tickers
    if not selected_tickers:
        logger.info(
            "Manual report skipped: user_id=%s selected_tickers=0",
            user_settings.user_id,
        )
        await send_text(
            update,
            context,
            EMPTY_SELECTED_TICKERS_TEXT,
            reply_markup=main_menu_keyboard(),
        )
        return

    logger.info(
        "Manual report selected tickers: user_id=%s count=%s",
        user_settings.user_id,
        len(selected_tickers),
    )
    await send_text(update, context, "Готовлю отчёт по закрытым свечам...")

    try:
        result = await asyncio.to_thread(
            collect_market_analysis,
            settings,
            user_settings.timeframe,
            selected_tickers,
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
        warning = build_manual_stale_candle_warning(
            result,
            expected_candle_key=get_expected_candle_key(
                user_settings.timeframe,
                result.updated_at,
                settings=settings,
            ),
            timezone_name=settings.timezone_name,
        )
        if warning:
            text = f"{warning}\n\n{text}"

    store.save_last_report(
        user_id=user_settings.user_id,
        timeframe=user_settings.timeframe,
        text=text,
        candle_time=result.latest_candle_key,
        created_at=result.updated_at.isoformat(),
    )

    await send_text(update, context, text, reply_markup=report_actions_keyboard())


def build_manual_stale_candle_warning(
    result,
    *,
    expected_candle_key: str,
    timezone_name: str,
) -> str | None:
    latest_available_candle_key = getattr(result, "latest_raw_candle_key", None)
    if latest_available_candle_key is None:
        latest_available_candle_key = strip_candle_key_source(result.latest_candle_key)
    if latest_available_candle_key is None:
        return None
    if latest_available_candle_key >= expected_candle_key:
        return None

    suffix = (
        f" {timezone_label(timezone_name)}"
        if result.timeframe in INTRADAY_TIMEFRAME_MINUTES
        else ""
    )
    title = "⚠️ Источник данных ещё не отдал свежую свечу."
    return "\n".join(
        [
            title,
            (
                "Ожидалась свеча: "
                f"{format_candle_key_period(expected_candle_key, result.timeframe)}{suffix}"
            ),
            (
                "Последняя доступная свеча: "
                f"{format_candle_key_period(latest_available_candle_key, result.timeframe)}{suffix}"
            ),
            "",
            "Показан последний доступный отчёт.",
        ]
    )


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
    store = get_user_settings_store(context)
    user_settings = store.ensure_selected_tickers(
        user_settings.user_id,
        get_all_tickers(context),
    )
    selected_tickers = user_settings.selected_tickers
    if not selected_tickers:
        logger.info(
            "Turnover report skipped: user_id=%s selected_tickers=0",
            user_settings.user_id,
        )
        await send_text(
            update,
            context,
            EMPTY_SELECTED_TICKERS_TEXT,
            reply_markup=main_menu_keyboard(),
        )
        return

    logger.info(
        "Turnover report selected tickers: user_id=%s count=%s",
        user_settings.user_id,
        len(selected_tickers),
    )
    await send_text(update, context, "Готовлю отчёт по обороту...")

    try:
        result = await asyncio.to_thread(
            collect_market_analysis,
            settings,
            user_settings.timeframe,
            selected_tickers,
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


async def send_tickers_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    page: int = 0,
) -> None:
    user = update.effective_user
    if user is None:
        return

    store = get_user_settings_store(context)
    all_tickers = get_all_tickers(context)
    user_settings = store.ensure_selected_tickers(user.id, all_tickers)
    set_tickers_menu_page(context, user.id, page)
    logger.info(
        "User opened tickers menu: user_id=%s selected=%s total=%s page=%s",
        user.id,
        len(user_settings.selected_tickers),
        len(all_tickers),
        page,
    )
    await show_tickers_menu(
        update,
        context,
        user_settings=user_settings,
        all_tickers=all_tickers,
        page=page,
    )


async def toggle_ticker_selection(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    callback_data: str,
) -> None:
    user = update.effective_user
    if user is None:
        return

    ticker = callback_data.removeprefix(CALLBACK_TICKER_TOGGLE_PREFIX)
    store = get_user_settings_store(context)
    all_tickers = get_all_tickers(context)
    user_settings = store.toggle_selected_ticker(user.id, ticker, all_tickers)
    page = get_tickers_menu_page(context, user.id)
    logger.info(
        "User toggled ticker: user_id=%s ticker=%s selected=%s total=%s",
        user.id,
        ticker,
        len(user_settings.selected_tickers),
        len(all_tickers),
    )
    await show_tickers_menu(
        update,
        context,
        user_settings=user_settings,
        all_tickers=all_tickers,
        page=page,
    )


async def select_all_tickers(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.effective_user
    if user is None:
        return

    store = get_user_settings_store(context)
    all_tickers = get_all_tickers(context)
    user_settings = store.select_all_tickers(user.id, all_tickers)
    page = get_tickers_menu_page(context, user.id)
    logger.info(
        "User selected all tickers: user_id=%s selected=%s",
        user.id,
        len(user_settings.selected_tickers),
    )
    await show_tickers_menu(
        update,
        context,
        user_settings=user_settings,
        all_tickers=all_tickers,
        page=page,
    )


async def clear_ticker_selection(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.effective_user
    if user is None:
        return

    store = get_user_settings_store(context)
    all_tickers = get_all_tickers(context)
    user_settings = store.clear_selected_tickers(user.id)
    page = get_tickers_menu_page(context, user.id)
    logger.info("User cleared all tickers: user_id=%s", user.id)
    await show_tickers_menu(
        update,
        context,
        user_settings=user_settings,
        all_tickers=all_tickers,
        page=page,
        notice=(
            "Вы сняли все тикеры. Автоуведомления и ручная проверка не будут работать, "
            "пока вы не выберете хотя бы один тикер."
        ),
    )


async def save_ticker_selection(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.effective_user
    if user is None:
        return

    store = get_user_settings_store(context)
    all_tickers = get_all_tickers(context)
    user_settings = store.ensure_selected_tickers(user.id, all_tickers)
    page = get_tickers_menu_page(context, user.id)
    logger.info(
        "User saved tickers: user_id=%s selected=%s total=%s",
        user.id,
        len(user_settings.selected_tickers),
        len(all_tickers),
    )
    await show_tickers_menu(
        update,
        context,
        user_settings=user_settings,
        all_tickers=all_tickers,
        page=page,
        notice=(
            f"✅ Список тикеров сохранён. Выбрано: "
            f"{len(user_settings.selected_tickers)} из {len(all_tickers)}."
        ),
    )


async def show_tickers_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    user_settings: UserSettings,
    all_tickers: list[str],
    page: int,
    notice: str | None = None,
) -> None:
    total_pages = max(1, (len(all_tickers) + TICKERS_PAGE_SIZE - 1) // TICKERS_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    set_tickers_menu_page(context, user_settings.user_id, page)
    text = (
        "📋 Мои тикеры\n\n"
        f"Выбрано: {len(user_settings.selected_tickers)} из {len(all_tickers)}\n\n"
        "Нажмите на тикер, чтобы включить или отключить его.\n\n"
        f"Страница {page + 1}/{total_pages}"
    )
    if notice:
        text = f"{text}\n\n{notice}"

    await edit_or_send_text(
        update,
        context,
        text,
        reply_markup=build_tickers_keyboard(
            user_settings.selected_tickers,
            all_tickers,
            page=page,
            page_size=TICKERS_PAGE_SIZE,
        ),
    )


async def edit_or_send_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup,
) -> None:
    query = update.callback_query
    message = query.message if query is not None else None
    edit_text = getattr(message, "edit_text", None)
    if edit_text is not None:
        try:
            await edit_text(text, reply_markup=reply_markup)
            return
        except TelegramError:
            logger.exception("Failed to edit tickers menu message")

    await send_text(update, context, text, reply_markup=reply_markup)


def parse_tickers_page(callback_data: str) -> int:
    raw_page = callback_data.removeprefix(CALLBACK_TICKERS_PAGE_PREFIX)
    try:
        return max(0, int(raw_page))
    except ValueError:
        return 0


def get_tickers_menu_page(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> int:
    pages = context.application.bot_data.setdefault(TICKERS_MENU_PAGE_STATE_KEY, {})
    if not isinstance(pages, dict):
        pages = {}
        context.application.bot_data[TICKERS_MENU_PAGE_STATE_KEY] = pages
    try:
        return max(0, int(pages.get(user_id, 0)))
    except (TypeError, ValueError):
        return 0


def set_tickers_menu_page(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    page: int,
) -> None:
    pages = context.application.bot_data.setdefault(TICKERS_MENU_PAGE_STATE_KEY, {})
    if not isinstance(pages, dict):
        pages = {}
        context.application.bot_data[TICKERS_MENU_PAGE_STATE_KEY] = pages
    pages[user_id] = max(0, int(page))


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
        f"Источник данных: {configured_data_source_label(settings)}\n"
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
        "Бот отслеживает акции MOEX из tickers.txt через T-Invest API. "
        "MOEX ISS API используется как резервный источник.\n\n"
        "Таймфрейм — период одной свечи: 1m, 2m, 3m, 5m, 10m, 15m, 30m, "
        "1h, 2h, 4h, 1d, 1w или 1mo.\n\n"
        "При проверке бот берёт две последние закрытые свечи выбранного таймфрейма "
        "и проверяет условие: close последней свечи > high предыдущей свечи. "
        "Простыми словами: акция закрылась выше максимума предыдущей свечи.\n\n"
        "Текущая незакрытая свеча не используется. Для T-Invest бот берёт последнюю "
        "закрытую свечу сразу; новые intraday-таймфреймы работают через T-Invest.\n\n"
        "Автоуведомления за 1 день, 1 неделю и 1 месяц включены в базовом наборе. "
        "В разделе уведомлений можно дополнительно включить любой поддерживаемый таймфрейм; "
        "например, 10m будет работать вместе с 1d, 1w и 1mo. Бот проверяет новую "
        "закрытую свечу примерно раз в минуту и не отправляет пустые автоотчёты, "
        "если подходящих тикеров нет.\n\n"
        "Статусы X2, X3, X4 и далее показываются только в автоуведомлениях. X2 означает, "
        "что тикер второй раз подряд попал в выборку на разных закрытых свечах выбранного таймфрейма. "
        "Если тикер перестал попадать, счётчик сбрасывается.\n\n"
        "В отчётах не показывается volume. Оборот считается в рублях; для T-Invest "
        "используется close * volume * lot.\n\n"
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
        return len(get_available_tickers(settings.tickers_file, allow_missing=True))
    except FileNotFoundError:
        return 0


async def post_init(application: Application) -> None:
    settings: Settings = application.bot_data["settings"]
    store: UserSettingsStore = application.bot_data["user_settings_store"]
    all_tickers = get_available_tickers(settings.tickers_file, allow_missing=True)

    if settings.telegram_chat_id is not None:
        bootstrap_user_id = settings.allowed_user_id or settings.telegram_chat_id
        store.bootstrap_user(
            chat_id=settings.telegram_chat_id,
            user_id=bootstrap_user_id,
        )
        store.ensure_selected_tickers(bootstrap_user_id, all_tickers)

    application.bot_data["scheduler"] = start_scheduler(application)


async def post_shutdown(application: Application) -> None:
    await stop_scheduler(application)


def main() -> None:
    settings = load_settings()
    setup_logging(
        settings.log_file,
        token=settings.telegram_bot_token,
        extra_secrets=[settings.tinkoff_invest_token],
    )
    token_present = bool(settings.tinkoff_invest_token)
    logger.info(
        "Market data config: supported_timeframes=%s USE_TINVEST_AS_PRIMARY=%s "
        "USE_MOEX_FALLBACK=%s T-Invest token present=%s",
        ",".join(SUPPORTED_TIMEFRAMES),
        str(settings.use_tinvest_as_primary).lower(),
        str(settings.use_moex_fallback).lower(),
        str(token_present).lower(),
    )
    if not token_present:
        logger.warning("T-Invest token is missing. Using MOEX fallback.")

    user_settings_store = UserSettingsStore(
        settings.user_settings_path,
        default_timeframe=settings.default_timeframe,
        default_notification_timeframes=settings.default_notification_timeframes,
        default_auto_notifications=settings.auto_notifications,
    )

    application = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .connection_pool_size(TELEGRAM_CONNECTION_POOL_SIZE)
        .pool_timeout(TELEGRAM_POOL_TIMEOUT_SECONDS)
        .get_updates_connection_pool_size(TELEGRAM_GET_UPDATES_POOL_SIZE)
        .get_updates_pool_timeout(TELEGRAM_POOL_TIMEOUT_SECONDS)
        .concurrent_updates(TELEGRAM_CONCURRENT_UPDATES)
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
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))

    logger.info("Bot is starting")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
