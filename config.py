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
        scheduler_interval_seconds=_int_from_env("SCHEDULER_INTERVAL_SECONDS", 60),
    )
