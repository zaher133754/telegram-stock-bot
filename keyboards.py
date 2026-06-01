from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from utils import TIMEFRAME_LABELS


CALLBACK_START_PANEL = "menu:start"
CALLBACK_RESTART = "menu:restart"
CALLBACK_CHECK_NOW = "report:check"
CALLBACK_TIMEFRAME_MENU = "menu:timeframe"
CALLBACK_NOTIFICATIONS = "menu:notifications"
CALLBACK_TOGGLE_DAILY_REPORT = "notify:daily"
CALLBACK_TOGGLE_WEEKLY_REPORT = "notify:weekly"
CALLBACK_TOGGLE_MONTHLY_REPORT = "notify:monthly"
CALLBACK_LAST_REPORT = "menu:last_report"
CALLBACK_VOLUMES = "menu:volumes"
CALLBACK_TICKERS = "menu:tickers"
CALLBACK_SETTINGS = "menu:settings"
CALLBACK_HELP = "menu:help"
CALLBACK_MAIN_MENU = "menu:main"
CALLBACK_TIMEFRAME_PREFIX = "tf:"


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("▶️ Старт", callback_data=CALLBACK_START_PANEL),
                InlineKeyboardButton("🔄 Рестарт", callback_data=CALLBACK_RESTART),
            ],
            [
                InlineKeyboardButton("🔍 Проверить сейчас", callback_data=CALLBACK_CHECK_NOW),
                InlineKeyboardButton("⏱ Таймфрейм", callback_data=CALLBACK_TIMEFRAME_MENU),
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
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    TIMEFRAME_LABELS["1m"],
                    callback_data=f"{CALLBACK_TIMEFRAME_PREFIX}1m",
                ),
                InlineKeyboardButton(
                    TIMEFRAME_LABELS["10m"],
                    callback_data=f"{CALLBACK_TIMEFRAME_PREFIX}10m",
                ),
            ],
            [
                InlineKeyboardButton(
                    TIMEFRAME_LABELS["1h"],
                    callback_data=f"{CALLBACK_TIMEFRAME_PREFIX}1h",
                ),
                InlineKeyboardButton(
                    TIMEFRAME_LABELS["1d"],
                    callback_data=f"{CALLBACK_TIMEFRAME_PREFIX}1d",
                ),
            ],
            [
                InlineKeyboardButton(
                    TIMEFRAME_LABELS["1w"],
                    callback_data=f"{CALLBACK_TIMEFRAME_PREFIX}1w",
                ),
                InlineKeyboardButton(
                    TIMEFRAME_LABELS["1mo"],
                    callback_data=f"{CALLBACK_TIMEFRAME_PREFIX}1mo",
                ),
            ],
            [InlineKeyboardButton("⬅️ Назад", callback_data=CALLBACK_MAIN_MENU)],
        ]
    )


def after_timeframe_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔍 Проверить сейчас", callback_data=CALLBACK_CHECK_NOW)],
            [InlineKeyboardButton("🔔 Уведомления", callback_data=CALLBACK_NOTIFICATIONS)],
            [InlineKeyboardButton("⬅️ Главное меню", callback_data=CALLBACK_MAIN_MENU)],
        ]
    )


def notifications_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Дневной Вкл/Выкл", callback_data=CALLBACK_TOGGLE_DAILY_REPORT)],
            [InlineKeyboardButton("Недельный Вкл/Выкл", callback_data=CALLBACK_TOGGLE_WEEKLY_REPORT)],
            [InlineKeyboardButton("Месячный Вкл/Выкл", callback_data=CALLBACK_TOGGLE_MONTHLY_REPORT)],
            [InlineKeyboardButton("⬅️ Главное меню", callback_data=CALLBACK_MAIN_MENU)],
        ]
    )


def report_actions_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔄 Обновить", callback_data=CALLBACK_CHECK_NOW)],
            [InlineKeyboardButton("⏱ Таймфрейм", callback_data=CALLBACK_TIMEFRAME_MENU)],
            [InlineKeyboardButton("⬅️ Главное меню", callback_data=CALLBACK_MAIN_MENU)],
        ]
    )


def main_menu_only_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Главное меню", callback_data=CALLBACK_MAIN_MENU)]]
    )
