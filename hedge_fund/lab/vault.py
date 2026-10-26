"""The holdout vault — a DataClient that cannot see the future or the embargo.

Research backtests get their data through this wrapper. Two rules, both
enforced by raising (never by silently clamping, which would hide a bug that
then reappears in production):

1. Nothing after `research_end`. Any date-bounded query whose end is past the
   cutoff raises VaultViolation; anything the provider returns that is dated
   past the cutoff (late filings, news, earnings records) is dropped too, as
   defence in depth.
2. Nothing about an embargoed ticker. Those names exist only for the
   one-shot holdout evaluation.

"Latest" endpoints with no date (get_earnings, get_snapshot) are refused
outright: they are not point-in-time by construction.

This is enforcement at the data layer, which is what the agents' MCP tools
go through. An agent with a raw shell and the provider API key can still
bypass it — see lab/README.md "Threat model".
"""

from __future__ import annotations

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
from hedge_fund.data.protocol import DataClient


class VaultViolation(PermissionError):
    """Research asked for data it is not allowed to see."""


_EARNINGS_OVERFETCH = 40  # extra records fetched so filtering keeps `limit` real ones


def _day(stamp: str | None) -> str | None:
    return stamp[:10] if stamp else None


class VaultedDataClient:
    def __init__(self, inner: DataClient, *, research_end: str, embargoed: list[str] | None = None) -> None:
        self._inner = inner
        self.research_end = research_end
        self.embargoed = {t.upper() for t in (embargoed or [])}
        self.violations: list[str] = []

    # ------------------------------------------------------------------
    # Guards
    # ------------------------------------------------------------------

    def _deny(self, message: str) -> None:
        self.violations.append(message)
        raise VaultViolation(message)

    def _ticker(self, ticker: str) -> None:
        if ticker.upper() in self.embargoed:
            self._deny(f"{ticker} is embargoed for the holdout; research may not query it")

    def _end(self, ticker: str, end_date: str | None, what: str) -> None:
        if end_date is not None and end_date[:10] > self.research_end:
            self._deny(f"{what} for {ticker} through {end_date} is past the research cutoff {self.research_end}")

    def _visible(self, stamp: str | None) -> bool:
        day = _day(stamp)
        return day is None or day <= self.research_end

    # ------------------------------------------------------------------
    # DataClient protocol
    # ------------------------------------------------------------------

    def get_prices(self, ticker: str, start_date: str, end_date: str, **kwargs) -> list[Price]:
        self._ticker(ticker)
        self._end(ticker, end_date, "prices")
        return [p for p in self._inner.get_prices(ticker, start_date, end_date, **kwargs) if self._visible(p.time)]

    def get_snapshot(self, ticker: str) -> PriceSnapshot | None:
        self._deny(f"live quote for {ticker}: research has no 'now'")

    def get_financial_metrics(self, ticker: str, end_date: str, period: str = "ttm", limit: int = 10) -> list[FinancialMetrics]:
        self._ticker(ticker)
        self._end(ticker, end_date, "financial metrics")
        rows = self._inner.get_financial_metrics(ticker, end_date, period, limit)
        return [m for m in rows if self._visible(m.filing_date or m.report_period)]

    def get_news(self, ticker: str, end_date: str, start_date: str | None = None, limit: int = 1000) -> list[CompanyNews]:
        self._ticker(ticker)
        self._end(ticker, end_date, "news")
        return [n for n in self._inner.get_news(ticker, end_date, start_date, limit) if self._visible(n.date)]

    def get_insider_trades(self, ticker: str, end_date: str, start_date: str | None = None, limit: int = 1000) -> list[InsiderTrade]:
        self._ticker(ticker)
        self._end(ticker, end_date, "insider trades")
        rows = self._inner.get_insider_trades(ticker, end_date, start_date, limit)
        return [t for t in rows if self._visible(t.filing_date)]

    def get_company_facts(self, ticker: str) -> CompanyFacts | None:
        self._ticker(ticker)
        return self._inner.get_company_facts(ticker)

    def get_earnings(self, ticker: str) -> Earnings | None:
        self._deny(f"latest earnings for {ticker}: not point-in-time; use get_earnings_history")

    def get_earnings_history(self, ticker: str, limit: int = 12) -> list[EarningsRecord]:
        self._ticker(ticker)
        rows = self._inner.get_earnings_history(ticker, limit + _EARNINGS_OVERFETCH)
        visible = [r for r in rows if self._visible(r.filing_date or r.report_period)]
        if len(visible) <= limit:
            return visible
        keep = set(id(r) for r in sorted(visible, key=lambda r: r.filing_date or r.report_period, reverse=True)[:limit])
        return [r for r in visible if id(r) in keep]

    def get_market_cap(self, ticker: str, end_date: str) -> float | None:
        self._ticker(ticker)
        self._end(ticker, end_date, "market cap")
        return self._inner.get_market_cap(ticker, end_date)
