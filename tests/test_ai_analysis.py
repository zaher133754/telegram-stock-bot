from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from ai_analysis import build_market_context
from charting_ai import generate_ai_chart
from moex_client import Candle


MOSCOW = ZoneInfo("Europe/Moscow")
PROJECT_DIR = Path(__file__).resolve().parents[1]


def make_candles(prices: list[float]) -> list[Candle]:
    start = datetime(2025, 1, 1, tzinfo=MOSCOW)
    candles: list[Candle] = []
    for index, close in enumerate(prices):
        begin = start + timedelta(days=index)
        open_price = prices[index - 1] if index else close
        high = max(open_price, close) + 1.0
        low = min(open_price, close) - 1.0
        candles.append(
            Candle(
                ticker="TEST",
                begin=begin,
                end=begin,
                open=open_price,
                high=high,
                low=low,
                close=close,
                volume=1000,
            )
        )
    return candles


class AIAnalysisTests(unittest.TestCase):
    def test_empty_candles_build_no_trade_text(self) -> None:
        result = build_market_context("SBER", [])

        self.assertTrue(result.no_trade_setup)
        self.assertEqual(result.setup_quality, "no_data")
        self.assertIn("MOEX не вернул дневные свечи", result.analysis_text)
        self.assertIn("наблюдение со стороны", result.analysis_text)

    def test_few_candles_do_not_crash_without_entry(self) -> None:
        result = build_market_context("SBER", make_candles([100, 101, 100.5, 101.2]))

        self.assertTrue(result.no_trade_setup)
        self.assertIsNone(result.entry_price)
        self.assertIn("AI-анализ: SBER", result.analysis_text)
        self.assertIn("Точка входа", result.analysis_text)

    def test_downtrend_is_observation_from_side(self) -> None:
        prices = [200 - index * 0.25 for index in range(260)]
        result = build_market_context("GAZP", make_candles(prices))

        self.assertEqual(result.trend_state, "downtrend")
        self.assertTrue(result.no_trade_setup)
        self.assertIsNone(result.entry_price)
        self.assertIn("наблюдение со стороны", result.analysis_text)

    def test_weak_setup_text_is_still_built(self) -> None:
        prices = [100 + index * 0.01 for index in range(10)]
        result = build_market_context("LKOH", make_candles(prices))

        self.assertTrue(result.no_trade_setup)
        self.assertIsNone(result.stop_price)
        self.assertIsNone(result.take_price)
        self.assertIn("качественная точка входа не выделена", result.analysis_text)

    def test_chart_is_created_for_analysis(self) -> None:
        prices = []
        price = 100.0
        for index in range(260):
            price *= 1.0004
            if index > 210:
                price = 110 + (index % 12 - 6) * 0.18
            prices.append(price)

        result = build_market_context("NVTK", make_candles(prices))
        output_dir = PROJECT_DIR / ".tmp" / "test_ai_charts"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / "ai_analysis_NVTK.png"
        if output_path.exists():
            output_path.unlink()

        path = generate_ai_chart(result, output_dir=output_dir, candles_limit=120)

        self.assertTrue(path.exists())
        self.assertGreater(path.stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()
