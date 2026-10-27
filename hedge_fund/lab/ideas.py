"""The idea ledger: every hypothesis the lab has considered, tested or killed.

Farming ideas at scale has two failure modes: re-proposing the same idea
forever, and quietly forgetting the ones that died. Ideas are registry
records (kind "idea", ids I…), status changes are "idea_update" records, and
proposals that look like an existing idea are refused with a pointer to it.

An idea's `family` slug is what trials are tagged with (`run_backtest(family=…)`),
so the ledger shows how many backtests each idea has consumed.
"""

from __future__ import annotations

import re

from hedge_fund.lab.registry import Registry

STATUSES = ("proposed", "needs_data", "testing", "alive", "dead", "holdout")
FAMILY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,48}$")
DUPLICATE_THRESHOLD = 0.55
_STOP = set("a an the of to in on for and or with by is are be as at from that this its it into than "
            "stocks stock firms companies company returns return".split())


def _tokens(*texts: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", " ".join(texts).lower())
    return {w for w in words if w not in _STOP and len(w) > 2}


def similarity(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def ideas(registry: Registry) -> list[dict]:
    """Current state of every idea, with its update history folded in."""
    by_id: dict[str, dict] = {}
    for r in registry.records():
        p = r["payload"]
        if r["kind"] == "idea":
            by_id[p["idea_id"]] = {**p, "ts": r["ts"], "history": []}
        elif r["kind"] == "idea_update" and p.get("idea_id") in by_id:
            idea = by_id[p["idea_id"]]
            idea["status"] = p["status"]
            idea["history"].append({"ts": r["ts"], "status": p["status"], "note": p.get("note"), "by": p.get("by")})
    trials = registry.trials()
    for idea in by_id.values():
        fam = [t for t in trials if t.get("family") == idea["family"]]
        ok = [t for t in fam if t.get("status") == "ok" and (t.get("stats") or {}).get("sharpe") is not None]
        idea["n_trials"] = len(fam)
        idea["best_trial"] = max(ok, key=lambda t: t["stats"]["sharpe"])["trial_id"] if ok else None
    return list(by_id.values())


def propose(registry: Registry, *, title: str, mechanism: str, prediction: str, kill_criteria: str,
            family: str, data_needed: str = "", data_sources: str = "", references: str = "",
            proposed_by: str = "unknown") -> dict:
    for label, val in (("title", title), ("mechanism", mechanism), ("prediction", prediction),
                       ("kill_criteria", kill_criteria)):
        if not val or not val.strip():
            raise ValueError(f"{label} is required — an idea without it is not testable")
    if not FAMILY_RE.match(family):
        raise ValueError("family must be a lowercase slug (letters, digits, dashes)")
    existing = ideas(registry)
    mine = _tokens(title, mechanism)
    for idea in existing:
        if idea["family"] == family:
            return {"duplicate_of": idea["idea_id"], "reason": f"family {family!r} already exists",
                    "existing": _brief(idea)}
        score = similarity(mine, _tokens(idea["title"], idea["mechanism"]))
        if score >= DUPLICATE_THRESHOLD:
            return {"duplicate_of": idea["idea_id"], "reason": f"{score:.0%} overlap with an existing idea",
                    "existing": _brief(idea)}
    rec = registry.append("idea", {
        "title": title.strip(), "mechanism": mechanism.strip(), "prediction": prediction.strip(),
        "kill_criteria": kill_criteria.strip(), "family": family, "data_needed": data_needed.strip(),
        "data_sources": data_sources.strip(), "references": references.strip(),
        "proposed_by": proposed_by, "status": "proposed",
    }, id_field="idea_id", id_prefix="I")
    return {"idea_id": rec["payload"]["idea_id"], "family": family, "status": "proposed"}


def update(registry: Registry, idea_id: str, status: str, note: str, by: str = "unknown") -> dict:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    if not any(i["idea_id"] == idea_id for i in ideas(registry)):
        raise ValueError(f"no idea {idea_id}")
    if not note.strip():
        raise ValueError("a note is required: why did the status change? (dead ideas need a cause of death)")
    registry.append("idea_update", {"idea_id": idea_id, "status": status, "note": note.strip(), "by": by})
    return {"idea_id": idea_id, "status": status}


def _brief(idea: dict) -> dict:
    return {k: idea.get(k) for k in ("idea_id", "title", "family", "status", "n_trials", "best_trial",
                                      "proposed_by", "ts")}


def listing(registry: Registry, status: str | None = None, verbose: bool = False) -> list[dict]:
    rows = [i for i in ideas(registry) if status in (None, "", i["status"])]
    rows.sort(key=lambda i: i["ts"], reverse=True)
    if verbose:
        return rows
    return [{**_brief(i), "prediction": i["prediction"], "data_needed": i.get("data_needed"),
             "last_note": (i["history"][-1]["note"] if i["history"] else None)} for i in rows]
