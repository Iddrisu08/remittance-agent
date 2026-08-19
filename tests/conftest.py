"""Test fixtures.

The engine is built from `settings` at import time, so the database path must
be redirected BEFORE any app module is imported. conftest is imported first by
pytest, which makes this the right place for it.
"""

import os
import pathlib
import tempfile
import uuid

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="remit-tests-"))
os.environ["AGENT_DB_PATH"] = str(_TMP / "test.db")
os.environ["AGENT_STEP_LATENCY_MS"] = "0"  # no simulated latency under test

import pytest  # noqa: E402
from sqlmodel import Session  # noqa: E402

from app.db import engine, init_db  # noqa: E402
from app.models import Run  # noqa: E402
from app.runner import materialize_steps  # noqa: E402

GOAL = "Send $500 to my mom in Accra at the best rate"


@pytest.fixture(scope="session", autouse=True)
def _schema():
    init_db()


@pytest.fixture
def session():
    with Session(engine) as s:
        yield s


@pytest.fixture
def make_run():
    """Create a run row directly, bypassing the API."""

    def _make(goal: str = GOAL, **kw) -> str:
        rid = uuid.uuid4().hex
        with Session(engine) as s:
            run = Run(id=rid, idempotency_key=rid, request_hash=rid, goal=goal, **kw)
            s.add(run)
            s.commit()
            materialize_steps(s, run)
        return rid

    return _make


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.api import app

    # Context manager so the lifespan (and init_db) actually runs.
    with TestClient(app) as c:
        yield c
