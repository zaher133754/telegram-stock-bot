from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import date, datetime, tzinfo
from statistics import mean

from moex_client import Candle, MarketDataError, MoexClient


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PriceLevel:
    price: float
    touches: int
    last_date: date | None = None
    strength: str = "normal"


@dataclass(frozen=True)
class RangeInfo:
    lower: float
    upper: float
    width_pct: float
    lower_touches: int
    upper_touches: int


@dataclass(frozen=True)
class AIAnalysisResult:
    ticker: str
    as_of: date | None
    candles: list[Candle]
    current_price: float | None
    weekly_change_pct: float | None
    monthly_change_pct: float | None
    trend_state: str
    support_levels: list[PriceLevel]
    resistance_levels: list[PriceLevel]
    range_info: RangeInfo | None
    sma20: float | None
    sma50: float | None
    sma200: float | None
    last_swing_high: float | None
    last_swing_low: float | None
    atr: float | None
    atr_pct: float | None
    price_vs_sma: dict[str, str]
    strong_support_near: bool
    strong_resistance_near: bool
    entry_type: str | None
    entry_price: float | None
    stop_price: float | None
    take_price: float | None
    risk_pct: float | None
    reward_pct: float | None
    risk_reward_ratio: float | None
    setup_quality: str
    no_trade_setup: bool
    no_trade_reason: str | None
    analysis_text: str = ""


@dataclass(frozen=True)
class AIMarketData:
    candles: list[Candle]
    current_price: float | None = None
    current_price_date: date | None = None


def fetch_daily_candles_for_ai(
    ticker: str,
    years: int = 3,
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
        return client.get_daily_candles_history(ticker, years=years)
    finally:
        client.close()


def fetch_market_data_for_ai(
    ticker: str,
    years: int = 3,
    *,
    board: str = "TQBR",
    timeout: float = 20,
    retries: int = 3,
    timezone: tzinfo | None = None,
) -> AIMarketData:
    client = MoexClient(
        board=board,
        timeout=timeout,
        retries=retries,
        timezone=timezone,
    )
    try:
        candles = client.get_daily_candles_history(ticker, years=years)
        try:
            quote = client.get_current_quote(ticker)
        except MarketDataError as exc:
            logger.warning(
                "AI analysis live quote is unavailable: ticker=%s error=%s",
                ticker,
                exc,
            )
            return AIMarketData(candles=candles)

        quote_date = quote.trade_date
        if quote_date is None and quote.updated_at is not None:
            quote_date = quote.updated_at.date()
        return AIMarketData(
            candles=candles,
            current_price=quote.current_price,
            current_price_date=quote_date,
        )
    finally:
        client.close()


def build_market_context(
    ticker: str,
    candles: list[Candle],
    *,
    current_price: float | None = None,
    current_price_date: date | None = None,
) -> AIAnalysisResult:
    ticker = ticker.strip().upper()
    candles = sorted(candles, key=lambda candle: (candle.begin, candle.end))

    if not candles:
        result = AIAnalysisResult(
            ticker=ticker,
            as_of=None,
            candles=[],
            current_price=None,
            weekly_change_pct=None,
            monthly_change_pct=None,
            trend_state="range",
            support_levels=[],
            resistance_levels=[],
            range_info=None,
            sma20=None,
            sma50=None,
            sma200=None,
            last_swing_high=None,
            last_swing_low=None,
            atr=None,
            atr_pct=None,
            price_vs_sma={},
            strong_support_near=False,
            strong_resistance_near=False,
            entry_type=None,
            entry_price=None,
            stop_price=None,
            take_price=None,
            risk_pct=None,
            reward_pct=None,
            risk_reward_ratio=None,
            setup_quality="no_data",
            no_trade_setup=True,
            no_trade_reason="MOEX не вернул дневные свечи для анализа.",
        )
        return replace(result, analysis_text=render_ai_analysis_text(result))

    closes = [candle.close for candle in candles]
    last_close = closes[-1]
    current_price = current_price if _is_positive_price(current_price) else last_close
    change_closes = _closes_with_current_price(
        closes,
        current_price=current_price,
        current_price_date=current_price_date,
        last_candle_date=candles[-1].begin.date(),
    )
    sma20 = _sma(closes, 20)
    sma50 = _sma(closes, 50)
    sma200 = _sma(closes, 200)
    atr = _atr(candles, 14)
    atr_pct = _pct(atr, current_price) if atr is not None else None
    swing_highs, swing_lows = _detect_swings(candles)
    support_levels = detect_support_levels(candles, current_price=current_price, atr=atr)
    resistance_levels = detect_resistance_levels(
        candles,
        current_price=current_price,
        atr=atr,
    )
    range_info = _detect_range_info(candles, current_price=current_price, atr=atr)
    trend_state = detect_trend_state(
        candles,
        range_info=range_info,
        sma20=sma20,
        sma50=sma50,
        sma200=sma200,
        current_price=current_price,
    )
    strong_support_near = _has_near_level(
        support_levels,
        current_price=current_price,
        atr=atr,
    )
    strong_resistance_near = _has_near_level(
        resistance_levels,
        current_price=current_price,
        atr=atr,
    )
    trade_plan = build_medium_term_trade_plan(
        current_price=current_price,
        trend_state=trend_state,
        support_levels=support_levels,
        resistance_levels=resistance_levels,
        range_info=range_info,
        atr=atr,
        sma20=sma20,
        sma50=sma50,
        sma200=sma200,
    )

    result = AIAnalysisResult(
        ticker=ticker,
        as_of=current_price_date or candles[-1].begin.date(),
        candles=candles,
        current_price=current_price,
        weekly_change_pct=_period_change(change_closes, 5),
        monthly_change_pct=_period_change(change_closes, 21),
        trend_state=trend_state,
        support_levels=support_levels,
        resistance_levels=resistance_levels,
        range_info=range_info,
        sma20=sma20,
        sma50=sma50,
        sma200=sma200,
        last_swing_high=swing_highs[-1][1] if swing_highs else None,
        last_swing_low=swing_lows[-1][1] if swing_lows else None,
        atr=atr,
        atr_pct=atr_pct,
        price_vs_sma={
            "SMA20": _price_position(current_price, sma20),
            "SMA50": _price_position(current_price, sma50),
            "SMA200": _price_position(current_price, sma200),
        },
        strong_support_near=strong_support_near,
        strong_resistance_near=strong_resistance_near,
        entry_type=trade_plan["entry_type"],
        entry_price=trade_plan["entry_price"],
        stop_price=trade_plan["stop_price"],
        take_price=trade_plan["take_price"],
        risk_pct=trade_plan["risk_pct"],
        reward_pct=trade_plan["reward_pct"],
        risk_reward_ratio=trade_plan["risk_reward_ratio"],
        setup_quality=trade_plan["setup_quality"],
        no_trade_setup=trade_plan["no_trade_setup"],
        no_trade_reason=trade_plan["no_trade_reason"],
    )
    return replace(result, analysis_text=render_ai_analysis_text(result))


def detect_support_levels(
    candles: list[Candle],
    current_price: float | None = None,
    atr: float | None = None,
    *,
    limit: int = 4,
) -> list[PriceLevel]:
    if not candles:
        return []

    current_price = current_price or candles[-1].close
    _, swing_lows = _detect_swings(candles)
    points = swing_lows[-40:]
    for lookback in (20, 50, 100):
        if len(candles) >= lookback:
            recent = candles[-lookback:]
            lowest = min(recent, key=lambda candle: candle.low)
            points.append((lowest.begin.date(), lowest.low))

    tolerance = _level_tolerance(current_price, atr)
    levels = _cluster_levels(points, tolerance=tolerance)
    levels = [level for level in levels if level.price <= current_price]
    levels.sort(key=lambda level: current_price - level.price)
    return levels[:limit]


def detect_resistance_levels(
    candles: list[Candle],
    current_price: float | None = None,
    atr: float | None = None,
    *,
    limit: int = 4,
) -> list[PriceLevel]:
    if not candles:
        return []

    current_price = current_price or candles[-1].close
    swing_highs, _ = _detect_swings(candles)
    points = swing_highs[-40:]
    for lookback in (20, 50, 100):
        if len(candles) >= lookback:
            recent = candles[-lookback:]
            highest = max(recent, key=lambda candle: candle.high)
            points.append((highest.begin.date(), highest.high))

    tolerance = _level_tolerance(current_price, atr)
    levels = _cluster_levels(points, tolerance=tolerance)
    levels = [level for level in levels if level.price >= current_price]
    levels.sort(key=lambda level: level.price - current_price)
    return levels[:limit]


def detect_trend_state(
    candles: list[Candle],
    *,
    range_info: RangeInfo | None = None,
    sma20: float | None = None,
    sma50: float | None = None,
    sma200: float | None = None,
    current_price: float | None = None,
) -> str:
    if len(candles) < 50:
        return "range"

    closes = [candle.close for candle in candles]
    current_price = current_price if _is_positive_price(current_price) else closes[-1]
    sma50_past = _sma(closes[:-20], 50) if len(closes) >= 70 else None
    sma50_slope_pct = _pct_change(sma50, sma50_past) if sma50 and sma50_past else 0.0

    if (
        sma20
        and sma50
        and current_price > sma20 > sma50
        and (sma200 is None or sma50 >= sma200)
        and sma50_slope_pct >= -0.5
    ):
        return "uptrend"

    if (
        sma20
        and sma50
        and current_price < sma20 < sma50
        and (sma200 is None or sma50 <= sma200)
        and sma50_slope_pct <= 0.5
    ):
        return "downtrend"

    if (
        sma50
        and current_price > sma50
        and sma50_slope_pct > 1.0
        and (sma200 is None or current_price > sma200)
    ):
        return "uptrend"

    if (
        sma50
        and current_price < sma50
        and sma50_slope_pct < -1.0
        and (sma200 is None or current_price < sma200)
    ):
        return "downtrend"

    if range_info is not None:
        return "range"

    return "range"


def build_medium_term_trade_plan(
    *,
    current_price: float,
    trend_state: str,
    support_levels: list[PriceLevel],
    resistance_levels: list[PriceLevel],
    range_info: RangeInfo | None,
    atr: float | None,
    sma20: float | None,
    sma50: float | None,
    sma200: float | None,
) -> dict[str, float | str | bool | None]:
    if trend_state == "downtrend":
        return _no_trade_plan(
            "структура остается медвежьей, качественную среднесрочную точку входа лучше не натягивать."
        )

    support = support_levels[0] if support_levels else None
    resistance = resistance_levels[0] if resistance_levels else None
    atr_value = atr or current_price * 0.02
    near_support_pct = (
        _pct(current_price - support.price, current_price) if support is not None else None
    )
    near_resistance_pct = (
        _pct(resistance.price - current_price, current_price)
        if resistance is not None
        else None
    )

    if range_info is not None:
        distance_to_lower_pct = _pct(current_price - range_info.lower, current_price)
        distance_to_upper_pct = _pct(range_info.upper - current_price, current_price)
        if distance_to_lower_pct <= max(2.5, _pct(atr_value, current_price) * 1.4):
            entry = _round_price(range_info.lower + max(atr_value * 0.2, range_info.lower * 0.004))
            stop = _round_price(range_info.lower - max(atr_value * 0.8, range_info.lower * 0.012))
            take = _choose_take_price(
                entry,
                resistance_levels,
                preferred_resistance=range_info.upper,
            )
            return _validate_trade_plan("от поддержки в диапазоне", entry, stop, take)
        if distance_to_upper_pct <= 2.5:
            return _no_trade_plan(
                "цена находится близко к верхней границе диапазона, запас хода для сделки 3-5% ограничен."
            )

    if support is not None and near_support_pct is not None and near_support_pct <= 2.5:
        entry = _round_price(support.price + max(atr_value * 0.2, support.price * 0.004))
        stop = _round_price(support.price - max(atr_value * 0.8, support.price * 0.012))
        take = _choose_take_price(entry, resistance_levels)
        return _validate_trade_plan("от поддержки", entry, stop, take)

    if (
        resistance is not None
        and near_resistance_pct is not None
        and near_resistance_pct <= 2.5
    ):
        entry = _round_price(resistance.price * 1.003)
        stop_base = support.price if support is not None else resistance.price - atr_value
        stop = _round_price(max(stop_base - atr_value * 0.3, resistance.price - atr_value * 0.9))
        higher_resistances = [
            level for level in resistance_levels[1:] if level.price > entry * 1.025
        ]
        take = _choose_take_price(entry, higher_resistances)
        return _validate_trade_plan("на закреплении выше сопротивления", entry, stop, take)

    if trend_state == "uptrend" and support is not None:
        pullback_base = support.price
        if sma20 is not None and sma20 < current_price:
            pullback_base = max(pullback_base, sma20)
        if sma50 is not None and sma50 < current_price:
            pullback_base = max(pullback_base, sma50)
        entry = _round_price(pullback_base + max(atr_value * 0.15, pullback_base * 0.003))
        stop = _round_price(pullback_base - max(atr_value * 0.8, pullback_base * 0.012))
        take = _choose_take_price(entry, resistance_levels)
        return _validate_trade_plan("после отката", entry, stop, take)

    return _no_trade_plan(
        "структура нейтральная, а рядом нет понятной поддержки или корректного пробойного уровня."
    )


def render_ai_analysis_text(result: AIAnalysisResult) -> str:
    analysis_date = result.as_of.strftime("%d.%m.%y") if result.as_of else datetime.now().strftime("%d.%m.%y")
    lines = [
        f"AI-анализ: {result.ticker}",
        f"Дата: {analysis_date}",
        "Таймфрейм: 1 день",
        "",
        "Текущая картина:",
    ]

    if result.current_price is None:
        lines.extend(
            [
                "- MOEX не вернул дневные свечи для анализа.",
                "- Качественную среднесрочную точку входа сейчас определить нельзя.",
                "",
                "Что видно на графике:",
                "- Данных недостаточно для расчета уровней, SMA и ATR.",
                "",
                "Среднесрочный сценарий:",
                "- Базовый сценарий: наблюдение со стороны.",
                "- Точка входа: не выделена.",
                "- Стоп: не задается без точки входа.",
                "- Тейк: не задается без точки входа.",
                "- Потенциал: нет расчетного сценария.",
                "- Риск: нет расчетного сценария.",
                "- Соотношение риск/прибыль: нет расчетного сценария.",
                "",
                "Вывод:",
                "- Качественной среднесрочной точки входа пока не видно.",
                "- Наиболее рационально сейчас наблюдение со стороны.",
                "",
                "Это аналитический обзор, не инвестиционная рекомендация.",
            ]
        )
        return "\n".join(lines)

    nearest_support = result.support_levels[0] if result.support_levels else None
    nearest_resistance = result.resistance_levels[0] if result.resistance_levels else None
    nearest_level_text = _nearest_level_text(
        result.current_price,
        nearest_support,
        nearest_resistance,
    )

    lines.extend(
        [
            f"- Акции торгуются у уровня {nearest_level_text}.",
            f"- Текущая цена: {_fmt_price(result.current_price)} ₽.",
            f"- За неделю изменение: {_fmt_pct(result.weekly_change_pct)}.",
            f"- За месяц изменение: {_fmt_pct(result.monthly_change_pct)}.",
            f"- Основной характер движения: {_trend_label(result.trend_state)}.",
            f"- Ближайшая поддержка: {_level_list(result.support_levels)}.",
            f"- Ближайшее сопротивление: {_level_list(result.resistance_levels)}.",
            "",
            "Что видно на графике:",
        ]
    )

    lines.extend(_chart_observations(result))
    lines.extend(["", "Среднесрочный сценарий:"])

    if result.no_trade_setup:
        lines.extend(
            [
                "- Базовый сценарий: наблюдение со стороны.",
                "- Точка входа: качественная точка входа не выделена.",
                "- Стоп: не задается без точки входа.",
                "- Тейк: не задается без точки входа.",
                "- Потенциал: нет расчетного сценария 3-5%.",
                "- Риск: нет расчетного сценария.",
                "- Соотношение риск/прибыль: нет расчетного сценария.",
                "",
                "Вывод:",
                f"- {result.no_trade_reason or 'Структура пока не дает понятного среднесрочного setup.'}",
                "- Если структура не улучшится, рационально наблюдение со стороны.",
                "- Наиболее рационально сейчас наблюдение со стороны.",
                "",
                "Это аналитический обзор, не инвестиционная рекомендация.",
            ]
        )
        return "\n".join(lines)

    lines.extend(
        [
            f"- Базовый сценарий: {_entry_type_label(result.entry_type)}.",
            f"- Точка входа: {_fmt_price(result.entry_price)} ₽.",
            f"- Стоп: {_fmt_price(result.stop_price)} ₽.",
            f"- Тейк: {_fmt_price(result.take_price)} ₽.",
            f"- Потенциал: {_fmt_pct(result.reward_pct)}.",
            f"- Риск: {_fmt_unsigned_pct(result.risk_pct)}.",
            f"- Соотношение риск/прибыль: {_fmt_rr(result.risk_reward_ratio)}.",
            "",
            "Вывод:",
            f"- С технической точки зрения приоритетным сценарием выглядит {_entry_type_label(result.entry_type)}.",
            "- Сценарий рассчитан на среднесрочное движение одной сделкой с целью около 3-5%.",
            "- Если цена не удержит расчетный уровень входа или быстро вернется под него, сценарий лучше отменить.",
            "",
            "Это аналитический обзор, не инвестиционная рекомендация.",
        ]
    )
    return "\n".join(lines)


def _detect_swings(
    candles: list[Candle],
    *,
    lookback: int = 252,
    window: int = 3,
) -> tuple[list[tuple[date, float]], list[tuple[date, float]]]:
    if len(candles) < window * 2 + 1:
        return [], []

    segment = candles[-lookback:]
    swing_highs: list[tuple[date, float]] = []
    swing_lows: list[tuple[date, float]] = []

    for index in range(window, len(segment) - window):
        current = segment[index]
        neighbours = segment[index - window : index] + segment[index + 1 : index + window + 1]
        if current.high >= max(candle.high for candle in neighbours):
            swing_highs.append((current.begin.date(), current.high))
        if current.low <= min(candle.low for candle in neighbours):
            swing_lows.append((current.begin.date(), current.low))

    return swing_highs, swing_lows


def _cluster_levels(
    points: list[tuple[date, float]],
    *,
    tolerance: float,
) -> list[PriceLevel]:
    if not points:
        return []

    clusters: list[list[tuple[date, float]]] = []
    for point in sorted(points, key=lambda item: item[1]):
        if not clusters:
            clusters.append([point])
            continue
        cluster_average = mean(price for _date, price in clusters[-1])
        if abs(point[1] - cluster_average) <= tolerance:
            clusters[-1].append(point)
        else:
            clusters.append([point])

    levels: list[PriceLevel] = []
    for cluster in clusters:
        prices = [price for _date, price in cluster]
        dates = [_date for _date, _price in cluster]
        touches = len(cluster)
        levels.append(
            PriceLevel(
                price=_round_price(mean(prices)),
                touches=touches,
                last_date=max(dates),
                strength="strong" if touches >= 3 else "normal",
            )
        )
    return levels


def _detect_range_info(
    candles: list[Candle],
    *,
    current_price: float,
    atr: float | None,
) -> RangeInfo | None:
    if len(candles) < 45:
        return None

    recent = candles[-60:]
    lower = min(candle.low for candle in recent)
    upper = max(candle.high for candle in recent)
    width_pct = _pct(upper - lower, current_price)
    if width_pct < 3.5 or width_pct > 18.0:
        return None

    tolerance = _level_tolerance(current_price, atr)
    lower_touches = sum(1 for candle in recent if abs(candle.low - lower) <= tolerance)
    upper_touches = sum(1 for candle in recent if abs(candle.high - upper) <= tolerance)
    if lower_touches < 2 or upper_touches < 2:
        return None

    return RangeInfo(
        lower=_round_price(lower),
        upper=_round_price(upper),
        width_pct=width_pct,
        lower_touches=lower_touches,
        upper_touches=upper_touches,
    )


def _atr(candles: list[Candle], period: int) -> float | None:
    if len(candles) < 2:
        return None

    true_ranges: list[float] = []
    for previous, current in zip(candles[-period - 1 : -1], candles[-period:]):
        true_ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    if not true_ranges:
        ranges = [candle.high - candle.low for candle in candles[-period:]]
        return mean(ranges) if ranges else None
    return mean(true_ranges)


def _sma(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return mean(values[-period:])


def _is_positive_price(value: float | None) -> bool:
    return value is not None and value > 0


def _closes_with_current_price(
    closes: list[float],
    *,
    current_price: float,
    current_price_date: date | None,
    last_candle_date: date,
) -> list[float]:
    if not closes or current_price == closes[-1]:
        return closes
    if current_price_date is not None and current_price_date > last_candle_date:
        return [*closes, current_price]
    return [*closes[:-1], current_price]


def _period_change(values: list[float], sessions: int) -> float | None:
    if len(values) <= sessions:
        return None
    return _pct_change(values[-1], values[-sessions - 1])


def _price_position(price: float, average: float | None) -> str:
    if average is None:
        return "нет данных"
    delta = _pct_change(price, average)
    if abs(delta) <= 0.5:
        return "рядом"
    return "выше" if delta > 0 else "ниже"


def _has_near_level(
    levels: list[PriceLevel],
    *,
    current_price: float,
    atr: float | None,
) -> bool:
    if not levels:
        return False
    tolerance = max(_level_tolerance(current_price, atr), current_price * 0.015)
    return abs(levels[0].price - current_price) <= tolerance


def _level_tolerance(current_price: float, atr: float | None) -> float:
    return max(current_price * 0.012, (atr or 0) * 0.6)


def _choose_take_price(
    entry: float,
    resistance_levels: list[PriceLevel],
    *,
    preferred_resistance: float | None = None,
) -> float:
    target_min = entry * 1.03
    target_base = entry * 1.04
    target_max = entry * 1.05

    candidates = [level.price for level in resistance_levels if level.price > entry]
    if preferred_resistance is not None and preferred_resistance > entry:
        candidates.insert(0, preferred_resistance)

    for resistance in candidates:
        adjusted = resistance * 0.995
        if target_min <= adjusted <= target_max:
            return _round_price(adjusted)
        if adjusted > target_max:
            return _round_price(target_base)

    return _round_price(target_base)


def _validate_trade_plan(
    entry_type: str,
    entry: float,
    stop: float,
    take: float,
) -> dict[str, float | str | bool | None]:
    risk_pct = _pct(entry - stop, entry)
    reward_pct = _pct(take - entry, entry)
    risk_reward_ratio = reward_pct / risk_pct if risk_pct > 0 else None

    if risk_pct <= 0 or reward_pct <= 0:
        return _no_trade_plan("расчетная точка входа не дает корректный стоп и тейк.")
    if reward_pct < 2.8:
        return _no_trade_plan("до ближайшей цели нет нужного потенциала 3-5%.")
    if reward_pct > 5.5:
        take = _round_price(entry * 1.05)
        reward_pct = _pct(take - entry, entry)
        risk_reward_ratio = reward_pct / risk_pct if risk_pct > 0 else None
    if risk_pct > 4.5:
        return _no_trade_plan("стоп получается слишком широким для сделки с целью 3-5%.")
    if risk_reward_ratio is None or risk_reward_ratio < 1.0:
        return _no_trade_plan("соотношение риск/прибыль слабое для среднесрочного setup.")

    return {
        "entry_type": entry_type,
        "entry_price": entry,
        "stop_price": stop,
        "take_price": take,
        "risk_pct": risk_pct,
        "reward_pct": reward_pct,
        "risk_reward_ratio": risk_reward_ratio,
        "setup_quality": "good" if risk_reward_ratio >= 1.2 else "acceptable",
        "no_trade_setup": False,
        "no_trade_reason": None,
    }


def _no_trade_plan(reason: str) -> dict[str, float | str | bool | None]:
    return {
        "entry_type": None,
        "entry_price": None,
        "stop_price": None,
        "take_price": None,
        "risk_pct": None,
        "reward_pct": None,
        "risk_reward_ratio": None,
        "setup_quality": "weak",
        "no_trade_setup": True,
        "no_trade_reason": reason,
    }


def _chart_observations(result: AIAnalysisResult) -> list[str]:
    observations = [
        (
            "- Цена относительно средних: "
            f"SMA20 - {result.price_vs_sma.get('SMA20', 'нет данных')}, "
            f"SMA50 - {result.price_vs_sma.get('SMA50', 'нет данных')}, "
            f"SMA200 - {result.price_vs_sma.get('SMA200', 'нет данных')}."
        )
    ]

    if result.range_info is not None:
        observations.append(
            "- Инструмент торгуется в локальном диапазоне "
            f"{_fmt_price(result.range_info.lower)}-{_fmt_price(result.range_info.upper)} ₽."
        )
    elif result.trend_state == "uptrend":
        observations.append("- Основным сценарием остается движение в восходящей структуре.")
    elif result.trend_state == "downtrend":
        observations.append("- Локально ситуация выглядит медвежьей, признаков устойчивого разворота пока не видно.")
    else:
        observations.append("- Движение выглядит боковым: цена не дает чистого направленного преимущества.")

    if result.strong_support_near:
        observations.append("- В качестве поддержки рассматривается ближайший подтвержденный уровень.")
    if result.strong_resistance_near:
        observations.append("- Рядом есть сопротивление, поэтому вход под уровнем требует осторожности.")
    if result.last_swing_high is not None and result.last_swing_low is not None:
        observations.append(
            "- Последние значимые swing-уровни: high "
            f"{_fmt_price(result.last_swing_high)} ₽, low {_fmt_price(result.last_swing_low)} ₽."
        )
    return observations


def _nearest_level_text(
    current_price: float,
    support: PriceLevel | None,
    resistance: PriceLevel | None,
) -> str:
    if support is None and resistance is None:
        return f"{_fmt_price(current_price)} ₽ без близкого подтвержденного уровня"
    if support is None:
        return f"сопротивления {_fmt_price(resistance.price)} ₽"
    if resistance is None:
        return f"поддержки {_fmt_price(support.price)} ₽"
    support_distance = abs(current_price - support.price)
    resistance_distance = abs(resistance.price - current_price)
    if support_distance <= resistance_distance:
        return f"поддержки {_fmt_price(support.price)} ₽"
    return f"сопротивления {_fmt_price(resistance.price)} ₽"


def _level_list(levels: list[PriceLevel]) -> str:
    if not levels:
        return "не выделена"
    return ", ".join(_fmt_price(level.price) + " ₽" for level in levels[:3])


def _trend_label(value: str) -> str:
    return {
        "uptrend": "бычий",
        "downtrend": "медвежий",
        "range": "боковой",
    }.get(value, value)


def _entry_type_label(value: str | None) -> str:
    return {
        "от поддержки": "работа от поддержки",
        "от поддержки в диапазоне": "работа от нижней границы локального диапазона",
        "на закреплении выше сопротивления": "вход только при закреплении выше сопротивления",
        "после отката": "вход после отката к поддержке или средней",
    }.get(value, "наблюдение со стороны")


def _fmt_price(value: float | None) -> str:
    if value is None:
        return "нет данных"
    if float(value).is_integer():
        return f"{int(value)}"
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "нет данных"
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.2f}%"


def _fmt_unsigned_pct(value: float | None) -> str:
    if value is None:
        return "нет данных"
    return f"{abs(value):.2f}%"


def _fmt_rr(value: float | None) -> str:
    if value is None:
        return "нет данных"
    return f"{value:.2f}"


def _round_price(value: float) -> float:
    if value >= 1000:
        return round(value, 1)
    if value >= 10:
        return round(value, 2)
    return round(value, 4)


def _pct(value: float, base: float) -> float:
    if base == 0:
        return 0.0
    return value / base * 100


def _pct_change(current: float | None, previous: float | None) -> float:
    if current is None or previous in (None, 0):
        return 0.0
    return (current - previous) / previous * 100
