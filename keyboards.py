from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from config import SUPPORTED_TIMEFRAMES
from utils import TIMEFRAME_LABELS


MAIN_MENU = "main_menu"
REFRESH = "refresh"
TIMEFRAME_MENU = "timeframe_menu"

CALLBACK_START_PANEL = "menu:start"
CALLBACK_RESTART = "menu:restart"
CALLBACK_NOTIFICATIONS = "menu:notifications"
CALLBACK_NOTIFICATION_TIMEFRAME_MENU = "notify:timeframe_menu"
CALLBACK_TOGGLE_NOTIFICATIONS = "notify:toggle"
CALLBACK_LAST_REPORT = "menu:last_report"
CALLBACK_VOLUMES = "menu:volumes"
CALLBACK_TICKERS = "tickers_menu"
CALLBACK_TICKERS_PAGE_PREFIX = "tickers_page:"
CALLBACK_TICKER_TOGGLE_PREFIX = "ticker_toggle:"
CALLBACK_TICKERS_ALL = "tickers_all"
CALLBACK_TICKERS_NONE = "tickers_none"
CALLBACK_TICKERS_SAVE = "tickers_save"
CALLBACK_SETTINGS = "menu:settings"
CALLBACK_HELP = "menu:help"
CALLBACK_TIMEFRAME_PREFIX = "tf:"
CALLBACK_NOTIFICATION_TIMEFRAME_PREFIX = "notify_tf:"

LEGACY_CALLBACK_ALIASES = {
    "menu:main": MAIN_MENU,
    "menu:tickers": CALLBACK_TICKERS,
    "report:check": REFRESH,
    "menu:timeframe": TIMEFRAME_MENU,
}


def normalize_callback_data(data: str) -> str:
    return LEGACY_CALLBACK_ALIASES.get(data, data)


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return build_main_menu_keyboard()


def build_main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("▶️ Старт", callback_data=CALLBACK_START_PANEL),
                InlineKeyboardButton("🔄 Рестарт", callback_data=CALLBACK_RESTART),
            ],
            [
                InlineKeyboardButton("🔍 Проверить сейчас", callback_data=REFRESH),
                InlineKeyboardButton("⏱ Таймфрейм", callback_data=TIMEFRAME_MENU),
            ],
            [
                InlineKeyboardButton("🔔 Уведомления", callback_data=CALLBACK_NOTIFICATIONS),
                InlineKeyboardButton("📄 Последний отчёт", callback_data=CALLBACK_LAST_REPORT),
            ],
            [
                InlineKeyboardButton("💰 Оборот", callback_data=CALLBACK_VOLUMES),
                InlineKeyboardButton("📋 Мои тикеры", callback_data=CALLBACK_TICKERS),
            ],
            [
                InlineKeyboardButton("⚙️ Настройки", callback_data=CALLBACK_SETTINGS),
                InlineKeyboardButton("❓ Помощь", callback_data=CALLBACK_HELP),
            ],
        ]
    )


def build_tickers_keyboard(
    selected_tickers: list[str] | tuple[str, ...],
    all_tickers: list[str] | tuple[str, ...],
    page: int = 0,
    page_size: int = 20,
) -> InlineKeyboardMarkup:
    page_size = max(1, page_size)
    tickers = [str(ticker).strip().upper() for ticker in all_tickers if str(ticker).strip()]
    selected = {str(ticker).strip().upper() for ticker in selected_tickers}
    total_pages = max(1, (len(tickers) + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    page_tickers = tickers[page * page_size : (page + 1) * page_size]

    rows: list[list[InlineKeyboardButton]] = []
    for index in range(0, len(page_tickers), 2):
        row: list[InlineKeyboardButton] = []
        for ticker in page_tickers[index : index + 2]:
            prefix = "✅" if ticker in selected else "❌"
            row.append(
                InlineKeyboardButton(
                    f"{prefix} {ticker}",
                    callback_data=f"{CALLBACK_TICKER_TOGGLE_PREFIX}{ticker}",
                )
            )
        rows.append(row)

    previous_page = max(0, page - 1)
    next_page = min(total_pages - 1, page + 1)
    rows.extend(
        [
            [
                InlineKeyboardButton(
                    "⬅️ Назад",
                    callback_data=f"{CALLBACK_TICKERS_PAGE_PREFIX}{previous_page}",
                ),
                InlineKeyboardButton(
                    "➡️ Далее",
                    callback_data=f"{CALLBACK_TICKERS_PAGE_PREFIX}{next_page}",
                ),
            ],
            [
                InlineKeyboardButton("✅ Выбрать все", callback_data=CALLBACK_TICKERS_ALL),
                InlineKeyboardButton("Снять все", callback_data=CALLBACK_TICKERS_NONE),
            ],
            [InlineKeyboardButton("Сохранить", callback_data=CALLBACK_TICKERS_SAVE)],
            [InlineKeyboardButton("Главное меню", callback_data=MAIN_MENU)],
        ]
    )
    return InlineKeyboardMarkup(rows)


def timeframe_keyboard() -> InlineKeyboardMarkup:
    return build_timeframe_keyboard(CALLBACK_TIMEFRAME_PREFIX, back_to=MAIN_MENU)


def notification_timeframe_keyboard(
    enabled_timeframes: list[str] | tuple[str, ...] | None = None,
) -> InlineKeyboardMarkup:
    return build_timeframe_keyboard(
        CALLBACK_NOTIFICATION_TIMEFRAME_PREFIX,
        back_to=CALLBACK_NOTIFICATIONS,
        enabled_timeframes=enabled_timeframes,
    )


def build_timeframe_keyboard(
    prefix: str,
    *,
    back_to: str,
    enabled_timeframes: list[str] | tuple[str, ...] | None = None,
) -> InlineKeyboardMarkup:
    enabled = set(enabled_timeframes or [])

    def button_label(timeframe: str) -> str:
        label = TIMEFRAME_LABELS[timeframe]
        if timeframe in enabled:
            return f"✅ {label}"
        return label

    rows: list[list[InlineKeyboardButton]] = []
    for index in range(0, len(SUPPORTED_TIMEFRAMES), 2):
        rows.append(
            [
                InlineKeyboardButton(
                    button_label(timeframe),
                    callback_data=f"{prefix}{timeframe}",
                )
                for timeframe in SUPPORTED_TIMEFRAMES[index : index + 2]
            ]
        )
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data=back_to)])
    return InlineKeyboardMarkup(rows)


def after_timeframe_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔍 Проверить сейчас", callback_data=REFRESH)],
            [InlineKeyboardButton("🔔 Уведомления", callback_data=CALLBACK_NOTIFICATIONS)],
            [InlineKeyboardButton("⬅️ Главное меню", callback_data=MAIN_MENU)],
        ]
    )


def notifications_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "⏱ Добавить/убрать таймфреймы",
                    callback_data=CALLBACK_NOTIFICATION_TIMEFRAME_MENU,
                )
            ],
            [
                InlineKeyboardButton(
                    "🔔 Включить/выключить уведомления",
                    callback_data=CALLBACK_TOGGLE_NOTIFICATIONS,
                )
            ],
            [InlineKeyboardButton("🔍 Проверить сейчас", callback_data=REFRESH)],
            [InlineKeyboardButton("⬅️ Главное меню", callback_data=MAIN_MENU)],
        ]
    )


def report_actions_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔄 Обновить", callback_data=REFRESH)],
            [InlineKeyboardButton("⏱ Таймфрейм", callback_data=TIMEFRAME_MENU)],
            [InlineKeyboardButton("⬅️ Главное меню", callback_data=MAIN_MENU)],
        ]
    )


def main_menu_only_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Главное меню", callback_data=MAIN_MENU)]]
    )
