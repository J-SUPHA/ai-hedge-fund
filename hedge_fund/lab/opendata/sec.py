"""SEC EDGAR, scraped: point-in-time fundamentals, earnings releases, insider trades.

Point-in-time is the whole point. Every XBRL fact in `companyfacts` carries
the date it was `filed`. A metrics row for a 10-Q/10-K filed on day C is built
ONLY from facts filed on or before C (restatements filed later are invisible
to it), and that row's `filing_date` is C, so the vault can drop it when C is
past the research cutoff.

Flow items (revenue, income, cash flow) are reported as quarters in income
statements but as year-to-date totals in 10-Q cash-flow statements; both are
reduced to discrete quarters, then summed into trailing-twelve-month values.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Iterable

from hedge_fund.data.models import (
    CompanyFacts,
    EarningsData,
    EarningsRecord,
    FinancialMetrics,
    InsiderTrade,
)
from hedge_fund.data.sessions import NEW_YORK
from hedge_fund.lab.opendata.http import Fetcher

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
OLDER_SUBMISSIONS_URL = "https://data.sec.gov/submissions/{name}"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accn}/{doc}"

DAY = 86400
PERIODIC_FORMS = {"10-Q", "10-K", "10-Q/A", "10-K/A"}

FLOW = {
    "revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet"],
    "cost_of_revenue": ["CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold"],
    "gross_profit": ["GrossProfit"],
    "operating_income": ["OperatingIncomeLoss"],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "eps": ["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted", "EarningsPerShareBasic"],
    "ocf": ["NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"],
    "dividends": ["PaymentsOfDividends", "PaymentsOfDividendsCommonStock"],
}
INSTANT = {
    "equity": ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "assets": ["Assets"],
    "liabilities": ["Liabilities"],
    "current_assets": ["AssetsCurrent"],
    "current_liabilities": ["LiabilitiesCurrent"],
    "lt_debt": ["LongTermDebtNoncurrent", "LongTermDebt"],
    "st_debt": ["LongTermDebtCurrent", "ShortTermBorrowings", "DebtCurrent"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue"],
}

SIC_DIVISIONS = [  # (low, high, sector)
    (100, 999, "Agriculture"), (1000, 1499, "Mining"), (1500, 1799, "Construction"),
    (2000, 3999, "Manufacturing"), (4000, 4999, "Transportation & Utilities"),
    (5000, 5199, "Wholesale Trade"), (5200, 5999, "Retail Trade"),
    (6000, 6799, "Finance, Insurance & Real Estate"), (7000, 8999, "Services"),
    (9100, 9999, "Public Administration"),
]

TX_CODES = {"P": "Purchase", "S": "Sale", "A": "Award", "M": "Option Exercise", "F": "Tax Withholding",
            "G": "Gift", "D": "Disposition to Issuer", "C": "Conversion", "X": "Option Exercise",
            "J": "Other", "W": "Will/Inheritance"}


def _d(s: str) -> date:
    return date.fromisoformat(s[:10])


# ---------------------------------------------------------------------------
# Raw documents
# ---------------------------------------------------------------------------

class SEC:
    def __init__(self, fetcher: Fetcher) -> None:
        self.f = fetcher
        self._facts: dict[int, "FactIndex"] = {}
        self._filings: dict[int, list[dict]] = {}
        self._profile: dict[int, dict] = {}

    @lru_cache(maxsize=1)
    def _ticker_map(self) -> dict[str, dict]:
        body = self.f.get(TICKERS_URL, ttl=7 * DAY) or {}
        return {row["ticker"].upper(): row for row in body.values()}

    def cik(self, ticker: str) -> int | None:
        m = self._ticker_map()
        t = ticker.upper()
        row = m.get(t) or m.get(t.replace(".", "-")) or m.get(t.replace("-", "."))
        return int(row["cik_str"]) if row else None

    def _submissions(self, cik: int) -> dict:
        return self.f.get(SUBMISSIONS_URL.format(cik=cik), ttl=DAY) or {}

    def profile(self, cik: int) -> dict:
        if cik not in self._profile:
            s = self._submissions(cik)
            self._profile[cik] = {k: s.get(k) for k in ("name", "sic", "sicDescription", "exchanges", "tickers")}
        return self._profile[cik]

    def filings(self, cik: int) -> list[dict]:
        """Every filing (recent + archived pages), as dicts, newest first."""
        if cik in self._filings:
            return self._filings[cik]
        s = self._submissions(cik)
        tables = [(s.get("filings") or {}).get("recent") or {}]
        for page in (s.get("filings") or {}).get("files") or []:
            tables.append(self.f.get(OLDER_SUBMISSIONS_URL.format(name=page["name"]), ttl=7 * DAY) or {})
        rows = []
        for t in tables:
            n = len(t.get("accessionNumber", []))
            for i in range(n):
                rows.append({k: (t.get(k) or [None] * n)[i] for k in (
                    "accessionNumber", "filingDate", "acceptanceDateTime", "reportDate", "form",
                    "items", "primaryDocument")})
        rows.sort(key=lambda r: (r["filingDate"] or "", r["acceptanceDateTime"] or ""), reverse=True)
        self._filings[cik] = rows
        return rows

    def facts(self, cik: int) -> "FactIndex":
        if cik not in self._facts:
            self._facts[cik] = FactIndex(self.f.get(FACTS_URL.format(cik=cik), ttl=DAY) or {})
        return self._facts[cik]

    def document(self, cik: int, accession: str, doc: str) -> str | None:
        """A filed document. Filed documents never change: cached forever."""
        url = ARCHIVE_URL.format(cik=cik, accn=accession.replace("-", ""), doc=doc)
        return self.f.get(url, ttl=None, kind="text")


# ---------------------------------------------------------------------------
# XBRL facts → point-in-time values
# ---------------------------------------------------------------------------

class FactIndex:
    """Facts by concept, each record: (start|None, end, val, filed, form, fp, fy)."""

    def __init__(self, body: dict) -> None:
        self.records: dict[str, list[tuple]] = {}
        for taxonomy in ("us-gaap", "dei", "ifrs-full"):
            for concept, data in ((body.get("facts") or {}).get(taxonomy) or {}).items():
                for unit, rows in (data.get("units") or {}).items():
                    if unit not in ("USD", "USD/shares", "shares"):
                        continue
                    recs = self.records.setdefault(concept, [])
                    for r in rows:
                        if "filed" not in r or "end" not in r or r.get("val") is None:
                            continue
                        recs.append((r.get("start"), r["end"], float(r["val"]), r["filed"],
                                     r.get("form"), r.get("fp"), r.get("fy")))

    def asof(self, concepts: list[str], cutoff: str) -> dict[tuple, float]:
        """{(start, end): val} using the latest value filed on or before cutoff.
        Earlier concept names win when two report the same period."""
        out: dict[tuple, tuple[str, float]] = {}
        for concept in reversed(concepts):  # earlier names overwrite later ones
            best: dict[tuple, tuple[str, float]] = {}
            for start, end, val, filed, *_ in self.records.get(concept, ()):
                if filed > cutoff:
                    continue
                key = (start, end)
                if key not in best or filed >= best[key][0]:
                    best[key] = (filed, val)
            out.update(best)
        return {k: v for k, (_, v) in out.items()}


def _days(start: str, end: str) -> int:
    return (_d(end) - _d(start)).days


def discrete_quarters(values: dict[tuple, float]) -> dict[str, tuple[str, float]]:
    """{end: (start, value)} for ~3-month periods, derived from quarters, YTD and annual totals."""
    q: dict[str, tuple[str, float]] = {}
    cumulative: dict[str, list[tuple[str, float]]] = {}  # start -> [(end, val)]
    for (start, end), val in values.items():
        if not start:
            continue
        n = _days(start, end)
        if 80 <= n <= 100:
            q[end] = (start, val)
        if 80 <= n <= 380:
            cumulative.setdefault(start, []).append((end, val))
    changed = True
    while changed:  # chain YTD differences: 6M - 3M, 9M - 6M, 12M - 9M
        changed = False
        for start, series in cumulative.items():
            series.sort()
            for (e1, v1), (e2, v2) in zip(series, series[1:]):
                if e2 not in q and 80 <= _days(e1, e2) <= 100:
                    q[e2] = ((_d(e1) + timedelta(days=1)).isoformat(), v2 - v1)
                    changed = True
        # Annual total + three discrete quarters inside it -> the missing fourth
        # (10-Ks usually report only the year; Q4 is never filed on its own).
        for (start, end), val in values.items():
            if not start or not 350 <= _days(start, end) <= 380:
                continue
            inside = sorted(e for e, (s, _) in q.items() if s >= start and e <= end)
            if end in q or len(inside) != 3:
                continue
            tail_start = (_d(inside[-1]) + timedelta(days=1)).isoformat()
            if 80 <= _days(tail_start, end) <= 100:
                q[end] = (tail_start, val - sum(q[e][1] for e in inside))
                changed = True
    return q


def _near(q: dict[str, Any], target: date, tol: int = 7) -> str | None:
    best = None
    for end in q:
        gap = abs((_d(end) - target).days)
        if gap <= tol and (best is None or gap < abs((_d(best) - target).days)):
            best = end
    return best


def ttm(values: dict[tuple, float], end: str) -> float | None:
    """Trailing twelve months ending at *end*: an annual figure if reported, else 4 quarters."""
    for (start, e), val in values.items():
        if start and e == end and 350 <= _days(start, e) <= 380:
            return val
    q = discrete_quarters(values)
    total, cursor = 0.0, _d(end)
    for _ in range(4):
        hit = _near(q, cursor)
        if hit is None:
            return None
        start, val = q[hit]
        total += val
        cursor = _d(start) - timedelta(days=1)
    return total


def instant(values: dict[tuple, float], end: str, tol: int = 7) -> float | None:
    best, gap = None, tol + 1
    for (start, e), val in values.items():
        if start:
            continue
        g = abs((_d(e) - _d(end)).days)
        if g < gap:
            best, gap = val, g
    return best


def latest_instant(values: dict[tuple, float], on_or_before: str) -> float | None:
    pts = sorted((e, v) for (s, e), v in values.items() if not s and e <= on_or_before)
    return pts[-1][1] if pts else None


def _div(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b == 0:
        return None
    return a / b


def _growth(now: float | None, before: float | None) -> float | None:
    if now is None or before is None or before <= 0:
        return None
    return now / before - 1.0


# ---------------------------------------------------------------------------
# Public builders
# ---------------------------------------------------------------------------

def metrics_rows(sec: SEC, ticker: str, end_date: str, limit: int, price_on) -> list[FinancialMetrics]:
    """One TTM row per 10-Q/10-K filed on or before end_date, newest first.
    `price_on(day) -> float | None` supplies the close used for valuation ratios."""
    cik = sec.cik(ticker)
    if cik is None:
        return []
    facts = sec.facts(cik)
    rows: list[FinancialMetrics] = []
    seen_periods: set[str] = set()
    for filing in sec.filings(cik):
        if filing["form"] not in PERIODIC_FORMS or not filing["reportDate"]:
            continue
        filed, period = filing["filingDate"], filing["reportDate"]
        if filed > end_date[:10] or period in seen_periods:
            continue
        seen_periods.add(period)
        row = _metrics_at(facts, ticker, period, filed, price_on)
        if row is not None:
            rows.append(row)
        if len(rows) >= limit:
            break
    return rows


def _metrics_at(facts: FactIndex, ticker: str, period: str, filed: str, price_on) -> FinancialMetrics | None:
    v = {k: facts.asof(names, filed) for k, names in {**FLOW, **INSTANT}.items()}
    shares_v = facts.asof(["EntityCommonStockSharesOutstanding"], filed)

    rev = ttm(v["revenue"], period)
    ni = ttm(v["net_income"], period)
    if rev is None and ni is None:
        return None
    cogs = ttm(v["cost_of_revenue"], period)
    gp = ttm(v["gross_profit"], period)
    if gp is None and rev is not None and cogs is not None:
        gp = rev - cogs
    op = ttm(v["operating_income"], period)
    eps = ttm(v["eps"], period)
    ocf, capex = ttm(v["ocf"], period), ttm(v["capex"], period)
    fcf = ocf - capex if ocf is not None and capex is not None else None
    divs = ttm(v["dividends"], period)

    equity, assets = instant(v["equity"], period), instant(v["assets"], period)
    liabilities = instant(v["liabilities"], period)
    ca, cl = instant(v["current_assets"], period), instant(v["current_liabilities"], period)
    lt, st = instant(v["lt_debt"], period), instant(v["st_debt"], period)
    debt = (lt or 0.0) + (st or 0.0) if (lt is not None or st is not None) else None
    cash = instant(v["cash"], period)
    shares = latest_instant(shares_v, filed)

    year_ago = (_d(period) - timedelta(days=364)).isoformat()
    rev_prev, ni_prev = ttm(v["revenue"], year_ago), ttm(v["net_income"], year_ago)
    eps_prev, op_prev = ttm(v["eps"], year_ago), ttm(v["operating_income"], year_ago)
    eq_prev = instant(v["equity"], year_ago, tol=10)
    ocf_prev, capex_prev = ttm(v["ocf"], year_ago), ttm(v["capex"], year_ago)
    fcf_prev = ocf_prev - capex_prev if ocf_prev is not None and capex_prev is not None else None

    price = price_on(filed)
    mcap = price * shares if price is not None and shares else None
    ev = mcap + (debt or 0.0) - (cash or 0.0) if mcap is not None else None
    return FinancialMetrics(
        ticker=ticker, report_period=period, period="ttm", currency="USD", filing_date=filed,
        market_cap=mcap, enterprise_value=ev,
        price_to_earnings_ratio=_div(price, eps) if eps and eps > 0 else None,
        price_to_book_ratio=_div(mcap, equity) if equity and equity > 0 else None,
        price_to_sales_ratio=_div(mcap, rev),
        free_cash_flow_yield=_div(fcf, mcap),
        gross_margin=_div(gp, rev), operating_margin=_div(op, rev), net_margin=_div(ni, rev),
        return_on_equity=_div(ni, equity) if equity and equity > 0 else None,
        return_on_assets=_div(ni, assets),
        current_ratio=_div(ca, cl),
        debt_to_equity=_div(debt, equity) if equity and equity > 0 else None,
        debt_to_assets=_div(debt, assets),
        revenue_growth=_growth(rev, rev_prev), earnings_growth=_growth(ni, ni_prev),
        book_value_growth=_growth(equity, eq_prev), earnings_per_share_growth=_growth(eps, eps_prev),
        free_cash_flow_growth=_growth(fcf, fcf_prev), operating_income_growth=_growth(op, op_prev),
        payout_ratio=_div(abs(divs), ni) if divs is not None and ni and ni > 0 else None,
        earnings_per_share=eps,
        book_value_per_share=_div(equity, shares),
        free_cash_flow_per_share=_div(fcf, shares),
    )


def shares_outstanding(sec: SEC, ticker: str, on_or_before: str) -> float | None:
    cik = sec.cik(ticker)
    if cik is None:
        return None
    return latest_instant(sec.facts(cik).asof(["EntityCommonStockSharesOutstanding"], on_or_before),
                          on_or_before)


def company_facts(sec: SEC, ticker: str) -> CompanyFacts | None:
    cik = sec.cik(ticker)
    if cik is None:
        return None
    p = sec.profile(cik)
    sic = int(p["sic"]) if p.get("sic") and str(p["sic"]).isdigit() else None
    sector = next((name for lo, hi, name in SIC_DIVISIONS if sic and lo <= sic <= hi), None)
    return CompanyFacts(ticker=ticker, name=p.get("name"), cik=str(cik), sector=sector,
                        industry=p.get("sicDescription"), sic_code=str(sic) if sic else None,
                        sic_industry=p.get("sicDescription"), sic_sector=sector,
                        exchange=(p.get("exchanges") or [None])[0])


def _accepted(filing: dict) -> tuple[str, str | None]:
    """(New York date, ISO datetime) the filing became public."""
    acc = filing.get("acceptanceDateTime")
    if acc:
        moment = datetime.fromisoformat(acc.replace("Z", "+00:00")).astimezone(NEW_YORK)
        return moment.date().isoformat(), moment.isoformat()
    return filing["filingDate"], None


def _surprise(now: float | None, before: float | None) -> str | None:
    if now is None or before is None:
        return None
    if abs(now - before) < 1e-9:
        return "MEET"
    return "BEAT" if now > before else "MISS"


def earnings_history(sec: SEC, ticker: str, limit: int) -> list[EarningsRecord]:
    """Earnings releases (8-K item 2.02), newest first, stamped at SEC acceptance.

    Actuals come from XBRL (the same figures the press release reported, tagged
    in the following 10-Q/10-K). Surprise is a PROXY: versus the same quarter
    a year earlier (seasonal random walk) — there is no free consensus.
    """
    cik = sec.cik(ticker)
    if cik is None:
        return []
    facts = sec.facts(cik)
    today = date.today().isoformat()
    eps_q = discrete_quarters(facts.asof(FLOW["eps"], today))
    rev_q = discrete_quarters(facts.asof(FLOW["revenue"], today))
    out: list[EarningsRecord] = []
    for filing in sec.filings(cik):
        if filing["form"] != "8-K" or "2.02" not in (filing.get("items") or ""):
            continue
        day, moment = _accepted(filing)
        ended = [e for e in eps_q if e < day and (_d(day) - _d(e)).days <= 100]
        if not ended:
            continue
        period = max(ended)
        prior = _near(eps_q, _d(period) - timedelta(days=364))
        rev_now = rev_q.get(_near(rev_q, _d(period)) or "", (None, None))[1]
        rev_prior_end = _near(rev_q, _d(period) - timedelta(days=364))
        rev_prior = rev_q[rev_prior_end][1] if rev_prior_end else None
        eps_now, eps_prior = eps_q[period][1], (eps_q[prior][1] if prior else None)
        out.append(EarningsRecord(
            ticker=ticker, report_period=period, source_type="8-K", filing_date=day,
            filing_datetime=moment, accession_number=filing["accessionNumber"], currency="USD",
            quarterly=EarningsData(earnings_per_share=eps_now, eps_surprise=_surprise(eps_now, eps_prior),
                                   revenue=rev_now, revenue_surprise=_surprise(rev_now, rev_prior)),
        ))
        if len(out) >= limit:
            break
    return out


def _text(node: ET.Element | None, path: str) -> str | None:
    if node is None:
        return None
    hit = node.find(path)
    if hit is None:
        return None
    val = hit.findtext("value") if hit.find("value") is not None else hit.text
    return val.strip() if val else None


def _num(s: str | None) -> float | None:
    try:
        return float(s) if s not in (None, "") else None
    except ValueError:
        return None


def parse_form4(xml_text: str, ticker: str, filing_date: str) -> list[InsiderTrade]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    owner = root.find("reportingOwner")
    name = _text(owner, "reportingOwnerId/rptOwnerName") or "unknown"
    rel = owner.find("reportingOwnerRelationship") if owner is not None else None
    is_director = (_text(rel, "isDirector") or "0").lower() in ("1", "true")
    title = _text(rel, "officerTitle") or ("Director" if is_director else None)
    issuer = _text(root, "issuer/issuerName")
    trades = []
    for tx in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        code = _text(tx, "transactionCoding/transactionCode")
        shares = _num(_text(tx, "transactionAmounts/transactionShares"))
        price = _num(_text(tx, "transactionAmounts/transactionPricePerShare"))
        direction = _text(tx, "transactionAmounts/transactionAcquiredDisposedCode")
        after = _num(_text(tx, "postTransactionAmounts/sharesOwnedFollowingTransaction"))
        signed = -shares if shares is not None and direction == "D" else shares
        trades.append(InsiderTrade(
            ticker=ticker, name=name, filing_date=filing_date, is_board_director=is_director,
            issuer=issuer, title=title, transaction_date=_text(tx, "transactionDate"),
            transaction_type=TX_CODES.get(code or "", code), transaction_shares=signed,
            transaction_price_per_share=price,
            transaction_value=(signed * price) if signed is not None and price is not None else None,
            shares_owned_after_transaction=after,
            shares_owned_before_transaction=(after - signed) if after is not None and signed is not None else None,
            security_title=_text(tx, "securityTitle"),
        ))
    return trades


def insider_trades(sec: SEC, ticker: str, end_date: str, start_date: str | None, limit: int) -> list[InsiderTrade]:
    """Form 4 open-market and other transactions, by filing date, newest first."""
    cik = sec.cik(ticker)
    if cik is None:
        return []
    out: list[InsiderTrade] = []
    for filing in sec.filings(cik):
        if filing["form"] not in ("4", "4/A"):
            continue
        filed = filing["filingDate"]
        if filed > end_date[:10]:
            continue
        if start_date and filed < start_date[:10]:
            break
        doc = (filing.get("primaryDocument") or "").split("/")[-1]  # strip the xsl render prefix
        if not doc.endswith(".xml"):
            continue
        xml_text = sec.document(cik, filing["accessionNumber"], doc)
        if xml_text:
            out.extend(parse_form4(xml_text, ticker, filed))
        if len(out) >= limit:
            break
    return out[:limit]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


__all__ = ["SEC", "company_facts", "earnings_history", "insider_trades", "metrics_rows",
           "parse_form4", "shares_outstanding", "ttm", "discrete_quarters"]


def iter_tickers(sec: SEC) -> Iterable[str]:
    """Every ticker SEC maps to a CIK (current listings only)."""
    return sec._ticker_map().keys()
