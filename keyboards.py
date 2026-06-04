from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

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
CALLBACK_TICKERS = "menu:tickers"
CALLBACK_SETTINGS = "menu:settings"
CALLBACK_HELP = "menu:help"
CALLBACK_TIMEFRAME_PREFIX = "tf:"
CALLBACK_NOTIFICATION_TIMEFRAME_PREFIX = "notify_tf:"

LEGACY_CALLBACK_ALIASES = {
    "menu:main": MAIN_MENU,
    "report:check": REFRESH,
    "menu:timeframe": TIMEFRAME_MENU,
}


def normalize_callback_data(data: str) -> str:
    return LEGACY_CALLBACK_ALIASES.get(data, data)


def main_menu_keyboard() -> InlineKeyboardMarkup:
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

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    button_label("1m"),
                    callback_data=f"{prefix}1m",
                ),
                InlineKeyboardButton(
                    button_label("10m"),
                    callback_data=f"{prefix}10m",
                ),
            ],
            [
                InlineKeyboardButton(
                    button_label("1h"),
                    callback_data=f"{prefix}1h",
                ),
                InlineKeyboardButton(
                    button_label("1d"),
                    callback_data=f"{prefix}1d",
                ),
            ],
            [
                InlineKeyboardButton(
                    button_label("1w"),
                    callback_data=f"{prefix}1w",
                ),
                InlineKeyboardButton(
                    button_label("1mo"),
                    callback_data=f"{prefix}1mo",
                ),
            ],
            [InlineKeyboardButton("⬅️ Назад", callback_data=back_to)],
        ]
    )


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
