"""Lab settings — frozen at `aihf-lab init`, hashed into every registry record.

The holdout is defined by a fixed START date, not a rolling window: data that
was ever in the holdout must never drift back into research. The research
window is everything strictly before `holdout_start`; the holdout is
[holdout_start, latest completed session].
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date, timedelta
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from hedge_fund.paths import USER_DIR, write_atomic

CONFIG_FILE = "lab.yaml"


def lab_dir() -> Path:
    """Where lab state lives. AIHF_LAB_DIR overrides (tests, separate labs)."""
    override = os.environ.get("AIHF_LAB_DIR")
    return Path(override).expanduser() if override else USER_DIR / "lab"


class LabConfig(BaseModel):
    """The lab's rules. Changing them after research has started is logged."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    holdout_start: str = Field(description="first day of the holdout; research must end before it")
    embargoed_tickers: list[str] = Field(
        default_factory=list,
        description="tickers research may never touch; they are only seen in a holdout evaluation",
    )
    holdout_budget: int = Field(default=1, ge=1, description="holdout evaluations allowed per candidate")
    benchmark_whitelist: list[str] = Field(
        default_factory=lambda: ["SPY"],
        description="benchmarks a mandate may use; never embargoed",
    )

    @field_validator("holdout_start")
    @classmethod
    def _iso_date(cls, value: str) -> str:
        return date.fromisoformat(value).isoformat()

    @field_validator("embargoed_tickers", "benchmark_whitelist")
    @classmethod
    def _upper(cls, tickers: list[str]) -> list[str]:
        return sorted({t.strip().upper() for t in tickers if t.strip()})

    @model_validator(mode="after")
    def _benchmarks_not_embargoed(self) -> LabConfig:
        clash = set(self.embargoed_tickers) & set(self.benchmark_whitelist)
        if clash:
            raise ValueError(f"benchmarks cannot be embargoed: {sorted(clash)}")
        return self

    @property
    def research_end(self) -> str:
        """The last day research may see."""
        return (date.fromisoformat(self.holdout_start) - timedelta(days=1)).isoformat()

    def config_hash(self) -> str:
        canonical = json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def default_config(today: date | None = None) -> LabConfig:
    """Two years of holdout, nothing embargoed: a sane starting point."""
    today = today or date.today()
    return LabConfig(holdout_start=(today - timedelta(days=730)).isoformat())


def config_path(root: Path | None = None) -> Path:
    return (root or lab_dir()) / CONFIG_FILE


def load_config(root: Path | None = None) -> LabConfig:
    path = config_path(root)
    if not path.exists():
        raise FileNotFoundError(f"no lab at {path.parent} — run `aihf-lab init` first")
    return LabConfig(**(yaml.safe_load(path.read_text()) or {}))


def save_config(config: LabConfig, root: Path | None = None) -> Path:
    path = config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(path, yaml.safe_dump(config.model_dump(), sort_keys=False))
    return path
