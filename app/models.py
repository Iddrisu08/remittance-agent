"""
Persistence layer for the remittance agent run loop.

Three tables, one invariant each:

  Run         one agent run, keyed by an idempotency key (UNIQUE).
  Step        one attempted tool call, holding its cached output artifact.
  LedgerEntry append-only money. The ONLY source of truth for credits spent.

Accounting rules enforced by these types:
  * credits are milli-credits (mc), int. 1 credit = 1000 mc. No floats.
  * USD is cents (int), GHS is pesewas (int), FX is basis points (int).
  * `credits_used` is not a column anywhere. It is SUM(ledger.amount_mc).
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import JSON, Column, Index, UniqueConstraint
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
# str-mixin so SQLite stores the readable name and FastAPI serialises it
# without a custom encoder.


class RunStatus(str, enum.Enum):
    """Lifecycle of a run. Exactly one of these is terminal-per-run at a time."""

    queued = "queued"                        # row committed, no step executed yet
    running = "running"                       # a worker holds this run (in-flight)
    completed = "completed"                   # all steps succeeded, receipt written
    failed = "failed"                         # a tool call failed -> resumable
    max_steps_exceeded = "max_steps_exceeded"  # bound hit, NOT a failure
    max_credits_exceeded = "max_credits_exceeded"  # budget bound hit, NOT a failure

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL

    @property
    def is_resumable(self) -> bool:
        # Only a genuine tool failure may resume. Bound-exceeded runs must not
        # resume under the same budget, or the bound would mean nothing.
        return self is RunStatus.failed


_TERMINAL = frozenset(
    {
        RunStatus.completed,
        RunStatus.failed,
        RunStatus.max_steps_exceeded,
        RunStatus.max_credits_exceeded,
    }
)


class StepStatus(str, enum.Enum):
    pending = "pending"      # planned, not yet attempted
    running = "running"
    succeeded = "succeeded"  # output_json is a usable artifact -> replayable
    failed = "failed"        # charged nothing; resume restarts here
    skipped = "skipped"      # never reached (bound hit or an earlier step failed)


class StepName(str, enum.Enum):
    """The fixed pipeline. Order here is execution order."""

    plan = "plan"
    validate_recipient = "validate_recipient"
    fetch_fx_rates = "fetch_fx_rates"
    select_best_rate = "select_best_rate"
    lock_quote = "lock_quote"
    execute_transfer = "execute_transfer"
    write_receipt = "write_receipt"


class LedgerKind(str, enum.Enum):
    charge = "charge"      # amount_mc > 0
    refund = "refund"      # amount_mc < 0
    adjustment = "adjustment"


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


class Run(SQLModel, table=True):
    __tablename__ = "runs"
    __table_args__ = (
        # The idempotency guarantee. Not an application-level check: a UNIQUE
        # index is the only thing that survives two concurrent POSTs.
        UniqueConstraint("idempotency_key", name="uq_runs_idempotency_key"),
    )

    id: str = Field(primary_key=True)  # uuid4 hex, opaque, non-enumerable
    idempotency_key: str = Field(index=True)
    # Where the key came from, so GET can tell the client whether it was
    # deduped on their header or on our body hash.
    idempotency_source: str = Field(default="header")  # "header" | "body_hash"
    request_hash: str  # sha256 of the canonical request body

    goal: str
    status: RunStatus = Field(default=RunStatus.queued, index=True)

    # --- bounds: snapshotted per run, never read from config at check time ---
    # If these lived only in settings, changing MAX_CREDITS tomorrow would
    # retroactively change whether yesterday's run was legitimately bounded.
    max_steps: int = Field(default=8)
    max_credits_mc: int = Field(default=6_000)  # 6.000 credits; see config.Settings

    # The bound is checked against this, and it is NOT len(steps): a resume
    # replays cached steps without attempting them, so the two diverge on
    # purpose. There is no separate cursor -- the loop finds its restart point
    # by skipping steps that already hold a cached artifact.
    steps_attempted: int = Field(default=0)

    # --- fault injection, part of the request contract ---
    fail_at_step: Optional[StepName] = Field(default=None)

    # --- parsed intent, filled by the `plan` step (nullable until then) ---
    amount_usd_cents: Optional[int] = Field(default=None)
    recipient_name: Optional[str] = Field(default=None)
    dest_country: Optional[str] = Field(default=None)   # "GH"
    dest_currency: Optional[str] = Field(default=None)  # "GHS"

    # --- outcome ---
    # Denormalised failure detail so GET /runs/{id} needs no step scan to
    # render the error banner.
    failed_step: Optional[StepName] = Field(default=None)
    error_code: Optional[str] = Field(default=None)
    error_message: Optional[str] = Field(default=None)
    receipt: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSON(none_as_null=True)))

    # --- in-flight bookkeeping ---
    # A duplicate POST arriving while status == running is answered from these:
    # 202 + Retry-After rather than a second execution.
    lease_expires_at: Optional[datetime] = Field(default=None)
    resume_count: int = Field(default=0)

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    started_at: Optional[datetime] = Field(default=None)
    finished_at: Optional[datetime] = Field(default=None)

    # NOTE: there is deliberately no `credits_used` column here. See LedgerEntry.


# ---------------------------------------------------------------------------
# Step
# ---------------------------------------------------------------------------


class Step(SQLModel, table=True):
    __tablename__ = "steps"
    __table_args__ = (
        # One row per ordinal per run: a resume UPDATEs the failed step in
        # place instead of appending a duplicate. Steps are mutable state;
        # only the ledger is append-only.
        UniqueConstraint("run_id", "idx", name="uq_steps_run_idx"),
        Index("ix_steps_run_idx", "run_id", "idx"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: str = Field(foreign_key="runs.id", index=True)

    idx: int                 # 0-based position in the pipeline; UI sort key
    name: StepName
    status: StepStatus = Field(default=StepStatus.pending)
    attempt: int = Field(default=0)  # bumped by resume; 0 = never executed

    # The cached artifact. Non-null + status succeeded is precisely the
    # condition for "replay this at zero credits" on resume.
    output_json: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSON(none_as_null=True)))

    # Rate-card price of this tool at execution time. Display/pricing metadata
    # only: it is written for transparency and is NEVER summed to compute a
    # balance. The charge that actually happened is the ledger row.
    quoted_cost_mc: int = Field(default=0)

    error_code: Optional[str] = Field(default=None)
    error_message: Optional[str] = Field(default=None)

    started_at: Optional[datetime] = Field(default=None)
    finished_at: Optional[datetime] = Field(default=None)
    duration_ms: Optional[int] = Field(default=None)
    # True when this step was served from cache on the most recent resume, so
    # the UI can grey it out and show 0 mc against it.
    replayed: bool = Field(default=False)


# ---------------------------------------------------------------------------
# LedgerEntry
# ---------------------------------------------------------------------------


class LedgerEntry(SQLModel, table=True):
    """Append-only. Never UPDATEd, never DELETEd.

    Total spend for a run is always:
        SELECT COALESCE(SUM(amount_mc), 0) FROM ledger WHERE run_id = ?
    A correction is a new negative row, not an edit.
    """

    __tablename__ = "ledger"
    __table_args__ = (
        # The double-charge guard. entry_key is deterministic, e.g.
        # "step:3:fetch_fx_rates:attempt:1:charge". A retried or duplicated
        # write hits this constraint and is swallowed as a no-op, so the
        # ledger cannot double-bill even if the loop crashes mid-commit.
        UniqueConstraint("run_id", "entry_key", name="uq_ledger_run_entry_key"),
        Index("ix_ledger_run_created", "run_id", "created_at"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: str = Field(foreign_key="runs.id", index=True)
    step_id: Optional[int] = Field(default=None, foreign_key="steps.id")

    entry_key: str
    kind: LedgerKind = Field(default=LedgerKind.charge)
    # Signed milli-credits. int, always. Positive = charge, negative = refund.
    amount_mc: int
    # Copied, not joined: a ledger row must stay readable even if the step
    # row is later rewritten by a resume.
    step_name: Optional[StepName] = Field(default=None)
    step_idx: Optional[int] = Field(default=None)
    attempt: int = Field(default=0)
    reason: str = ""

    created_at: datetime = Field(default_factory=utcnow)
