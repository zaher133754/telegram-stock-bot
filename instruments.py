from __future__ import annotations

import logging
from pathlib import Path

from tinkoff_client import load_russian_stock_tickers_from_tinkoff
from utils import load_tickers


logger = logging.getLogger(__name__)


def get_available_tickers(
    tickers_file: Path,
    *,
    allow_missing: bool = False,
) -> list[str]:
    try:
        tickers = load_tickers(tickers_file)
    except FileNotFoundError:
        if not allow_missing:
            raise
        logger.warning("Tickers file is missing: %s", tickers_file)
        tickers = []

    if tickers:
        return tickers

    logger.info("Tickers file is empty, trying Tinkoff Invest API fallback")
    return load_russian_stock_tickers_from_tinkoff()
