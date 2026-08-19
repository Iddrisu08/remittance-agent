"""
The tool rate card and the charging policy.

Two things live here and nowhere else:

  1. PIPELINE  -- the ordered list of steps the loop executes.
  2. RATE_CARD -- what each tool costs, in milli-credits (int).

Charging policy (the rules the loop must obey):

  * A step is charged when, and only when, it SUCCEEDS and leaves a usable
    artifact in `step.output_json`. Work you can reuse is work you pay for.
  * A step that FAILS is free. The caller got nothing; billing for it would
    make retries punitive and would make `fail_at_step` demos misleading.
  * A step REPLAYED from cache on resume is free. It was already paid for
    on the attempt that produced the artifact. Charging twice for one
    artifact is exactly the bug the ledger's UNIQUE key exists to prevent.
  * Charges are appended to the ledger. Nothing is ever recomputed into a
    stored total.
"""

from __future__ import annotations

from typing import Iterable

from .models import LedgerKind, StepName

# --- unit ------------------------------------------------------------------
MC_PER_CREDIT = 1_000  # 1 credit = 1000 milli-credits


# --- pipeline --------------------------------------------------------------
# Execution order. Index into this tuple IS `Step.idx`.
PIPELINE: tuple[StepName, ...] = (
    StepName.plan,
    StepName.validate_recipient,
    StepName.fetch_fx_rates,
    StepName.select_best_rate,
    StepName.lock_quote,
    StepName.execute_transfer,
    StepName.write_receipt,
)


# --- rate card -------------------------------------------------------------
# Costs vary by tool and are meant to be legible: cheap local reasoning,
# mid-priced single API calls, expensive fan-out and money movement.
RATE_CARD: dict[StepName, int] = {
    # Local reasoning over the goal string. No network.
    StepName.plan: 250,
    # One KYC/registry lookup against the payout partner.
    StepName.validate_recipient: 400,
    # Fan-out to 3 liquidity providers. The most expensive read.
    StepName.fetch_fx_rates: 1_200,
    # Pure integer comparison over rates already in hand.
    StepName.select_best_rate: 100,
    # Reserves inventory at a provider for a TTL; the provider bills us
    # whether or not we execute.
    StepName.lock_quote: 800,
    # Moves money. Irreversible, and priced like it.
    StepName.execute_transfer: 2_500,
    # Renders and persists the receipt artifact.
    StepName.write_receipt: 150,
}

# A missing or stray entry is a startup failure, not a KeyError mid-transfer.
assert set(RATE_CARD) == set(PIPELINE), "RATE_CARD must cover PIPELINE exactly"
assert all(isinstance(v, int) and v >= 0 for v in RATE_CARD.values()), (
    "rate card must be non-negative ints (milli-credits)"
)


def cost_of(step: StepName) -> int:
    """Rate-card price of one tool call, in milli-credits."""
    return RATE_CARD[step]


def full_run_cost_mc(steps: Iterable[StepName] = PIPELINE) -> int:
    """Cost of a clean end-to-end run. 5_400 mc = 5.400 credits."""
    return sum(RATE_CARD[s] for s in steps)


def can_afford(step: StepName, spent_mc: int, budget_mc: int) -> bool:
    """The budget gate, evaluated BEFORE the step runs.

    Checks only this step's own cost, not the whole remaining pipeline: the
    loop should get as far as the budget genuinely allows and then stop on a
    real boundary, rather than refusing to start work it could have paid for.
    """
    return spent_mc + cost_of(step) <= budget_mc


def ledger_key(step_idx: int, step: StepName, attempt: int, kind: LedgerKind) -> str:
    """Deterministic idempotency key for one ledger row.

    Same (run, step, attempt, kind) can only ever produce one row, enforced by
    UNIQUE(run_id, entry_key). A charge retried after a crash mid-commit
    collides and is discarded instead of double-billing.
    """
    return f"step:{step_idx}:{step.value}:attempt:{attempt}:{kind.value}"


def format_mc(mc: int) -> str:
    """Render milli-credits as credits WITHOUT touching a float.

    5_400 -> "5.400". Integer div/mod only; mc / 1000 would reintroduce
    binary floating point into the one place users actually read the number.
    """
    sign = "-" if mc < 0 else ""
    mc = abs(mc)
    return f"{sign}{mc // MC_PER_CREDIT}.{mc % MC_PER_CREDIT:03d}"
