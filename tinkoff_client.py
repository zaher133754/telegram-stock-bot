from __future__ import annotations

import logging
import os


logger = logging.getLogger(__name__)


def load_russian_stock_tickers_from_tinkoff(token: str | None = None) -> list[str]:
    token = token or os.getenv("TINKOFF_INVEST_TOKEN") or os.getenv("TINKOFF_TOKEN")
    if not token:
        logger.warning("Tinkoff fallback skipped: token is not configured")
        return []

    try:
        from tinkoff.invest import Client, InstrumentStatus
    except ImportError:
        logger.warning("Tinkoff fallback skipped: tinkoff-investments is not installed")
        return []

    tickers: list[str] = []
    seen: set[str] = set()
    with Client(token) as client:
        shares = client.instruments.shares(
            instrument_status=InstrumentStatus.INSTRUMENT_STATUS_BASE,
        ).instruments

    for share in shares:
        ticker = str(getattr(share, "ticker", "")).strip().upper()
        currency = str(getattr(share, "currency", "")).strip().lower()
        exchange = str(getattr(share, "exchange", "")).strip().lower()
        if not ticker or ticker in seen:
            continue
        if currency and currency != "rub":
            continue
        if exchange and "moex" not in exchange and "spb" in exchange:
            continue
        seen.add(ticker)
        tickers.append(ticker)

    logger.info("Loaded %s Russian stock tickers from Tinkoff Invest API", len(tickers))
    return tickers
