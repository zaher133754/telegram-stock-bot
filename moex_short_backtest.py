from __future__ import annotations

import json
import math
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen


TICKERS = [
    "NVTK",
    "PHOR",
    "RUAL",
    "OZON",
    "POSI",
    "SNGSP",
    "TATNP",
    "MTSS",
    "CHMF",
    "T",
    "X5",
    "MOEX",
    "DOMRF",
    "ROSN",
]

DATE_FROM = "2023-06-19"
DATE_TILL = "2026-06-19"
BOARD = "TQBR"
FEE = 0.009
DEPOSITS = [20_000.0, 50_000.0]
BASE = "https://iss.moex.com/iss"


@dataclass
class Bar:
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass
class Dividend:
    registry_close: date
    value: float
    currency: str


@dataclass
class Trade:
    entry_date: date
    exit_date: date
    qty: int
    entry: float
    exit: float
    gross: float
    fees: float
    dividends: float
    net: float


def fetch_json(path: str, params: dict[str, object] | None = None) -> dict:
    params = params or {}
    params = {"iss.meta": "off", **params}
    url = f"{BASE}{path}?{urlencode(params)}"
    req = Request(url, headers={"User-Agent": "codex-moex-backtest/1.0"})
    with urlopen(req, timeout=40) as resp:
        return json.loads(resp.read().decode("utf-8"))


def table_rows(payload: dict, name: str) -> list[dict[str, object]]:
    block = payload.get(name) or {}
    columns = block.get("columns") or []
    rows = block.get("data") or []
    return [dict(zip(columns, row)) for row in rows]


def fetch_candles(secid: str) -> list[Bar]:
    out: list[Bar] = []
    start = 0
    while True:
        payload = fetch_json(
            f"/engines/stock/markets/shares/boards/{BOARD}/securities/{secid}/candles.json",
            {
                "from": DATE_FROM,
                "till": DATE_TILL,
                "interval": 24,
                "start": start,
                "iss.only": "candles",
            },
        )
        rows = table_rows(payload, "candles")
        for r in rows:
            out.append(
                Bar(
                    date=datetime.strptime(str(r["begin"])[:10], "%Y-%m-%d").date(),
                    open=float(r["open"]),
                    high=float(r["high"]),
                    low=float(r["low"]),
                    close=float(r["close"]),
                    volume=int(float(r["volume"])),
                )
            )
        if len(rows) < 500:
            break
        start += len(rows)
        time.sleep(0.05)

    out.sort(key=lambda b: b.date)
    dedup: dict[date, Bar] = {}
    for bar in out:
        dedup[bar.date] = bar
    return [dedup[d] for d in sorted(dedup)]


def fetch_lotsize(secid: str) -> int:
    payload = fetch_json(
        f"/engines/stock/markets/shares/securities/{secid}.json",
        {"iss.only": "securities"},
    )
    rows = table_rows(payload, "securities")
    for row in rows:
        if row.get("BOARDID") == BOARD and row.get("LOTSIZE"):
            return int(row["LOTSIZE"])
    for row in rows:
        if row.get("LOTSIZE"):
            return int(row["LOTSIZE"])
    return 1


def fetch_dividends(secid: str) -> list[Dividend]:
    payload = fetch_json(f"/securities/{secid}/dividends.json", {"iss.only": "dividends"})
    rows = table_rows(payload, "dividends")
    out: list[Dividend] = []
    for r in rows:
        if not r.get("registryclosedate") or r.get("value") is None:
            continue
        try:
            registry_close = datetime.strptime(str(r["registryclosedate"]), "%Y-%m-%d").date()
        except ValueError:
            continue
        if not (date.fromisoformat(DATE_FROM) <= registry_close <= date.fromisoformat(DATE_TILL)):
            continue
        currency = str(r.get("currencyid") or "")
        if currency != "RUB":
            continue
        out.append(Dividend(registry_close=registry_close, value=float(r["value"]), currency=currency))
    return out


def sma(values: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    total = 0.0
    for i, value in enumerate(values):
        total += value
        if i >= n:
            total -= values[i - n]
        if i >= n - 1:
            out[i] = total / n
    return out


def rolling_std(values: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    for i in range(n - 1, len(values)):
        out[i] = statistics.pstdev(values[i - n + 1 : i + 1])
    return out


def rolling_min(values: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    for i in range(n, len(values)):
        out[i] = min(values[i - n : i])
    return out


def rolling_max(values: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    for i in range(n, len(values)):
        out[i] = max(values[i - n : i])
    return out


def rsi(values: list[float], n: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) <= n:
        return out
    gains = []
    losses = []
    for i in range(1, n + 1):
        delta = values[i] - values[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    avg_gain = sum(gains) / n
    avg_loss = sum(losses) / n
    out[n] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    for i in range(n + 1, len(values)):
        delta = values[i] - values[i - 1]
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)
        avg_gain = (avg_gain * (n - 1) + gain) / n
        avg_loss = (avg_loss * (n - 1) + loss) / n
        out[i] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return out


def ema(values: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) < n:
        return out
    alpha = 2.0 / (n + 1)
    prev = sum(values[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(values)):
        prev = values[i] * alpha + prev * (1 - alpha)
        out[i] = prev
    return out


SignalFn = Callable[[list[Bar]], tuple[list[bool], list[bool]]]


def strat_sma_trend(bars: list[Bar]) -> tuple[list[bool], list[bool]]:
    close = [b.close for b in bars]
    ma20 = sma(close, 20)
    ma60 = sma(close, 60)
    entry = [False] * len(bars)
    exit_ = [False] * len(bars)
    for i in range(len(bars)):
        if ma20[i] is None or ma60[i] is None:
            continue
        entry[i] = close[i] < ma20[i] < ma60[i]
        exit_[i] = close[i] > ma20[i] or ma20[i] >= ma60[i]
    return entry, exit_


def strat_donchian_breakdown(bars: list[Bar]) -> tuple[list[bool], list[bool]]:
    close = [b.close for b in bars]
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    low20 = rolling_min(lows, 20)
    high10 = rolling_max(highs, 10)
    entry = [False] * len(bars)
    exit_ = [False] * len(bars)
    for i in range(len(bars)):
        if low20[i] is not None:
            entry[i] = close[i] < low20[i]
        if high10[i] is not None:
            exit_[i] = close[i] > high10[i]
    return entry, exit_


def strat_ma_cross(bars: list[Bar]) -> tuple[list[bool], list[bool]]:
    close = [b.close for b in bars]
    ma20 = sma(close, 20)
    ma50 = sma(close, 50)
    entry = [False] * len(bars)
    exit_ = [False] * len(bars)
    for i in range(1, len(bars)):
        if None in (ma20[i - 1], ma50[i - 1], ma20[i], ma50[i]):
            continue
        entry[i] = ma20[i - 1] >= ma50[i - 1] and ma20[i] < ma50[i]
        exit_[i] = ma20[i - 1] <= ma50[i - 1] and ma20[i] > ma50[i]
    return entry, exit_


def strat_rsi_overbought(bars: list[Bar]) -> tuple[list[bool], list[bool]]:
    close = [b.close for b in bars]
    r = rsi(close, 14)
    entry = [False] * len(bars)
    exit_ = [False] * len(bars)
    for i in range(len(bars)):
        if r[i] is None:
            continue
        entry[i] = r[i] >= 70
        exit_[i] = r[i] <= 50
    return entry, exit_


def strat_bollinger_reversal(bars: list[Bar]) -> tuple[list[bool], list[bool]]:
    close = [b.close for b in bars]
    ma20 = sma(close, 20)
    sd20 = rolling_std(close, 20)
    entry = [False] * len(bars)
    exit_ = [False] * len(bars)
    for i in range(len(bars)):
        if ma20[i] is None or sd20[i] is None:
            continue
        upper = ma20[i] + 2.0 * sd20[i]
        entry[i] = close[i] > upper
        exit_[i] = close[i] <= ma20[i]
    return entry, exit_


def strat_macd_down(bars: list[Bar]) -> tuple[list[bool], list[bool]]:
    close = [b.close for b in bars]
    e12 = ema(close, 12)
    e26 = ema(close, 26)
    macd: list[float | None] = [None] * len(close)
    for i in range(len(close)):
        if e12[i] is not None and e26[i] is not None:
            macd[i] = e12[i] - e26[i]
    macd_values = [0.0 if v is None else v for v in macd]
    sig_raw = ema(macd_values, 9)
    ma50 = sma(close, 50)
    entry = [False] * len(bars)
    exit_ = [False] * len(bars)
    for i in range(1, len(bars)):
        if None in (macd[i - 1], sig_raw[i - 1], macd[i], sig_raw[i], ma50[i]):
            continue
        entry[i] = macd[i - 1] >= sig_raw[i - 1] and macd[i] < sig_raw[i] and close[i] < ma50[i]
        exit_[i] = macd[i - 1] <= sig_raw[i - 1] and macd[i] > sig_raw[i]
    return entry, exit_


STRATEGIES: dict[str, SignalFn] = {
    "SMA20<60 trend": strat_sma_trend,
    "Donchian 20/10 breakdown": strat_donchian_breakdown,
    "MA20/50 bear cross": strat_ma_cross,
    "RSI14>=70 reversal": strat_rsi_overbought,
    "Bollinger20 upper reversal": strat_bollinger_reversal,
    "MACD bear cross": strat_macd_down,
}


def dividend_ex_dates(bars: list[Bar], dividends: list[Dividend]) -> dict[date, float]:
    dates = [b.date for b in bars]
    ex: dict[date, float] = {}
    if not dates:
        return ex
    switch_t1 = date(2023, 7, 31)
    for div in dividends:
        lag = 1 if div.registry_close >= switch_t1 else 2
        last_buy_idx = None
        for i in range(0, len(dates) - lag):
            settle_date = dates[i + lag]
            if settle_date <= div.registry_close:
                last_buy_idx = i
        if last_buy_idx is None:
            ex_idx = next((i for i, d in enumerate(dates) if d >= div.registry_close), None)
        else:
            ex_idx = last_buy_idx + 1
        if ex_idx is not None and ex_idx < len(dates):
            ex[dates[ex_idx]] = ex.get(dates[ex_idx], 0.0) + div.value
    return ex


def backtest(
    bars: list[Bar],
    lot_size: int,
    ex_dividends: dict[date, float],
    entry_signal: list[bool],
    exit_signal: list[bool],
    deposit: float,
) -> dict[str, object]:
    equity = deposit
    qty = 0
    entry_price = 0.0
    entry_date: date | None = None
    entry_fee = 0.0
    divs_for_trade = 0.0
    trades: list[Trade] = []
    curve: list[float] = []

    def close_position(exit_date: date, exit_price: float) -> None:
        nonlocal equity, qty, entry_price, entry_date, entry_fee, divs_for_trade
        if qty <= 0 or entry_date is None:
            return
        exit_fee = exit_price * qty * FEE
        gross = (entry_price - exit_price) * qty
        net = gross - entry_fee - exit_fee - divs_for_trade
        equity += gross - exit_fee
        trades.append(
            Trade(
                entry_date=entry_date,
                exit_date=exit_date,
                qty=qty,
                entry=entry_price,
                exit=exit_price,
                gross=gross,
                fees=entry_fee + exit_fee,
                dividends=divs_for_trade,
                net=net,
            )
        )
        qty = 0
        entry_price = 0.0
        entry_date = None
        entry_fee = 0.0
        divs_for_trade = 0.0

    for i, bar in enumerate(bars):
        if qty > 0 and bar.date in ex_dividends:
            debit = ex_dividends[bar.date] * qty
            equity -= debit
            divs_for_trade += debit

        if i > 0 and qty > 0 and exit_signal[i - 1]:
            close_position(bar.date, bar.open)

        if i > 0 and qty == 0 and entry_signal[i - 1] and equity > 0:
            lots = math.floor(equity / (bar.open * lot_size))
            new_qty = lots * lot_size
            if new_qty > 0:
                qty = new_qty
                entry_price = bar.open
                entry_date = bar.date
                entry_fee = entry_price * qty * FEE
                equity -= entry_fee
                divs_for_trade = 0.0

        mtm = equity
        if qty > 0:
            mtm += (entry_price - bar.close) * qty
        curve.append(mtm)

    if qty > 0:
        close_position(bars[-1].date, bars[-1].close)
        curve[-1] = equity

    peak = deposit
    max_dd = 0.0
    for value in curve:
        peak = max(peak, value)
        if peak > 0:
            max_dd = max(max_dd, (peak - value) / peak)

    wins = sum(1 for t in trades if t.net > 0)
    exposure_days = 0
    for t in trades:
        exposure_days += max(1, (t.exit_date - t.entry_date).days)
    return {
        "final": equity,
        "profit": equity - deposit,
        "return_pct": (equity / deposit - 1.0) * 100.0,
        "max_dd_pct": max_dd * 100.0,
        "trades": len(trades),
        "wins": wins,
        "win_rate_pct": (wins / len(trades) * 100.0) if trades else 0.0,
        "avg_trade": (sum(t.net for t in trades) / len(trades)) if trades else 0.0,
        "best_trade": max((t.net for t in trades), default=0.0),
        "worst_trade": min((t.net for t in trades), default=0.0),
        "exposure_days": exposure_days,
    }


def fmt_money(v: float) -> str:
    return f"{v:,.0f}".replace(",", " ")


def main() -> int:
    all_results: list[dict[str, object]] = []
    coverage: list[dict[str, object]] = []

    for secid in TICKERS:
        try:
            bars = fetch_candles(secid)
            lot_size = fetch_lotsize(secid)
            dividends = fetch_dividends(secid)
        except Exception as exc:
            print(f"ERROR {secid}: {exc}", file=sys.stderr)
            continue
        if not bars:
            print(f"NO_DATA {secid}", file=sys.stderr)
            continue
        ex_divs = dividend_ex_dates(bars, dividends)
        coverage.append(
            {
                "ticker": secid,
                "first": bars[0].date.isoformat(),
                "last": bars[-1].date.isoformat(),
                "bars": len(bars),
                "lot": lot_size,
                "dividends": len(dividends),
            }
        )
        for strategy, fn in STRATEGIES.items():
            entry, exit_ = fn(bars)
            for dep in DEPOSITS:
                res = backtest(bars, lot_size, ex_divs, entry, exit_, dep)
                all_results.append(
                    {
                        "ticker": secid,
                        "strategy": strategy,
                        "deposit": dep,
                        **res,
                    }
                )
        time.sleep(0.1)

    print("COVERAGE")
    for row in coverage:
        print(
            f"{row['ticker']:6} {row['first']}..{row['last']} "
            f"bars={row['bars']:4} lot={row['lot']:3} dividends={row['dividends']}"
        )

    print("\nBEST_BY_TICKER")
    for dep in DEPOSITS:
        print(f"\nDEPOSIT {fmt_money(dep)} RUB")
        for secid in TICKERS:
            rows = [r for r in all_results if r["ticker"] == secid and r["deposit"] == dep]
            if not rows:
                continue
            best = max(rows, key=lambda r: (float(r["profit"]), -float(r["max_dd_pct"])))
            print(
                f"{secid:6} {best['strategy']:<28} "
                f"profit={fmt_money(float(best['profit'])):>8} "
                f"ret={float(best['return_pct']):7.2f}% "
                f"dd={float(best['max_dd_pct']):6.2f}% "
                f"trades={int(best['trades']):2d} win={float(best['win_rate_pct']):5.1f}%"
            )

    print("\nAGGREGATE_BY_STRATEGY_SUM_OF_INDEPENDENT_TICKERS")
    for dep in DEPOSITS:
        print(f"\nDEPOSIT {fmt_money(dep)} RUB")
        for strategy in STRATEGIES:
            rows = [r for r in all_results if r["strategy"] == strategy and r["deposit"] == dep]
            profit = sum(float(r["profit"]) for r in rows)
            trades = sum(int(r["trades"]) for r in rows)
            winners = sum(1 for r in rows if float(r["profit"]) > 0)
            print(
                f"{strategy:<28} sum_profit={fmt_money(profit):>9} "
                f"trades={trades:3d} profitable_tickers={winners:2d}/{len(rows)}"
            )

    print("\nALL_RESULTS_TSV")
    headers = [
        "ticker",
        "strategy",
        "deposit",
        "profit",
        "return_pct",
        "max_dd_pct",
        "trades",
        "win_rate_pct",
        "avg_trade",
        "best_trade",
        "worst_trade",
    ]
    print("\t".join(headers))
    for r in all_results:
        print(
            "\t".join(
                [
                    str(r["ticker"]),
                    str(r["strategy"]),
                    f"{float(r['deposit']):.0f}",
                    f"{float(r['profit']):.2f}",
                    f"{float(r['return_pct']):.4f}",
                    f"{float(r['max_dd_pct']):.4f}",
                    str(int(r["trades"])),
                    f"{float(r['win_rate_pct']):.2f}",
                    f"{float(r['avg_trade']):.2f}",
                    f"{float(r['best_trade']):.2f}",
                    f"{float(r['worst_trade']):.2f}",
                ]
            )
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
