"""The test registry — an append-only, hash-chained log of every experiment.

Why it exists: an automated research org can run thousands of backtests. If
only the winners are remembered, every reported Sharpe is inflated by
selection. The registry records EVERY attempt — failures and vault
violations included — so the deflated Sharpe ratio has an honest
denominator, and so anyone can audit who tried what, when.

Format: registry.jsonl, one record per line:

    {"seq": 7, "ts": "...", "kind": "trial", "payload": {...},
     "prev": "<hash of seq 6>", "hash": "<sha256 of this record>"}

Kinds: genesis · config_change · trial · holdout_request · holdout_decision ·
holdout_result. Records are never edited or deleted; `verify()` recomputes
the chain and raises on any tampering. Writers take an exclusive file lock,
so concurrent agents append safely.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import numpy as np

REGISTRY_FILE = "registry.jsonl"
LOCK_FILE = ".registry.lock"
GENESIS_PREV = "0" * 64

# Trial statuses that count as attempts. "rejected" requests never ran (they
# asked for data past the cutoff) — logged for audit, excluded from N.
COUNTED_STATUSES = ("ok", "error", "vault_violation")


class RegistryError(RuntimeError):
    """The registry is corrupt, tampered with, or asked to do something it forbids."""


def _digest(seq: int, ts: str, kind: str, payload: dict, prev: str) -> str:
    body = json.dumps({"seq": seq, "ts": ts, "kind": kind, "payload": payload, "prev": prev},
                      sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode()).hexdigest()


class Registry:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.path = self.root / REGISTRY_FILE
        self._lock_path = self.root / LOCK_FILE

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self._lock_path, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def append(self, kind: str, payload: dict, *, id_field: str | None = None, id_prefix: str = "") -> dict:
        """Append one record; returns it (with seq and hash).

        With `id_field`, the payload gets `{id_field: f"{id_prefix}{seq:06d}"}`
        assigned under the lock, so concurrent writers never share an id."""
        with self._locked():
            records = self._read()
            prev = records[-1]["hash"] if records else GENESIS_PREV
            seq = len(records)
            if seq == 0 and kind != "genesis":
                raise RegistryError("registry not initialised — run `aihf-lab init`")
            ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
            if id_field:
                payload = {**payload, id_field: f"{id_prefix}{seq:06d}"}
            payload = json.loads(json.dumps(payload, default=str))  # normalise for hashing
            record = {"seq": seq, "ts": ts, "kind": kind, "payload": payload, "prev": prev,
                      "hash": _digest(seq, ts, kind, payload, prev)}
            with open(self.path, "a") as fh:
                fh.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")
                fh.flush()
            return record

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def _read(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        with open(self.path) as fh:
            for n, line in enumerate(fh):
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise RegistryError(f"line {n + 1}: not JSON ({exc})") from exc
        return out

    def records(self, kind: str | None = None) -> list[dict]:
        recs = self._read()
        return [r for r in recs if kind is None or r["kind"] == kind]

    def exists(self) -> bool:
        return self.path.exists() and bool(self._read())

    def verify(self) -> int:
        """Recompute the chain. Returns the record count; raises on tampering."""
        prev = GENESIS_PREV
        recs = self._read()
        for i, r in enumerate(recs):
            if r.get("seq") != i:
                raise RegistryError(f"record {i}: seq {r.get('seq')} out of order")
            if r.get("prev") != prev:
                raise RegistryError(f"record {i}: chain broken (prev hash mismatch)")
            expected = _digest(r["seq"], r["ts"], r["kind"], r["payload"], r["prev"])
            if r.get("hash") != expected:
                raise RegistryError(f"record {i}: hash mismatch — record was edited")
            prev = r["hash"]
        if recs and recs[0]["kind"] != "genesis":
            raise RegistryError("record 0 is not a genesis record")
        return len(recs)

    # ------------------------------------------------------------------
    # Views
    # ------------------------------------------------------------------

    def trials(self, *, candidate_id: str | None = None, family: str | None = None) -> list[dict]:
        out = []
        for r in self.records("trial"):
            p = r["payload"]
            if candidate_id and p.get("candidate_id") != candidate_id:
                continue
            if family and p.get("family") != family:
                continue
            out.append({**p, "seq": r["seq"], "ts": r["ts"]})
        return out

    def trial(self, trial_id: str) -> dict | None:
        for t in self.trials():
            if t.get("trial_id") == trial_id:
                return t
        return None

    def trial_universe(self) -> tuple[int, float]:
        """(N attempts, variance of per-period Sharpe across completed trials).

        N counts every research trial, failed ones included — an attempt that
        crashed was still an attempt someone would have reported had it won.
        """
        trials = [t for t in self.trials() if t.get("status") in COUNTED_STATUSES]
        srs = [t["stats"]["sharpe"] for t in trials
               if t.get("status") == "ok" and t.get("stats", {}).get("sharpe") is not None]
        var = float(np.var(srs, ddof=1)) if len(srs) >= 2 else 0.0
        return len(trials), var

    def holdout_requests(self) -> list[dict]:
        decisions = {r["payload"]["request_id"]: r["payload"] for r in self.records("holdout_decision")}
        results = {r["payload"]["request_id"]: r["payload"] for r in self.records("holdout_result")}
        out = []
        for r in self.records("holdout_request"):
            p = r["payload"]
            rid = p["request_id"]
            status = "pending"
            if rid in decisions:
                status = decisions[rid]["decision"]
            if rid in results:
                status = "evaluated" if results[rid].get("status") == "ok" else "failed"
            out.append({**p, "status": status, "ts": r["ts"],
                        "decision": decisions.get(rid), "result": results.get(rid)})
        return out

    def holdout_looks(self, candidate_id: str) -> int:
        """Holdout evaluations already spent (or in flight) for a candidate."""
        return sum(1 for h in self.holdout_requests()
                   if h["candidate_id"] == candidate_id and h["status"] in ("pending", "approved", "evaluated", "failed"))

    def genesis_config_hash(self) -> str | None:
        """The config hash currently in force (genesis or latest config_change)."""
        current = None
        for r in self._read():
            if r["kind"] in ("genesis", "config_change"):
                current = r["payload"].get("config_hash")
        return current
