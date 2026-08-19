"""
Runtime configuration.

Deliberately a frozen dataclass over os.environ rather than pydantic-settings:
settings here are six scalars, and this keeps the dependency list to
fastapi + sqlmodel + uvicorn. Every value is an int or a str -- there is no
float in the config surface, same as the accounting.

All values are overridable by env var with the AGENT_ prefix, e.g.
    AGENT_MAX_CREDITS_MC=2000 uvicorn app.main:app
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = APP_DIR.parent


def _int_env(name: str, default: int) -> int:
    """Read an int env var. Rejects floats loudly instead of truncating."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)  # int("6000.0") raises, and that is the point
    except ValueError as exc:
        raise RuntimeError(
            f"{name} must be an integer (got {raw!r}). "
            "Credits and money are integers everywhere in this service."
        ) from exc


@dataclass(frozen=True)
class Settings:
    # --- storage ---
    db_path: Path = PROJECT_DIR / "data" / "agent.db"
    static_dir: Path = APP_DIR / "static"

    # --- loop bounds (defaults for a new run; snapshotted onto the run row) ---
    max_steps: int = 8
    max_credits_mc: int = 6_000  # 6.000 credits; a clean full run costs 5.400

    # --- ceilings: a request may LOWER its own bounds, never raise them ---
    # Without these, `max_credits: 10**9` in the request body makes the budget
    # cap a suggestion. The ceiling is the operator's guarantee.
    max_steps_ceiling: int = 8
    max_credits_ceiling_mc: int = 6_000

    # --- in-flight lease ---
    # A run in `running` whose lease has expired is treated as abandoned
    # (crashed worker) and may be reclaimed by a resume.
    lease_seconds: int = 30

    # --- mocked tool behaviour ---
    # Simulated per-step latency, in ms. Non-zero on purpose: the UI is
    # specified to show steps appearing incrementally under polling, which is
    # unobservable if all 7 steps finish inside one request.
    step_latency_ms: int = 350

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.db_path}"

    def __post_init__(self) -> None:
        if self.max_steps < 1:
            raise RuntimeError("max_steps must be >= 1")
        if self.max_credits_mc < 0:
            raise RuntimeError("max_credits_mc must be >= 0")
        if self.max_steps > self.max_steps_ceiling:
            raise RuntimeError("default max_steps exceeds its own ceiling")
        if self.max_credits_mc > self.max_credits_ceiling_mc:
            raise RuntimeError("default max_credits_mc exceeds its own ceiling")


def _load() -> Settings:
    db_path = Path(
        os.environ.get("AGENT_DB_PATH", str(PROJECT_DIR / "data" / "agent.db"))
    ).expanduser()
    return Settings(
        db_path=db_path,
        max_steps=_int_env("AGENT_MAX_STEPS", 8),
        max_credits_mc=_int_env("AGENT_MAX_CREDITS_MC", 6_000),
        max_steps_ceiling=_int_env("AGENT_MAX_STEPS_CEILING", 8),
        max_credits_ceiling_mc=_int_env("AGENT_MAX_CREDITS_CEILING_MC", 6_000),
        lease_seconds=_int_env("AGENT_LEASE_SECONDS", 30),
        step_latency_ms=_int_env("AGENT_STEP_LATENCY_MS", 350),
    )


settings = _load()
