from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_chat_id: int | None
    allowed_user_id: int | None
    report_time: str
    timezone_name: str
    timezone: ZoneInfo
    tickers_file: Path
    chat_id_file: Path
    log_file: Path
    moex_board: str
    moex_timeout_seconds: float

    @property
    def report_hour(self) -> int:
        return int(self.report_time.split(":", 1)[0])

    @property
    def report_minute(self) -> int:
        return int(self.report_time.split(":", 1)[1])


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


def _validate_report_time(value: str) -> str:
    parts = value.split(":")
    if len(parts) != 2:
        raise ValueError("REPORT_TIME must use HH:MM format")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError as exc:
        raise ValueError("REPORT_TIME must use HH:MM format") from exc
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("REPORT_TIME hour must be 0-23 and minute must be 0-59")
    return f"{hour:02d}:{minute:02d}"


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

    return Settings(
        telegram_bot_token=token,
        telegram_chat_id=_optional_int("TELEGRAM_CHAT_ID"),
        allowed_user_id=_optional_int("ALLOWED_USER_ID"),
        report_time=_validate_report_time(_get_env("REPORT_TIME", "23:00")),
        timezone_name=timezone_name,
        timezone=timezone,
        tickers_file=_path_from_env("TICKERS_FILE", "tickers.txt"),
        chat_id_file=_path_from_env("CHAT_ID_FILE", "chat_id.txt"),
        log_file=_path_from_env("LOG_FILE", "bot.log"),
        moex_board=_get_env("MOEX_BOARD", "TQBR").upper(),
        moex_timeout_seconds=timeout_seconds,
    )
