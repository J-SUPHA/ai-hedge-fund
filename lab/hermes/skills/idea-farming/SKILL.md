---
name: idea-farming
description: "Use when farming new trading hypotheses for the aihf lab. Discover, vet, dedupe, record, route."
version: 1.0.0
metadata:
  hermes:
    tags: [aihf-lab, research, ideation, hypotheses, alt-data]
---

# Idea farming

Find investment ideas **yourself**: from the literature, from public data
nobody has wired up yet, from how markets and institutions actually work. The
lab gives you the backtester and the data; your job is the hypotheses.

## Each farming run

1. **Orient.** `mcp__aihf_lab__lab_status` (cutoff, data capabilities, datasets)
   and `mcp__aihf_lab__list_ideas` (everything already proposed, alive or dead).
   Read the dead ones' notes: don't re-propose a corpse, but a *different
   mechanism* on the same data is fair game.
2. **Search widely before choosing.** Use web search and extraction across
   different kinds of source, not one:
   - academic finance (anomaly papers, replication studies, working papers)
   - regulatory and government disclosures and filings (who must report what,
     to whom, with what delay)
   - public government and institutional datasets
   - market-structure mechanics (index rules, calendars, forced flows,
     settlement, lockups, rebalances)
   - alternative data that is scrapeable for free
   - practitioner writing and post-mortems of strategies that stopped working
   Note each candidate's source URL.
3. **Vet each candidate** before proposing. Kill it yourself if any answer is bad:
   - **Mechanism**: why does the mispricing exist, and who is on the other
     side? Why hasn't it been arbitraged away (cost, capacity, constraint,
     attention, delay)?
   - **Point-in-time**: when exactly does each data record become PUBLIC?
     (event date vs disclosure date). Data that only exists with hindsight is
     useless.
   - **Testable here**: US equities, daily/weekly/monthly rebalance,
     long-only or long/short, a universe you can name before seeing results.
   - **Data**: is it in `data_capabilities` already? If not, is it free and
     scrapeable with a history going back before the research cutoff?
4. **Record** survivors with `mcp__aihf_lab__propose_idea`. Write: a
   mechanism, a prediction (direction, horizon, universe), kill criteria, a
   family slug, data_needed, and data_sources, including the public-availability
   rule. If it returns `duplicate_of`, move on.
5. **Route** (director only) at most 3 of the best per run, prioritising
   mechanism quality and data readiness:
   - data already available → `[impl]` card to `quant`, then a `[red]` card to
     `redteam` with `parents=[impl]`.
   - data missing but scrapeable → `[data]` card to `quant` first (load skill
     `alt-data-ingest`); the `[impl]` card gets `parents=[data]`.
   - Set the idea to `testing` or `needs_data` with `update_idea`.
6. **Report**: ideas found, ideas killed during vetting (one line why), ideas
   proposed, cards created.

## Rules

- Quality over count: 2 sharp, testable ideas beat 10 vague ones. Every
  backtest raises the bar for all of them (deflated Sharpe).
- Never test anything yourself during farming, and never look at data after
  the research cutoff.
- A famous idea is fine if the mechanism still plausibly holds and the
  lab hasn't tested it. Say in `references` where it comes from.
