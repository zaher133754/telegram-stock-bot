from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
MOEX_TIMEZONE_NAME = "Europe/Moscow"
SUPPORTED_TIMEFRAMES = ("1m", "10m", "1h", "1d", "1w", "1mo")
TIMEFRAME_ALIASES = {
    "5m": "1m",
    "15m": "10m",
}
WEEKDAY_NAMES = {
    "MONDAY": 0,
    "TUESDAY": 1,
    "WEDNESDAY": 2,
    "THURSDAY": 3,
    "FRIDAY": 4,
    "SATURDAY": 5,
    "SUNDAY": 6,
}


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_chat_id: int | None
    allowed_user_id: int | None
    timezone_name: str
    timezone: ZoneInfo
    market: str
    default_timeframe: str
    default_notification_timeframes: tuple[str, ...]
    auto_notifications: bool
    user_settings_path: Path
    tickers_file: Path
    log_file: Path
    moex_board: str
    moex_timeout_seconds: float
    scheduler_interval_seconds: int
    moex_request_retries: int = 3
    intraday_check_delay_seconds: int = 30
    daily_report_time: time = time(23, 55)
    weekly_report_day: int = WEEKDAY_NAMES["FRIDAY"]
    weekly_report_time: time = time(23, 55)
    monthly_report_time: time = time(23, 55)
    send_empty_reports_for_higher_timeframes: bool = True


def _get_env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _optional_int(name: str) -> int | None:
    value = _get_env(name)
    if not value:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _path_from_env(name: str, default: str) -> Path:
    value = _get_env(name, default)
    path = Path(value)
    if not path.is_absolute():
        path = BASE_DIR / path
    return path


def _bool_from_env(name: str, default: bool) -> bool:
    value = _get_env(name)
    if not value:
        return default
    return value.lower() in {"1", "true", "yes", "y", "on", "да", "вкл"}


def _int_from_env(name: str, default: int) -> int:
    value = _get_env(name)
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return parsed


def _non_negative_int_from_env(name: str, default: int) -> int:
    value = _get_env(name)
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if parsed < 0:
        raise ValueError(f"{name} must be zero or greater")
    return parsed


def _aliased_int_from_env(name: str, legacy_name: str, default: int) -> int:
    if _get_env(name):
        return _int_from_env(name, default)
    return _int_from_env(legacy_name, default)


def _time_from_env(name: str, default: str) -> time:
    value = _get_env(name, default)
    try:
        return datetime.strptime(value, "%H:%M").time()
    except ValueError as exc:
        raise ValueError(f"{name} must use HH:MM format") from exc


def _weekday_from_env(name: str, default: str) -> int:
    value = _get_env(name, default).upper()
    if value not in WEEKDAY_NAMES:
        allowed = ", ".join(WEEKDAY_NAMES)
        raise ValueError(f"{name} must be one of: {allowed}")
    return WEEKDAY_NAMES[value]


def _validate_timeframe(name: str, value: str) -> str:
    normalized = TIMEFRAME_ALIASES.get(value.strip().lower(), value.strip().lower())
    if normalized not in SUPPORTED_TIMEFRAMES:
        allowed = ", ".join(SUPPORTED_TIMEFRAMES)
        raise ValueError(f"{name} must be one of: {allowed}")
    return normalized


def _validate_timeframes(name: str, value: str) -> tuple[str, ...]:
    raw_values = [part.strip() for part in value.split(",") if part.strip()]
    if not raw_values:
        raw_values = ["1d", "1w", "1mo"]

    normalized = {
        _validate_timeframe(name, raw_value)
        for raw_value in raw_values
    }
    normalized.update({"1d", "1w", "1mo"})
    return tuple(
        timeframe for timeframe in SUPPORTED_TIMEFRAMES if timeframe in normalized
    )


def load_settings() -> Settings:
    load_dotenv(BASE_DIR / ".env")

    token = _get_env("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set. Put the bot token into .env.")

    timezone_name = _get_env("TIMEZONE", MOEX_TIMEZONE_NAME)
    if timezone_name != MOEX_TIMEZONE_NAME:
        raise ValueError(f"TIMEZONE must be {MOEX_TIMEZONE_NAME}")
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise RuntimeError(
            f"Timezone {timezone_name!r} was not found. On Windows, install tzdata."
        ) from exc

    timeout_value = _get_env(
        "MOEX_REQUEST_TIMEOUT",
        _get_env("MOEX_TIMEOUT_SECONDS", "20"),
    )
    try:
        timeout_seconds = float(timeout_value)
    except ValueError as exc:
        raise ValueError("MOEX_REQUEST_TIMEOUT must be a number") from exc
    if timeout_seconds <= 0:
        raise ValueError("MOEX_REQUEST_TIMEOUT must be greater than zero")

    market = _get_env("MARKET", "MOEX").upper()
    if market != "MOEX":
        raise ValueError("Only MARKET=MOEX is supported")

    intraday_check_delay_seconds = _non_negative_int_from_env(
        "INTRADAY_CHECK_DELAY_SECONDS",
        30,
    )
    if intraday_check_delay_seconds >= 600:
        raise ValueError("INTRADAY_CHECK_DELAY_SECONDS must be less than 600")

    return Settings(
        telegram_bot_token=token,
        telegram_chat_id=_optional_int("TELEGRAM_CHAT_ID"),
        allowed_user_id=_optional_int("ALLOWED_USER_ID"),
        timezone_name=timezone_name,
        timezone=timezone,
        market=market,
        default_timeframe=_validate_timeframe(
            "DEFAULT_TIMEFRAME",
            _get_env("DEFAULT_TIMEFRAME", "1d"),
        ),
        default_notification_timeframes=_validate_timeframes(
            "DEFAULT_NOTIFICATION_TIMEFRAMES",
            _get_env(
                "DEFAULT_NOTIFICATION_TIMEFRAMES",
                _get_env("DEFAULT_NOTIFICATION_TIMEFRAME", "1d,1w,1mo"),
            ),
        ),
        auto_notifications=_bool_from_env("AUTO_NOTIFICATIONS", True),
        user_settings_path=_path_from_env("USER_SETTINGS_PATH", "user_settings.json"),
        tickers_file=_path_from_env("TICKERS_FILE", "tickers.txt"),
        log_file=_path_from_env("LOG_FILE", "bot.log"),
        moex_board=_get_env("MOEX_BOARD", "TQBR").upper(),
        moex_timeout_seconds=timeout_seconds,
        moex_request_retries=_int_from_env("MOEX_REQUEST_RETRIES", 3),
        scheduler_interval_seconds=_aliased_int_from_env(
            "AUTO_SCHEDULER_INTERVAL_SECONDS",
            "SCHEDULER_INTERVAL_SECONDS",
            60,
        ),
        intraday_check_delay_seconds=intraday_check_delay_seconds,
        daily_report_time=_time_from_env("DAILY_REPORT_TIME", "23:55"),
        weekly_report_day=_weekday_from_env("WEEKLY_REPORT_DAY", "FRIDAY"),
        weekly_report_time=_time_from_env("WEEKLY_REPORT_TIME", "23:55"),
        monthly_report_time=_time_from_env("MONTHLY_REPORT_TIME", "23:55"),
        send_empty_reports_for_higher_timeframes=_bool_from_env(
            "SEND_EMPTY_REPORTS_FOR_HIGHER_TIMEFRAMES",
            True,
        ),
    )
