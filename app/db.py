"""
Engine, session, schema creation, and the ledger primitives.

The two ledger functions live here rather than in the run loop on purpose:
`credits_spent` (the only legal way to obtain a total) and `charge` (the only
legal way to spend) sit side by side, so the derive-never-store rule and the
double-charge guard are readable in one screen.
"""

from __future__ import annotations

from typing import Iterator, Optional

from sqlalchemy import event, func, select
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine

from .config import settings
from .models import LedgerEntry, LedgerKind, StepName
from .pricing import ledger_key

engine = create_engine(
    settings.database_url,
    # FastAPI serves requests on a threadpool; the default SQLite guard would
    # reject a connection reused across threads.
    connect_args={"check_same_thread": False},
    echo=False,
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record) -> None:
    """SQLite defaults are wrong for a service. Fix them on every connection."""
    cur = dbapi_conn.cursor()
    # OFF by default in SQLite -- without this, `step.run_id -> runs.id` is a
    # comment, not a constraint.
    cur.execute("PRAGMA foreign_keys=ON")
    # WAL lets the UI poll GET /runs/{id} while the loop is writing steps.
    # In the default rollback journal, readers block writers and the
    # incremental-progress UI stalls.
    cur.execute("PRAGMA journal_mode=WAL")
    # Wait for a lock instead of raising "database is locked" immediately.
    cur.execute("PRAGMA busy_timeout=5000")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.close()


def init_db() -> None:
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    SQLModel.metadata.create_all(engine)


def get_session() -> Iterator[Session]:
    """FastAPI dependency. One session per request."""
    with Session(engine) as session:
        yield session


# ---------------------------------------------------------------------------
# Ledger primitives
# ---------------------------------------------------------------------------


def credits_spent(session: Session, run_id: str) -> int:
    """Total milli-credits spent on a run. THE definition of credits_used.

    Nothing else in this codebase may compute a total. There is no column to
    read, so there is nothing to drift.
    """
    # COALESCE so a run with no ledger rows sums to 0, not NULL.
    total = session.scalar(
        select(func.coalesce(func.sum(LedgerEntry.amount_mc), 0)).where(
            LedgerEntry.run_id == run_id
        )
    )
    return int(total or 0)


def charge(
    session: Session,
    *,
    run_id: str,
    step_idx: int,
    step_name: StepName,
    attempt: int,
    amount_mc: int,
    reason: str,
    step_id: Optional[int] = None,
    kind: LedgerKind = LedgerKind.charge,
) -> Optional[LedgerEntry]:
    """Append one ledger row. Idempotent.

    Returns the row that was written, or None if this exact charge already
    existed. The dedup is the UNIQUE(run_id, entry_key) constraint, not a
    prior SELECT: a check-then-insert loses to a concurrent writer, and the
    thing being protected here is money.

    Caller contract: only call this for a step that SUCCEEDED. Failed steps
    and cache replays are free, so they simply never reach this function.
    """
    if not isinstance(amount_mc, int):  # pragma: no cover - defensive
        raise TypeError("amount_mc must be int milli-credits, never a float")

    entry = LedgerEntry(
        run_id=run_id,
        step_id=step_id,
        entry_key=ledger_key(step_idx, step_name, attempt, kind),
        kind=kind,
        amount_mc=amount_mc,
        step_name=step_name,
        step_idx=step_idx,
        attempt=attempt,
        reason=reason,
    )
    savepoint = session.begin_nested()  # so a collision doesn't poison the tx
    try:
        session.add(entry)
        savepoint.commit()
        return entry
    except IntegrityError:
        savepoint.rollback()
        return None  # already charged for this (run, step, attempt) -- no-op
