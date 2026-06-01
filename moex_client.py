from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Iterable, Protocol

import requests


logger = logging.getLogger(__name__)


class MarketDataError(RuntimeError):
    pass


@dataclass(frozen=True)
class CurrentQuote:
    ticker: str
    current_price: float
    previous_close: float


@dataclass(frozen=True)
class DailyClose:
    ticker: str
    trade_date: date
    close: float


@dataclass(frozen=True)
class DailyCloseComparison:
    ticker: str
    trade_date: date
    close: float
    previous_trade_date: date
    previous_close: float


class MarketDataClient(Protocol):
    def get_current_quote(self, ticker: str) -> CurrentQuote:
        ...

    def get_daily_close_comparison(self, ticker: str) -> DailyCloseComparison:
        ...


class MoexClient:
    BASE_URL = "https://iss.moex.com/iss"

    CURRENT_PRICE_FIELDS = (
        "LAST",
        "LCURRENTPRICE",
        "MARKETPRICE2",
        "MARKETPRICE",
        "WAPRICE",
        "CLOSE",
        "LEGALCLOSEPRICE",
    )
    PREVIOUS_CLOSE_FIELDS = (
        "PREVPRICE",
        "PREVLEGALCLOSEPRICE",
        "PREVWAPRICE",
        "PREVADMITTEDQUOTE",
    )
    DAILY_CLOSE_FIELDS = (
        "CLOSE",
        "LEGALCLOSEPRICE",
        "WAPRICE",
        "MARKETPRICE2",
    )

    def __init__(
        self,
        *,
        board: str = "TQBR",
        timeout: float = 10,
        session: requests.Session | None = None,
    ) -> None:
        self.board = board.upper()
        self.timeout = timeout
        self.session = session or requests.Session()

    def get_current_quote(self, ticker: str) -> CurrentQuote:
        ticker = ticker.upper()
        payload = self._get_json(
            f"/engines/stock/markets/shares/boards/{self.board}/securities/{ticker}.json",
            params={
                "iss.meta": "off",
                "iss.only": "securities,marketdata",
            },
        )
        securities = self._table_rows(payload, "securities")
        marketdata = self._table_rows(payload, "marketdata")

        if not securities and not marketdata:
            raise MarketDataError(f"{ticker}: MOEX did not return security data")

        security_row = securities[0] if securities else {}
        market_row = marketdata[0] if marketdata else {}

        current_price = self._first_positive_number(
            market_row,
            self.CURRENT_PRICE_FIELDS,
        )
        previous_close = self._first_positive_number(
            security_row,
            self.PREVIOUS_CLOSE_FIELDS,
        ) or self._first_positive_number(market_row, self.PREVIOUS_CLOSE_FIELDS)

        if current_price is None or previous_close is None:
            logger.info("%s: falling back to historical closes", ticker)
            closes = self.get_recent_daily_closes(ticker, limit=2)
            if len(closes) < 2:
                raise MarketDataError(
                    f"{ticker}: not enough current or historical data for comparison"
                )
            if current_price is None:
                current_price = closes[-1].close
            if previous_close is None:
                previous_close = closes[-2].close

        return CurrentQuote(
            ticker=ticker,
            current_price=current_price,
            previous_close=previous_close,
        )

    def get_daily_close_comparison(self, ticker: str) -> DailyCloseComparison:
        ticker = ticker.upper()
        closes = self.get_recent_daily_closes(ticker, limit=2)
        if len(closes) < 2:
            raise MarketDataError(f"{ticker}: less than two closed trading days found")

        previous_close = closes[-2]
        last_close = closes[-1]
        return DailyCloseComparison(
            ticker=ticker,
            trade_date=last_close.trade_date,
            close=last_close.close,
            previous_trade_date=previous_close.trade_date,
            previous_close=previous_close.close,
        )

    def get_recent_daily_closes(self, ticker: str, *, limit: int = 2) -> list[DailyClose]:
        ticker = ticker.upper()
        for days_back in (14, 45, 120, 365):
            rows = self._get_history_rows(ticker, days_back=days_back)
            closes = self._parse_daily_closes(ticker, rows)
            if len(closes) >= limit:
                return closes[-limit:]
        return closes[-limit:] if closes else []

    def _get_history_rows(self, ticker: str, *, days_back: int) -> list[dict[str, Any]]:
        from_date = date.today() - timedelta(days=days_back)
        payload = self._get_json(
            f"/history/engines/stock/markets/shares/boards/{self.board}/securities/{ticker}.json",
            params={
                "iss.meta": "off",
                "from": from_date.isoformat(),
                "start": 0,
            },
        )
        return self._table_rows(payload, "history")

    def _get_json(self, path: str, *, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.BASE_URL}{path}"
        try:
            response = self.session.get(url, params=params, timeout=self.timeout)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            raise MarketDataError(f"MOEX request failed: {exc}") from exc
        except ValueError as exc:
            raise MarketDataError("MOEX returned invalid JSON") from exc

    @staticmethod
    def _table_rows(payload: dict[str, Any], table_name: str) -> list[dict[str, Any]]:
        table = payload.get(table_name) or {}
        columns = table.get("columns") or []
        data = table.get("data") or []
        return [dict(zip(columns, row)) for row in data]

    def _parse_daily_closes(
        self,
        ticker: str,
        rows: Iterable[dict[str, Any]],
    ) -> list[DailyClose]:
        closes: list[DailyClose] = []
        for row in rows:
            trade_date_raw = row.get("TRADEDATE")
            if not trade_date_raw:
                continue
            close = self._first_positive_number(row, self.DAILY_CLOSE_FIELDS)
            if close is None:
                continue
            try:
                trade_date = date.fromisoformat(str(trade_date_raw))
            except ValueError:
                continue
            closes.append(DailyClose(ticker=ticker, trade_date=trade_date, close=close))

        closes.sort(key=lambda item: item.trade_date)
        return closes

    @staticmethod
    def _first_positive_number(row: dict[str, Any], fields: Iterable[str]) -> float | None:
        for field in fields:
            value = row.get(field)
            number = MoexClient._to_float(value)
            if number is not None and number > 0:
                return number
        return None

    @staticmethod
    def _to_float(value: Any) -> float | None:
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
