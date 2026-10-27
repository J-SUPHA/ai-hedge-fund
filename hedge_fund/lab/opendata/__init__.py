"""Open data: scraped public sources behind aihf's DataClient protocol.

No vendor API key. Every source is free and public, and every record keeps the
date it became PUBLIC, so the holdout vault can enforce point-in-time.

    yahoo.py   daily OHLCV (split-adjusted) from Yahoo's chart endpoint
    sec.py     SEC EDGAR: point-in-time TTM fundamentals from XBRL company
               facts (every fact carries its `filed` date), earnings releases
               (8-K item 2.02 acceptance timestamps), insider trades (Form 4
               XML), company profile (SIC)
    http.py    one throttled, retrying, disk-cached fetcher per host
    client.py  OpenDataClient — the DataClient the lab uses by default

Known limits (also reported by `lab_status`): no historical news source
(ingest scraped news through the alt-data store); earnings "surprise" is
versus the same quarter a year earlier (no free consensus estimates); prices
are split- but not dividend-adjusted; Yahoo lacks most delisted tickers
(survivorship bias — prefer universes fixed in advance).
"""

from hedge_fund.lab.opendata.client import OpenDataClient, OpenDataUnsupported
from hedge_fund.lab.opendata.http import Fetcher, OpenDataError

__all__ = ["Fetcher", "OpenDataClient", "OpenDataError", "OpenDataUnsupported"]
