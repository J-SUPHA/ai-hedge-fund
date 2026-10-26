# aihf research lab

This fork adds a research-lab layer on top of virattt's aihf engine. Many agents
(Hermes profiles) can design and test fund mandates without being able to
fool themselves or each other.

```
 isolated Hermes (~/.hermes-lab): director · quant · redteam · ops on board `hedge-fund-lab`
                 │  MCP: aihf_lab (12 tools)            human: aihf-lab CLI
                 ▼                                              ▼
 ┌─────────────────────── hedge_fund/lab ───────────────────────────────┐
 │ runner   research_backtest ─► VaultedDataClient ─► aihf backtest_fund│
 │          request_holdout ─► (human) decide_holdout ─► one-shot eval  │
 │ registry append-only, hash-chained log of EVERY trial (+ failures)   │
 │ stats    Sharpe · PSR · deflated Sharpe (N = all trials in the lab)  │
 └──────────────────────────────────────────────────────────────────────┘
                 state: ~/.hedge-fund/lab/{lab.yaml, registry.jsonl, results/}
```

## Install

```bash
uv venv .venv --python 3.11 && uv pip install --python .venv/bin/python -e '.[lab]' pytest
.venv/bin/aihf-lab init --holdout-start 2024-10-26      # default: two years ago
.venv/bin/python -m pytest hedge_fund/lab -q

# The agents run on an ISOLATED Hermes (clean clone, own HERMES_HOME), never your everyday one:
lab/hermes/install-isolated-hermes.sh                    # → ~/Desktop/hermes-lab/bin/hermes-lab
~/Desktop/hermes-lab/bin/hermes-lab auth add nous --type oauth
HERMES_BIN=~/Desktop/hermes-lab/bin/hermes-lab lab/hermes/setup.sh                  # profiles + MCP + board
HERMES_BIN=~/Desktop/hermes-lab/bin/hermes-lab lab/hermes/setup.sh --install-roles  # after reviewing roles/*.md
```

See `lab/hermes/README.md` for the team, the isolation and upgrades.

You need a Financial Datasets key, plus one LLM key if a mandate uses LLM analysts.
aihf reads both from `~/.hedge-fund/.env` (`FINANCIAL_DATASETS_API_KEY=…`).

## The pieces

**Holdout vault** (`vault.py`). Research data ends the day before `holdout_start`.
The wrapper raises `VaultViolation` on any date-bounded query past the cutoff
or about an embargoed ticker. It drops late-dated rows from endpoints that
have no date bound (news, filings, earnings history). It refuses the
"latest" endpoints (`get_snapshot`, `get_earnings`) outright. A request whose
window reaches past the cutoff is logged as `rejected` and never runs. A
model that peeks mid-backtest is logged as a counted `vault_violation`.

**Test registry** (`registry.py`). Every research run is appended to
`registry.jsonl`: ok, error, vault violation, or rejected. Each record holds
the mandate spec, universe, window, requester, kanban task, git SHA, metrics,
and daily returns, and is hash-chained to the previous record (`aihf-lab
verify`). Concurrent writers take a file lock. If `lab.yaml` is edited
outside `init --force`, it shows up as `config_drift`.

**Candidates.** A candidate is the content hash of a mandate with its name
excluded, so renaming a mandate doesn't buy another holdout look.

**Deflated Sharpe** (`stats.py`). This is Bailey & López de Prado (2014),
with N = every counted trial in the lab and V = the variance of per-period
Sharpe across completed trials. It's computed live, so a candidate's DSR
falls as the lab runs more experiments. That's intended.

**Holdout gate.** An agent calls `request_holdout(candidate, universe,
rationale)`. A human runs `aihf-lab holdout approve H… --approver jai`, which
runs the mandate once over `[holdout_start, latest close]` and records the
result. Each candidate gets `holdout_budget` looks (default 1). A rejection
returns the look, since nothing was seen. The CLI refuses approvals when it
detects a Hermes profile or kanban worker environment.

**MCP tools** (`aihf-lab serve-mcp`): `lab_status`, `list_analysts`,
`list_strategies`, `validate_mandate`, `run_backtest`, `list_trials`,
`get_trial`, `candidate_report`, `request_holdout`, `list_holdouts`,
`paper_funds`, `paper_scorecard`. Some things are deliberately absent:
approvals, paper tick/halt/resume, and config changes.

## Threat model (be honest about it)

The vault is enforced at the data layer that the lab's tools use. These are
the gaps:

- An agent with a shell and the Financial Datasets key, which lives in
  `~/.hedge-fund/.env`, can query the provider directly. It can also read
  `results/holdout-*`. The fix is to run agent profiles on a sandboxed
  terminal backend (Docker/Modal) without that key or the lab directory
  mounted, and to expose the MCP server over HTTP from a separate user or
  host.
- The registry is tamper-evident, not tamper-proof. Anyone who can write the
  file can rewrite the whole chain. Periodically anchor the latest hash
  somewhere external (a git commit or a signed note).
- Trials killed mid-run (SIGKILL) are not recorded.
- LLM analysts may remember the research period from pre-training. aihf's
  blind prompts reduce this, and the holdout should be later than the
  models' training cutoff where possible.
