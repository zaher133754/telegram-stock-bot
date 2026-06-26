from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import requests

from moex_client import Candle, MoexClient


MOSCOW = ZoneInfo("Europe/Moscow")


class MoexRetryTests(unittest.TestCase):
    def test_request_is_retried_and_counted(self) -> None:
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"candles": {"columns": [], "data": []}}

        session = Mock()
        session.get.side_effect = [requests.Timeout("temporary timeout"), response]
        client = MoexClient(session=session, retries=3, timeout=20)

        with self.assertLogs("moex_client", level="WARNING"):
            payload = client._get_json("/test.json", params={})

        self.assertEqual(payload, {"candles": {"columns": [], "data": []}})
        self.assertEqual(client.request_count, 2)
        self.assertEqual(session.get.call_count, 2)

    def test_non_retryable_http_error_is_not_retried(self) -> None:
        response = Mock()
        response.status_code = 404
        error = requests.HTTPError("not found", response=response)

        session = Mock()
        session.get.side_effect = error
        client = MoexClient(session=session, retries=3, timeout=20)

        with self.assertRaisesRegex(RuntimeError, "HTTP 404"):
            client._get_json("/test.json", params={})

        self.assertEqual(client.request_count, 1)
        self.assertEqual(session.get.call_count, 1)


class MoexPaginationTests(unittest.TestCase):
    def test_candle_loading_jumps_to_tail_page(self) -> None:
        first_page = {
            "candles": {
                "columns": ["BEGIN"],
                "data": [["old"]],
            },
            "candles.cursor": {
                "columns": ["INDEX", "TOTAL", "PAGESIZE"],
                "data": [[0, 2000, 500]],
            },
        }
        tail_page = {
            "candles": {
                "columns": ["BEGIN"],
                "data": [["new"]],
            },
            "candles.cursor": {
                "columns": ["INDEX", "TOTAL", "PAGESIZE"],
                "data": [[1500, 2000, 500]],
            },
        }
        client = MoexClient(session=Mock())
        client._get_json = Mock(side_effect=[first_page, tail_page])

        rows = client._get_candle_rows(
            "SBER",
            interval=1,
            days_back=7,
            required_rows=21,
        )

        self.assertEqual(rows, [{"BEGIN": "new"}])
        starts = [
            call.kwargs["params"]["start"]
            for call in client._get_json.call_args_list
        ]
        self.assertEqual(starts, [0, 1500])


class CurrentQuoteTests(unittest.TestCase):
    def test_current_quote_uses_marketdata_last(self) -> None:
        client = MoexClient(timezone=MOSCOW, session=Mock())
        client._get_json = Mock(
            return_value={
                "marketdata": {
                    "columns": [
                        "SECID",
                        "LAST",
                        "LCURRENTPRICE",
                        "MARKETPRICE",
                        "SYSTIME",
                    ],
                    "data": [["SBER", 291.22, 291.28, 298.94, "2026-06-26 12:23:34"]],
                },
                "securities": {
                    "columns": ["SECID", "PREVPRICE"],
                    "data": [["SBER", 295.16]],
                },
            }
        )

        quote = client.get_current_quote("SBER")

        self.assertEqual(quote.current_price, 291.22)
        self.assertEqual(quote.previous_close, 295.16)
        self.assertEqual(quote.trade_date, date(2026, 6, 26))
        self.assertEqual(quote.updated_at, datetime(2026, 6, 26, 12, 23, 34, tzinfo=MOSCOW))

    def test_current_quote_falls_back_to_lcurrentprice(self) -> None:
        client = MoexClient(timezone=MOSCOW, session=Mock())
        client._get_json = Mock(
            return_value={
                "marketdata": {
                    "columns": ["SECID", "LAST", "LCURRENTPRICE", "SYSTIME"],
                    "data": [["GAZP", None, 96.01, "2026-06-26 12:23:49"]],
                },
                "securities": {
                    "columns": ["SECID", "PREVPRICE"],
                    "data": [["GAZP", 98.07]],
                },
            }
        )

        quote = client.get_current_quote("GAZP")

        self.assertEqual(quote.current_price, 96.01)
        self.assertEqual(quote.previous_close, 98.07)


class HourlyDebugTests(unittest.TestCase):
    def test_hourly_debug_logs_last_five_candles_and_selection(self) -> None:
        candles = []
        for hour in range(17, 22):
            begin = datetime(2026, 6, 4, hour, tzinfo=MOSCOW)
            candles.append(
                Candle(
                    ticker="SBER",
                    begin=begin,
                    end=begin + timedelta(minutes=59),
                    open=100,
                    high=102,
                    low=99,
                    close=101,
                    volume=1,
                )
            )

        client = MoexClient(timezone=MOSCOW, session=Mock())
        now = datetime(2026, 6, 4, 21, 0, 45, tzinfo=MOSCOW)
        closed = client._closed_candles(candles, "1h", now=now)

        with self.assertLogs("moex_client", level="INFO") as logs:
            client._log_hourly_candle_debug(
                ticker="SBER",
                candles=candles,
                closed_candles=closed,
                now=now,
            )

        output = "\n".join(logs.output)
        self.assertIn("now_msk=2026-06-04 21:00:45+03:00", output)
        self.assertIn("received_candles=5", output)
        self.assertIn("begin=2026-06-04 21:00:00+03:00", output)
        self.assertIn("closed=false", output)
        self.assertIn("selected_last_closed=2026-06-04 20:00:00+03:00", output)
        self.assertIn("selected_previous_closed=2026-06-04 19:00:00+03:00", output)
        self.assertIn("candle_key=2026-06-04 20:00", output)


if __name__ == "__main__":
    unittest.main()
