"""HTTP behaviour: idempotency, in-flight duplicates, resume, bounds."""

import threading
import uuid
from datetime import timedelta

from sqlalchemy import func, select
from sqlmodel import Session

from app.db import engine
from app.models import Run, RunStatus, utcnow

GOAL = {"goal": "Send $500 to my mom in Accra at the best rate"}


def _wait(client, run_id, tries=200):
    for _ in range(tries):
        data = client.get(f"/runs/{run_id}").json()
        if data["terminal"]:
            return data
    raise AssertionError("run never reached a terminal state")


def _key():
    return uuid.uuid4().hex


# --------------------------------------------------------------------- basics


def test_create_returns_the_full_plan_immediately(client):
    r = client.post("/runs", json=GOAL, headers={"Idempotency-Key": _key()})
    assert r.status_code == 201
    body = r.json()
    assert r.headers["Location"] == f"/runs/{body['id']}"
    assert [s["status"] for s in body["steps"]] == ["pending"] * 7


def test_run_completes_with_ledger_and_receipt(client):
    rid = client.post("/runs", json=GOAL, headers={"Idempotency-Key": _key()}).json()["id"]
    done = _wait(client, rid)
    assert done["status"] == "completed"
    assert done["credits"]["used_mc"] == 5_400
    assert done["credits"]["used_display"] == "5.400"
    assert len(done["ledger"]) == 7
    # the reported total is the sum of the rows, not an independent number
    assert sum(e["amount_mc"] for e in done["ledger"]) == done["credits"]["used_mc"]
    assert done["receipt"]["state"] == "settled"


def test_unknown_run_is_404(client):
    assert client.get("/runs/does-not-exist").status_code == 404


# ---------------------------------------------------------------- idempotency


def test_duplicate_request_replays_without_recharging(client):
    key = _key()
    first = client.post("/runs", json=GOAL, headers={"Idempotency-Key": key})
    rid = first.json()["id"]
    _wait(client, rid)

    second = client.post("/runs", json=GOAL, headers={"Idempotency-Key": key})
    assert second.status_code == 200
    assert second.headers["Idempotency-Replayed"] == "true"
    assert second.json()["id"] == rid
    assert second.json()["credits"]["used_mc"] == 5_400
    assert len(second.json()["ledger"]) == 7


def test_same_key_different_body_is_rejected(client):
    key = _key()
    client.post("/runs", json=GOAL, headers={"Idempotency-Key": key})
    r = client.post("/runs", json={"goal": "Send $900 to my mom in Accra"},
                    headers={"Idempotency-Key": key})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "idempotency_key_reuse"


def test_missing_header_falls_back_to_a_body_hash(client):
    body = {"goal": f"Send $250 to my sister in Kumasi {uuid.uuid4().hex}"}
    first = client.post("/runs", json=body)
    assert first.status_code == 201
    assert first.json()["idempotency_source"] == "body_hash"

    # Defaults are normalised before hashing, so spelling one out is the
    # SAME request, not a second one.
    again = client.post("/runs", json={**body, "max_steps": 8})
    assert again.json()["id"] == first.json()["id"]


def test_duplicate_of_an_in_flight_run_is_202_and_does_not_re_execute(client):
    """TestClient waits for background tasks, so seed a genuinely live run."""
    from app.api import CreateRunRequest

    body = {"goal": f"Send $75 to my uncle in Tamale {uuid.uuid4().hex}"}
    key = _key()
    with Session(engine) as s:
        live = Run(
            id=uuid.uuid4().hex,
            idempotency_key=key,
            request_hash=CreateRunRequest(**body).body_hash(),
            goal=body["goal"],
            status=RunStatus.running,
            lease_expires_at=utcnow() + timedelta(seconds=30),
        )
        s.add(live)
        s.commit()
        live_id = live.id

    r = client.post("/runs", json=body, headers={"Idempotency-Key": key})
    assert r.status_code == 202
    assert r.headers["Retry-After"] == "1"
    assert r.headers["Idempotency-Replayed"] == "true"
    assert r.json()["id"] == live_id

    with Session(engine) as s:
        assert s.get(Run, live_id).status is RunStatus.running


def test_concurrent_identical_posts_create_exactly_one_run(client):
    key = _key()
    body = {"goal": f"Send $310 to my brother in Takoradi {uuid.uuid4().hex}"}
    codes, lock = [], threading.Lock()

    def fire():
        resp = client.post("/runs", json=body, headers={"Idempotency-Key": key})
        with lock:
            codes.append(resp.status_code)

    threads = [threading.Thread(target=fire) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert codes.count(201) == 1, f"more than one run was created: {codes}"
    with Session(engine) as s:
        n = s.scalar(select(func.count()).select_from(Run).where(Run.idempotency_key == key))
        rid = s.scalar(select(Run.id).where(Run.idempotency_key == key))
    assert n == 1
    done = _wait(client, rid)
    assert done["credits"]["used_mc"] == 5_400, "charged more than once"
    assert len(done["ledger"]) == 7


# --------------------------------------------------------------------- resume


def test_failure_then_resume_over_http(client):
    r = client.post("/runs", json={**GOAL, "fail_at_step": "execute_transfer"},
                    headers={"Idempotency-Key": _key()})
    rid = r.json()["id"]
    failed = _wait(client, rid)
    assert failed["status"] == "failed"
    assert failed["failed_step"] == "execute_transfer"
    assert failed["credits"]["used_mc"] == 2_750
    assert failed["resumable"] is True

    resumed = client.post(f"/runs/{rid}/resume")
    assert resumed.status_code == 202
    done = _wait(client, rid)

    assert done["status"] == "completed"
    assert done["credits"]["used_mc"] == 5_400
    assert [s["name"] for s in done["steps"] if s["replayed"]] == [
        "plan", "validate_recipient", "fetch_fx_rates", "select_best_rate", "lock_quote"
    ]
    assert done["receipt"] is not None


def test_resuming_a_completed_run_is_a_no_op(client):
    rid = client.post("/runs", json=GOAL, headers={"Idempotency-Key": _key()}).json()["id"]
    _wait(client, rid)
    r = client.post(f"/runs/{rid}/resume")
    assert r.status_code == 200
    assert r.json()["credits"]["used_mc"] == 5_400


def test_resume_of_unknown_run_is_404(client):
    assert client.post("/runs/nope/resume").status_code == 404


# --------------------------------------------------------------------- bounds


def test_bounded_run_reports_a_bound_not_a_failure(client):
    r = client.post("/runs", json={**GOAL, "max_credits_mc": 2_000},
                    headers={"Idempotency-Key": _key()})
    done = _wait(client, r.json()["id"])
    assert done["status"] == "max_credits_exceeded"
    assert done["status"] != "failed"
    assert done["credits"]["used_mc"] <= 2_000
    assert done["resumable"] is False


def test_bounded_run_refuses_resume(client):
    r = client.post("/runs", json={**GOAL, "max_steps": 3},
                    headers={"Idempotency-Key": _key()})
    rid = r.json()["id"]
    assert _wait(client, rid)["status"] == "max_steps_exceeded"
    resumed = client.post(f"/runs/{rid}/resume")
    assert resumed.status_code == 409
    assert resumed.json()["detail"]["code"] == "not_resumable"


def test_request_cannot_raise_its_own_bounds_past_the_ceiling(client):
    assert client.post("/runs", json={**GOAL, "max_credits_mc": 10**9}).status_code == 422
    assert client.post("/runs", json={**GOAL, "max_steps": 500}).status_code == 422


# ----------------------------------------------------------------------- meta


def test_meta_exposes_the_rate_card(client):
    m = client.get("/meta").json()
    assert len(m["pipeline"]) == 7
    assert m["full_run_cost_mc"] == 5_400
    assert m["full_run_cost_display"] == "5.400"
    assert sum(s["cost_mc"] for s in m["pipeline"]) == m["full_run_cost_mc"]


def test_index_page_is_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Remittance Agent" in r.text
