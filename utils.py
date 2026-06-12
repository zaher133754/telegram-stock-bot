from __future__ import annotations

from pathlib import Path


TIMEFRAME_LABELS = {
    "1m": "1 минута",
    "2m": "2 минуты",
    "3m": "3 минуты",
    "5m": "5 минут",
    "10m": "10 минут",
    "15m": "15 минут",
    "30m": "30 минут",
    "1h": "1 час",
    "2h": "2 часа",
    "4h": "4 часа",
    "1d": "1 день",
    "1w": "1 неделя",
    "1mo": "1 месяц",
}


def load_tickers(path: Path) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(f"Tickers file not found: {path}")

    tickers: list[str] = []
    seen: set[str] = set()
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip().upper()
        if not line or line in seen:
            continue
        seen.add(line)
        tickers.append(line)
    return tickers


def timeframe_label(timeframe: str) -> str:
    return TIMEFRAME_LABELS.get(timeframe, timeframe)


def timeframe_list_label(timeframes: list[str] | tuple[str, ...]) -> str:
    return ", ".join(timeframe_label(timeframe) for timeframe in timeframes)


def timezone_label(timezone_name: str) -> str:
    if timezone_name == "Europe/Moscow":
        return "МСК"
    return timezone_name


def enabled_label(value: bool) -> str:
    return "включены" if value else "выключены"


def enabled_short_label(value: bool) -> str:
    return "включён" if value else "выключен"


def split_telegram_message(text: str, *, limit: int = 3900) -> list[str]:
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    current_length = 0

    for line in text.splitlines():
        line_length = len(line) + 1
        if current and current_length + line_length > limit:
            chunks.append("\n".join(current).strip())
            current = []
            current_length = 0
        current.append(line)
        current_length += line_length

    if current:
        chunks.append("\n".join(current).strip())

    return [chunk for chunk in chunks if chunk]
