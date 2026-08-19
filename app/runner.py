"""
The bounded run loop.

Invariants this module is responsible for:

  1. BOUNDS ARE CHECKED BEFORE A STEP RUNS, never after. Hitting one is a
     terminal state of its own (`max_steps_exceeded` / `max_credits_exceeded`),
     never `failed`.
  2. A step is charged only after it succeeds and its artifact is persisted.
     Failed steps are free. Replayed steps are free.
  3. Spend is re-derived from the ledger on every iteration. The loop holds no
     running total in a variable it could get wrong.
  4. Every step commits before the next begins, so a poller sees progress
     arrive one step at a time and a crash loses at most one step.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlmodel import Session, select

from .config import settings
from .db import charge, credits_spent, engine
from .models import Run, RunStatus, Step, StepName, StepStatus, utcnow
from .pricing import PIPELINE, can_afford, cost_of
from .tools import TOOLS, ToolContext, ToolError


# ---------------------------------------------------------------------------
# Step materialisation
# ---------------------------------------------------------------------------


def materialize_steps(session: Session, run: Run) -> list[Step]:
    """Create the 7 pending step rows up front. Idempotent.

    The whole plan exists in the DB before anything executes, so the UI can
    render all 7 rows greyed out on the first poll and fill them in. It also
    means "which step did we not reach" is answerable from data rather than
    inferred from a missing row.
    """
    existing = ordered_steps(session, run.id)
    if existing:
        return existing
    rows = [
        Step(run_id=run.id, idx=i, name=name, status=StepStatus.pending)
        for i, name in enumerate(PIPELINE)
    ]
    session.add_all(rows)
    session.commit()
    return ordered_steps(session, run.id)


def ordered_steps(session: Session, run_id: str) -> list[Step]:
    return list(
        session.exec(select(Step).where(Step.run_id == run_id).order_by(Step.idx))
    )


def _artifacts(steps: list[Step]) -> dict[str, Any]:
    """Cached outputs of everything that has succeeded so far."""
    return {
        s.name.value: s.output_json
        for s in steps
        if s.status is StepStatus.succeeded and s.output_json is not None
    }


# ---------------------------------------------------------------------------
# Terminal transitions
# ---------------------------------------------------------------------------


def _skip_remaining(session: Session, steps: list[Step], from_idx: int) -> None:
    """Mark unreached steps `skipped` so the UI can show where we stopped."""
    for s in steps:
        if s.idx >= from_idx and s.status is StepStatus.pending:
            s.status = StepStatus.skipped
            session.add(s)


def _finish(
    session: Session,
    run: Run,
    status: RunStatus,
    *,
    error_code: Optional[str] = None,
    error_message: Optional[str] = None,
    failed_step: Optional[StepName] = None,
) -> None:
    run.status = status
    run.error_code = error_code
    run.error_message = error_message
    run.failed_step = failed_step
    run.finished_at = utcnow()
    run.updated_at = utcnow()
    run.lease_expires_at = None  # terminal runs hold no lease
    session.add(run)
    session.commit()


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


def execute_run(run_id: str) -> None:
    """Drive a run to a terminal state. Safe to call as a background task.

    Opens its own session: the request that started this has already returned
    its 202 by the time the loop is running.
    """
    with Session(engine) as session:
        run = session.get(Run, run_id)
        if run is None:  # pragma: no cover - defensive
            return
        if run.status.is_terminal:
            return  # already done; never re-execute a finished run

        steps = materialize_steps(session, run)

        run.status = RunStatus.running
        run.started_at = run.started_at or utcnow()
        run.lease_expires_at = utcnow() + timedelta(seconds=settings.lease_seconds)
        run.updated_at = utcnow()
        session.add(run)
        session.commit()

        for step in steps:
            # ---------- replay: cached artifact, zero credits, no attempt ----
            if step.status is StepStatus.succeeded and step.output_json is not None:
                # Does NOT touch steps_attempted: a replay is not an attempt,
                # so resuming can never consume the step budget.
                if not step.replayed and run.resume_count > 0:
                    step.replayed = True
                    session.add(step)
                    session.commit()
                continue

            # ---------- bounds: checked BEFORE the step executes -------------
            if run.steps_attempted >= run.max_steps:
                _skip_remaining(session, steps, step.idx)
                _finish(
                    session, run, RunStatus.max_steps_exceeded,
                    error_code="max_steps_exceeded",
                    error_message=(
                        f"step budget exhausted: {run.steps_attempted}/"
                        f"{run.max_steps} attempts used before '{step.name.value}'"
                    ),
                )
                return

            # Spend is re-read from the ledger, not carried in a variable.
            spent_mc = credits_spent(session, run.id)
            if not can_afford(step.name, spent_mc, run.max_credits_mc):
                _skip_remaining(session, steps, step.idx)
                _finish(
                    session, run, RunStatus.max_credits_exceeded,
                    error_code="max_credits_exceeded",
                    error_message=(
                        f"credit budget exhausted: '{step.name.value}' costs "
                        f"{cost_of(step.name)}mc, {run.max_credits_mc - spent_mc}mc "
                        f"of {run.max_credits_mc}mc remain"
                    ),
                )
                return

            # ---------- execute ---------------------------------------------
            step.status = StepStatus.running
            step.started_at = utcnow()
            step.replayed = False
            step.quoted_cost_mc = cost_of(step.name)
            run.steps_attempted += 1  # the bound counts attempts, not successes
            run.lease_expires_at = utcnow() + timedelta(seconds=settings.lease_seconds)
            session.add_all([step, run])
            session.commit()

            ctx = ToolContext(
                run_id=run.id, goal=run.goal, artifacts=_artifacts(steps)
            )
            started = datetime.now(timezone.utc)
            try:
                if _should_inject_failure(run, step):
                    raise ToolError(
                        "injected_failure",
                        f"simulated failure at '{step.name.value}' (fail_at_step)",
                    )
                output = TOOLS[step.name](ctx)
            except ToolError as exc:
                _fail_step(session, step, exc.code, exc.message, started)
                # The failed step is NOT charged: the caller got no artifact.
                _finish(
                    session, run, RunStatus.failed,
                    error_code=exc.code,
                    error_message=exc.message,
                    failed_step=step.name,
                )
                return
            except Exception as exc:  # unexpected: still a clean terminal state
                _fail_step(session, step, "internal_error", repr(exc), started)
                _finish(
                    session, run, RunStatus.failed,
                    error_code="internal_error",
                    error_message=repr(exc),
                    failed_step=step.name,
                )
                return

            # ---------- persist artifact, THEN charge -----------------------
            step.output_json = output
            step.status = StepStatus.succeeded
            step.finished_at = utcnow()
            step.duration_ms = _elapsed_ms(started)
            step.error_code = None
            step.error_message = None
            _apply_side_effects(run, step, output)
            run.updated_at = utcnow()
            session.add_all([step, run])
            session.commit()  # artifact durable before money moves

            charge(
                session,
                run_id=run.id,
                step_id=step.id,
                step_idx=step.idx,
                step_name=step.name,
                attempt=step.attempt,
                amount_mc=cost_of(step.name),
                reason=f"{step.name.value} succeeded",
            )
            session.commit()

        _finish(session, run, RunStatus.completed)


def _should_inject_failure(run: Run, step: Step) -> bool:
    """`fail_at_step` fires on the FIRST attempt only.

    A permanent injected fault would make resume untestable -- the run could
    never get past the same step. Modelling it as transient matches the real
    failure it stands in for (a provider timeout) and lets one request
    demonstrate both the failure path and the recovery path.
    """
    return run.fail_at_step is step.name and step.attempt == 0


def _fail_step(
    session: Session, step: Step, code: str, message: str, started: datetime
) -> None:
    step.status = StepStatus.failed
    step.error_code = code
    step.error_message = message
    step.finished_at = utcnow()
    step.duration_ms = _elapsed_ms(started)
    step.output_json = None  # no partial artifact may survive a failure
    session.add(step)
    session.commit()


def _elapsed_ms(started: datetime) -> int:
    return int((datetime.now(timezone.utc) - started).total_seconds() * 1000)


def _apply_side_effects(run: Run, step: Step, output: dict[str, Any]) -> None:
    """Promote a few plan fields onto the run, and the receipt at the end.

    Denormalisation for the API layer only -- the step artifact remains the
    source of truth for everything here.
    """
    if step.name is StepName.plan:
        run.amount_usd_cents = output.get("amount_usd_cents")
        run.recipient_name = output.get("recipient_name")
        run.dest_country = output.get("dest_country")
        run.dest_currency = output.get("dest_currency")
    elif step.name is StepName.write_receipt:
        run.receipt = output


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------


class NotResumable(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def prepare_resume(session: Session, run: Run) -> Run:
    """Reset a failed run so `execute_run` restarts at the failed step.

    Succeeded steps keep status + output_json untouched, which is exactly what
    makes the replay free -- resume adds no special "free" accounting path, it
    simply never reaches `charge` for a cached step.
    """
    if run.status is RunStatus.running and not _lease_expired(run):
        raise NotResumable("run_in_flight", "run is currently executing")
    if not run.status.is_resumable and not (
        run.status is RunStatus.running and _lease_expired(run)
    ):
        raise NotResumable(
            "not_resumable",
            f"a run in state '{run.status.value}' cannot be resumed"
            + (
                " -- bounds would be meaningless if exceeding one were retryable"
                if run.status
                in (RunStatus.max_steps_exceeded, RunStatus.max_credits_exceeded)
                else ""
            ),
        )

    steps = ordered_steps(session, run.id)
    for step in steps:
        # Rewind the failed step and everything after it that never ran.
        if step.status in (StepStatus.failed, StepStatus.skipped, StepStatus.running):
            if step.status is not StepStatus.skipped:
                # A fresh attempt number: this is what lets the ledger charge
                # for the retry while still rejecting a duplicate of attempt 0.
                step.attempt += 1
            step.status = StepStatus.pending
            step.error_code = None
            step.error_message = None
            step.output_json = None
            step.started_at = None
            step.finished_at = None
            step.duration_ms = None
            session.add(step)

    run.resume_count += 1
    run.status = RunStatus.queued
    run.error_code = None
    run.error_message = None
    run.failed_step = None
    run.finished_at = None
    run.updated_at = utcnow()
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


def _lease_expired(run: Run) -> bool:
    if run.lease_expires_at is None:
        return True
    expires = run.lease_expires_at
    if expires.tzinfo is None:  # SQLite hands back naive datetimes
        expires = expires.replace(tzinfo=timezone.utc)
    return expires < datetime.now(timezone.utc)
