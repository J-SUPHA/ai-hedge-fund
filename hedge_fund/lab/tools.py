"""The agent-facing lab API: plain functions, JSON in, JSON out.

The MCP server registers these verbatim; tests call them directly. Errors
come back as {"error": ...} rather than exceptions so an agent can read and
react to them.

Deliberately NOT here: approving a holdout, ticking/halting a paper fund,
changing the lab config. Those are human/ops actions on the CLI.
"""

from __future__ import annotations

import functools
import traceback
from pathlib import Path
from typing import Any, Callable

import yaml

from hedge_fund.lab import runner
from hedge_fund.lab.runner import Lab

STRATEGIES_DIR = Path(__file__).resolve().parent.parent / "strategies"
EXAMPLE_MANDATE = Path(__file__).resolve().parent.parent / "fund" / "example.yaml"

_lab_root: Path | None = None
_data_client = None  # tests inject a fake; production opens Financial Datasets per call


def configure(root: Path | None = None, data_client=None) -> None:
    global _lab_root, _data_client
    _lab_root, _data_client = root, data_client


def _lab() -> Lab:
    return Lab(_lab_root)


def _safe(fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — agents get the message, not a stack
            return {"error": f"{type(exc).__name__}: {exc}",
                    "where": traceback.extract_tb(exc.__traceback__)[-1].name}
    return wrapper


# ---------------------------------------------------------------------------
# Orientation
# ---------------------------------------------------------------------------

@_safe
def lab_status() -> dict:
    """The lab's rules and counters: research cutoff, embargo, trial count, pending holdouts."""
    lab = _lab()
    n, var = lab.registry.trial_universe()
    holdouts = lab.registry.holdout_requests()
    return {
        "lab_dir": str(lab.root),
        "research_end": lab.config.research_end,
        "holdout_start": lab.config.holdout_start,
        "embargoed_tickers": lab.config.embargoed_tickers,
        "benchmark_whitelist": lab.config.benchmark_whitelist,
        "holdout_budget_per_candidate": lab.config.holdout_budget,
        "trials_counted": n,
        "sharpe_variance_across_trials": var,
        "holdouts_pending": [h["request_id"] for h in holdouts if h["status"] == "pending"],
        "holdouts_evaluated": sum(1 for h in holdouts if h["status"] == "evaluated"),
        "registry_records": lab.registry.verify(),
        "config_drift": lab.config_drift(),
        "rules": [
            "Research data ends at research_end; asking for later data is refused and logged.",
            "Embargoed tickers are refused in research.",
            "Every backtest is registered and raises the bar (DSR) for every other candidate.",
            "Judge results by deflated.dsr (probability true Sharpe > what luck explains), not raw Sharpe.",
            "Each candidate gets holdout_budget looks at the holdout, approved by a human.",
        ],
    }


@_safe
def list_analysts() -> dict:
    """Registered alpha models and whether each may short."""
    from hedge_fund.signals import ALPHA_MODEL_REGISTRY, get_investment_approach
    from hedge_fund.signals.llm_agent import LLMAgent

    return {"analysts": [
        {"name": name, "investment_approach": get_investment_approach(name),
         "kind": "llm_agent" if issubclass(cls, LLMAgent) else "quant",
         "doc": (cls.__doc__ or "").strip().split("\n")[0]}
        for name, cls in sorted(ALPHA_MODEL_REGISTRY.items())
    ]}


@_safe
def list_strategies() -> dict:
    """Built-in strategy pods (YAML) plus the example mandate to copy from."""
    return {
        "strategies": {p.stem: p.read_text() for p in sorted(STRATEGIES_DIR.glob("*.yaml"))},
        "example_mandate": EXAMPLE_MANDATE.read_text(),
    }


@_safe
def validate_mandate(mandate_yaml: str) -> dict:
    """Parse a mandate without running it. Returns its candidate_id (content hash)."""
    spec = runner.parse_mandate(mandate_yaml)
    from hedge_fund.fund import Fund
    Fund(spec)  # constructs every analyst: catches unknown model names and bad params
    return {"ok": True, "candidate_id": runner.candidate_id(spec), "name": spec.name,
            "normalized_yaml": yaml.safe_dump(spec.model_dump(mode="json"), sort_keys=False)}


# ---------------------------------------------------------------------------
# Research
# ---------------------------------------------------------------------------

@_safe
def run_backtest(mandate_yaml: str, universe: str, start: str = "", end: str = "",
                 hypothesis: str = "", family: str = "", requested_by: str = "") -> dict:
    """Run a vaulted research backtest and register it. Costs LLM calls if the mandate uses LLM analysts."""
    return runner.research_backtest(
        _lab(), mandate_yaml, universe, start or None, end or None,
        hypothesis=hypothesis, family=family, requested_by=requested_by or None, data_client=_data_client,
    )


@_safe
def list_trials(candidate_id: str = "", family: str = "", limit: int = 20) -> dict:
    """Most recent registered trials, newest first."""
    return {"trials": runner.list_trials(_lab(), candidate_id=candidate_id or None,
                                         family=family or None, limit=limit)}


@_safe
def get_trial(trial_id: str) -> dict:
    """One trial's summary, with its deflated Sharpe recomputed against today's trial count."""
    found = runner.trial_summary(_lab(), trial_id)
    return found or {"error": f"no trial {trial_id}"}


@_safe
def candidate_report(candidate_id: str) -> dict:
    """Everything about one candidate: all trials, best trial deflated, holdout looks left."""
    return runner.candidate_report(_lab(), candidate_id)


@_safe
def request_holdout(candidate_id: str, universe: str, rationale: str, requested_by: str = "") -> dict:
    """Ask a human to spend this candidate's holdout look. Does not run anything."""
    return runner.request_holdout(_lab(), candidate_id, universe, rationale, requested_by=requested_by or None)


@_safe
def list_holdouts() -> dict:
    """Every holdout request: pending, rejected, evaluated (with results)."""
    rows = []
    for h in _lab().registry.holdout_requests():
        r = h.get("result") or {}
        rows.append({k: v for k, v in {
            "request_id": h["request_id"], "candidate_id": h["candidate_id"],
            "mandate_name": h.get("mandate_name"), "status": h["status"], "universe": h["universe"],
            "requested_by": h.get("requested_by"), "rationale": h.get("rationale"),
            "decision": (h.get("decision") or {}).get("note"),
            "metrics": r.get("metrics"), "deflated": r.get("deflated"), "error": r.get("error"),
        }.items() if v is not None})
    return {"holdouts": rows}


# ---------------------------------------------------------------------------
# Paper funds (read-only)
# ---------------------------------------------------------------------------

@_safe
def paper_funds() -> dict:
    """Every deployed paper fund, one status line each."""
    from hedge_fund.paper import list_deployed
    from hedge_fund.paths import PAPER_DIR
    from hedge_fund.run import _status_line

    return {"funds": [_status_line(d) for d in list_deployed(PAPER_DIR)]}


@_safe
def paper_scorecard(name: str) -> dict:
    """Grade every analyst and strategy on a paper fund's real ledger."""
    from hedge_fund.paper import Ledger, validate_fund_name
    from hedge_fund.paths import PAPER_DIR
    from hedge_fund.scorecard import score

    validate_fund_name(name)
    records = Ledger(PAPER_DIR / name).records()
    if not records:
        return {"fund": name, "note": "no sessions recorded yet"}
    return score(records).model_dump(mode="json")


TOOLS: list[Callable[..., Any]] = [
    lab_status, list_analysts, list_strategies, validate_mandate, run_backtest, list_trials,
    get_trial, candidate_report, request_holdout, list_holdouts, paper_funds, paper_scorecard,
]
