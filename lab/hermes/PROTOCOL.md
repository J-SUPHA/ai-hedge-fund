# Research protocol (shared by every lab profile)

The lab exists to find investment edge **without fooling ourselves**. Every rule
below exists because automated research produces false discoveries by default.

## The pipeline (Kanban board `hedge-fund-lab`)

```
[farm] director   farms ideas (skill `idea-farming`) → propose_idea → idea ledger
  │
[hyp]  director   writes a falsifiable hypothesis card for the best ideas
  │
[data] quant      only if the data is missing: scrape + import it point-in-time
  │               (skill `alt-data-ingest`)
[impl] quant      implements / configures it, runs ≤ 5 registered backtests
  │
[red]  redteam    tries to kill it (independent re-runs, robustness, code audit)
  │  pass
[hold] director   request_holdout(candidate_id, …)  → HUMAN approves on the CLI
  │  survives holdout
[ops]  ops        deploys a paper fund (human-approved), ticks it, reports
```

Card titles start with the stage tag: `[farm]`, `[hyp]`, `[data]`, `[impl]`, `[red]`, `[hold]`, `[ops]`.
Every idea lives in the ledger (`propose_idea` / `list_ideas` / `update_idea`):
set it to `testing`, `needs_data`, `alive` or `dead` as it moves, and a dead idea
gets its cause of death in the note.
Every card names its **family** (a short slug for the hypothesis, e.g. `pead-smallcap`).
Pass that slug as `family` and your profile name as `requested_by` on every
`run_backtest` call, so the registry ties trials back to the card.

## Hard rules

1. **Every backtest is permanent.** The registry (`aihf-lab`) logs every run,
   failures included. Each run raises the bar for every candidate in the lab
   (it increases N in the deflated Sharpe). Test hypotheses, not knob-twiddles.
2. **Budget: ≤ 5 research backtests per `[impl]` card** and ≤ 5 per `[red]` card.
   Need more? Block the card and say why.
3. **Judge by `deflated.dsr`, not raw Sharpe.** DSR is the probability the true
   Sharpe beats what luck across all lab trials explains. Promotion bar for a
   holdout request: `dsr ≥ 0.95` on the best trial **and** positive
   `excess_return_pct` **and** a red-team pass.
4. **Never look past the cutoff.** Research data ends at `research_end`
   (`lab_status`). Never query a data provider directly for later data,
   never use an embargoed ticker, never read `~/.hedge-fund/lab/results/holdout-*`.
5. **Agents never approve.** Holdout approvals (`aihf-lab holdout approve`),
   paper deployment, resuming a halted fund and anything touching real money
   are human actions. Ask via `kanban_block(kind="needs_input")`.
6. **No pushes.** Commit to local branches only. Never push, never open PRs,
   unless a human says so on the card.
7. **Write down what failed.** A dead hypothesis is a result. Put the reason in
   the card summary so the director never re-queues it.

## Data

The lab scrapes public sources: no vendor API key, no vendor limits
(`lab_status.data_capabilities` lists exactly what exists):
Yahoo daily prices, SEC EDGAR point-in-time fundamentals, 8-K earnings
releases, Form 4 insider trades, company profiles. There is **no news source**:
scrape it into the alt-data store if an idea needs it. Anything new you scrape
goes in the alt-data store with a per-row `available_date` (when it became
public), and analysts read it with `data_client.get_events(name, ticker, end_date=date)`.

## Tools

- MCP server `aihf_lab` (tools appear as `mcp__aihf_lab__*`): `lab_status`,
  `list_analysts`, `list_strategies`, `validate_mandate`, `run_backtest`,
  `list_trials`, `get_trial`, `candidate_report`, `request_holdout`,
  `list_holdouts`, `paper_funds`, `paper_scorecard`, `propose_idea`, `list_ideas`,
  `update_idea`, `list_datasets`, `describe_dataset`.
- New analyst code lives in a git worktree. The MCP server imports the MAIN
  checkout, so backtest new code from the worktree root with the CLI:
  `<repo>/.venv/bin/python -m hedge_fund.lab.cli backtest mandate.yaml --universe … --family … --hypothesis …`
  It writes to the same registry.
- Mandate format: `hedge_fund/fund/example.yaml`. Analyst interface:
  `hedge_fund/signals/base.py` (`AlphaModel.predict(ticker, date, data_client) -> Signal`).
