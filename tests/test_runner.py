"""The bounded loop: charging policy, terminal states, resume."""

from sqlmodel import Session

from app.db import charge, credits_spent, engine
from app.models import LedgerEntry, Run, RunStatus, StepName, StepStatus
from app.pricing import RATE_CARD, full_run_cost_mc
from app.runner import NotResumable, execute_run, ordered_steps, prepare_resume
import pytest

COST = RATE_CARD
THROUGH_LOCK = (
    COST[StepName.plan] + COST[StepName.validate_recipient]
    + COST[StepName.fetch_fx_rates] + COST[StepName.select_best_rate]
    + COST[StepName.lock_quote]
)


def _state(run_id):
    with Session(engine) as s:
        return s.get(Run, run_id), ordered_steps(s, run_id), credits_spent(s, run_id)


# --------------------------------------------------------------- happy path


def test_clean_run_completes_and_charges_the_rate_card(make_run):
    rid = make_run()
    execute_run(rid)
    run, steps, spent = _state(rid)
    assert run.status is RunStatus.completed
    assert spent == full_run_cost_mc() == 5_400
    assert all(s.status is StepStatus.succeeded for s in steps)
    assert run.receipt["received"]["display"].startswith("GHS ")


def test_credits_used_is_never_stored_only_derived():
    """The Run model must not carry a credits column at all."""
    assert not [f for f in Run.model_fields if "credits_used" in f]


def test_every_succeeded_step_has_exactly_one_ledger_row(make_run):
    rid = make_run()
    execute_run(rid)
    with Session(engine) as s:
        rows = s.exec(
            LedgerEntry.__table__.select().where(LedgerEntry.run_id == rid)
        ).all()
    assert len(rows) == 7


# ------------------------------------------------------------ partial failure


def test_failure_keeps_completed_charges_and_the_failed_step_is_free(make_run):
    rid = make_run(fail_at_step=StepName.execute_transfer)
    execute_run(rid)
    run, steps, spent = _state(rid)

    assert run.status is RunStatus.failed
    assert run.failed_step is StepName.execute_transfer
    assert spent == THROUGH_LOCK == 2_750, "failed step must not be charged"

    failed = steps[5]
    assert failed.status is StepStatus.failed
    assert failed.output_json is None, "no partial artifact may survive a failure"
    # completed steps cached their outputs
    assert all(s.output_json is not None for s in steps[:5])


def test_natural_failure_on_first_step_charges_nothing(make_run):
    rid = make_run(goal="Send $500 to my mom in Lagos")
    execute_run(rid)
    run, _, spent = _state(rid)
    assert run.status is RunStatus.failed
    assert run.error_code == "unsupported_corridor"
    assert spent == 0


# -------------------------------------------------------------------- resume


def test_resume_replays_cached_steps_at_zero_credits(make_run):
    rid = make_run(fail_at_step=StepName.execute_transfer)
    execute_run(rid)
    _, _, spent_at_failure = _state(rid)

    with Session(engine) as s:
        prepare_resume(s, s.get(Run, rid))
    execute_run(rid)

    run, steps, spent = _state(rid)
    assert run.status is RunStatus.completed
    # Recovering costs exactly what never failing would have cost.
    assert spent == full_run_cost_mc()
    assert spent - spent_at_failure == COST[StepName.execute_transfer] + COST[StepName.write_receipt]
    assert [s.replayed for s in steps[:5]] == [True] * 5
    assert steps[5].attempt == 1


def test_resume_does_not_consume_the_step_budget_for_replays(make_run):
    rid = make_run(fail_at_step=StepName.execute_transfer)
    execute_run(rid)
    with Session(engine) as s:
        prepare_resume(s, s.get(Run, rid))
    execute_run(rid)
    run, _, _ = _state(rid)
    # 6 attempts before the failure + 2 after == 8, not 13.
    assert run.steps_attempted == 8 <= run.max_steps


def test_completed_run_is_not_re_executed(make_run):
    rid = make_run()
    execute_run(rid)
    execute_run(rid)  # second call must be a no-op
    _, _, spent = _state(rid)
    assert spent == full_run_cost_mc()


def test_completed_run_cannot_be_resumed(make_run):
    rid = make_run()
    execute_run(rid)
    with Session(engine) as s:
        with pytest.raises(NotResumable):
            prepare_resume(s, s.get(Run, rid))


# -------------------------------------------------------------------- bounds


def test_step_bound_is_its_own_terminal_state_not_a_failure(make_run):
    rid = make_run(max_steps=3)
    execute_run(rid)
    run, steps, spent = _state(rid)
    assert run.status is RunStatus.max_steps_exceeded
    assert run.status is not RunStatus.failed
    assert run.steps_attempted == 3
    assert spent == COST[StepName.plan] + COST[StepName.validate_recipient] + COST[StepName.fetch_fx_rates]
    assert [s.status for s in steps[3:]] == [StepStatus.skipped] * 4


def test_credit_bound_stops_before_the_step_it_cannot_afford(make_run):
    rid = make_run(max_credits_mc=2_000)
    execute_run(rid)
    run, steps, spent = _state(rid)
    assert run.status is RunStatus.max_credits_exceeded
    # select_best_rate (100mc) fits at 1850; lock_quote (800mc) does not.
    assert spent == 1_950 <= run.max_credits_mc
    assert steps[3].status is StepStatus.succeeded
    assert steps[4].status is StepStatus.skipped


def test_bound_is_checked_before_the_step_executes(make_run):
    """Spend can never exceed the cap, not even transiently."""
    for cap in (0, 100, 250, 649, 650, 1_849, 5_399, 5_400):
        rid = make_run(max_credits_mc=cap)
        execute_run(rid)
        run, _, spent = _state(rid)
        assert spent <= cap, f"overspent at cap={cap}"


def test_bounded_run_cannot_be_resumed(make_run):
    rid = make_run(max_steps=2)
    execute_run(rid)
    with Session(engine) as s:
        run = s.get(Run, rid)
        assert not run.status.is_resumable
        with pytest.raises(NotResumable) as exc:
            prepare_resume(s, run)
        assert exc.value.code == "not_resumable"


# -------------------------------------------------------------------- ledger


def test_ledger_rejects_a_duplicate_charge_for_the_same_attempt(make_run):
    rid = make_run()
    with Session(engine) as s:
        kw = dict(run_id=rid, step_idx=0, step_name=StepName.plan, amount_mc=250,
                  reason="t")
        assert charge(s, attempt=0, **kw) is not None
        assert charge(s, attempt=0, **kw) is None, "double charge was not suppressed"
        s.commit()
        assert credits_spent(s, rid) == 250
        # a genuine retry is a different attempt, and IS chargeable
        assert charge(s, attempt=1, **kw) is not None
        s.commit()
        assert credits_spent(s, rid) == 500
