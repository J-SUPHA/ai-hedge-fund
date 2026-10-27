"""Alt-data store: scraped datasets that analysts can read point-in-time.

Agents find new data (filings, disclosures, government datasets, web
scrapes) and import it here. Every row MUST say when the information became
PUBLIC (`available_date`). That is not the date something happened: a trade
made on the 1st and disclosed on the 30th is available on the 30th. The vault
serves a row only once `available_date` <= the date the analyst asks about,
and never past the research cutoff.

    <lab>/altdata/<name>/events.jsonl   one event per line, sorted by available_date
    <lab>/altdata/<name>/meta.json      description, source, availability rule, hash

Each import is registered (kind "dataset", content hash), and every trial
records which dataset hashes it read, so editing data after seeing a result
is visible in the audit trail.

Analysts read it through the data client handed to `predict`:

    rows = data_client.get_events("my-dataset", ticker, end_date=date)
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from datetime import date
from pathlib import Path
from typing import Iterable

from hedge_fund.lab.registry import Registry
from hedge_fund.paths import write_atomic

ALTDATA_DIR = "altdata"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
MARKET_WIDE = "*"  # ticker for rows that are not about one company


def _root(lab_root: Path) -> Path:
    return Path(lab_root) / ALTDATA_DIR


def _iso(value, field: str, n: int) -> str:
    try:
        return date.fromisoformat(str(value).strip()[:10]).isoformat()
    except ValueError as exc:
        raise ValueError(f"row {n}: {field} {value!r} is not an ISO date") from exc


def normalize_rows(rows: Iterable[dict]) -> list[dict]:
    out = []
    for n, raw in enumerate(rows, 1):
        row = {k: v for k, v in dict(raw).items() if v not in (None, "")}
        if "available_date" not in row:
            raise ValueError(f"row {n}: missing available_date (when did this become public?)")
        row["available_date"] = _iso(row["available_date"], "available_date", n)
        if "event_date" in row:
            row["event_date"] = _iso(row["event_date"], "event_date", n)
            if row["event_date"] > row["available_date"]:
                raise ValueError(f"row {n}: event_date {row['event_date']} is after available_date "
                                 f"{row['available_date']}; nothing is public before it happens")
        row["ticker"] = str(row.get("ticker") or MARKET_WIDE).strip().upper()
        out.append(row)
    out.sort(key=lambda r: (r["available_date"], r["ticker"]))
    return out


def write_dataset(lab_root: Path, registry: Registry, name: str, rows: Iterable[dict], *,
                  description: str, source: str, available_rule: str, created_by: str) -> dict:
    """Import (or replace) a dataset. Returns its meta. Every version is registered."""
    if not NAME_RE.match(name):
        raise ValueError("dataset name must be lowercase letters, digits and dashes (2-64 chars)")
    for label, val in (("description", description), ("source", source), ("available_rule", available_rule)):
        if not val or not val.strip():
            raise ValueError(f"{label} is required")
    rows = normalize_rows(rows)
    if not rows:
        raise ValueError("no rows")
    body = "\n".join(json.dumps(r, sort_keys=True, default=str) for r in rows) + "\n"
    digest = hashlib.sha256(body.encode()).hexdigest()[:16]
    folder = _root(lab_root) / name
    folder.mkdir(parents=True, exist_ok=True)
    write_atomic(folder / "events.jsonl", body)
    meta = {
        "name": name, "description": description, "source": source, "available_rule": available_rule,
        "created_by": created_by, "hash": digest, "rows": len(rows),
        "tickers": len({r["ticker"] for r in rows}),
        "fields": sorted({k for r in rows for k in r}),
        "first_available": rows[0]["available_date"], "last_available": rows[-1]["available_date"],
    }
    write_atomic(folder / "meta.json", json.dumps(meta, indent=2))
    registry.append("dataset", meta)
    _cache.pop(name, None)
    return meta


def import_csv(lab_root: Path, registry: Registry, name: str, path: Path, **meta) -> dict:
    with open(path, newline="") as fh:
        return write_dataset(lab_root, registry, name, csv.DictReader(fh), **meta)


_cache: dict[str, tuple[float, list[dict]]] = {}


def load(lab_root: Path, name: str) -> list[dict]:
    path = _root(lab_root) / name / "events.jsonl"
    if not path.exists():
        raise KeyError(f"no dataset {name!r}; see list_datasets")
    mtime = path.stat().st_mtime
    hit = _cache.get(name)
    if hit and hit[0] == mtime:
        return hit[1]
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    _cache[name] = (mtime, rows)
    return rows


def meta(lab_root: Path, name: str) -> dict:
    path = _root(lab_root) / name / "meta.json"
    if not path.exists():
        raise KeyError(f"no dataset {name!r}")
    return json.loads(path.read_text())


def list_datasets(lab_root: Path) -> list[dict]:
    root = _root(lab_root)
    if not root.exists():
        return []
    return [json.loads(p.read_text()) for p in sorted(root.glob("*/meta.json"))]


def query(rows: list[dict], ticker: str | None, end_date: str, start_date: str | None = None) -> list[dict]:
    end = end_date[:10]
    t = ticker.upper() if ticker else None
    return [r for r in rows
            if r["available_date"] <= end
            and (start_date is None or r["available_date"] >= start_date[:10])
            and (t is None or r["ticker"] in (t, MARKET_WIDE))]


class EventsClient:
    """Adds `get_events` to any DataClient (holdout runs; research uses the vault)."""

    def __init__(self, inner, lab_root: Path) -> None:
        self._inner = inner
        self._lab_root = Path(lab_root)
        self.datasets_used: dict[str, str] = {}

    def get_events(self, name: str, ticker: str | None = None, end_date: str = "",
                   start_date: str | None = None) -> list[dict]:
        if not end_date:
            raise ValueError("get_events needs end_date (the date you are deciding on)")
        self.datasets_used[name] = meta(self._lab_root, name)["hash"]
        return query(load(self._lab_root, name), ticker, end_date, start_date)

    def __getattr__(self, attr):
        return getattr(self._inner, attr)
