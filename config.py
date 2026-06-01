from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
SUPPORTED_TIMEFRAMES = ("1m", "10m", "1h", "1d", "1w", "1mo")
TIMEFRAME_ALIASES = {
    "5m": "1m",
    "15m": "10m",
}
WEEKDAYS = {
    "MONDAY",
    "TUESDAY",
    "WEDNESDAY",
    "THURSDAY",
    "FRIDAY",
    "SATURDAY",
    "SUNDAY",
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
    auto_daily_report: bool
    auto_weekly_report: bool
    auto_monthly_report: bool
    daily_report_time: str
    weekly_report_day: str
    weekly_report_time: str
    monthly_report_time: str
    user_settings_path: Path
    tickers_file: Path
    log_file: Path
    moex_board: str
    moex_timeout_seconds: float


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


def _validate_timeframe(value: str) -> str:
    value = TIMEFRAME_ALIASES.get(value.strip().lower(), value.strip().lower())
    if value not in SUPPORTED_TIMEFRAMES:
        allowed = ", ".join(SUPPORTED_TIMEFRAMES)
        raise ValueError(f"DEFAULT_TIMEFRAME must be one of: {allowed}")
    return value


def _validate_report_time(name: str, value: str) -> str:
    parts = value.split(":")
    if len(parts) != 2:
        raise ValueError(f"{name} must use HH:MM format")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError as exc:
        raise ValueError(f"{name} must use HH:MM format") from exc
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError(f"{name} hour must be 0-23 and minute must be 0-59")
    return f"{hour:02d}:{minute:02d}"


def _validate_weekday(value: str) -> str:
    value = value.strip().upper()
    if value not in WEEKDAYS:
        allowed = ", ".join(sorted(WEEKDAYS))
        raise ValueError(f"WEEKLY_REPORT_DAY must be one of: {allowed}")
    return value


def load_settings() -> Settings:
    load_dotenv(BASE_DIR / ".env")

    token = _get_env("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set. Put the bot token into .env.")

    timezone_name = _get_env("TIMEZONE", "Europe/Moscow")
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise RuntimeError(
            f"Timezone {timezone_name!r} was not found. On Windows, install tzdata."
        ) from exc

    timeout_value = _get_env("MOEX_TIMEOUT_SECONDS", "10")
    try:
        timeout_seconds = float(timeout_value)
    except ValueError as exc:
        raise ValueError("MOEX_TIMEOUT_SECONDS must be a number") from exc

    market = _get_env("MARKET", "MOEX").upper()
    if market != "MOEX":
        raise ValueError("Only MARKET=MOEX is supported")

    return Settings(
        telegram_bot_token=token,
        telegram_chat_id=_optional_int("TELEGRAM_CHAT_ID"),
        allowed_user_id=_optional_int("ALLOWED_USER_ID"),
        timezone_name=timezone_name,
        timezone=timezone,
        market=market,
        default_timeframe=_validate_timeframe(_get_env("DEFAULT_TIMEFRAME", "1d")),
        auto_daily_report=_bool_from_env("AUTO_DAILY_REPORT", True),
        auto_weekly_report=_bool_from_env("AUTO_WEEKLY_REPORT", True),
        auto_monthly_report=_bool_from_env("AUTO_MONTHLY_REPORT", True),
        daily_report_time=_validate_report_time(
            "DAILY_REPORT_TIME",
            _get_env("DAILY_REPORT_TIME", "23:55"),
        ),
        weekly_report_day=_validate_weekday(_get_env("WEEKLY_REPORT_DAY", "FRIDAY")),
        weekly_report_time=_validate_report_time(
            "WEEKLY_REPORT_TIME",
            _get_env("WEEKLY_REPORT_TIME", "23:55"),
        ),
        monthly_report_time=_validate_report_time(
            "MONTHLY_REPORT_TIME",
            _get_env("MONTHLY_REPORT_TIME", "23:55"),
        ),
        user_settings_path=_path_from_env("USER_SETTINGS_PATH", "user_settings.json"),
        tickers_file=_path_from_env("TICKERS_FILE", "tickers.txt"),
        log_file=_path_from_env("LOG_FILE", "bot.log"),
        moex_board=_get_env("MOEX_BOARD", "TQBR").upper(),
        moex_timeout_seconds=timeout_seconds,
    )
