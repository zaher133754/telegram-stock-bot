from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta, tzinfo
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
    previous_close: float | None = None
    trade_date: date | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class DailyCloseComparison:
    ticker: str
    trade_date: date
    close: float
    previous_trade_date: date
    previous_close: float


class MarketDataClient(Protocol):
    def get_candles(
        self,
        ticker: str,
        timeframe: str,
        limit: int = 2,
        *,
        now: datetime | None = None,
        weekly_close_day: int | None = None,
        weekly_close_time: datetime_time | None = None,
        monthly_close_time: datetime_time | None = None,
    ) -> list[Candle]:
        ...

    def get_last_two_closed_candles(
        self,
        ticker: str,
        timeframe: str,
        *,
        now: datetime | None = None,
        weekly_close_day: int | None = None,
        weekly_close_time: datetime_time | None = None,
        monthly_close_time: datetime_time | None = None,
    ) -> list[Candle]:
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
INTRADAY_TIMEFRAME_MINUTES = {
    "1m": 1,
    "10m": 10,
    "1h": 60,
}


def get_candles(
    ticker: str,
    timeframe: str,
    limit: int = 2,
    *,
    board: str = "TQBR",
    timeout: float = 20,
    retries: int = 3,
    timezone: tzinfo | None = None,
) -> list[Candle]:
    client = MoexClient(
        board=board,
        timeout=timeout,
        retries=retries,
        timezone=timezone,
    )
    try:
        return client.get_candles(ticker, timeframe, limit=limit)
    finally:
        client.close()


def get_last_two_closed_candles(
    ticker: str,
    timeframe: str,
    *,
    board: str = "TQBR",
    timeout: float = 20,
    retries: int = 3,
    timezone: tzinfo | None = None,
) -> list[Candle]:
    client = MoexClient(
        board=board,
        timeout=timeout,
        retries=retries,
        timezone=timezone,
    )
    try:
        return client.get_last_two_closed_candles(ticker, timeframe)
    finally:
        client.close()


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

    candles.sort(key=lambda candle: _candle_sort_key(candle, timezone))
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
        timeout: float = 20,
        retries: int = 3,
        timezone: tzinfo | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.board = board.upper()
        self.timeout = timeout
        self.retries = max(1, retries)
        self.timezone = timezone or ZoneInfo("Europe/Moscow")
        self.session = session or requests.Session()
        self._owns_session = session is None
        self.request_count = 0

    def get_candles(
        self,
        ticker: str,
        timeframe: str,
        limit: int = 2,
        *,
        now: datetime | None = None,
        weekly_close_day: int | None = None,
        weekly_close_time: datetime_time | None = None,
        monthly_close_time: datetime_time | None = None,
    ) -> list[Candle]:
        ticker = ticker.upper()
        timeframe = _validate_timeframe(timeframe)
        if limit <= 0:
            return []

        interval = DIRECT_TIMEFRAME_INTERVALS[timeframe]
        rows = self._get_candle_rows(
            ticker,
            interval=interval,
            days_back=_days_back_for_timeframe(timeframe),
            required_rows=limit + 1,
        )
        candles = normalize_candle_data(ticker, rows, timezone=self.timezone)

        current_time = _as_timezone(now or datetime.now(self.timezone), self.timezone)
        closed = self._closed_candles(
            candles,
            timeframe,
            now=current_time,
            weekly_close_day=weekly_close_day,
            weekly_close_time=weekly_close_time,
            monthly_close_time=monthly_close_time,
        )
        if timeframe == "1h":
            self._log_hourly_candle_debug(
                ticker=ticker,
                candles=candles,
                closed_candles=closed,
                now=current_time,
            )
        return closed[-limit:]

    def get_last_two_closed_candles(
        self,
        ticker: str,
        timeframe: str,
        *,
        now: datetime | None = None,
        weekly_close_day: int | None = None,
        weekly_close_time: datetime_time | None = None,
        monthly_close_time: datetime_time | None = None,
    ) -> list[Candle]:
        candles = self.get_candles(
            ticker,
            timeframe,
            limit=20,
            now=now,
            weekly_close_day=weekly_close_day,
            weekly_close_time=weekly_close_time,
            monthly_close_time=monthly_close_time,
        )
        if len(candles) < 2:
            raise MarketDataError(
                f"{ticker.upper()}: less than two closed candles found for {timeframe}"
            )
        return candles[-2:]

    def get_current_quote(self, ticker: str) -> CurrentQuote:
        ticker = ticker.upper()
        payload = self._get_json(
            f"/engines/stock/markets/shares/boards/{self.board}/securities/{ticker}.json",
            params={
                "iss.meta": "off",
                "iss.only": "securities,marketdata",
                "securities.columns": "SECID,PREVPRICE",
                "marketdata.columns": (
                    "SECID,LAST,LCURRENTPRICE,MARKETPRICE2,MARKETPRICE,WAPRICE,"
                    "CLOSEPRICE,PREVPRICE,LCLOSEPRICE,TRADEDATE,SYSTIME,TIME,UPDATETIME"
                ),
            },
        )
        market_row = _first_row_for_ticker(self._table_rows(payload, "marketdata"), ticker)
        security_row = _first_row_for_ticker(self._table_rows(payload, "securities"), ticker)

        current_price = _first_positive_float(
            market_row,
            (
                "LAST",
                "LCURRENTPRICE",
                "MARKETPRICE2",
                "MARKETPRICE",
                "WAPRICE",
                "CLOSEPRICE",
            ),
        )
        previous_close = (
            _first_positive_float(market_row, ("PREVPRICE", "LCLOSEPRICE"))
            or _first_positive_float(security_row, ("PREVPRICE",))
        )
        updated_at = _parse_quote_datetime(market_row, self.timezone)
        trade_date = _parse_moex_date(_pick(market_row or {}, "TRADEDATE"))
        if trade_date is None and updated_at is not None:
            trade_date = updated_at.date()

        if current_price is None:
            candles = self.get_last_two_closed_candles(ticker, "1d")
            return CurrentQuote(
                ticker=ticker,
                current_price=candles[-1].close,
                previous_close=candles[-2].close,
                trade_date=candles[-1].end.date(),
            )

        return CurrentQuote(
            ticker=ticker,
            current_price=current_price,
            previous_close=previous_close,
            trade_date=trade_date,
            updated_at=updated_at,
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

    def get_daily_candles_history(self, ticker: str, *, years: int = 3) -> list[Candle]:
        ticker = ticker.upper()
        years = max(1, int(years))
        rows = self._get_candle_rows(
            ticker,
            interval=DIRECT_TIMEFRAME_INTERVALS["1d"],
            days_back=years * 366 + 14,
            required_rows=years * 260 + 30,
        )
        candles = normalize_candle_data(ticker, rows, timezone=self.timezone)
        current_time = datetime.now(self.timezone)
        closed = self._closed_candles(candles, "1d", now=current_time)
        cutoff = current_time.date() - timedelta(days=years * 366 + 7)
        return [candle for candle in closed if candle.begin.date() >= cutoff]

    def _get_candle_rows(
        self,
        ticker: str,
        *,
        interval: int,
        days_back: int,
        required_rows: int,
    ) -> list[dict[str, Any]]:
        from_date = datetime.now(self.timezone).date() - timedelta(days=days_back)
        rows: list[dict[str, Any]] = []
        start = 0
        first_page = True

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
            if first_page:
                tail_start = _tail_page_start(
                    cursor,
                    required_rows=required_rows,
                    rows_count=len(page_rows),
                )
                first_page = False
                if tail_start is not None and tail_start > start:
                    rows = []
                    start = tail_start
                    continue

            next_start = _next_start(cursor, fallback_start=start, rows_count=len(page_rows))
            if next_start is None or next_start <= start or next_start >= 10000:
                break
            start = next_start

        if not rows:
            raise MarketDataError(f"{ticker}: MOEX returned no candles")
        return rows

    def _get_json(self, path: str, *, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.BASE_URL}{path}"
        last_error: Exception | None = None

        for attempt in range(1, self.retries + 1):
            self.request_count += 1
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
                response.raise_for_status()
                return response.json()
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                if attempt >= self.retries or not _is_retryable_error(exc):
                    raise handle_moex_errors(exc) from exc
                logger.warning(
                    "MOEX request retry: attempt=%s/%s url=%s error=%s",
                    attempt,
                    self.retries,
                    url,
                    exc,
                )
                time.sleep(min(0.5 * attempt, 2.0))

        raise handle_moex_errors(last_error or RuntimeError("MOEX request failed"))

    def close(self) -> None:
        if self._owns_session:
            self.session.close()

    @staticmethod
    def _table_rows(payload: dict[str, Any], table_name: str) -> list[dict[str, Any]]:
        table = payload.get(table_name) or {}
        columns = table.get("columns") or []
        data = table.get("data") or []
        return [dict(zip(columns, row)) for row in data]

    def _closed_candles(
        self,
        candles: Iterable[Candle],
        timeframe: str,
        *,
        now: datetime | None = None,
        weekly_close_day: int | None = None,
        weekly_close_time: datetime_time | None = None,
        monthly_close_time: datetime_time | None = None,
    ) -> list[Candle]:
        timeframe = _validate_timeframe(timeframe)
        current_time = _as_timezone(now or datetime.now(self.timezone), self.timezone)
        closed = [
            candle
            for candle in candles
            if self._candle_close_time(
                candle,
                timeframe,
                weekly_close_day=weekly_close_day,
                weekly_close_time=weekly_close_time,
                monthly_close_time=monthly_close_time,
            )
            <= current_time
        ]
        closed.sort(key=lambda candle: _candle_sort_key(candle, self.timezone))
        return closed

    def _candle_close_time(
        self,
        candle: Candle,
        timeframe: str,
        *,
        weekly_close_day: int | None = None,
        weekly_close_time: datetime_time | None = None,
        monthly_close_time: datetime_time | None = None,
    ) -> datetime:
        if (
            timeframe == "1w"
            and weekly_close_day is not None
            and weekly_close_time is not None
        ):
            return weekly_configured_close_time(
                candle,
                weekly_close_day=weekly_close_day,
                weekly_close_time=weekly_close_time,
                timezone=self.timezone,
            )
        if timeframe == "1mo" and monthly_close_time is not None:
            return monthly_configured_close_time(
                candle,
                monthly_close_time=monthly_close_time,
                timezone=self.timezone,
            )
        return candle_close_time(candle, timeframe, timezone=self.timezone)

    def _log_hourly_candle_debug(
        self,
        *,
        ticker: str,
        candles: list[Candle],
        closed_candles: list[Candle],
        now: datetime,
    ) -> None:
        now_msk = _as_timezone(now, self.timezone)
        received = candles[-5:]
        logger.info(
            "1h debug: ticker=%s now_msk=%s received_candles=%s",
            ticker,
            _format_debug_datetime(now_msk),
            len(received),
        )
        for index, candle in enumerate(received, start=1):
            is_closed = (
                candle_close_time(candle, "1h", timezone=self.timezone) <= now_msk
            )
            logger.info(
                "1h debug candle: ticker=%s index=%s begin=%s end=%s close=%s high=%s closed=%s",
                ticker,
                index,
                _format_debug_datetime(_as_timezone(candle.begin, self.timezone)),
                _format_debug_datetime(_as_timezone(candle.end, self.timezone)),
                candle.close,
                candle.high,
                str(is_closed).lower(),
            )

        selected_last = closed_candles[-1] if closed_candles else None
        selected_previous = closed_candles[-2] if len(closed_candles) >= 2 else None
        logger.info(
            "1h debug selected: ticker=%s selected_last_closed=%s "
            "selected_previous_closed=%s candle_key=%s",
            ticker,
            _format_debug_datetime(_as_timezone(selected_last.begin, self.timezone))
            if selected_last
            else None,
            _format_debug_datetime(_as_timezone(selected_previous.begin, self.timezone))
            if selected_previous
            else None,
            candle_key(selected_last, "1h", timezone=self.timezone)
            if selected_last
            else None,
        )


def candle_close_time(
    candle: Candle,
    timeframe: str,
    *,
    timezone: tzinfo | None = None,
) -> datetime:
    timeframe = _validate_timeframe(timeframe)
    timezone = timezone or ZoneInfo("Europe/Moscow")
    begin = _as_timezone(candle.begin, timezone)

    if timeframe in INTRADAY_TIMEFRAME_MINUTES:
        return begin + timedelta(minutes=INTRADAY_TIMEFRAME_MINUTES[timeframe])

    if timeframe == "1d":
        next_date = begin.date() + timedelta(days=1)
        return datetime(next_date.year, next_date.month, next_date.day, tzinfo=timezone)

    if timeframe == "1w":
        week_start = begin.date() - timedelta(days=begin.weekday())
        next_week = week_start + timedelta(days=7)
        return datetime(next_week.year, next_week.month, next_week.day, tzinfo=timezone)

    if begin.month == 12:
        return datetime(begin.year + 1, 1, 1, tzinfo=timezone)
    return datetime(begin.year, begin.month + 1, 1, tzinfo=timezone)


def weekly_configured_close_time(
    candle: Candle,
    *,
    weekly_close_day: int,
    weekly_close_time: datetime_time,
    timezone: tzinfo | None = None,
) -> datetime:
    timezone = timezone or ZoneInfo("Europe/Moscow")
    begin = _as_timezone(candle.begin, timezone)
    week_start = begin.date() - timedelta(days=begin.weekday())
    close_day = week_start + timedelta(days=max(0, min(6, int(weekly_close_day))))
    return datetime.combine(close_day, weekly_close_time, tzinfo=timezone)


def monthly_configured_close_time(
    candle: Candle,
    *,
    monthly_close_time: datetime_time,
    timezone: tzinfo | None = None,
) -> datetime:
    timezone = timezone or ZoneInfo("Europe/Moscow")
    begin = _as_timezone(candle.begin, timezone)
    last_day = calendar_month_last_day(begin.date())
    return datetime.combine(last_day, monthly_close_time, tzinfo=timezone)


def calendar_month_last_day(value: date) -> date:
    if value.month == 12:
        return date(value.year, 12, 31)
    first_next_month = date(value.year, value.month + 1, 1)
    return first_next_month - timedelta(days=1)


def candle_key(
    candle: Candle,
    timeframe: str,
    *,
    timezone: tzinfo | None = None,
) -> str:
    timeframe = _validate_timeframe(timeframe)
    timezone = timezone or ZoneInfo("Europe/Moscow")
    begin = _as_timezone(candle.begin, timezone)

    if timeframe in INTRADAY_TIMEFRAME_MINUTES:
        return begin.strftime("%Y-%m-%d %H:%M")
    if timeframe == "1d":
        return begin.strftime("%Y-%m-%d")
    if timeframe == "1w":
        iso_year, iso_week, _ = begin.isocalendar()
        return f"{iso_year}-W{iso_week:02d}"
    return begin.strftime("%Y-%m")


def _validate_timeframe(timeframe: str) -> str:
    value = timeframe.strip().lower()
    if value not in SUPPORTED_TIMEFRAMES:
        raise MarketDataError(f"Unsupported timeframe: {timeframe}")
    return value


def _is_retryable_error(error: Exception) -> bool:
    if isinstance(error, requests.HTTPError):
        response = error.response
        if response is None:
            return True
        return response.status_code == 429 or response.status_code >= 500
    return isinstance(error, (requests.RequestException, ValueError))


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


def _as_timezone(value: datetime, timezone: tzinfo) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone)
    return value.astimezone(timezone)


def _candle_sort_key(candle: Candle, timezone: tzinfo) -> tuple[datetime, datetime]:
    return (
        _as_timezone(candle.begin, timezone),
        _as_timezone(candle.end, timezone),
    )


def _format_debug_datetime(value: datetime) -> str:
    return value.isoformat(sep=" ", timespec="seconds")


def _first_row_for_ticker(
    rows: list[dict[str, Any]],
    ticker: str,
) -> dict[str, Any] | None:
    ticker = ticker.upper()
    for raw_row in rows:
        row = {str(key).upper(): value for key, value in raw_row.items()}
        if str(row.get("SECID", "")).upper() == ticker:
            return row
    return rows[0] if rows else None


def _first_positive_float(
    row: dict[str, Any] | None,
    fields: Iterable[str],
) -> float | None:
    if row is None:
        return None
    for field in fields:
        value = _to_float(_pick(row, field))
        if value is not None and value > 0:
            return value
    return None


def _parse_moex_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def _parse_quote_datetime(
    row: dict[str, Any] | None,
    timezone: tzinfo,
) -> datetime | None:
    if row is None:
        return None

    systime = _parse_moex_datetime(_pick(row, "SYSTIME"), timezone)
    if systime is not None:
        return systime

    trade_date = _parse_moex_date(_pick(row, "TRADEDATE"))
    time_value = _pick(row, "UPDATETIME") or _pick(row, "TIME")
    if trade_date is None or time_value in (None, ""):
        return None

    try:
        parsed_time = datetime_time.fromisoformat(str(time_value).strip())
    except ValueError:
        return None
    return datetime.combine(trade_date, parsed_time, tzinfo=timezone)


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


def _tail_page_start(
    cursor: list[dict[str, Any]],
    *,
    required_rows: int,
    rows_count: int,
) -> int | None:
    if not cursor:
        return None

    row = {str(key).upper(): value for key, value in cursor[0].items()}
    total = _to_int(row.get("TOTAL"))
    page_size = _to_int(row.get("PAGESIZE")) or rows_count
    if total is None or page_size <= 0 or total <= page_size:
        return None

    tail_size = max(required_rows, page_size)
    return max(total - tail_size, 0)
