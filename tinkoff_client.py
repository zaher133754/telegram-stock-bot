from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from config import Settings
from moex_client import Candle, MarketDataError, candle_close_time, candle_key
from utils import load_tickers


logger = logging.getLogger(__name__)

TINVEST_SOURCE = "TINVEST"
NANO_UNITS = 1_000_000_000

TINVEST_INTERVAL_NAMES = {
    "1m": ("CANDLE_INTERVAL_1_MIN", "CANDLE_INTERVAL_MINUTE"),
    "2m": ("CANDLE_INTERVAL_2_MIN",),
    "3m": ("CANDLE_INTERVAL_3_MIN",),
    "5m": ("CANDLE_INTERVAL_5_MIN",),
    "10m": ("CANDLE_INTERVAL_10_MIN",),
    "15m": ("CANDLE_INTERVAL_15_MIN",),
    "30m": ("CANDLE_INTERVAL_30_MIN",),
    "1h": ("CANDLE_INTERVAL_HOUR", "CANDLE_INTERVAL_1_HOUR"),
    "2h": ("CANDLE_INTERVAL_2_HOUR",),
    "4h": ("CANDLE_INTERVAL_4_HOUR",),
    "1d": ("CANDLE_INTERVAL_DAY",),
    "1w": ("CANDLE_INTERVAL_WEEK",),
    "1mo": ("CANDLE_INTERVAL_MONTH",),
}

TINVEST_FETCH_WINDOWS = {
    "1m": (timedelta(days=1), timedelta(days=14)),
    "2m": (timedelta(days=1), timedelta(days=14)),
    "3m": (timedelta(days=1), timedelta(days=14)),
    "5m": (timedelta(days=1), timedelta(days=21)),
    "10m": (timedelta(days=1), timedelta(days=30)),
    "15m": (timedelta(days=1), timedelta(days=45)),
    "30m": (timedelta(days=2), timedelta(days=60)),
    "1h": (timedelta(days=7), timedelta(days=90)),
    "2h": (timedelta(days=14), timedelta(days=180)),
    "4h": (timedelta(days=30), timedelta(days=365)),
    "1d": (timedelta(days=365), timedelta(days=730)),
    "1w": (timedelta(days=365), timedelta(days=365 * 5)),
    "1mo": (timedelta(days=365 * 5), timedelta(days=365 * 10)),
}


def convert_quotation_to_float(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)

    units = getattr(value, "units", 0) or 0
    nano = getattr(value, "nano", 0) or 0
    try:
        return float(units) + float(nano) / NANO_UNITS
    except (TypeError, ValueError):
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0


def get_tinvest_instruments(settings: Settings) -> list[dict[str, Any]]:
    return _get_tinvest_instruments(settings, force_refresh=False)


def get_tinvest_instrument_by_ticker(
    settings: Settings,
    ticker: str,
) -> dict[str, Any] | None:
    ticker_name = str(ticker).strip().upper()
    if not ticker_name:
        return None

    instruments = _get_tinvest_instruments(settings, force_refresh=False)
    instrument = _find_cached_instrument(instruments, ticker_name)
    if instrument is not None:
        return instrument

    if _token(settings):
        instruments = _get_tinvest_instruments(settings, force_refresh=True)
        instrument = _find_cached_instrument(instruments, ticker_name)
        if instrument is not None:
            return instrument

    logger.warning("T-Invest: ticker not found: %s", ticker_name)
    return None


def get_tinvest_candles(
    settings: Settings,
    ticker: str,
    timeframe: str,
) -> list[Candle]:
    instrument = get_tinvest_instrument_by_ticker(settings, ticker)
    if instrument is None:
        raise MarketDataError(f"T-Invest: ticker not found: {ticker.upper()}")
    return _get_tinvest_candles_for_instrument(settings, instrument, timeframe)


def get_last_closed_tinvest_candles(
    settings: Settings,
    ticker: str,
    timeframe: str,
) -> list[Candle]:
    candles = get_tinvest_candles(settings, ticker, timeframe)
    if len(candles) < 2:
        raise MarketDataError(
            f"{ticker.upper()}: not enough closed candles for {timeframe}"
        )
    return candles[-2:]


def collect_tinvest_analysis(
    settings: Settings,
    tickers: list[str] | tuple[str, ...] | None,
    timeframe: str,
):
    from analytics import AnalysisResult, compare_last_two_candles, normalize_tickers

    token = _token(settings)
    if not token:
        raise MarketDataError("T-Invest token is missing")
    _import_tinvest()

    selected_tickers = normalize_tickers(tickers)
    if tickers is None:
        selected_tickers = load_tickers(settings.tickers_file)

    comparisons = []
    failures: list[tuple[str, str]] = []
    debug_last_candles: list[Candle] = []

    logger.info(
        "Collecting T-Invest analysis: timeframe=%s tickers=%s timeout=%s",
        timeframe,
        len(selected_tickers),
        getattr(settings, "tinvest_request_timeout_seconds", 20),
    )

    for ticker in selected_tickers:
        ticker_name = str(ticker).strip().upper()
        try:
            instrument = get_tinvest_instrument_by_ticker(settings, ticker_name)
            if instrument is None:
                raise MarketDataError(f"T-Invest: ticker not found: {ticker_name}")

            candles = _get_tinvest_candles_for_instrument(
                settings,
                instrument,
                timeframe,
            )
            if len(candles) < 2:
                logger.warning(
                    "T-Invest: not enough closed candles: ticker=%s timeframe=%s count=%s",
                    ticker_name,
                    timeframe,
                    len(candles),
                )
                raise MarketDataError(
                    f"{ticker_name}: not enough closed candles for {timeframe}"
                )
            selected = candles[-2:]
            if timeframe == "1h" and not debug_last_candles:
                debug_last_candles = candles[-5:]
            comparison = compare_last_two_candles(selected)
        except MarketDataError as exc:
            logger.warning("T-Invest API error for %s: %s", ticker_name, exc)
            failures.append((ticker_name, str(exc)))
            continue
        except Exception as exc:
            logger.exception("T-Invest API error for %s", ticker_name)
            failures.append((ticker_name, str(exc)))
            continue

        comparisons.append(comparison)
        logger.info(
            "T-Invest candles selected: ticker=%s timeframe=%s "
            "latest_closed_candle_key=%s previous_closed_candle_key=%s",
            ticker_name,
            timeframe,
            candle_key(comparison.last, timeframe, timezone=settings.timezone),
            candle_key(comparison.previous, timeframe, timezone=settings.timezone),
        )

    return AnalysisResult(
        timeframe=timeframe,
        comparisons=comparisons,
        failures=failures,
        updated_at=datetime.now(settings.timezone),
        tickers_count=len(selected_tickers),
        source=TINVEST_SOURCE,
        debug_last_candles=debug_last_candles,
    )


def load_russian_stock_tickers_from_tinkoff(token: str | None = None) -> list[str]:
    token = token or os.getenv("TINKOFF_INVEST_TOKEN") or os.getenv("TINKOFF_TOKEN")
    if not token:
        logger.warning("Tinkoff fallback skipped: token is not configured")
        return []

    try:
        Client, _CandleInterval, InstrumentStatus = _import_tinvest()
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


def _get_tinvest_candles_for_instrument(
    settings: Settings,
    instrument: dict[str, Any],
    timeframe: str,
) -> list[Candle]:
    token = _token(settings)
    if not token:
        raise MarketDataError("T-Invest token is missing")

    Client, CandleInterval, _InstrumentStatus = _import_tinvest()
    interval = _resolve_candle_interval(CandleInterval, timeframe)
    figi = str(instrument.get("figi") or "").strip()
    ticker = str(instrument.get("ticker") or "").strip().upper()
    lot = _positive_int(instrument.get("lot"), default=1)
    if not figi:
        raise MarketDataError(f"{ticker}: T-Invest FIGI is missing")

    now_utc = datetime.now(timezone.utc)
    chunk_size, max_lookback = TINVEST_FETCH_WINDOWS[timeframe]
    earliest = now_utc - max_lookback
    chunk_to = now_utc
    rows: list[Any] = []

    with Client(token) as client:
        while chunk_to > earliest:
            chunk_from = max(earliest, chunk_to - chunk_size)
            response = client.market_data.get_candles(
                figi=figi,
                from_=chunk_from,
                to=chunk_to,
                interval=interval,
            )
            rows.extend(getattr(response, "candles", []) or [])
            if len(rows) >= 8:
                break
            chunk_to = chunk_from

    candles = [
        _normalize_tinvest_candle(
            raw_candle,
            ticker=ticker,
            timeframe=timeframe,
            lot=lot,
            settings=settings,
        )
        for raw_candle in rows
        if _is_complete_tinvest_candle(raw_candle)
    ]
    current_time = datetime.now(settings.timezone)
    closed = [
        candle
        for candle in candles
        if candle_close_time(candle, timeframe, timezone=settings.timezone)
        <= current_time
    ]
    closed.sort(key=lambda candle: candle.begin)
    return closed


def _normalize_tinvest_candle(
    raw_candle: Any,
    *,
    ticker: str,
    timeframe: str,
    lot: int,
    settings: Settings,
) -> Candle:
    begin = _as_settings_timezone(getattr(raw_candle, "time"), settings)
    open_price = convert_quotation_to_float(getattr(raw_candle, "open", None))
    high_price = convert_quotation_to_float(getattr(raw_candle, "high", None))
    low_price = convert_quotation_to_float(getattr(raw_candle, "low", None))
    close_price = convert_quotation_to_float(getattr(raw_candle, "close", None))
    volume = float(getattr(raw_candle, "volume", 0) or 0)
    turnover_rub = close_price * volume * lot
    placeholder = Candle(
        ticker=ticker,
        begin=begin,
        end=begin,
        open=open_price,
        high=high_price,
        low=low_price,
        close=close_price,
        volume=volume,
        value=turnover_rub,
    )
    return Candle(
        ticker=ticker,
        begin=begin,
        end=candle_close_time(placeholder, timeframe, timezone=settings.timezone),
        open=open_price,
        high=high_price,
        low=low_price,
        close=close_price,
        volume=volume,
        value=turnover_rub,
    )


def _get_tinvest_instruments(
    settings: Settings,
    *,
    force_refresh: bool,
) -> list[dict[str, Any]]:
    cache_path = Path(settings.tinvest_instruments_cache_file)
    if not force_refresh:
        cached = _read_instruments_cache(cache_path)
        if cached:
            return cached

    token = _token(settings)
    if not token:
        logger.warning("T-Invest token is missing. Using MOEX fallback.")
        return []

    try:
        Client, _CandleInterval, InstrumentStatus = _import_tinvest()
    except ImportError as exc:
        logger.warning("T-Invest API error: tinkoff-investments is not installed")
        raise MarketDataError("tinkoff-investments is not installed") from exc

    try:
        with Client(token) as client:
            shares = client.instruments.shares(
                instrument_status=InstrumentStatus.INSTRUMENT_STATUS_BASE,
            ).instruments
    except Exception as exc:
        logger.warning("T-Invest API error while loading instruments: %s", exc)
        raise MarketDataError(str(exc)) from exc

    instruments = [_instrument_to_cache_row(share) for share in shares]
    instruments = [instrument for instrument in instruments if instrument.get("ticker")]
    instruments.sort(key=lambda instrument: str(instrument.get("ticker") or ""))
    _write_instruments_cache(cache_path, instruments)
    logger.info("T-Invest instruments cached: count=%s path=%s", len(instruments), cache_path)
    return instruments


def _read_instruments_cache(cache_path: Path) -> list[dict[str, Any]]:
    if not cache_path.exists():
        return []
    try:
        raw = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("T-Invest instruments cache is invalid: %s", cache_path)
        return []
    if not isinstance(raw, list):
        return []
    instruments = [_normalize_cached_instrument(item) for item in raw]
    return [instrument for instrument in instruments if instrument.get("ticker")]


def _write_instruments_cache(
    cache_path: Path,
    instruments: list[dict[str, Any]],
) -> None:
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(instruments, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        logger.exception("Failed to write T-Invest instruments cache: %s", cache_path)


def _instrument_to_cache_row(share: Any) -> dict[str, Any]:
    return _normalize_cached_instrument(
        {
            "ticker": getattr(share, "ticker", ""),
            "figi": getattr(share, "figi", ""),
            "name": getattr(share, "name", ""),
            "lot": getattr(share, "lot", 1),
            "class_code": getattr(share, "class_code", ""),
            "currency": getattr(share, "currency", ""),
            "instrument_uid": getattr(
                share,
                "instrument_uid",
                getattr(share, "uid", ""),
            ),
        }
    )


def _normalize_cached_instrument(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        "ticker": str(value.get("ticker") or "").strip().upper(),
        "figi": str(value.get("figi") or "").strip(),
        "name": str(value.get("name") or "").strip(),
        "lot": _positive_int(value.get("lot"), default=1),
        "class_code": str(value.get("class_code") or "").strip(),
        "currency": str(value.get("currency") or "").strip().lower(),
        "instrument_uid": str(value.get("instrument_uid") or "").strip(),
    }


def _find_cached_instrument(
    instruments: list[dict[str, Any]],
    ticker: str,
) -> dict[str, Any] | None:
    for instrument in instruments:
        if str(instrument.get("ticker") or "").strip().upper() == ticker:
            return instrument
    return None


def _resolve_candle_interval(CandleInterval: Any, timeframe: str) -> Any:
    try:
        candidates = TINVEST_INTERVAL_NAMES[timeframe]
    except KeyError as exc:
        raise MarketDataError(f"Unsupported T-Invest timeframe: {timeframe}") from exc

    for name in candidates:
        if hasattr(CandleInterval, name):
            return getattr(CandleInterval, name)

    available = [
        name
        for name in dir(CandleInterval)
        if name.startswith("CANDLE_INTERVAL")
    ]
    logger.warning(
        "T-Invest interval is not supported by installed SDK: timeframe=%s",
        timeframe,
    )
    raise MarketDataError(
        f"T-Invest interval is not supported by installed SDK: timeframe={timeframe}. "
        f"Available: {', '.join(available)}"
    )


def _is_complete_tinvest_candle(raw_candle: Any) -> bool:
    is_complete = getattr(raw_candle, "is_complete", None)
    if is_complete is None:
        return True
    return bool(is_complete)


def _as_settings_timezone(value: datetime, settings: Settings) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc).astimezone(settings.timezone)
    return value.astimezone(settings.timezone)


def _positive_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _token(settings: Settings) -> str:
    return str(getattr(settings, "tinkoff_invest_token", "") or "").strip()


def _import_tinvest():
    from tinkoff.invest import CandleInterval, Client, InstrumentStatus

    return Client, CandleInterval, InstrumentStatus
