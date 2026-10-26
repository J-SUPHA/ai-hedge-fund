"""Research backtests and holdout evaluations — the only two ways the lab runs a fund.

    research_backtest   any agent, any time. Data goes through the vault
                        (nothing after the cutoff, nothing embargoed). Every
                        call is registered — success, failure, or a refused
                        attempt to peek past the cutoff.

    request_holdout     an agent asks for a candidate's one look at the
                        holdout. Nothing runs yet.

    decide_holdout      a HUMAN approves or rejects (CLI only; not exposed
                        over MCP). Approval runs the mandate once over the
                        holdout window and records the result, final.

A *candidate* is a mandate's content hash (name excluded), so renaming a
mandate cannot buy it a second holdout look.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from contextlib import ExitStack
from datetime import date as _date
from datetime import timedelta
from pathlib import Path
from typing import Any

import yaml

from hedge_fund.backtesting import backtest_fund
from hedge_fund.data.protocol import DataClient
from hedge_fund.data.sessions import completed_through
from hedge_fund.fund import Fund, FundSpec, normalize_universe
from hedge_fund.lab.config import LabConfig, lab_dir, load_config, save_config
from hedge_fund.lab.registry import Registry, RegistryError
from hedge_fund.lab.stats import deflated_sharpe, moments, returns_from_nav, sharpe
from hedge_fund.lab.vault import VaultedDataClient, VaultViolation
from hedge_fund.paths import write_atomic
from hedge_fund.scorecard import score

DEFAULT_WEEKS = 78  # matches aihf's default backtest window
RESULTS_DIR = "results"


# ---------------------------------------------------------------------------
# The lab handle
# ---------------------------------------------------------------------------

class Lab:
    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root else lab_dir()
        self.config: LabConfig = load_config(self.root)
        self.registry = Registry(self.root)
        if not self.registry.exists():
            raise RegistryError(f"no registry at {self.registry.path} — run `aihf-lab init`")

    @property
    def results_dir(self) -> Path:
        return self.root / RESULTS_DIR

    def config_drift(self) -> str | None:
        """A message if lab.yaml no longer matches what the registry recorded."""
        recorded = self.registry.genesis_config_hash()
        current = self.config.config_hash()
        if recorded != current:
            return (f"lab.yaml hash {current} != registry {recorded}: the config was edited "
                    "outside `aihf-lab init --force`")
        return None

    @staticmethod
    def init(config: LabConfig, root: Path | None = None, *, force: bool = False, actor: str = "human") -> Lab:
        root = Path(root) if root else lab_dir()
        registry = Registry(root)
        if registry.exists():
            if not force:
                raise RegistryError(f"lab already initialised at {root}; --force records a config change")
            save_config(config, root)
            registry.append("config_change", {"config": config.model_dump(),
                                              "config_hash": config.config_hash(), "actor": actor})
        else:
            save_config(config, root)
            registry.append("genesis", {"config": config.model_dump(),
                                        "config_hash": config.config_hash(), "actor": actor})
        (root / RESULTS_DIR).mkdir(parents=True, exist_ok=True)
        return Lab(root)


# ---------------------------------------------------------------------------
# Mandates and candidates
# ---------------------------------------------------------------------------

def parse_mandate(mandate: str | dict | FundSpec) -> FundSpec:
    """A FundSpec from a spec, a dict, YAML text, or a path to a YAML file."""
    if isinstance(mandate, FundSpec):
        return mandate
    if isinstance(mandate, dict):
        return FundSpec(**mandate)
    text = mandate
    if "\n" not in mandate and mandate.strip().endswith((".yaml", ".yml")):
        path = Path(mandate).expanduser()
        if not path.exists():
            raise ValueError(f"mandate file not found: {path}")
        text = path.read_text()
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("mandate must be a YAML mapping (see hedge_fund/fund/example.yaml)")
    return FundSpec(**data)


def candidate_id(spec: FundSpec) -> str:
    """Content hash of everything that changes behaviour (the name does not)."""
    body = spec.model_dump(mode="json", exclude={"name"})
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return "C" + hashlib.sha256(canonical.encode()).hexdigest()[:12]


def _git_state() -> dict[str, Any]:
    here = Path(__file__).resolve().parent
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=here, capture_output=True, text=True,
                             timeout=5).stdout.strip() or None
        dirty = bool(subprocess.run(["git", "status", "--porcelain", "--", "hedge_fund"], cwd=here,
                                    capture_output=True, text=True, timeout=5).stdout.strip())
        return {"git_sha": sha, "git_dirty": dirty}
    except (OSError, subprocess.SubprocessError):
        return {"git_sha": None, "git_dirty": None}


def _caller(requested_by: str | None) -> dict[str, str | None]:
    """Who asked: explicit, else the Hermes profile/task in the environment."""
    hermes_home = os.environ.get("HERMES_HOME", "")
    profile = Path(hermes_home).name if "/profiles/" in hermes_home else None
    return {
        "requested_by": requested_by or profile or os.environ.get("USER") or "unknown",
        "kanban_task": os.environ.get("HERMES_KANBAN_TASK"),
    }


def _metrics(result) -> dict:
    m = result.metrics
    return {
        "total_return_pct": m.total_return_pct,
        "annualized_return_pct": m.annualized_return_pct,
        "sharpe_ratio": m.sharpe_ratio,
        "max_drawdown_pct": m.max_drawdown_pct,
        "benchmark_return_pct": m.benchmark_return_pct,
        "excess_return_pct": m.excess_return_pct,
        "n_cycles": m.n_cycles,
        "n_orders": m.n_orders,
        "traded_notional": m.traded_notional,
        "trading_cost": m.trading_cost,
    }


def _return_stats(nav: list[float], benchmark_nav: list[float]) -> tuple[dict, list[float]]:
    rets = returns_from_nav(nav)
    bench = returns_from_nav(benchmark_nav)
    excess = [r - b for r, b in zip(rets, bench)] if len(bench) == len(rets) else []
    skew, kurt = moments(rets)
    return {"sharpe": sharpe(rets), "excess_sharpe": sharpe(excess) if excess else None,
            "skew": skew, "kurtosis": kurt, "n_periods": len(rets)}, rets


def _scorecard_brief(result) -> dict | None:
    try:
        card = score(result.records)
    except Exception:  # a scorecard is a nicety; never fail a trial over it
        return None
    return {
        "analysts": [a.model_dump() for a in card.analysts],
        "strategies": [s.model_dump() for s in card.strategies],
    }


def _open_data(stack: ExitStack, data_client: DataClient | None) -> DataClient:
    if data_client is not None:
        return data_client
    from hedge_fund.data import CachedDataClient, FDClient
    from hedge_fund.tui.keys import apply_credentials

    apply_credentials()
    raw = stack.enter_context(FDClient())
    return CachedDataClient(raw)


def _save_result(lab: Lab, stem: str, result) -> Path:
    lab.results_dir.mkdir(parents=True, exist_ok=True)
    path = lab.results_dir / f"{stem}.json"
    write_atomic(path, result.model_dump_json())
    return path


# ---------------------------------------------------------------------------
# Research
# ---------------------------------------------------------------------------

def research_backtest(
    lab: Lab,
    mandate: str | dict | FundSpec,
    universe: list[str] | str,
    start: str | None = None,
    end: str | None = None,
    *,
    requested_by: str | None = None,
    hypothesis: str = "",
    family: str = "",
    data_client: DataClient | None = None,
) -> dict:
    """Run one vaulted research backtest and register it. Returns a summary.

    Invalid mandates raise ValueError without a registry entry (nothing ran).
    A request that reaches past the cutoff or into the embargo is registered
    as `rejected` (visible to auditors, not counted as a trial) and raises
    VaultViolation. Anything that ran is registered, success or failure.
    """
    spec = parse_mandate(mandate)
    if isinstance(universe, str):
        universe = universe.replace(",", " ").split()
    universe = normalize_universe(universe)
    cfg = lab.config
    end = end or cfg.research_end
    start = start or (_date.fromisoformat(end) - timedelta(weeks=DEFAULT_WEEKS)).isoformat()
    cid = candidate_id(spec)
    base = {
        "candidate_id": cid, "mandate_name": spec.name, "spec": spec.model_dump(mode="json"),
        "universe": universe, "start": start, "end": end, "hypothesis": hypothesis,
        "family": family or None, "config_hash": cfg.config_hash(), **_caller(requested_by), **_git_state(),
    }
    if spec.benchmark not in cfg.benchmark_whitelist:
        raise ValueError(f"benchmark {spec.benchmark} not in the lab whitelist {cfg.benchmark_whitelist}")
    if start >= end:
        raise ValueError(f"start {start} must be before end {end}")

    problems = []
    if end > cfg.research_end:
        problems.append(f"end {end} is past the research cutoff {cfg.research_end}")
    embargoed = sorted(set(universe) & set(cfg.embargoed_tickers))
    if embargoed:
        problems.append(f"universe includes embargoed tickers {embargoed}")
    if problems:
        lab.registry.append("trial", {**base, "status": "rejected", "error": "; ".join(problems)},
                            id_field="trial_id", id_prefix="T")
        raise VaultViolation("; ".join(problems))

    t0 = time.time()
    with ExitStack() as stack:
        try:
            data = _open_data(stack, data_client)
            vault = VaultedDataClient(data, research_end=cfg.research_end, embargoed=cfg.embargoed_tickers)
            result = backtest_fund(Fund(spec, blind=True), start, end, vault, universe)
        except VaultViolation as exc:
            rec = lab.registry.append("trial", {**base, "status": "vault_violation", "error": str(exc),
                                                "duration_s": round(time.time() - t0, 2)},
                                      id_field="trial_id", id_prefix="T")
            return _summary(lab, rec["payload"])
        except Exception as exc:  # noqa: BLE001 — every failure is a registered attempt
            rec = lab.registry.append("trial", {**base, "status": "error",
                                                "error": f"{type(exc).__name__}: {exc}",
                                                "duration_s": round(time.time() - t0, 2)},
                                      id_field="trial_id", id_prefix="T")
            return _summary(lab, rec["payload"])

    stats, rets = _return_stats(result.nav, result.benchmark_nav)
    stem = f"{cid}-{result.start}-{result.end}-{int(time.time() * 1000)}"
    path = _save_result(lab, stem, result)
    rec = lab.registry.append("trial", {
        **base, "status": "ok", "start": result.start, "end": result.end,
        "metrics": _metrics(result), "stats": stats, "returns": [round(r, 10) for r in rets],
        "scorecard": _scorecard_brief(result), "result_path": str(path),
        "duration_s": round(time.time() - t0, 2),
    }, id_field="trial_id", id_prefix="T")
    return _summary(lab, rec["payload"])


def deflate(lab: Lab, returns: list[float]) -> dict:
    """DSR of a return series against everything the lab has tried so far."""
    n, var = lab.registry.trial_universe()
    return deflated_sharpe(returns, max(n, 1), var)


def _summary(lab: Lab, trial: dict) -> dict:
    """What an agent sees: no raw return series, DSR computed live."""
    out = {k: trial.get(k) for k in (
        "trial_id", "candidate_id", "mandate_name", "status", "error", "universe", "start", "end",
        "family", "hypothesis", "requested_by", "kanban_task", "metrics", "result_path", "duration_s",
        "git_sha", "git_dirty",
    ) if trial.get(k) is not None}
    if trial.get("status") == "ok":
        out["deflated"] = deflate(lab, trial.get("returns") or [])
        out["excess_sharpe_annual"] = (trial["stats"]["excess_sharpe"] * 252 ** 0.5
                                       if trial["stats"].get("excess_sharpe") is not None else None)
        if trial.get("scorecard"):
            out["analysts"] = [
                {k: a.get(k) for k in ("name", "n_views", "hit_rate", "ic_mean", "ic_t_stat")}
                for a in trial["scorecard"]["analysts"]
            ]
    return out


def trial_summary(lab: Lab, trial_id: str) -> dict | None:
    t = lab.registry.trial(trial_id)
    return _summary(lab, t) if t else None


def list_trials(lab: Lab, *, candidate_id: str | None = None, family: str | None = None, limit: int = 20) -> list[dict]:
    trials = lab.registry.trials(candidate_id=candidate_id, family=family)[-limit:]
    rows = []
    for t in reversed(trials):
        m = t.get("metrics") or {}
        rows.append({
            "trial_id": t.get("trial_id"), "candidate_id": t.get("candidate_id"),
            "mandate_name": t.get("mandate_name"), "status": t.get("status"), "family": t.get("family"),
            "requested_by": t.get("requested_by"), "universe": t.get("universe"),
            "window": f"{t.get('start')}..{t.get('end')}", "ts": t.get("ts"),
            "sharpe_ratio": m.get("sharpe_ratio"), "excess_return_pct": m.get("excess_return_pct"),
            "error": t.get("error"),
        })
    return rows


def candidate_report(lab: Lab, cid: str) -> dict:
    trials = lab.registry.trials(candidate_id=cid)
    if not trials:
        raise ValueError(f"no trials for candidate {cid}")
    ok = [t for t in trials if t.get("status") == "ok" and t["stats"].get("sharpe") is not None]
    best = max(ok, key=lambda t: t["stats"]["sharpe"]) if ok else None
    looks = lab.registry.holdout_looks(cid)
    return {
        "candidate_id": cid,
        "mandate_name": trials[-1].get("mandate_name"),
        "spec": trials[-1].get("spec"),
        "n_trials": len(trials),
        "n_ok": len(ok),
        "statuses": {s: sum(1 for t in trials if t.get("status") == s) for s in {t.get("status") for t in trials}},
        "best_trial": _summary(lab, best) if best else None,
        "lab_trials_total": lab.registry.trial_universe()[0],
        "holdout_looks_used": looks,
        "holdout_looks_left": max(lab.config.holdout_budget - looks, 0),
        "holdouts": [h for h in lab.registry.holdout_requests() if h["candidate_id"] == cid],
    }


# ---------------------------------------------------------------------------
# Holdout
# ---------------------------------------------------------------------------

def request_holdout(lab: Lab, cid: str, universe: list[str] | str, rationale: str, *,
                    requested_by: str | None = None) -> dict:
    """Queue a candidate for its one look at the holdout. A human decides."""
    if isinstance(universe, str):
        universe = universe.replace(",", " ").split()
    universe = normalize_universe(universe)
    ok = [t for t in lab.registry.trials(candidate_id=cid) if t.get("status") == "ok"]
    if not ok:
        raise ValueError(f"{cid} has no successful research trial; nothing to evaluate")
    looks = lab.registry.holdout_looks(cid)
    if looks >= lab.config.holdout_budget:
        raise RegistryError(f"{cid} has used {looks}/{lab.config.holdout_budget} holdout looks")
    if not rationale.strip():
        raise ValueError("a rationale is required: why should this candidate spend its holdout look?")
    rec = lab.registry.append("holdout_request", {
        "candidate_id": cid, "spec": ok[-1]["spec"], "mandate_name": ok[-1].get("mandate_name"),
        "universe": universe, "rationale": rationale, "research_trials": [t["trial_id"] for t in ok],
        **_caller(requested_by),
    }, id_field="request_id", id_prefix="H")
    return {"request_id": rec["payload"]["request_id"], "status": "pending",
            "note": "a human must run `aihf-lab holdout approve <id>`"}


def decide_holdout(lab: Lab, request_id: str, *, approve: bool, approver: str, note: str = "",
                   data_client: DataClient | None = None, end: str | None = None) -> dict:
    """Human gate. Approval runs the one-shot holdout evaluation and records it."""
    req = next((h for h in lab.registry.holdout_requests() if h["request_id"] == request_id), None)
    if req is None:
        raise ValueError(f"no holdout request {request_id}")
    if req["status"] != "pending":
        raise RegistryError(f"{request_id} is already {req['status']}")
    lab.registry.append("holdout_decision", {
        "request_id": request_id, "candidate_id": req["candidate_id"],
        "decision": "approved" if approve else "rejected", "approver": approver, "note": note,
    })
    if not approve:
        return {"request_id": request_id, "status": "rejected"}

    cfg = lab.config
    start, end = cfg.holdout_start, end or completed_through()
    spec = FundSpec(**req["spec"])
    base = {"request_id": request_id, "candidate_id": req["candidate_id"], "universe": req["universe"],
            "start": start, "end": end, "config_hash": cfg.config_hash(), **_git_state()}
    t0 = time.time()
    with ExitStack() as stack:
        try:
            data = _open_data(stack, data_client)
            result = backtest_fund(Fund(spec, blind=True), start, end, data, req["universe"])
        except Exception as exc:  # noqa: BLE001 — a failed look is still a spent look
            lab.registry.append("holdout_result", {**base, "status": "error",
                                                   "error": f"{type(exc).__name__}: {exc}"})
            return {"request_id": request_id, "status": "failed", "error": str(exc)}
    stats, rets = _return_stats(result.nav, result.benchmark_nav)
    path = _save_result(lab, f"holdout-{request_id}-{req['candidate_id']}", result)
    deflated = deflate(lab, rets)
    lab.registry.append("holdout_result", {
        **base, "status": "ok", "start": result.start, "end": result.end, "metrics": _metrics(result),
        "stats": stats, "deflated": deflated, "result_path": str(path),
        "duration_s": round(time.time() - t0, 2),
    })
    return {"request_id": request_id, "status": "evaluated", "metrics": _metrics(result),
            "deflated": deflated, "result_path": str(path)}


def init_lab(holdout_start: str | None, embargo: list[str], budget: int, root: Path | None = None,
             *, force: bool = False, actor: str = "human") -> Lab:
    from hedge_fund.lab.config import default_config

    cfg = default_config()
    cfg = LabConfig(**{**cfg.model_dump(),
                       **({"holdout_start": holdout_start} if holdout_start else {}),
                       "embargoed_tickers": embargo, "holdout_budget": budget})
    return Lab.init(cfg, root, force=force, actor=actor)


__all__ = [
    "Lab", "candidate_id", "candidate_report", "decide_holdout", "deflate", "init_lab", "list_trials",
    "parse_mandate", "request_holdout", "research_backtest", "trial_summary", "load_config",
]
