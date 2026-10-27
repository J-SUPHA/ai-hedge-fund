"""OpenDataClient: aihf's DataClient protocol, served from scraped public sources.

Contract kept from aihf: empty means "genuinely none", infrastructure
failures raise. Things no free source provides raise OpenDataUnsupported
instead of returning a misleading empty list.
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

from hedge_fund.data.models import (
    CompanyFacts,
    CompanyNews,
    Earnings,
    EarningsRecord,
    FinancialMetrics,
    InsiderTrade,
    Price,
    PriceSnapshot,
)
from hedge_fund.lab.opendata import sec as _sec
from hedge_fund.lab.opendata import yahoo
from hedge_fund.lab.opendata.http import Fetcher

CAPABILITIES = {
    "prices": "Yahoo daily OHLCV, split-adjusted (not dividend-adjusted); daily bars only",
    "financial_metrics": "SEC XBRL, point-in-time TTM rows per 10-Q/10-K, dated by filing",
    "company_facts": "SEC submissions (name, SIC industry/sector, exchange)",
    "earnings_history": "SEC 8-K item 2.02 releases at acceptance time; surprise = vs year-ago quarter (proxy, no consensus)",
    "insider_trades": "SEC Form 4 XML, by filing date",
    "market_cap": "SEC shares outstanding (dei) x Yahoo close",
    "news": "UNSUPPORTED: no free historical news source; ingest scraped news via the alt-data store",
    "survivorship": "Yahoo lacks most delisted tickers; SEC ticker map is current listings only",
}


class OpenDataUnsupported(NotImplementedError):
    """No free source provides this; say so loudly rather than return []."""


class OpenDataClient:
    source = "open"

    def __init__(self, cache_dir: Path | None = None, *, fetcher: Fetcher | None = None) -> None:
        self.fetcher = fetcher or Fetcher(cache_dir)
        self.sec = _sec.SEC(self.fetcher)

    # -- prices -------------------------------------------------------------

    def get_prices(self, ticker: str, start_date: str, end_date: str, interval: str = "day",
                   interval_multiplier: int = 1, **_) -> list[Price]:
        if interval != "day" or interval_multiplier != 1:
            raise OpenDataUnsupported(f"open data serves daily bars only (asked {interval_multiplier} {interval})")
        return yahoo.daily_bars(self.fetcher, ticker, start_date, end_date)

    @lru_cache(maxsize=4096)
    def close_on_or_before(self, ticker: str, day: str) -> float | None:
        start = (date.fromisoformat(day[:10]) - timedelta(days=10)).isoformat()
        bars = self.get_prices(ticker, start, day[:10])
        return bars[-1].close if bars else None

    def get_snapshot(self, ticker: str) -> PriceSnapshot | None:
        today = date.today().isoformat()
        bars = self.get_prices(ticker, (date.today() - timedelta(days=10)).isoformat(), today)
        if not bars:
            return None
        return PriceSnapshot(ticker=ticker, price=bars[-1].close, time=bars[-1].time)

    # -- fundamentals -------------------------------------------------------

    @lru_cache(maxsize=2048)
    def _metrics(self, ticker: str, end_date: str, limit: int) -> tuple[FinancialMetrics, ...]:
        return tuple(_sec.metrics_rows(self.sec, ticker, end_date, limit,
                                       lambda day: self.close_on_or_before(ticker, day)))

    def get_financial_metrics(self, ticker: str, end_date: str, period: str = "ttm",
                              limit: int = 10) -> list[FinancialMetrics]:
        if period != "ttm":
            raise OpenDataUnsupported(f"open data builds TTM rows only (asked {period})")
        return list(self._metrics(ticker.upper(), end_date[:10], limit))

    def get_company_facts(self, ticker: str) -> CompanyFacts | None:
        return _sec.company_facts(self.sec, ticker.upper())

    def get_market_cap(self, ticker: str, end_date: str) -> float | None:
        shares = _sec.shares_outstanding(self.sec, ticker.upper(), end_date[:10])
        price = self.close_on_or_before(ticker.upper(), end_date[:10])
        return shares * price if shares and price else None

    # -- events -------------------------------------------------------------

    @lru_cache(maxsize=512)
    def _earnings(self, ticker: str, limit: int) -> tuple[EarningsRecord, ...]:
        return tuple(_sec.earnings_history(self.sec, ticker, limit))

    def get_earnings_history(self, ticker: str, limit: int = 12) -> list[EarningsRecord]:
        return list(self._earnings(ticker.upper(), limit))

    def get_earnings(self, ticker: str) -> Earnings | None:
        latest = self.get_earnings_history(ticker, 1)
        if not latest:
            return None
        r = latest[0]
        return Earnings(ticker=r.ticker, report_period=r.report_period, currency=r.currency,
                        quarterly=r.quarterly)

    def get_insider_trades(self, ticker: str, end_date: str, start_date: str | None = None,
                           limit: int = 1000) -> list[InsiderTrade]:
        return _sec.insider_trades(self.sec, ticker.upper(), end_date, start_date, limit)

    def get_news(self, ticker: str, end_date: str, start_date: str | None = None,
                 limit: int = 1000) -> list[CompanyNews]:
        raise OpenDataUnsupported(CAPABILITIES["news"])

    # context-manager parity with FDClient
    def __enter__(self) -> OpenDataClient:
        return self

    def __exit__(self, *exc) -> None:
        return None
