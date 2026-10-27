---
name: alt-data-ingest
description: "Use when an aihf lab idea needs data the lab lacks. Scrape it, date it point-in-time, import it, use it."
version: 1.0.0
metadata:
  hermes:
    tags: [aihf-lab, data, scraping, point-in-time]
---

# Alt-data ingest

Turn a public source into a dataset analysts can read without lookahead.

## Procedure

1. **Find the primary source**: official disclosures, bulk downloads,
   archives, APIs. Prefer the primary source over aggregators; if you use an
   aggregator, verify a sample of rows against the primary.
2. **Decide `available_date` per row.** This is the first day the record was
   public, NOT when the event happened. Examples of the distinction: a
   transaction date vs the date the disclosure was filed or posted; a report
   period vs its filing date. If only the event date is known, use a
   conservative public date and write the rule down. If you cannot determine
   it, the dataset is unusable. Say so.
3. **Scrape politely**: cache raw responses under
   `~/.hedge-fund/cache/scrape/<name>/`, throttle (≥1s per request unless the
   site documents a higher rate), identify yourself in the User-Agent, and
   respect robots.txt/terms.
4. **Write rows** (CSV or JSONL) with at least `ticker` (or `*` for
   market-wide), `available_date`, and `event_date` when known, plus whatever
   fields the idea needs. Map names to tickers carefully and drop rows you
   can't map, counting them.
5. **Import** (registered, hashed):
   ```bash
   {{REPO}}/.venv/bin/aihf-lab data import <name> rows.csv \
     --description "…" --source "<url>" --available-rule "<how available_date was set>"
   ```
   Then `mcp__aihf_lab__describe_dataset` to check the research-window rows.
6. **Use it from an analyst** (in your worktree):
   ```python
   rows = data_client.get_events("<name>", ticker, end_date=date)   # only rows public by `date`
   ```
   The vault refuses dates past the research cutoff and embargoed tickers.
7. Complete the `[data]` card with: name, hash, row/ticker counts, date
   range, the availability rule, mapping losses, and known gaps or biases
   (coverage changes, missing years, survivorship).

## Pitfalls

- Re-importing the same name replaces the data and registers a new hash.
  Trials record the hash they used, so changing data after seeing results
  is visible. Fix real errors only, and say why in the card.
- History that starts after the research cutoff can't be tested; check
  coverage before writing code.
