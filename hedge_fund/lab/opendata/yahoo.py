"""Daily bars from Yahoo's chart endpoint, fetched one calendar year at a time.

aihf asks for many overlapping windows (every session re-reads a lookback),
so prices are fetched per (ticker, year) and cached: past years forever, the
current year for six hours. Prices are split-adjusted ("close"), not
dividend-adjusted. Bars are stamped at midnight New York time, the same
convention as Financial Datasets, so `time[:10]` is the session date.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from math import isfinite

from hedge_fund.data.models import Price
from hedge_fund.data.sessions import NEW_YORK
from hedge_fund.lab.opendata.http import Fetcher

CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?period1={p1}&period2={p2}&interval=1d&events=split"
CURRENT_YEAR_TTL = 6 * 3600


def yahoo_symbol(ticker: str) -> str:
    return ticker.upper().replace(".", "-")  # BRK.B -> BRK-B


def _epoch(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


def _stamp(day: date) -> str:
    midnight = datetime(day.year, day.month, day.day, tzinfo=NEW_YORK)
    return midnight.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def year_bars(fetcher: Fetcher, ticker: str, year: int) -> list[Price]:
    today = date.today()
    if year > today.year:
        return []
    p1, p2 = _epoch(date(year, 1, 1)), _epoch(date(year + 1, 1, 1))
    ttl = None if year < today.year else CURRENT_YEAR_TTL
    body = fetcher.get(CHART.format(symbol=yahoo_symbol(ticker), p1=p1, p2=p2), ttl=ttl)
    if not body:
        return []
    result = (body.get("chart") or {}).get("result")
    if not result:
        return []
    r = result[0]
    stamps = r.get("timestamp") or []
    quote = ((r.get("indicators") or {}).get("quote") or [{}])[0]
    bars: dict[str, Price] = {}
    for i, ts in enumerate(stamps):
        try:
            o, h, l, c, v = (quote[k][i] for k in ("open", "high", "low", "close", "volume"))
        except (KeyError, IndexError):
            continue
        if c is None or not isfinite(c) or c <= 0:
            continue
        day = datetime.fromtimestamp(ts, NEW_YORK).date()
        if day.year != year:
            continue
        bars[day.isoformat()] = Price(open=o or c, high=h or c, low=l or c, close=c, volume=int(v or 0),
                                      time=_stamp(day))
    return [bars[k] for k in sorted(bars)]


def daily_bars(fetcher: Fetcher, ticker: str, start: str, end: str) -> list[Price]:
    s, e = date.fromisoformat(start[:10]), date.fromisoformat(end[:10])
    out: list[Price] = []
    for year in range(s.year, e.year + 1):
        out.extend(b for b in year_bars(fetcher, ticker, year) if start[:10] <= b.time[:10] <= end[:10])
    return out
