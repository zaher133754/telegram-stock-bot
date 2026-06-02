from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from typing import Any, Iterable, Protocol
from zoneinfo import ZoneInfo

import requests


logger = logging.getLogger(__name__)


class MarketDataError(RuntimeError):
    pass


@dataclass(frozen=True)
class Candle:
    ticker: str
    begin: datetime
    end: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    value: float | None = None


@dataclass(frozen=True)
class CurrentQuote:
    ticker: str
    current_price: float
    previous_close: float


@dataclass(frozen=True)
class DailyCloseComparison:
    ticker: str
    trade_date: date
    close: float
    previous_trade_date: date
    previous_close: float


class MarketDataClient(Protocol):
    def get_candles(self, ticker: str, timeframe: str, limit: int = 2) -> list[Candle]:
        ...

    def get_last_two_closed_candles(self, ticker: str, timeframe: str) -> list[Candle]:
        ...


DIRECT_TIMEFRAME_INTERVALS = {
    "1m": 1,
    "10m": 10,
    "1h": 60,
    "1d": 24,
    "1w": 7,
    "1mo": 31,
}
SUPPORTED_TIMEFRAMES = set(DIRECT_TIMEFRAME_INTERVALS)


def get_candles(
    ticker: str,
    timeframe: str,
    limit: int = 2,
    *,
    board: str = "TQBR",
    timeout: float = 10,
    timezone: tzinfo | None = None,
) -> list[Candle]:
    client = MoexClient(board=board, timeout=timeout, timezone=timezone)
    return client.get_candles(ticker, timeframe, limit=limit)


def get_last_two_closed_candles(
    ticker: str,
    timeframe: str,
    *,
    board: str = "TQBR",
    timeout: float = 10,
    timezone: tzinfo | None = None,
) -> list[Candle]:
    client = MoexClient(board=board, timeout=timeout, timezone=timezone)
    return client.get_last_two_closed_candles(ticker, timeframe)


def normalize_candle_data(
    ticker: str,
    rows: Iterable[dict[str, Any]],
    *,
    timezone: tzinfo | None = None,
) -> list[Candle]:
    timezone = timezone or ZoneInfo("Europe/Moscow")
    candles: list[Candle] = []

    for raw_row in rows:
        row = {str(key).upper(): value for key, value in raw_row.items()}
        begin = _parse_moex_datetime(_pick(row, "BEGIN"), timezone)
        end = _parse_moex_datetime(_pick(row, "END"), timezone)
        open_price = _to_float(_pick(row, "OPEN"))
        close_price = _to_float(_pick(row, "CLOSE"))
        high_price = _to_float(_pick(row, "HIGH"))
        low_price = _to_float(_pick(row, "LOW"))
        volume = _to_float(_pick(row, "VOLUME")) or 0.0
        value = _to_float(_pick(row, "VALUE"))

        if (
            begin is None
            or open_price is None
            or close_price is None
            or high_price is None
            or low_price is None
        ):
            continue

        candles.append(
            Candle(
                ticker=ticker.upper(),
                begin=begin,
                end=end or begin,
                open=open_price,
                high=high_price,
                low=low_price,
                close=close_price,
                volume=volume,
                value=value,
            )
        )

    candles.sort(key=lambda candle: (candle.end, candle.begin))
    return candles


def handle_moex_errors(error: Exception) -> MarketDataError:
    if isinstance(error, MarketDataError):
        return error
    if isinstance(error, requests.HTTPError):
        response = error.response
        if response is not None:
            return MarketDataError(
                f"MOEX request failed with HTTP {response.status_code}: {response.reason}"
            )
    if isinstance(error, requests.RequestException):
        return MarketDataError(f"MOEX request failed: {error}")
    if isinstance(error, ValueError):
        return MarketDataError("MOEX returned invalid JSON")
    return MarketDataError(str(error))


class MoexClient:
    BASE_URL = "https://iss.moex.com/iss"

    def __init__(
        self,
        *,
        board: str = "TQBR",
        timeout: float = 10,
        timezone: tzinfo | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.board = board.upper()
        self.timeout = timeout
        self.timezone = timezone or ZoneInfo("Europe/Moscow")
        self.session = session or requests.Session()

    def get_candles(self, ticker: str, timeframe: str, limit: int = 2) -> list[Candle]:
        ticker = ticker.upper()
        timeframe = _validate_timeframe(timeframe)
        if limit <= 0:
            return []

        interval = DIRECT_TIMEFRAME_INTERVALS[timeframe]
        rows = self._get_candle_rows(
            ticker,
            interval=interval,
            days_back=_days_back_for_timeframe(timeframe),
        )
        candles = normalize_candle_data(ticker, rows, timezone=self.timezone)

        closed = self._closed_candles(candles)
        return closed[-limit:]

    def get_last_two_closed_candles(self, ticker: str, timeframe: str) -> list[Candle]:
        candles = self.get_candles(ticker, timeframe, limit=20)
        if len(candles) < 2:
            raise MarketDataError(
                f"{ticker.upper()}: less than two closed candles found for {timeframe}"
            )
        return candles[-2:]

    def get_current_quote(self, ticker: str) -> CurrentQuote:
        candles = self.get_last_two_closed_candles(ticker, "1d")
        return CurrentQuote(
            ticker=ticker.upper(),
            current_price=candles[-1].close,
            previous_close=candles[-2].close,
        )

    def get_daily_close_comparison(self, ticker: str) -> DailyCloseComparison:
        candles = self.get_last_two_closed_candles(ticker, "1d")
        return DailyCloseComparison(
            ticker=ticker.upper(),
            trade_date=candles[-1].end.date(),
            close=candles[-1].close,
            previous_trade_date=candles[-2].end.date(),
            previous_close=candles[-2].close,
        )

    def _get_candle_rows(
        self,
        ticker: str,
        *,
        interval: int,
        days_back: int,
    ) -> list[dict[str, Any]]:
        from_date = datetime.now(self.timezone).date() - timedelta(days=days_back)
        rows: list[dict[str, Any]] = []
        start = 0

        while True:
            payload = self._get_json(
                f"/engines/stock/markets/shares/boards/{self.board}/securities/{ticker}/candles.json",
                params={
                    "iss.meta": "off",
                    "from": from_date.isoformat(),
                    "interval": interval,
                    "start": start,
                },
            )
            page_rows = self._table_rows(payload, "candles")
            if not page_rows:
                break

            rows.extend(page_rows)
            cursor = self._table_rows(payload, "candles.cursor")
            next_start = _next_start(cursor, fallback_start=start, rows_count=len(page_rows))
            if next_start is None or next_start <= start or next_start >= 10000:
                break
            start = next_start

        if not rows:
            raise MarketDataError(f"{ticker}: MOEX returned no candles")
        return rows

    def _get_json(self, path: str, *, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.BASE_URL}{path}"
        try:
            response = self.session.get(url, params=params, timeout=self.timeout)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            raise handle_moex_errors(exc) from exc

    @staticmethod
    def _table_rows(payload: dict[str, Any], table_name: str) -> list[dict[str, Any]]:
        table = payload.get(table_name) or {}
        columns = table.get("columns") or []
        data = table.get("data") or []
        return [dict(zip(columns, row)) for row in data]

    def _closed_candles(self, candles: Iterable[Candle]) -> list[Candle]:
        now = datetime.now(self.timezone)
        closed = [candle for candle in candles if candle.end < now]
        closed.sort(key=lambda candle: (candle.end, candle.begin))
        return closed


def _validate_timeframe(timeframe: str) -> str:
    value = timeframe.strip().lower()
    if value not in SUPPORTED_TIMEFRAMES:
        raise MarketDataError(f"Unsupported timeframe: {timeframe}")
    return value


def _days_back_for_timeframe(timeframe: str) -> int:
    return {
        "1m": 7,
        "10m": 14,
        "1h": 45,
        "1d": 180,
        "1w": 900,
        "1mo": 2500,
    }[timeframe]


def _parse_moex_datetime(value: Any, timezone: tzinfo) -> datetime | None:
    if value in (None, ""):
        return None

    raw = str(value).strip().replace(" ", "T")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone)
    return parsed.astimezone(timezone)


def _pick(row: dict[str, Any], field: str) -> Any:
    return row.get(field.upper())


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _next_start(
    cursor: list[dict[str, Any]],
    *,
    fallback_start: int,
    rows_count: int,
) -> int | None:
    if cursor:
        row = {str(key).upper(): value for key, value in cursor[0].items()}
        index = _to_int(row.get("INDEX")) or fallback_start
        total = _to_int(row.get("TOTAL"))
        page_size = _to_int(row.get("PAGESIZE")) or rows_count
        next_start = index + page_size
        if total is not None and next_start >= total:
            return None
        return next_start

    if rows_count == 0:
        return None
    return fallback_start + rows_count
