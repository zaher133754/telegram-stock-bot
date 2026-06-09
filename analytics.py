from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo

from config import Settings
from moex_client import (
    Candle,
    MarketDataError,
    MoexClient,
    candle_close_time,
    candle_key,
)
from utils import load_tickers, timeframe_label, timezone_label


logger = logging.getLogger(__name__)

MONTH_NAMES = {
    1: "январь",
    2: "февраль",
    3: "март",
    4: "апрель",
    5: "май",
    6: "июнь",
    7: "июль",
    8: "август",
    9: "сентябрь",
    10: "октябрь",
    11: "ноябрь",
    12: "декабрь",
}

INTRADAY_TIMEFRAME_MINUTES = {
    "1m": 1,
    "10m": 10,
    "1h": 60,
}

TRADING_CONDITION_TEXT = "close последней закрытой свечи > high предыдущей закрытой свечи"


@dataclass(frozen=True)
class CandleComparison:
    ticker: str
    last: Candle
    previous: Candle
    percent_change: float
    matches_condition: bool


@dataclass(frozen=True)
class AnalysisResult:
    timeframe: str
    comparisons: list[CandleComparison]
    failures: list[tuple[str, str]]
    updated_at: datetime
    tickers_count: int
    moex_requests_count: int = 0
    debug_last_candles: list[Candle] = field(default_factory=list)

    @property
    def reference_comparison(self) -> CandleComparison | None:
        if not self.comparisons:
            return None
        return max(
            self.comparisons,
            key=lambda item: candle_key(item.last, self.timeframe),
        )

    @property
    def latest_candle_key(self) -> str | None:
        reference = self.reference_comparison
        if reference is None:
            return None
        return candle_key(reference.last, self.timeframe)

    @property
    def previous_candle_key(self) -> str | None:
        reference = self.reference_comparison
        if reference is None:
            return None
        return candle_key(reference.previous, self.timeframe)

    @property
    def current_period_items(self) -> list[CandleComparison]:
        latest_candle_key = self.latest_candle_key
        if latest_candle_key is None:
            return []
        return [
            item
            for item in self.comparisons
            if candle_key(item.last, self.timeframe) == latest_candle_key
        ]

    @property
    def matched_items(self) -> list[CandleComparison]:
        return [item for item in self.current_period_items if item.matches_condition]

    @property
    def turnover_items(self) -> list[CandleComparison]:
        return sorted(
            [item for item in self.current_period_items if item.last.value is not None],
            key=lambda item: item.last.value or 0,
            reverse=True,
        )


def collect_moex_analysis(
    settings: Settings,
    timeframe: str,
    tickers: list[str] | tuple[str, ...] | None = None,
) -> AnalysisResult:
    selected_tickers = normalize_tickers(tickers)
    if tickers is None:
        selected_tickers = load_tickers(settings.tickers_file)
    client = MoexClient(
        board=settings.moex_board,
        timeout=settings.moex_timeout_seconds,
        retries=settings.moex_request_retries,
        timezone=settings.timezone,
    )
    try:
        return analyze_tickers(
            client=client,
            tickers=selected_tickers,
            timeframe=timeframe,
            timezone=settings.timezone,
        )
    finally:
        client.close()


def analyze_tickers(
    *,
    client: MoexClient,
    tickers: list[str],
    timeframe: str,
    timezone: tzinfo,
) -> AnalysisResult:
    comparisons: list[CandleComparison] = []
    failures: list[tuple[str, str]] = []
    debug_last_candles: list[Candle] = []

    for ticker in tickers:
        try:
            if timeframe == "1h":
                candles = client.get_candles(ticker, timeframe, limit=5)
                if len(candles) < 2:
                    raise MarketDataError(
                        f"{ticker.upper()}: less than two closed candles found for {timeframe}"
                    )
                if not debug_last_candles:
                    debug_last_candles = candles[-5:]
            else:
                candles = client.get_last_two_closed_candles(ticker, timeframe)
            comparison = compare_last_two_candles(candles)
        except MarketDataError as exc:
            logger.warning("Failed to get candle data for %s: %s", ticker, exc)
            failures.append((ticker, str(exc)))
            continue
        except Exception as exc:
            logger.exception("Unexpected candle data error for %s", ticker)
            failures.append((ticker, str(exc)))
            continue

        comparisons.append(comparison)
        log_missing_turnover(comparison)

    return AnalysisResult(
        timeframe=timeframe,
        comparisons=comparisons,
        failures=failures,
        updated_at=datetime.now(timezone),
        tickers_count=len(tickers),
        moex_requests_count=int(getattr(client, "request_count", 0)),
        debug_last_candles=debug_last_candles,
    )


def normalize_tickers(value: list[str] | tuple[str, ...] | None) -> list[str]:
    if value is None:
        return []

    tickers: list[str] = []
    seen: set[str] = set()
    for raw_ticker in value:
        ticker = str(raw_ticker).strip().upper()
        if not ticker or ticker in seen:
            continue
        tickers.append(ticker)
        seen.add(ticker)
    return tickers


def compare_last_two_candles(candles: list[Candle]) -> CandleComparison:
    if len(candles) < 2:
        raise ValueError("Need at least two closed candles")

    previous = candles[-2]
    last = candles[-1]
    percent = calculate_breakout_percent(last.close, previous.high)
    return CandleComparison(
        ticker=last.ticker,
        last=last,
        previous=previous,
        percent_change=percent,
        matches_condition=last.close > previous.high,
    )


def calculate_percent_change(last_close: float, previous_close: float) -> float:
    if previous_close == 0:
        return 0.0
    return (last_close - previous_close) / previous_close * 100


def calculate_breakout_percent(close_last: float, high_previous: float) -> float:
    return calculate_percent_change(close_last, high_previous)


def build_manual_report(result: AnalysisResult, *, timezone_name: str) -> str:
    lines: list[str] = [
        "🔍 Проверка по закрытым свечам",
        f"Таймфрейм: {timeframe_label(result.timeframe)}",
        "",
        "Условие:",
        TRADING_CONDITION_TEXT,
        "",
    ]
    lines.extend(format_reference_period_lines(result, timezone_name=timezone_name))

    matched_items = result.matched_items
    if matched_items:
        append_condition_items(lines, matched_items)
    else:
        lines.extend(
            [
                (
                    "Сейчас нет акций, у которых последняя закрытая свеча закрылась "
                    "выше high предыдущей свечи на выбранном таймфрейме."
                ),
                "",
            ]
        )

    if result.failures:
        lines.extend(format_failures(result.failures))
        lines.append("")

    lines.append(
        f"Обновлено: {result.updated_at:%H:%M} {timezone_label(timezone_name)}"
    )
    return "\n".join(lines).strip()


def build_auto_notification_report(
    result: AnalysisResult,
    *,
    timezone_name: str,
    streaks: dict[str, int],
) -> str:
    lines: list[str] = [
        "🔔 Автоуведомление MOEX",
        f"Таймфрейм: {timeframe_label(result.timeframe)}",
        "",
        "Условие:",
        TRADING_CONDITION_TEXT,
        "",
    ]
    lines.extend(format_reference_period_lines(result, timezone_name=timezone_name))

    matched_items = result.matched_items
    if matched_items:
        append_auto_condition_items(lines, matched_items, streaks=streaks)
    else:
        lines.extend(
            [
                "Подходящих тикеров на новой закрытой свече нет.",
                "",
            ]
        )

    if result.failures:
        lines.extend(format_failures(result.failures))
        lines.append("")

    lines.extend(
        [
            f"Обновлено: {result.updated_at:%H:%M} {timezone_label(timezone_name)}",
            "",
            "Это не инвестиционная рекомендация.",
        ]
    )
    return "\n".join(lines).strip()


def build_hourly_supplement_report(
    result: AnalysisResult,
    *,
    timezone_name: str,
    tickers: list[str],
    streaks: dict[str, int],
) -> str:
    if result.timeframe != "1h":
        raise ValueError("Hourly supplements are only supported for 1h")

    requested_tickers = {ticker.strip().upper() for ticker in tickers if ticker.strip()}
    supplement_items = [
        item
        for item in result.matched_items
        if item.ticker.upper() in requested_tickers
    ]

    lines: list[str] = [
        "🔔 Дополнение к часовому отчёту",
        f"Таймфрейм: {timeframe_label(result.timeframe)}",
        "",
        "Новые тикеры по той же закрытой часовой свече:",
        "",
    ]
    lines.extend(format_reference_period_lines(result, timezone_name=timezone_name))
    append_auto_condition_items(lines, supplement_items, streaks=streaks)

    if result.failures:
        lines.extend(format_failures(result.failures))
        lines.append("")

    lines.extend(
        [
            f"Обновлено: {result.updated_at:%H:%M} {timezone_label(timezone_name)}",
            "",
            "Это не инвестиционная рекомендация.",
        ]
    )
    return "\n".join(lines).strip()


def build_empty_auto_notification_report(
    result: AnalysisResult,
    *,
    timezone_name: str,
) -> str:
    reference = result.reference_comparison
    if reference is None:
        raise ValueError("Cannot build an empty report without closed candle data")

    timeframe = result.timeframe
    if timeframe == "1h":
        lines = [
            "🔔 Автоуведомление MOEX",
            f"Таймфрейм: {timeframe_label(timeframe)}",
            "",
            "Условие:",
            TRADING_CONDITION_TEXT,
            "",
            *format_reference_period_lines(result, timezone_name=timezone_name),
            (
                "Нет тикеров, у которых close последней закрытой свечи выше "
                "high предыдущей закрытой свечи."
            ),
        ]
    elif timeframe == "1d":
        lines = [
            "📅 Дневной отчёт MOEX",
            "",
            "Условие:",
            "close последней закрытой дневной свечи > high предыдущей дневной свечи",
            "",
            f"Дата последней закрытой свечи: {format_date(reference.last.begin)}",
            f"Дата предыдущей закрытой свечи: {format_date(reference.previous.begin)}",
            "",
            "Нет тикеров, которые закрылись выше максимума предыдущего торгового дня.",
        ]
    elif timeframe == "1w":
        lines = [
            "📅 Недельный отчёт MOEX",
            "",
            "Условие:",
            "close последней закрытой недельной свечи > high предыдущей недельной свечи",
            "",
            f"Период последней закрытой недели: {format_period(reference.last, timeframe)}",
            f"Период предыдущей закрытой недели: {format_period(reference.previous, timeframe)}",
            "",
            "Нет тикеров, которые закрылись выше максимума предыдущей недели.",
        ]
    elif timeframe == "1mo":
        lines = [
            "📅 Месячный отчёт MOEX",
            "",
            "Условие:",
            "close последней закрытой месячной свечи > high предыдущей месячной свечи",
            "",
            f"Период последнего закрытого месяца: {format_period(reference.last, timeframe)}",
            f"Период предыдущего закрытого месяца: {format_period(reference.previous, timeframe)}",
            "",
            "Нет тикеров, которые закрылись выше максимума предыдущего месяца.",
        ]
    else:
        raise ValueError(f"Empty auto reports are not supported for {timeframe}")

    if result.failures:
        lines.extend(["", *format_failures(result.failures)])

    lines.extend(
        [
            "",
            f"Обновлено: {result.updated_at:%H:%M} {timezone_label(timezone_name)}",
            "",
            "Это не инвестиционная рекомендация.",
        ]
    )
    return "\n".join(lines).strip()


def build_turnover_report(
    result: AnalysisResult,
    *,
    timezone_name: str,
    limit: int = 10,
) -> str:
    lines: list[str] = [
        "💰 Оборот",
        f"Таймфрейм: {timeframe_label(result.timeframe)}",
        "Используются только закрытые свечи.",
        "",
    ]
    lines.extend(format_reference_period_lines(result, timezone_name=timezone_name))

    turnover_items = result.turnover_items[:limit]
    if turnover_items:
        for index, item in enumerate(turnover_items, start=1):
            lines.extend(
                [
                    f"{index}. {item.ticker}",
                    f"   Оборот последней свечи: {format_turnover(item.last.value or 0)}",
                ]
            )
            if item.previous.value is not None:
                lines.append(
                    f"   Оборот предыдущей свечи: {format_turnover(item.previous.value)}"
                )
            lines.append("")
    else:
        lines.extend(
            [
                "MOEX не вернул поле value для тикеров на выбранном таймфрейме.",
                "",
            ]
        )

    if result.failures:
        lines.extend(format_failures(result.failures))
        lines.append("")

    lines.append(
        f"Обновлено: {result.updated_at:%H:%M} {timezone_label(timezone_name)}"
    )
    return "\n".join(lines).strip()


def append_condition_items(
    lines: list[str],
    items: list[CandleComparison],
    *,
    streaks: dict[str, int] | None = None,
) -> None:
    streaks = streaks or {}
    for index, item in enumerate(items, start=1):
        ticker = format_ticker_with_streak(item.ticker, streaks.get(item.ticker, 1))
        lines.extend(
            [
                f"{index}. {ticker} — close: {format_price(item.last.close)} ₽",
                f"   High предыдущей свечи: {format_price(item.previous.high)} ₽",
                f"   Пробой high: {format_percent(item.percent_change)}",
            ]
        )
        lines.extend(format_turnover_lines(item))
        lines.append("")


def append_auto_condition_items(
    lines: list[str],
    items: list[CandleComparison],
    *,
    streaks: dict[str, int],
) -> None:
    for index, item in enumerate(items, start=1):
        ticker = format_ticker_with_streak(item.ticker, streaks.get(item.ticker, 1))
        lines.extend(
            [
                f"{index}. {ticker}",
                f"   Close: {format_price(item.last.close)} ₽",
                f"   High предыдущей свечи: {format_price(item.previous.high)} ₽",
                (
                    "   Пробой high: "
                    f"{format_percent(item.percent_change)}"
                ),
            ]
        )
        lines.extend(format_turnover_lines(item))
        lines.append("")


def format_ticker_with_streak(ticker: str, streak: int) -> str:
    if streak >= 2:
        return f"{ticker} (X{streak})"
    return ticker


def format_turnover_lines(item: CandleComparison) -> list[str]:
    lines: list[str] = []
    if item.last.value is not None:
        lines.append(f"   Оборот последней свечи: {format_turnover(item.last.value)}")
    if item.previous.value is not None:
        lines.append(
            f"   Оборот предыдущей свечи: {format_turnover(item.previous.value)}"
        )
    return lines


def format_reference_period_lines(
    result: AnalysisResult,
    *,
    timezone_name: str,
) -> list[str]:
    reference = result.reference_comparison
    if reference is None:
        return []

    suffix = (
        f" {timezone_label(timezone_name)}"
        if result.timeframe in INTRADAY_TIMEFRAME_MINUTES
        else ""
    )
    return [
        f"Период последней закрытой свечи: {format_period(reference.last, result.timeframe)}{suffix}",
        f"Период предыдущей закрытой свечи: {format_period(reference.previous, result.timeframe)}{suffix}",
        "",
    ]


def format_period(candle: Candle, timeframe: str) -> str:
    if timeframe in INTRADAY_TIMEFRAME_MINUTES:
        end = candle_close_time(candle, timeframe) - timedelta(minutes=1)
        return f"{candle.begin:%H:%M}–{end:%H:%M}"
    if timeframe == "1d":
        return format_date(candle.begin)
    if timeframe == "1w":
        end = candle_close_time(candle, timeframe) - timedelta(days=1)
        return f"{format_date(candle.begin)}–{format_date(end)}"
    if timeframe == "1mo":
        month = MONTH_NAMES.get(candle.begin.month, f"{candle.begin.month:02d}")
        return f"{month} {candle.begin.year}"
    return f"{candle.begin.isoformat()}–{candle.end.isoformat()}"


def format_candle_key_period(candle_key_value: str, timeframe: str) -> str:
    try:
        if timeframe in INTRADAY_TIMEFRAME_MINUTES:
            begin = datetime.strptime(candle_key_value, "%Y-%m-%d %H:%M")
            end = begin + timedelta(minutes=INTRADAY_TIMEFRAME_MINUTES[timeframe] - 1)
            return f"{begin:%H:%M}–{end:%H:%M}"

        if timeframe == "1d":
            return format_date(datetime.strptime(candle_key_value, "%Y-%m-%d"))

        if timeframe == "1w":
            year_part, week_part = candle_key_value.split("-W", 1)
            begin = datetime.fromisocalendar(int(year_part), int(week_part), 1)
            end = begin + timedelta(days=6)
            return f"{format_date(begin)}–{format_date(end)}"

        if timeframe == "1mo":
            year_part, month_part = candle_key_value.split("-", 1)
            month = int(month_part)
            month_name = MONTH_NAMES.get(month, f"{month:02d}")
            return f"{month_name} {int(year_part)}"
    except (TypeError, ValueError):
        return candle_key_value

    return candle_key_value


def format_date(value: datetime) -> str:
    return f"{value:%d.%m.%Y}"


def format_price(value: float) -> str:
    if float(value).is_integer():
        return f"{int(value)}"
    return f"{value:.2f}"


def format_percent(value: float) -> str:
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.2f}%"


def format_turnover(value: float) -> str:
    if value >= 1_000_000_000:
        return f"{format_compact_number(value / 1_000_000_000)} млрд ₽"
    if value >= 1_000_000:
        return f"{format_compact_number(value / 1_000_000)} млн ₽"
    if value >= 1_000:
        return f"{int(value // 1_000)} тыс ₽"
    return f"{format_compact_number(value)} ₽"


def format_compact_number(value: float) -> str:
    if float(value).is_integer():
        return f"{int(value)}"
    return f"{value:.2f}".rstrip("0").rstrip(".")


def format_failures(failed: list[tuple[str, str]]) -> list[str]:
    tickers = list(dict.fromkeys(ticker for ticker, _error in failed))
    return [
        "⚠️ Часть тикеров временно не удалось проверить: "
        f"{', '.join(tickers)}."
    ]


def log_missing_turnover(item: CandleComparison) -> None:
    if item.last.value is None:
        logger.warning(
            "%s: MOEX candle field VALUE is missing for last candle",
            item.ticker,
        )
    if item.previous.value is None:
        logger.warning(
            "%s: MOEX candle field VALUE is missing for previous candle",
            item.ticker,
        )


build_growth_report = build_manual_report
