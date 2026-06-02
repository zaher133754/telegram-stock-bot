from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo

from config import Settings
from moex_client import Candle, MarketDataError, MoexClient
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

TRADING_CONDITION_TEXT = "close последней свечи > high предыдущей свечи"


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
    latest_candle_time: str | None
    updated_at: datetime
    tickers_count: int

    @property
    def reference_comparison(self) -> CandleComparison | None:
        if not self.comparisons:
            return None
        return max(self.comparisons, key=lambda item: item.last.end)

    @property
    def current_period_items(self) -> list[CandleComparison]:
        reference = self.reference_comparison
        if reference is None:
            return []
        return [
            item
            for item in self.comparisons
            if item.last.end == reference.last.end
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


def collect_moex_analysis(settings: Settings, timeframe: str) -> AnalysisResult:
    tickers = load_tickers(settings.tickers_file)
    client = MoexClient(
        board=settings.moex_board,
        timeout=settings.moex_timeout_seconds,
        timezone=settings.timezone,
    )
    return analyze_tickers(
        client=client,
        tickers=tickers,
        timeframe=timeframe,
        timezone=settings.timezone,
    )


def analyze_tickers(
    *,
    client: MoexClient,
    tickers: list[str],
    timeframe: str,
    timezone: tzinfo,
) -> AnalysisResult:
    comparisons: list[CandleComparison] = []
    failures: list[tuple[str, str]] = []
    latest_candle_times: list[datetime] = []

    for ticker in tickers:
        try:
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
        latest_candle_times.append(comparison.last.end)
        log_missing_turnover(comparison)

    latest_candle_time = None
    if latest_candle_times:
        latest_candle_time = max(latest_candle_times).isoformat()

    return AnalysisResult(
        timeframe=timeframe,
        comparisons=comparisons,
        failures=failures,
        latest_candle_time=latest_candle_time,
        updated_at=datetime.now(timezone),
        tickers_count=len(tickers),
    )


def compare_last_two_candles(candles: list[Candle]) -> CandleComparison:
    if len(candles) < 2:
        raise ValueError("Need at least two closed candles")

    previous = candles[-2]
    last = candles[-1]
    percent = calculate_percent_change(last.close, previous.close)
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
        append_condition_items(lines, matched_items, streaks=streaks)
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
                (
                    f"{index}. {ticker} — close: {format_price(item.last.close)} ₽ "
                    f"/ {format_percent(item.percent_change)}"
                ),
                f"   High предыдущей свечи: {format_price(item.previous.high)} ₽",
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
        f"Период последней свечи: {format_period(reference.last, result.timeframe)}{suffix}",
        f"Период предыдущей свечи: {format_period(reference.previous, result.timeframe)}{suffix}",
        "",
    ]


def format_period(candle: Candle, timeframe: str) -> str:
    if timeframe in INTRADAY_TIMEFRAME_MINUTES:
        end = candle.end
        if end > candle.begin and end.second == 0 and end.microsecond == 0:
            end -= timedelta(minutes=1)
        return f"{candle.begin:%H:%M}–{end:%H:%M}"
    if timeframe == "1d":
        return format_date(candle.begin)
    if timeframe == "1w":
        end = (
            candle.end - timedelta(days=1)
            if candle.end.date() > candle.begin.date()
            else candle.end
        )
        return f"{format_date(candle.begin)}–{format_date(end)}"
    if timeframe == "1mo":
        month = MONTH_NAMES.get(candle.begin.month, f"{candle.begin.month:02d}")
        return f"{month} {candle.begin.year}"
    return f"{candle.begin.isoformat()}–{candle.end.isoformat()}"


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
    lines = ["Не удалось получить данные:"]
    for ticker, error in failed:
        lines.append(f"- {ticker}: {error}")
    return lines


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
