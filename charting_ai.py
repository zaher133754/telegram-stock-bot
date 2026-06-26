from __future__ import annotations

from pathlib import Path

from ai_analysis import AIAnalysisResult


DEFAULT_AI_CHART_DIR = Path(__file__).resolve().parent / ".tmp" / "ai_charts"


def generate_ai_chart(
    analysis: AIAnalysisResult,
    *,
    output_dir: Path | None = None,
    candles_limit: int = 150,
) -> Path:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    output_dir = output_dir or DEFAULT_AI_CHART_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"ai_analysis_{analysis.ticker}.png"

    fig, (ax, panel) = plt.subplots(
        1,
        2,
        figsize=(13, 7),
        gridspec_kw={"width_ratios": [4.7, 1.35]},
        constrained_layout=True,
    )
    fig.patch.set_facecolor("#ffffff")
    ax.set_facecolor("#ffffff")
    panel.set_facecolor("#f7f9fb")
    panel.axis("off")

    if not analysis.candles:
        ax.text(
            0.5,
            0.5,
            "Нет дневных свечей для построения графика",
            ha="center",
            va="center",
            transform=ax.transAxes,
            fontsize=13,
            color="#263238",
        )
        ax.set_title(f"AI-анализ {analysis.ticker} | 1D", loc="left", fontsize=16, pad=12)
        _draw_side_panel(panel, analysis)
        fig.savefig(output_path, dpi=140, facecolor=fig.get_facecolor())
        plt.close(fig)
        return output_path

    candles_limit = max(30, min(candles_limit, len(analysis.candles)))
    all_candles = analysis.candles
    candles = all_candles[-candles_limit:]
    start_index = len(all_candles) - len(candles)
    x_values = list(range(len(candles)))

    for x_value, candle in zip(x_values, candles):
        color = "#2e7d32" if candle.close >= candle.open else "#c62828"
        ax.vlines(x_value, candle.low, candle.high, color=color, linewidth=1.0, alpha=0.85)
        lower = min(candle.open, candle.close)
        height = abs(candle.close - candle.open)
        min_height = max(candle.close * 0.0008, 0.01)
        rect = Rectangle(
            (x_value - 0.32, lower),
            0.64,
            max(height, min_height),
            facecolor=color,
            edgecolor=color,
            linewidth=0.8,
            alpha=0.78,
        )
        ax.add_patch(rect)

    closes = [candle.close for candle in all_candles]
    for period, color, label in (
        (20, "#1565c0", "SMA20"),
        (50, "#6a1b9a", "SMA50"),
        (200, "#455a64", "SMA200"),
    ):
        series = _moving_average_series(closes, period)[start_index:]
        xs = [index for index, value in enumerate(series) if value is not None]
        ys = [value for value in series if value is not None]
        if xs and ys:
            ax.plot(xs, ys, color=color, linewidth=1.35, label=label)

    if analysis.support_levels:
        _draw_level(ax, analysis.support_levels[0].price, "Support", "#2e7d32", "--")
    if analysis.resistance_levels:
        _draw_level(ax, analysis.resistance_levels[0].price, "Resistance", "#c62828", "--")
    if analysis.current_price is not None:
        _draw_level(ax, analysis.current_price, "Current", "#ef6c00", ":")
    if not analysis.no_trade_setup:
        _draw_level(ax, analysis.entry_price, "Entry", "#0277bd", "-")
        _draw_level(ax, analysis.stop_price, "Stop", "#b71c1c", "-")
        _draw_level(ax, analysis.take_price, "Take", "#1b5e20", "-")

    ax.set_title(f"AI-анализ {analysis.ticker} | 1D", loc="left", fontsize=16, pad=12)
    ax.set_ylabel("Цена, ₽")
    ax.grid(True, axis="y", color="#d9e0e6", linewidth=0.8, alpha=0.85)
    ax.grid(False, axis="x")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color("#cfd8dc")
    ax.spines["bottom"].set_color("#cfd8dc")

    tick_step = max(1, len(candles) // 8)
    tick_positions = list(range(0, len(candles), tick_step))
    if tick_positions[-1] != len(candles) - 1:
        tick_positions.append(len(candles) - 1)
    ax.set_xticks(tick_positions)
    ax.set_xticklabels(
        [candles[index].begin.strftime("%d.%m") for index in tick_positions],
        rotation=0,
        ha="center",
    )
    ax.legend(loc="upper left", frameon=False, fontsize=9)
    ax.margins(x=0.02)

    _draw_side_panel(panel, analysis)
    fig.savefig(output_path, dpi=140, facecolor=fig.get_facecolor())
    plt.close(fig)
    return output_path


def _draw_level(ax, price: float | None, label: str, color: str, linestyle: str) -> None:
    if price is None:
        return
    ax.axhline(price, color=color, linewidth=1.2, linestyle=linestyle, alpha=0.9)
    x_right = ax.get_xlim()[1]
    ax.text(
        x_right,
        price,
        f" {label} {_fmt_price(price)} ",
        va="center",
        ha="right",
        fontsize=8.5,
        color=color,
        bbox={"facecolor": "#ffffff", "edgecolor": color, "boxstyle": "round,pad=0.18"},
    )


def _draw_side_panel(panel, analysis: AIAnalysisResult) -> None:
    panel.text(
        0.08,
        0.94,
        "План",
        fontsize=13,
        weight="bold",
        color="#263238",
        transform=panel.transAxes,
    )
    rows = [
        ("Entry", _fmt_price(analysis.entry_price) if analysis.entry_price else "наблюдение"),
        ("Stop", _fmt_price(analysis.stop_price) if analysis.stop_price else "-"),
        ("Take", _fmt_price(analysis.take_price) if analysis.take_price else "-"),
        ("Risk", _fmt_unsigned_pct(analysis.risk_pct)),
        ("Potential", _fmt_pct(analysis.reward_pct)),
    ]
    y = 0.84
    for label, value in rows:
        panel.text(0.08, y, label, fontsize=9, color="#607d8b", transform=panel.transAxes)
        panel.text(
            0.08,
            y - 0.045,
            value,
            fontsize=11,
            color="#263238",
            transform=panel.transAxes,
        )
        y -= 0.135

    panel.text(
        0.08,
        0.12,
        "Среднесрочный\nсценарий 3-5%",
        fontsize=10,
        color="#455a64",
        transform=panel.transAxes,
    )


def _moving_average_series(values: list[float], period: int) -> list[float | None]:
    series: list[float | None] = []
    running_sum = 0.0
    for index, value in enumerate(values):
        running_sum += value
        if index >= period:
            running_sum -= values[index - period]
        if index + 1 >= period:
            series.append(running_sum / period)
        else:
            series.append(None)
    return series


def _fmt_price(value: float | None) -> str:
    if value is None:
        return "-"
    if float(value).is_integer():
        return f"{int(value)} ₽"
    return f"{value:.2f}".rstrip("0").rstrip(".") + " ₽"


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "-"
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.2f}%"


def _fmt_unsigned_pct(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{abs(value):.2f}%"
