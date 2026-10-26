"""Lab tests: stats, registry, vault, research/holdout flow, MCP surface.

Fake data + fake analysts drive the REAL backtest engine, so the flow is
exercised end to end without network or LLM calls.
"""

from __future__ import annotations

import asyncio
import json

import numpy as np
import pytest

from hedge_fund.data.models import CompanyNews, EarningsRecord, Price
from hedge_fund.lab import runner, stats, tools
from hedge_fund.lab.config import LabConfig
from hedge_fund.lab.registry import Registry, RegistryError
from hedge_fund.lab.runner import Lab
from hedge_fund.lab.vault import VaultedDataClient, VaultViolation
from hedge_fund.models import Signal

# Three trading weeks of June 2024. Holdout starts 2024-06-15, so research
# sees weeks 1–2 and the holdout is week 3.
DAYS = ["2024-06-03", "2024-06-04", "2024-06-05", "2024-06-06", "2024-06-07",
        "2024-06-10", "2024-06-11", "2024-06-12", "2024-06-13", "2024-06-14",
        "2024-06-17", "2024-06-18", "2024-06-19", "2024-06-20", "2024-06-21"]
SERIES = {
    "SPY": {d: 100.0 + i for i, d in enumerate(DAYS)},
    "AAA": {d: 50.0 * (1.01 ** i) for i, d in enumerate(DAYS)},
    "BBB": {d: 80.0 * (0.995 ** i) for i, d in enumerate(DAYS)},
    "ZZZ": {d: 10.0 for d in DAYS},
}


class FakeData:
    def __init__(self, series=SERIES):
        self.series = series
        self.calls = []

    def get_prices(self, ticker, start_date, end_date, **kwargs):
        self.calls.append(("prices", ticker, end_date))
        return [Price(open=c, close=c, high=c, low=c, volume=1000, time=f"{d}T00:00:00Z")
                for d, c in sorted(self.series.get(ticker, {}).items()) if start_date <= d <= end_date]

    def get_news(self, ticker, end_date, start_date=None, limit=1000):
        return [CompanyNews(ticker=ticker, title="old", source="x", date="2024-06-10"),
                CompanyNews(ticker=ticker, title="future", source="x", date="2024-06-20")]

    def get_earnings_history(self, ticker, limit=12):
        rows = [EarningsRecord(ticker=ticker, report_period=f"2024-0{m}-01", source_type="8-K",
                               filing_date=f"2024-0{m}-20") for m in range(1, 8)]
        return list(reversed(rows))[:limit]  # newest first, like the API

    def get_company_facts(self, ticker):
        return None


class FakeAnalyst:
    investment_approach = "long_short"

    def __init__(self, **_):
        pass

    @property
    def name(self):
        return "fake"

    def predict(self, ticker, date, data_client):
        data_client.get_prices(ticker, "2024-06-01", date)  # touches the vault like a real model
        return Signal(model_name="fake", ticker=ticker, date=date, value={"AAA": 1.0, "BBB": -0.5}.get(ticker, 0.0))


class PeekingAnalyst(FakeAnalyst):
    investment_approach = "long_short"  # aihf requires it declared on the class itself

    def predict(self, ticker, date, data_client):
        data_client.get_prices(ticker, "2024-06-01", "2024-06-21")  # reaches into the holdout
        return Signal(model_name="fake", ticker=ticker, date=date, value=0.0)


MANDATE = """
schema_version: 2
name: test-fund
strategies:
  - name: solo
    blend: {mode: long_short}
    models: [{name: fake}]
risk: {max_position_pct: 1.0, max_gross_exposure: 1.0}
costs: {spread_bps: 0, commission_per_share: 0}
capital: 100000
rebalance: daily
benchmark: SPY
"""


@pytest.fixture(autouse=True)
def registered(monkeypatch):
    from hedge_fund.signals import ALPHA_MODEL_REGISTRY
    monkeypatch.setitem(ALPHA_MODEL_REGISTRY, "fake", FakeAnalyst)


@pytest.fixture
def lab(tmp_path):
    return Lab.init(LabConfig(holdout_start="2024-06-15", embargoed_tickers=["ZZZ"]), tmp_path)


def _bt(lab, mandate=MANDATE, universe="AAA,BBB", **kw):
    return runner.research_backtest(lab, mandate, universe, kw.pop("start", "2024-06-03"),
                                    kw.pop("end", None), data_client=FakeData(), requested_by="test", **kw)


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------

def test_deflated_sharpe_falls_as_trials_rise():
    rng = np.random.default_rng(0)
    rets = list(rng.normal(0.001, 0.01, 500))
    one = stats.deflated_sharpe(rets, 1, 0.0)
    many = stats.deflated_sharpe(rets, 1000, 0.05 ** 2)
    assert one["dsr"] == pytest.approx(one["psr"])  # no trials to deflate by
    assert many["dsr"] < one["dsr"]
    assert many["sr0"] > 0


def test_expected_max_sharpe_matches_monte_carlo():
    # The closed form approximates E[max of N iid N(0, V)]; check it against simulation.
    rng = np.random.default_rng(1)
    for n in (10, 100, 1000):
        simulated = rng.normal(0, 0.5, size=(4000, n)).max(axis=1).mean()
        assert stats.expected_max_sharpe(n, 0.25) == pytest.approx(simulated, rel=0.03)


def test_sharpe_undefined_on_flat_series():
    assert stats.sharpe([0.0, 0.0, 0.0]) is None
    assert stats.deflated_sharpe([0.0, 0.0], 5, 0.1)["dsr"] is None


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------

def test_registry_refuses_writes_before_genesis(tmp_path):
    with pytest.raises(RegistryError):
        Registry(tmp_path).append("trial", {})


def test_registry_detects_tampering(lab):
    _bt(lab)
    assert lab.registry.verify() == 2
    path = lab.registry.path
    lines = path.read_text().splitlines()
    rec = json.loads(lines[1])
    rec["payload"]["metrics"]["sharpe_ratio"] = 99.0
    lines[1] = json.dumps(rec)
    path.write_text("\n".join(lines) + "\n")
    with pytest.raises(RegistryError, match="edited"):
        lab.registry.verify()


def test_registry_detects_deleted_record(lab):
    _bt(lab)
    _bt(lab)
    lines = lab.registry.path.read_text().splitlines()
    lab.registry.path.write_text("\n".join([lines[0], lines[2]]) + "\n")
    with pytest.raises(RegistryError):
        lab.registry.verify()


def test_config_edit_outside_cli_is_flagged(lab):
    assert lab.config_drift() is None
    (lab.root / "lab.yaml").write_text((lab.root / "lab.yaml").read_text().replace("2024-06-15", "2024-06-21"))
    assert "edited" in Lab(lab.root).config_drift()


# ---------------------------------------------------------------------------
# vault
# ---------------------------------------------------------------------------

def test_vault_refuses_future_and_embargo():
    v = VaultedDataClient(FakeData(), research_end="2024-06-14", embargoed=["ZZZ"])
    assert len(v.get_prices("AAA", "2024-06-01", "2024-06-14")) == 10
    with pytest.raises(VaultViolation, match="past the research cutoff"):
        v.get_prices("AAA", "2024-06-01", "2024-06-17")
    with pytest.raises(VaultViolation, match="embargoed"):
        v.get_prices("ZZZ", "2024-06-01", "2024-06-10")
    with pytest.raises(VaultViolation):
        v.get_snapshot("AAA")
    with pytest.raises(VaultViolation):
        v.get_earnings("AAA")
    assert len(v.violations) == 4


def test_vault_drops_late_records_from_undated_endpoints():
    v = VaultedDataClient(FakeData(), research_end="2024-06-14")
    assert [n.title for n in v.get_news("AAA", "2024-06-14")] == ["old"]
    hist = v.get_earnings_history("AAA", limit=3)
    assert [r.filing_date for r in hist] == ["2024-05-20", "2024-04-20", "2024-03-20"]


# ---------------------------------------------------------------------------
# research + holdout flow
# ---------------------------------------------------------------------------

def test_research_backtest_runs_and_registers(lab):
    out = _bt(lab, hypothesis="AAA trends", family="trend")
    assert out["status"] == "ok", out
    assert out["end"] <= lab.config.research_end
    assert out["trial_id"] == "T000001"
    assert out["deflated"]["n_trials"] == 1
    assert "returns" not in out  # agents never get the raw series
    rows = runner.list_trials(lab)
    assert rows[0]["family"] == "trend" and rows[0]["requested_by"] == "test"


def test_renaming_a_mandate_keeps_the_candidate(lab):
    a = _bt(lab)
    b = _bt(lab, mandate=MANDATE.replace("test-fund", "shiny-new-name"))
    assert a["candidate_id"] == b["candidate_id"]
    c = _bt(lab, mandate=MANDATE.replace("max_position_pct: 1.0", "max_position_pct: 0.5"))
    assert c["candidate_id"] != a["candidate_id"]


def test_asking_past_cutoff_is_refused_and_logged_but_not_counted(lab):
    with pytest.raises(VaultViolation):
        _bt(lab, end="2024-06-21")
    with pytest.raises(VaultViolation):
        _bt(lab, universe="AAA,ZZZ")
    statuses = [t["status"] for t in lab.registry.trials()]
    assert statuses == ["rejected", "rejected"]
    assert lab.registry.trial_universe()[0] == 0


def test_model_that_peeks_is_caught_and_counted(lab, monkeypatch):
    from hedge_fund.signals import ALPHA_MODEL_REGISTRY
    monkeypatch.setitem(ALPHA_MODEL_REGISTRY, "fake", PeekingAnalyst)
    out = _bt(lab)
    assert out["status"] == "vault_violation"
    assert lab.registry.trial_universe()[0] == 1


def test_holdout_is_one_shot_and_human_gated(lab):
    cid = _bt(lab)["candidate_id"]
    req = runner.request_holdout(lab, cid, "AAA,BBB", "beats SPY in research", requested_by="director")
    with pytest.raises(RegistryError, match="holdout looks"):
        runner.request_holdout(lab, cid, "AAA,BBB", "again please")
    res = runner.decide_holdout(lab, req["request_id"], approve=True, approver="jai",
                                data_client=FakeData(), end="2024-06-21")
    assert res["status"] == "evaluated", res
    assert res["metrics"]["n_orders"] >= 0
    with pytest.raises(RegistryError, match="already"):
        runner.decide_holdout(lab, req["request_id"], approve=True, approver="jai", data_client=FakeData())
    report = runner.candidate_report(lab, cid)
    assert report["holdout_looks_left"] == 0
    assert report["holdouts"][0]["status"] == "evaluated"
    assert lab.registry.verify() == 5  # genesis, trial, request, decision, result


def test_rejected_holdout_saw_nothing_so_keeps_the_look(lab):
    cid = _bt(lab)["candidate_id"]
    req = runner.request_holdout(lab, cid, "AAA", "try it")
    assert runner.candidate_report(lab, cid)["holdout_looks_left"] == 0  # pending blocks a second request
    runner.decide_holdout(lab, req["request_id"], approve=False, approver="jai", note="not convinced")
    assert runner.candidate_report(lab, cid)["holdout_looks_left"] == 1


def test_holdout_requires_a_successful_trial(lab):
    with pytest.raises(ValueError, match="no successful"):
        runner.request_holdout(lab, "Cdeadbeef0000", "AAA", "why not")


# ---------------------------------------------------------------------------
# agent tools + MCP server
# ---------------------------------------------------------------------------

def test_tools_return_errors_not_exceptions(lab):
    tools.configure(lab.root, FakeData())
    try:
        assert tools.lab_status()["research_end"] == "2024-06-14"
        assert "fake" in {a["name"] for a in tools.list_analysts()["analysts"]}
        assert "fundamental-ls" in tools.list_strategies()["strategies"]
        assert tools.validate_mandate(MANDATE)["ok"] is True
        assert "error" in tools.validate_mandate("schema_version: 2\nname: x\n")
        assert "error" in tools.run_backtest(MANDATE, "AAA", end="2024-06-30")
        ok = tools.run_backtest(MANDATE, "AAA,BBB", start="2024-06-03")
        assert ok["status"] == "ok"
        assert tools.get_trial(ok["trial_id"])["trial_id"] == ok["trial_id"]
        assert tools.candidate_report(ok["candidate_id"])["n_ok"] == 1
        assert tools.request_holdout(ok["candidate_id"], "AAA", "test")["status"] == "pending"
        assert tools.list_holdouts()["holdouts"][0]["status"] == "pending"
    finally:
        tools.configure(None, None)


def test_cli_refuses_approval_from_an_agent_shell(monkeypatch, capsys):
    from hedge_fund.lab import cli

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_abc")
    monkeypatch.setattr("sys.argv", ["aihf-lab", "holdout", "approve", "H000002", "--approver", "quant"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    assert "human-only" in capsys.readouterr().err


def test_mcp_server_exposes_every_tool_and_no_approvals():
    from hedge_fund.lab.mcp_server import build_server

    server = build_server()
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert names == {fn.__name__ for fn in tools.TOOLS}
    assert not any("approve" in n or "tick" in n or "halt" in n for n in names)
