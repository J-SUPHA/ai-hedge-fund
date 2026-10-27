"""Idea ledger, alt-data store and open-data parsing — offline tests."""

from __future__ import annotations

import json

import pytest

from hedge_fund.lab import altdata, ideas, runner, tools
from hedge_fund.lab.config import LabConfig
from hedge_fund.lab.opendata import sec as secmod
from hedge_fund.lab.opendata import yahoo
from hedge_fund.lab.runner import Lab
from hedge_fund.lab.test_lab import MANDATE, FakeAnalyst, FakeData
from hedge_fund.lab.vault import VaultedDataClient, VaultViolation
from hedge_fund.models import Signal


@pytest.fixture(autouse=True)
def registered(monkeypatch):
    from hedge_fund.signals import ALPHA_MODEL_REGISTRY
    monkeypatch.setitem(ALPHA_MODEL_REGISTRY, "fake", FakeAnalyst)


@pytest.fixture
def lab(tmp_path):
    return Lab.init(LabConfig(holdout_start="2024-06-15", embargoed_tickers=["ZZZ"]), tmp_path)


# ---------------------------------------------------------------------------
# ideas
# ---------------------------------------------------------------------------

IDEA = dict(title="Insider cluster buying predicts drift",
            mechanism="Several insiders buying on the open market within a week signals private optimism; "
                      "retail ignores Form 4 filings so prices adjust slowly",
            prediction="Long names with 3+ insider purchases in 10 days outperform SPY over the next 20 sessions",
            kill_criteria="DSR < 0.5 or excess return <= 0 after doubled costs",
            family="insider-clusters", data_needed="Form 4 purchases", data_sources="SEC EDGAR Form 4")


def test_propose_and_dedupe(lab):
    first = ideas.propose(lab.registry, **IDEA, proposed_by="director")
    assert first["idea_id"].startswith("I")
    same_family = ideas.propose(lab.registry, **{**IDEA, "title": "something else entirely", "mechanism": "x y z"})
    assert same_family["duplicate_of"] == first["idea_id"]
    reworded = ideas.propose(lab.registry, **{**IDEA, "family": "insider-buys-2",
                                               "title": "Insider cluster buying predicts positive drift"})
    assert reworded["duplicate_of"] == first["idea_id"]
    different = ideas.propose(lab.registry, **{**IDEA, "family": "dividend-initiations",
                                                "title": "Dividend initiations",
                                                "mechanism": "Boards initiate payouts when confident in cash flows; "
                                                             "underreaction to the signal"})
    assert "idea_id" in different


def test_idea_requires_falsifiable_fields_and_death_note(lab):
    with pytest.raises(ValueError, match="kill_criteria"):
        ideas.propose(lab.registry, **{**IDEA, "kill_criteria": " "})
    iid = ideas.propose(lab.registry, **IDEA)["idea_id"]
    with pytest.raises(ValueError, match="note"):
        ideas.update(lab.registry, iid, "dead", "")
    ideas.update(lab.registry, iid, "dead", "DSR 0.12 after 4 trials", "redteam")
    row = ideas.listing(lab.registry)[0]
    assert row["status"] == "dead" and row["last_note"].startswith("DSR")


def test_idea_counts_its_family_trials(lab):
    iid = ideas.propose(lab.registry, **{**IDEA, "family": "fake-trend"})["idea_id"]
    runner.research_backtest(lab, MANDATE, "AAA,BBB", "2024-06-03", family="fake-trend", data_client=FakeData())
    row = next(i for i in ideas.listing(lab.registry) if i["idea_id"] == iid)
    assert row["n_trials"] == 1 and row["best_trial"].startswith("T")


# ---------------------------------------------------------------------------
# alt-data store + vault
# ---------------------------------------------------------------------------

ROWS = [
    {"ticker": "AAA", "event_date": "2024-05-01", "available_date": "2024-06-04", "amount": 50000},
    {"ticker": "BBB", "event_date": "2024-06-01", "available_date": "2024-06-10", "amount": 15000},
    {"ticker": "AAA", "event_date": "2024-06-12", "available_date": "2024-06-18", "amount": 99999},  # holdout
    {"ticker": "ZZZ", "event_date": "2024-05-01", "available_date": "2024-06-05", "amount": 1},       # embargoed
]


def _dataset(lab):
    return altdata.write_dataset(lab.root, lab.registry, "disclosures", ROWS, description="test disclosures",
                                 source="https://example.gov", available_rule="disclosure filing date",
                                 created_by="quant")


def test_dataset_rows_must_be_point_in_time(lab):
    with pytest.raises(ValueError, match="available_date"):
        altdata.write_dataset(lab.root, lab.registry, "bad", [{"ticker": "AAA"}], description="d", source="s",
                              available_rule="r", created_by="q")
    with pytest.raises(ValueError, match="after available_date"):
        altdata.write_dataset(lab.root, lab.registry, "bad", [{"ticker": "AAA", "event_date": "2024-02-01",
                                                               "available_date": "2024-01-01"}],
                              description="d", source="s", available_rule="r", created_by="q")


def test_vault_serves_events_point_in_time(lab):
    meta = _dataset(lab)
    v = VaultedDataClient(FakeData(), research_end="2024-06-14", embargoed=["ZZZ"], lab_root=lab.root)
    assert v.get_events("disclosures", "AAA", end_date="2024-06-03") == []
    assert [r["amount"] for r in v.get_events("disclosures", "AAA", end_date="2024-06-14")] == [50000]
    assert {r["ticker"] for r in v.get_events("disclosures", None, end_date="2024-06-14")} == {"AAA", "BBB"}
    with pytest.raises(VaultViolation):
        v.get_events("disclosures", "AAA", end_date="2024-06-20")
    with pytest.raises(VaultViolation):
        v.get_events("disclosures", "ZZZ", end_date="2024-06-14")
    assert v.datasets_used == {"disclosures": meta["hash"]}
    assert any(r["kind"] == "dataset" for r in lab.registry.records())


def test_describe_dataset_hides_the_holdout(lab):
    _dataset(lab)
    tools.configure(lab.root, FakeData())
    try:
        d = tools.describe_dataset("disclosures")
        assert d["research_rows"] == 3  # the 2024-06-18 row is holdout
        assert d["last_available_in_research"] == "2024-06-10"
        assert all(r["amount"] != 99999 and r["ticker"] != "ZZZ" for r in d["sample_rows"])
        assert tools.lab_status()["datasets"] == ["disclosures"]
    finally:
        tools.configure(None, None)


class EventAnalyst:
    """Long a name once a disclosure about it is public."""

    investment_approach = "long_only"

    def __init__(self, **_):
        pass

    @property
    def name(self):
        return "fake"

    def predict(self, ticker, date, data_client):
        rows = data_client.get_events("disclosures", ticker, end_date=date)
        return Signal(model_name="fake", ticker=ticker, date=date, value=1.0 if rows else 0.0)


def test_backtest_reads_alt_data_through_the_engine(lab, monkeypatch):
    from hedge_fund.signals import ALPHA_MODEL_REGISTRY
    monkeypatch.setitem(ALPHA_MODEL_REGISTRY, "fake", EventAnalyst)
    meta = _dataset(lab)
    out = runner.research_backtest(lab, MANDATE.replace("long_short", "long_only"), "AAA,BBB", "2024-06-03",
                                   data_client=FakeData())
    assert out["status"] == "ok", out
    assert out["datasets_used"] == {"disclosures": meta["hash"]}
    assert out["metrics"]["n_orders"] > 0


# ---------------------------------------------------------------------------
# open data parsing (no network)
# ---------------------------------------------------------------------------

def _facts(concept, rows, unit="USD", taxonomy="us-gaap"):
    return {"facts": {taxonomy: {concept: {"units": {unit: rows}}}}}


def test_ttm_from_quarters_and_ytd_cash_flows():
    # Income-statement style: discrete quarters. Cash-flow style: YTD totals.
    rows = [
        {"start": "2023-01-01", "end": "2023-03-31", "val": 10, "filed": "2023-05-01"},
        {"start": "2023-04-01", "end": "2023-06-30", "val": 20, "filed": "2023-08-01"},
        {"start": "2023-07-01", "end": "2023-09-30", "val": 30, "filed": "2023-11-01"},
        {"start": "2023-01-01", "end": "2023-12-31", "val": 100, "filed": "2024-02-01"},
        {"start": "2024-01-01", "end": "2024-03-31", "val": 15, "filed": "2024-05-01"},
    ]
    idx = secmod.FactIndex(_facts("Revenues", rows))
    v = idx.asof(["Revenues"], "2024-05-01")
    q = secmod.discrete_quarters(v)
    assert q["2023-12-31"][1] == pytest.approx(40)  # Q4 = FY - Q1..Q3
    assert secmod.ttm(v, "2024-03-31") == pytest.approx(20 + 30 + 40 + 15)
    assert secmod.ttm(v, "2023-12-31") == pytest.approx(100)  # annual figure used directly

    ytd = [
        {"start": "2023-01-01", "end": "2023-03-31", "val": 5, "filed": "2023-05-01"},
        {"start": "2023-01-01", "end": "2023-06-30", "val": 12, "filed": "2023-08-01"},
        {"start": "2023-01-01", "end": "2023-09-30", "val": 21, "filed": "2023-11-01"},
        {"start": "2023-01-01", "end": "2023-12-31", "val": 32, "filed": "2024-02-01"},
    ]
    q2 = secmod.discrete_quarters(secmod.FactIndex(_facts("X", ytd)).asof(["X"], "2024-12-31"))
    assert [round(q2[e][1]) for e in sorted(q2)] == [5, 7, 9, 11]


def test_facts_are_point_in_time():
    rows = [{"end": "2023-12-31", "val": 100, "filed": "2024-02-01"},
            {"end": "2023-12-31", "val": 80, "filed": "2024-09-01"}]  # restated later
    idx = secmod.FactIndex(_facts("StockholdersEquity", rows))
    assert secmod.instant(idx.asof(["StockholdersEquity"], "2024-03-01"), "2023-12-31") == 100
    assert secmod.instant(idx.asof(["StockholdersEquity"], "2024-12-01"), "2023-12-31") == 80
    assert idx.asof(["StockholdersEquity"], "2024-01-15") == {}


FORM4 = """<?xml version="1.0"?>
<ownershipDocument>
  <issuer><issuerName>Acme Corp</issuerName></issuer>
  <reportingOwner>
    <reportingOwnerId><rptOwnerName>Doe Jane</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship><isDirector>1</isDirector><officerTitle>CEO</officerTitle></reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2024-03-01</value></transactionDate>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>1000</value></transactionShares>
        <transactionPricePerShare><value>12.5</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <postTransactionAmounts><sharesOwnedFollowingTransaction><value>5000</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2024-03-02</value></transactionDate>
      <transactionCoding><transactionCode>S</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>200</value></transactionShares>
        <transactionPricePerShare><value>13</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <postTransactionAmounts><sharesOwnedFollowingTransaction><value>4800</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>"""


def test_parse_form4():
    buy, sell = secmod.parse_form4(FORM4, "ACME", "2024-03-04")
    assert (buy.transaction_type, buy.transaction_shares, buy.transaction_value) == ("Purchase", 1000, 12500)
    assert buy.is_board_director and buy.title == "CEO" and buy.shares_owned_before_transaction == 4000
    assert (sell.transaction_type, sell.transaction_shares) == ("Sale", -200)


class CannedFetcher:
    def __init__(self, body):
        self.body = body

    def get(self, url, *, ttl, kind="json"):
        return self.body


def test_yahoo_bars_are_stamped_by_new_york_session():
    # 2024-06-10 09:30 ET = 13:30 UTC
    body = {"chart": {"result": [{"timestamp": [1718026200, 1718112600],
                                  "indicators": {"quote": [{"open": [1, 2], "high": [1, 2], "low": [1, 2],
                                                            "close": [10.0, None], "volume": [5, 6]}]}}]}}
    bars = yahoo.year_bars(CannedFetcher(body), "AAA", 2024)
    assert [b.time for b in bars] == ["2024-06-10T04:00:00Z"]  # midnight New York; null close dropped
    assert bars[0].close == 10.0


def test_open_data_refuses_what_it_cannot_serve():
    from hedge_fund.lab.opendata import OpenDataClient, OpenDataUnsupported
    c = OpenDataClient(fetcher=CannedFetcher({}))
    with pytest.raises(OpenDataUnsupported):
        c.get_news("AAPL", "2024-01-01")
    with pytest.raises(OpenDataUnsupported):
        c.get_prices("AAPL", "2024-01-01", "2024-01-02", interval="minute")
