"""`aihf-lab` — the human/ops side of the research lab.

    aihf-lab init [--holdout-start 2024-10-01] [--embargo NVDA,TSLA] [--budget 1] [--force]
    aihf-lab status                       rules, counters, chain check (JSON)
    aihf-lab verify                       recompute the registry hash chain
    aihf-lab backtest MANDATE --universe AAPL,MSFT [--start --end --hypothesis --family]
    aihf-lab trials [--candidate C… --family F --limit 20]
    aihf-lab trial T000004
    aihf-lab candidate C…
    aihf-lab holdout list
    aihf-lab holdout approve H000007 --approver jai [--note "..."]
    aihf-lab holdout reject  H000007 --approver jai [--note "..."]
    aihf-lab ideas [--status dead]        the idea ledger
    aihf-lab data list
    aihf-lab data import NAME rows.csv|rows.jsonl --description … --source URL --available-rule "…"
                                          rows need ticker + available_date (when it became PUBLIC)
    aihf-lab serve-mcp                    MCP server over stdio (for Hermes)

Holdout approval is CLI-only on purpose: the agents can ask, a human decides.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from hedge_fund.lab import runner, tools
from hedge_fund.lab.runner import Lab


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="aihf-lab", description="aihf research lab: registry, vault, holdout gate")
    sub = p.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="create the lab (or record a config change with --force)")
    init.add_argument("--holdout-start", help="first holdout day (default: two years ago)")
    init.add_argument("--embargo", default="", help="tickers research may never see, comma separated")
    init.add_argument("--budget", type=int, default=1, help="holdout looks per candidate")
    init.add_argument("--force", action="store_true", help="change the config of an existing lab (logged)")

    sub.add_parser("status")
    sub.add_parser("verify")

    bt = sub.add_parser("backtest", help="registered, vaulted research backtest")
    bt.add_argument("mandate")
    bt.add_argument("--universe", required=True)
    bt.add_argument("--start")
    bt.add_argument("--end")
    bt.add_argument("--hypothesis", default="")
    bt.add_argument("--family", default="")
    bt.add_argument("--model", help="LLM for investor agents (HEDGE_FUND_LLM_MODEL)")

    tr = sub.add_parser("trials")
    tr.add_argument("--candidate")
    tr.add_argument("--family")
    tr.add_argument("--limit", type=int, default=20)

    one = sub.add_parser("trial")
    one.add_argument("trial_id")

    cand = sub.add_parser("candidate")
    cand.add_argument("candidate_id")

    ho = sub.add_parser("holdout")
    ho_sub = ho.add_subparsers(dest="action", required=True)
    ho_sub.add_parser("list")
    for verb in ("approve", "reject"):
        d = ho_sub.add_parser(verb)
        d.add_argument("request_id")
        d.add_argument("--approver", required=True, help="your name — recorded in the registry")
        d.add_argument("--note", default="")

    idea = sub.add_parser("ideas", help="the idea ledger")
    idea.add_argument("--status", default="")
    idea.add_argument("--verbose", action="store_true")

    data = sub.add_parser("data", help="alt-data store")
    data_sub = data.add_subparsers(dest="action", required=True)
    data_sub.add_parser("list")
    imp = data_sub.add_parser("import", help="import a scraped dataset (CSV or JSONL)")
    imp.add_argument("name")
    imp.add_argument("path")
    imp.add_argument("--description", required=True)
    imp.add_argument("--source", required=True, help="where it was scraped from (URL)")
    imp.add_argument("--available-rule", required=True,
                     help="how available_date was set, e.g. 'disclosure filing date, not trade date'")
    imp.add_argument("--created-by", default="")

    sub.add_parser("serve-mcp", help="MCP server over stdio")
    return p


def _inside_agent() -> str | None:
    """A tripwire, not a security boundary: refuse human-only actions when the
    environment says a Hermes profile or kanban worker is driving the shell."""
    if os.environ.get("HERMES_KANBAN_TASK"):
        return f"kanban task {os.environ['HERMES_KANBAN_TASK']}"
    if "/profiles/" in os.environ.get("HERMES_HOME", ""):
        return f"Hermes profile {os.environ['HERMES_HOME']}"
    return None


def main() -> None:
    args = _parser().parse_args()
    agent = _inside_agent()
    if agent and (args.command == "init" or (args.command == "holdout" and args.action != "list")):
        print(f"aihf-lab: refusing a human-only action from inside {agent}. "
              "Ask a human (kanban_block kind=needs_input).", file=sys.stderr)
        sys.exit(2)
    try:
        if args.command == "serve-mcp":
            from hedge_fund.lab.mcp_server import main as serve
            serve()
            return
        if args.command == "init":
            embargo = [t for t in args.embargo.replace(",", " ").split() if t]
            lab = runner.init_lab(args.holdout_start, embargo, args.budget, force=args.force,
                                  actor=os.environ.get("USER", "human"))
            _print({"lab_dir": str(lab.root), "config": lab.config.model_dump(),
                    "research_end": lab.config.research_end})
        elif args.command == "status":
            _print(tools.lab_status())
        elif args.command == "verify":
            _print({"ok": True, "records": Lab().registry.verify()})
        elif args.command == "backtest":
            if args.model:
                os.environ["HEDGE_FUND_LLM_MODEL"] = args.model
            _print(runner.research_backtest(Lab(), args.mandate, args.universe, args.start, args.end,
                                            hypothesis=args.hypothesis, family=args.family))
        elif args.command == "trials":
            _print(runner.list_trials(Lab(), candidate_id=args.candidate, family=args.family, limit=args.limit))
        elif args.command == "trial":
            _print(runner.trial_summary(Lab(), args.trial_id) or {"error": f"no trial {args.trial_id}"})
        elif args.command == "candidate":
            _print(runner.candidate_report(Lab(), args.candidate_id))
        elif args.command == "ideas":
            from hedge_fund.lab import ideas as _ideas
            _print(_ideas.listing(Lab().registry, args.status or None, args.verbose))
        elif args.command == "data":
            from hedge_fund.lab import altdata
            lab = Lab()
            if args.action == "list":
                _print(tools.list_datasets())
            else:
                from pathlib import Path
                path = Path(args.path).expanduser()
                meta = dict(description=args.description, source=args.source,
                            available_rule=args.available_rule,
                            created_by=args.created_by or runner._caller(None)["requested_by"])
                if path.suffix == ".jsonl":
                    rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
                    _print(altdata.write_dataset(lab.root, lab.registry, args.name, rows, **meta))
                else:
                    _print(altdata.import_csv(lab.root, lab.registry, args.name, path, **meta))
        elif args.command == "holdout":
            if args.action == "list":
                _print(tools.list_holdouts())
            else:
                _print(runner.decide_holdout(Lab(), args.request_id, approve=args.action == "approve",
                                             approver=args.approver, note=args.note))
    except Exception as exc:  # noqa: BLE001
        print(f"aihf-lab: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
