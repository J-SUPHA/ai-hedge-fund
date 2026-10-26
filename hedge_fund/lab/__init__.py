"""The research lab: a test registry, a holdout vault, and an agent-facing API.

aihf is the deterministic engine. The lab is the layer that lets many agents
(or people) run experiments against it without fooling themselves:

    config.py     the lab's frozen settings — where the holdout begins, which
                  tickers are embargoed, how many holdout looks a candidate gets
    vault.py      a DataClient wrapper that refuses anything after the research
                  cutoff or about an embargoed ticker (fail loud, never clamp)
    registry.py   an append-only, hash-chained log of EVERY trial ever run,
                  including failures — the denominator for overfitting stats
    stats.py      Sharpe, probabilistic Sharpe, deflated Sharpe ratio (DSR)
    runner.py     research backtests (vaulted, registered) and the one-shot,
                  human-approved holdout evaluation
    tools.py      the JSON-in/JSON-out surface the MCP server exposes
    mcp_server.py the MCP server (`aihf-lab serve-mcp`)
    cli.py        `aihf-lab` — init, status, verify, trials, holdout approvals

Lab state lives in ~/.hedge-fund/lab/ (override with AIHF_LAB_DIR).
"""

from hedge_fund.lab.config import LabConfig, lab_dir, load_config
from hedge_fund.lab.registry import Registry, RegistryError
from hedge_fund.lab.vault import VaultedDataClient, VaultViolation

__all__ = [
    "LabConfig",
    "Registry",
    "RegistryError",
    "VaultViolation",
    "VaultedDataClient",
    "lab_dir",
    "load_config",
]
