"""
HTTP layer.

    POST /runs             create (idempotent) and start a run
    GET  /runs/{id}        full state: run + steps + ledger + derived total
    POST /runs/{id}/resume replay cached steps free, re-execute from the failure
    GET  /meta             bounds + rate card, so the UI can price a run up front
    GET  /                 the single-page UI

Idempotency is enforced by the database, not by this module. The handler
attempts the INSERT and interprets the UNIQUE violation. A "check whether the
key exists, then insert" would pass under test and double-charge in production
the first time two requests arrive together.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from .config import settings
from .db import credits_spent, get_session, init_db
from .models import LedgerEntry, Run, RunStatus, Step, StepName
from .pricing import PIPELINE, RATE_CARD, format_mc, full_run_cost_mc
from .runner import (
    NotResumable,
    execute_run,
    materialize_steps,
    ordered_steps,
    prepare_resume,
)

@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Schema creation on startup keeps the run command to one line: there is
    # no migrate step to forget. create_all is a no-op once the tables exist.
    init_db()
    yield


app = FastAPI(title="Remittance Agent", version="1.0.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Request / response shapes
# ---------------------------------------------------------------------------


class CreateRunRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=500)
    # Fault injection, part of the contract.
    fail_at_step: Optional[StepName] = None
    # Bounds may be LOWERED per request, never raised past the operator ceiling.
    max_steps: Optional[int] = Field(default=None, ge=1)
    max_credits_mc: Optional[int] = Field(default=None, ge=0)

    @field_validator("max_steps")
    @classmethod
    def _cap_steps(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and v > settings.max_steps_ceiling:
            raise ValueError(f"max_steps may not exceed {settings.max_steps_ceiling}")
        return v

    @field_validator("max_credits_mc")
    @classmethod
    def _cap_credits(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and v > settings.max_credits_ceiling_mc:
            raise ValueError(
                f"max_credits_mc may not exceed {settings.max_credits_ceiling_mc}"
            )
        return v

    def normalized(self) -> dict[str, Any]:
        """Canonical form used for the body hash.

        Defaults are resolved BEFORE hashing, so {"goal": "x"} and
        {"goal": "x", "max_steps": 8} are recognised as the same request
        rather than creating two runs and charging twice.
        """
        return {
            "goal": self.goal.strip(),
            "fail_at_step": self.fail_at_step.value if self.fail_at_step else None,
            "max_steps": self.max_steps or settings.max_steps,
            "max_credits_mc": (
                settings.max_credits_mc
                if self.max_credits_mc is None
                else self.max_credits_mc
            ),
        }

    def body_hash(self) -> str:
        canonical = json.dumps(self.normalized(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:  # SQLite returns naive datetimes
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _step_view(s: Step) -> dict[str, Any]:
    return {
        "idx": s.idx,
        "name": s.name.value,
        "status": s.status.value,
        "attempt": s.attempt,
        "replayed": s.replayed,
        # What this step cost per the rate card. NOT an accounting figure --
        # a replayed step shows its price here but contributes no ledger row.
        "quoted_cost_mc": RATE_CARD[s.name],
        "quoted_cost_display": format_mc(RATE_CARD[s.name]),
        "output": s.output_json,
        "error_code": s.error_code,
        "error_message": s.error_message,
        "duration_ms": s.duration_ms,
        "started_at": _iso(s.started_at),
        "finished_at": _iso(s.finished_at),
    }


def run_view(session: Session, run: Run) -> dict[str, Any]:
    """The single response shape. GET, POST and resume all return this.

    Built fresh from the run row every time rather than replayed from a stored
    response body: a frozen snapshot would hand a caller a `failed` payload for
    a run that has since been resumed and completed.
    """
    steps = ordered_steps(session, run.id)
    ledger = list(
        session.exec(
            select(LedgerEntry)
            .where(LedgerEntry.run_id == run.id)
            .order_by(LedgerEntry.id)
        )
    )
    # The only place a total is ever produced: SUM over the ledger.
    total_mc = credits_spent(session, run.id)
    return {
        "id": run.id,
        "goal": run.goal,
        "status": run.status.value,
        "terminal": run.status.is_terminal,
        "resumable": run.status.is_resumable,
        "idempotency_key": run.idempotency_key,
        "idempotency_source": run.idempotency_source,
        "bounds": {
            "max_steps": run.max_steps,
            "steps_attempted": run.steps_attempted,
            "max_credits_mc": run.max_credits_mc,
            "max_credits_display": format_mc(run.max_credits_mc),
        },
        "credits": {
            "used_mc": total_mc,
            "used_display": format_mc(total_mc),
            "remaining_mc": run.max_credits_mc - total_mc,
            "remaining_display": format_mc(run.max_credits_mc - total_mc),
        },
        "intent": {
            "amount_usd_cents": run.amount_usd_cents,
            "recipient_name": run.recipient_name,
            "dest_country": run.dest_country,
            "dest_currency": run.dest_currency,
        },
        "fail_at_step": run.fail_at_step.value if run.fail_at_step else None,
        "failed_step": run.failed_step.value if run.failed_step else None,
        "error_code": run.error_code,
        "error_message": run.error_message,
        "resume_count": run.resume_count,
        "steps": [_step_view(s) for s in steps],
        "ledger": [
            {
                "id": e.id,
                "entry_key": e.entry_key,
                "kind": e.kind.value,
                "step_idx": e.step_idx,
                "step_name": e.step_name.value if e.step_name else None,
                "attempt": e.attempt,
                "amount_mc": e.amount_mc,
                "amount_display": format_mc(e.amount_mc),
                "reason": e.reason,
                "created_at": _iso(e.created_at),
            }
            for e in ledger
        ],
        "receipt": run.receipt,
        "created_at": _iso(run.created_at),
        "finished_at": _iso(run.finished_at),
    }


# ---------------------------------------------------------------------------
# POST /runs
# ---------------------------------------------------------------------------


@app.post("/runs")
def create_run(
    body: CreateRunRequest,
    response: Response,
    background: BackgroundTasks,
    session: Session = Depends(get_session),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    request_hash = body.body_hash()
    if idempotency_key and idempotency_key.strip():
        key, source = idempotency_key.strip(), "header"
    else:
        # No header: the body itself is the key. Resubmitting an identical
        # request is then a replay rather than a second charge.
        key, source = f"sha256:{request_hash}", "body_hash"

    norm = body.normalized()
    run = Run(
        id=uuid.uuid4().hex,
        idempotency_key=key,
        idempotency_source=source,
        request_hash=request_hash,
        goal=norm["goal"],
        fail_at_step=body.fail_at_step,
        max_steps=norm["max_steps"],
        max_credits_mc=norm["max_credits_mc"],
        status=RunStatus.queued,
    )
    try:
        session.add(run)
        session.commit()
    except IntegrityError:
        session.rollback()
        return _replay(session, key, request_hash, response)

    session.refresh(run)
    materialize_steps(session, run)  # full plan visible on the first poll
    view = run_view(session, run)

    # Runs in the background so the caller gets an id immediately and can poll.
    background.add_task(execute_run, run.id)

    response.status_code = 201
    response.headers["Idempotency-Key"] = key
    response.headers["Location"] = f"/runs/{run.id}"
    return view


def _replay(
    session: Session, key: str, request_hash: str, response: Response
) -> Any:
    """A run with this key already exists. Decide which kind of duplicate."""
    existing = session.exec(
        select(Run).where(Run.idempotency_key == key)
    ).one_or_none()
    if existing is None:  # pragma: no cover - lost a race with a rollback
        raise HTTPException(status_code=409, detail={"code": "idempotency_conflict"})

    if existing.request_hash != request_hash:
        # Same key, different body. Silently returning the old run would hide
        # a client bug and answer a question the caller did not ask.
        raise HTTPException(
            status_code=422,
            detail={
                "code": "idempotency_key_reuse",
                "message": (
                    "Idempotency-Key was already used with a different request "
                    "body"
                ),
                "run_id": existing.id,
            },
        )

    response.headers["Idempotency-Key"] = key
    response.headers["Idempotency-Replayed"] = "true"
    response.headers["Location"] = f"/runs/{existing.id}"

    if not existing.status.is_terminal:
        # In-flight duplicate: the original is still executing. Hand back the
        # current state and tell the caller when to look again. Never a second
        # execution, and never a 409 -- from the client's side this request
        # succeeded, they just got here twice.
        response.status_code = 202
        response.headers["Retry-After"] = "1"
    else:
        response.status_code = 200
    return run_view(session, existing)


# ---------------------------------------------------------------------------
# GET /runs/{id}
# ---------------------------------------------------------------------------


@app.get("/runs/{run_id}")
def get_run(run_id: str, session: Session = Depends(get_session)) -> Any:
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail={"code": "run_not_found"})
    return run_view(session, run)


# ---------------------------------------------------------------------------
# POST /runs/{id}/resume
# ---------------------------------------------------------------------------


@app.post("/runs/{run_id}/resume")
def resume_run(
    run_id: str,
    response: Response,
    background: BackgroundTasks,
    session: Session = Depends(get_session),
) -> Any:
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail={"code": "run_not_found"})

    if run.status is RunStatus.completed:
        # Already where the caller wants it. Idempotent no-op, not an error.
        response.status_code = 200
        return run_view(session, run)

    try:
        run = prepare_resume(session, run)
    except NotResumable as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": exc.message,
                    "status": run.status.value},
        ) from None

    background.add_task(execute_run, run.id)
    response.status_code = 202
    response.headers["Location"] = f"/runs/{run.id}"
    return run_view(session, run)


# ---------------------------------------------------------------------------
# Meta + static
# ---------------------------------------------------------------------------


@app.get("/meta")
def meta() -> Any:
    return {
        "pipeline": [
            {
                "idx": i,
                "name": s.value,
                "cost_mc": RATE_CARD[s],
                "cost_display": format_mc(RATE_CARD[s]),
            }
            for i, s in enumerate(PIPELINE)
        ],
        "full_run_cost_mc": full_run_cost_mc(),
        "full_run_cost_display": format_mc(full_run_cost_mc()),
        "defaults": {
            "max_steps": settings.max_steps,
            "max_credits_mc": settings.max_credits_mc,
        },
        "ceilings": {
            "max_steps": settings.max_steps_ceiling,
            "max_credits_mc": settings.max_credits_ceiling_mc,
        },
    }


app.mount("/static", StaticFiles(directory=str(settings.static_dir)), name="static")


@app.get("/")
def index() -> Any:
    page = settings.static_dir / "index.html"
    if page.exists():
        return FileResponse(page)
    return JSONResponse({"message": "UI not built yet", "try": "/meta"})
