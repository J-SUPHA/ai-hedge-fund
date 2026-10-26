"""MCP server exposing the lab to agents: `aihf-lab serve-mcp`.

Stdio by default (Hermes launches it per profile). Works with the mcp SDK
v2 (MCPServer) and v1 (FastMCP).

Wire into Hermes:

    hermes -p quant mcp add aihf_lab --command /path/to/.venv/bin/aihf-lab --args serve-mcp
"""

from __future__ import annotations

from hedge_fund.lab import tools

INSTRUCTIONS = """\
aihf research lab. You can design fund mandates, backtest them on the research
window (data ends at the cutoff in lab_status), and request a one-time holdout
evaluation that a human approves. Every backtest you run is permanently
registered and counted against everyone's results, so test hypotheses, not
random variations. Judge results by `deflated.dsr` (deflated Sharpe ratio:
probability the true Sharpe is above what luck across all lab trials explains),
not raw Sharpe. Start with lab_status, then list_analysts / list_strategies.
"""


def build_server():
    try:
        from mcp.server.mcpserver import MCPServer as Server  # mcp >= 2
    except ImportError:  # pragma: no cover — mcp 1.x
        from mcp.server.fastmcp import FastMCP as Server

    server = Server(name="aihf-lab", instructions=INSTRUCTIONS)
    for fn in tools.TOOLS:
        server.tool(name=fn.__name__, description=(fn.__doc__ or "").strip())(fn)
    return server


def main() -> None:
    build_server().run()


if __name__ == "__main__":
    main()
