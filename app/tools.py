"""
The seven mocked tools, plus the integer money math they run on.

Money representation, fixed for the whole service:
    USD  -> cents        (int)   $500.00      = 50_000
    GHS  -> pesewas      (int)   GHS 7,569.75 = 756_975
    rate -> basis points (int)   15.2000 GHS/USD = 152_000 bps

Conversion identity used everywhere:
    pesewas = usd_cents * rate_bps // 10_000
because (usd_cents/100) USD * (rate_bps/10_000) GHS/USD * 100 pesewas/GHS
collapses to exactly that -- one integer multiply and one floor division, no
intermediate float, no precision to lose.

Tools are deterministic: provider rates are drawn from a PRNG seeded by the
run id, so the same run always sees the same market and a resume re-executes
against the market it originally quoted.
"""

from __future__ import annotations

import hashlib
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import settings
from .models import StepName

BPS_SCALE = 10_000


class ToolError(Exception):
    """A tool failed. Carries a stable machine-readable code for the UI."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Integer money math
# ---------------------------------------------------------------------------


def usd_to_ghs(usd_cents: int, rate_bps: int) -> tuple[int, int]:
    """Convert cents -> pesewas. Returns (pesewas, dropped_remainder).

    Rounds DOWN, always. A remittance must never promise a payout larger than
    the arithmetic supports; rounding up by one pesewa per transfer is a real
    liability at volume. The discarded remainder is returned rather than
    silently swallowed so the receipt can account for every unit.
    """
    if usd_cents < 0 or rate_bps <= 0:
        raise ToolError("bad_conversion_input", "amount and rate must be positive")
    total = usd_cents * rate_bps  # exact: Python ints are unbounded
    return total // BPS_SCALE, total % BPS_SCALE


def net_payout_pesewas(usd_cents: int, fee_cents: int, rate_bps: int) -> int:
    """What the recipient actually receives after the provider's fee.

    This -- not rate_bps -- is what `select_best_rate` maximises. The headline
    rate is a marketing number; the payout is the product.
    """
    sendable = usd_cents - fee_cents
    if sendable <= 0:
        raise ToolError("fee_exceeds_amount", "provider fee exceeds the send amount")
    pesewas, _ = usd_to_ghs(sendable, rate_bps)
    return pesewas


def fmt_usd(cents: int) -> str:
    return f"${cents // 100}.{cents % 100:02d}"


def fmt_ghs(pesewas: int) -> str:
    return f"GHS {pesewas // 100:,}.{pesewas % 100:02d}"


def fmt_rate(bps: int) -> str:
    return f"{bps // BPS_SCALE}.{bps % BPS_SCALE:04d}"


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


@dataclass
class ToolContext:
    """Everything a tool may read. Tools never touch the DB or the run row."""

    run_id: str
    goal: str
    # Outputs of previously completed steps, keyed by step name. On resume
    # these come straight from the cached `step.output_json`, which is why a
    # replayed step is indistinguishable from a freshly executed one.
    artifacts: dict[str, Any] = field(default_factory=dict)

    def rng(self, salt: str) -> random.Random:
        """Deterministic PRNG, seeded by (run id, salt).

        The salt is the tool's own name. Seeding on the run id alone gives
        every tool the identical draw sequence, which silently makes
        quote_id == transfer_id. Per-tool seeding also means each tool is
        reproducible independently of execution order -- which is what a
        resume needs, since it re-runs one step in the middle.
        """
        seed = int(
            hashlib.sha256(f"{self.run_id}:{salt}".encode()).hexdigest()[:16], 16
        )
        return random.Random(seed)

    def need(self, step: StepName) -> dict[str, Any]:
        art = self.artifacts.get(step.value)
        if art is None:
            raise ToolError(
                "missing_dependency",
                f"{step.value} must complete before this step",
            )
        return art


def simulate_latency() -> None:
    """The one float in the codebase, and it is a duration, not money."""
    if settings.step_latency_ms > 0:
        time.sleep(settings.step_latency_ms / 1000)


# ---------------------------------------------------------------------------
# Corridor + provider fixtures
# ---------------------------------------------------------------------------

_DESTINATIONS = {
    "accra": ("GH", "GHS"), "kumasi": ("GH", "GHS"), "tamale": ("GH", "GHS"),
    "takoradi": ("GH", "GHS"), "ghana": ("GH", "GHS"),
}

_PROVIDERS = (
    # (id, name, base rate bps, flat fee cents)
    # Tuned so the highest headline rate is NOT the best deal: VoltaRemit
    # quotes the top rate but its $12 fee loses to AkuaPay's $1.99.
    ("akua", "AkuaPay", 152_000, 199),
    ("volta", "VoltaRemit", 154_500, 1_200),
    ("gulf", "GulfStream FX", 151_000, 0),
)

_AMOUNT_RE = re.compile(
    r"\$\s*([\d,]+(?:\.\d{1,2})?)|([\d,]+(?:\.\d{1,2})?)\s*(?:usd|dollars?|bucks)",
    re.I,
)
_RECIPIENT_RE = re.compile(
    r"\bto\s+(?:my\s+)?([a-z][a-z\s'-]{0,40}?)(?=\s+in\b|\s+at\b|\s*$|[,.])", re.I
)


def _parse_amount_cents(goal: str) -> int:
    m = _AMOUNT_RE.search(goal)
    if not m:
        raise ToolError("no_amount", "could not find an amount to send in the goal")
    raw = (m.group(1) or m.group(2)).replace(",", "")
    # Parse the decimal as text, never through float(): float("0.1")*100 is
    # 10.000000000000002, and that is how cents go missing.
    whole, _, frac = raw.partition(".")
    cents = int(whole) * 100 + int((frac + "00")[:2] or 0)
    if cents <= 0:
        raise ToolError("bad_amount", "amount must be greater than zero")
    if cents > 1_000_000_00:
        raise ToolError("amount_too_large", "amount exceeds the $1,000,000 limit")
    return cents


# ---------------------------------------------------------------------------
# The tools
# ---------------------------------------------------------------------------


def plan(ctx: ToolContext) -> dict[str, Any]:
    """Parse the plain-language goal into a structured intent."""
    simulate_latency()
    goal = ctx.goal.strip()
    amount_usd_cents = _parse_amount_cents(goal)

    dest = next(((c, cur) for k, (c, cur) in _DESTINATIONS.items()
                 if k in goal.lower()), None)
    if dest is None:
        raise ToolError(
            "unsupported_corridor",
            "only the US -> Ghana (GHS) corridor is supported",
        )
    country, currency = dest

    m = _RECIPIENT_RE.search(goal)
    recipient = (m.group(1).strip() if m else "")
    # "Send $500 to Accra" must not name the city as the recipient -- that
    # produces a receipt addressed to a place, and hides the fact that no
    # recipient was actually given.
    if recipient.lower() in _DESTINATIONS:
        recipient = ""
    recipient = recipient.title()

    return {
        "amount_usd_cents": amount_usd_cents,
        "amount_usd_display": fmt_usd(amount_usd_cents),
        "recipient_name": recipient or None,
        "dest_country": country,
        "dest_currency": currency,
        "plan": [s.value for s in StepName][1:],
    }


def validate_recipient(ctx: ToolContext) -> dict[str, Any]:
    """Mock KYC lookup at the payout partner."""
    simulate_latency()
    intent = ctx.need(StepName.plan)
    name = intent.get("recipient_name")
    if not name:
        raise ToolError(
            "recipient_not_found",
            "no recipient could be identified in the goal",
        )
    if any(ch.isdigit() for ch in name):
        raise ToolError("recipient_invalid", f"'{name}' is not a valid recipient name")

    rng = ctx.rng("validate_recipient")
    return {
        "recipient_name": name,
        "recipient_id": f"rcp_{rng.getrandbits(32):08x}",
        "payout_method": "mobile_money",
        "payout_network": "MTN MoMo",
        "masked_msisdn": f"+233 24 *** {rng.randrange(1000, 9999)}",
        "kyc_status": "verified",
        "country": intent["dest_country"],
    }


def fetch_fx_rates(ctx: ToolContext) -> dict[str, Any]:
    """Fan-out to the liquidity providers. The expensive read."""
    simulate_latency()
    intent = ctx.need(StepName.plan)
    rng = ctx.rng("validate_recipient")
    quotes = []
    for pid, name, base_bps, fee_cents in _PROVIDERS:
        # Deterministic jitter, integer bps only.
        rate_bps = base_bps + rng.randrange(-300, 301)
        quotes.append({
            "provider_id": pid,
            "provider": name,
            "rate_bps": rate_bps,
            "rate_display": fmt_rate(rate_bps),
            "fee_usd_cents": fee_cents,
            "fee_display": fmt_usd(fee_cents),
            "payout_pesewas": net_payout_pesewas(
                intent["amount_usd_cents"], fee_cents, rate_bps
            ),
        })
    return {
        "pair": "USD/GHS",
        "quote_count": len(quotes),
        "quotes": quotes,
    }


def select_best_rate(ctx: ToolContext) -> dict[str, Any]:
    """Pick the provider with the highest NET payout, not the best headline rate."""
    simulate_latency()
    quotes = ctx.need(StepName.fetch_fx_rates)["quotes"]
    # Tie-break on provider_id so the choice is stable under equal payouts.
    best = max(quotes, key=lambda q: (q["payout_pesewas"], q["provider_id"]))
    headline = max(quotes, key=lambda q: (q["rate_bps"], q["provider_id"]))
    return {
        **best,
        "payout_display": fmt_ghs(best["payout_pesewas"]),
        "beat_headline_rate": headline["provider_id"] != best["provider_id"],
        "runner_up_gap_pesewas": best["payout_pesewas"] - max(
            (q["payout_pesewas"] for q in quotes if q["provider_id"] != best["provider_id"]),
            default=0,
        ),
        "reason": "highest net payout after fees",
    }


def lock_quote(ctx: ToolContext) -> dict[str, Any]:
    """Reserve the rate at the chosen provider for a TTL."""
    simulate_latency()
    intent = ctx.need(StepName.plan)
    best = ctx.need(StepName.select_best_rate)
    rng = ctx.rng("lock_quote")

    sendable_cents = intent["amount_usd_cents"] - best["fee_usd_cents"]
    pesewas, remainder = usd_to_ghs(sendable_cents, best["rate_bps"])
    return {
        "quote_id": f"qt_{rng.getrandbits(48):012x}",
        "provider": best["provider"],
        "rate_bps": best["rate_bps"],
        "locked_ttl_seconds": 900,
        "debit_usd_cents": intent["amount_usd_cents"],
        "fee_usd_cents": best["fee_usd_cents"],
        "sendable_usd_cents": sendable_cents,
        "payout_pesewas": pesewas,
        "payout_display": fmt_ghs(pesewas),
        # Sub-pesewa dust dropped by the floor division, carried explicitly so
        # the receipt reconciles to the last unit.
        "rounding_remainder_bps_units": remainder,
    }


def execute_transfer(ctx: ToolContext) -> dict[str, Any]:
    """Move the money. Irreversible in the real world."""
    simulate_latency()
    quote = ctx.need(StepName.lock_quote)
    recipient = ctx.need(StepName.validate_recipient)
    rng = ctx.rng("execute_transfer")
    return {
        "transfer_id": f"trf_{rng.getrandbits(48):012x}",
        "quote_id": quote["quote_id"],
        "provider": quote["provider"],
        "recipient_id": recipient["recipient_id"],
        "debited_usd_cents": quote["debit_usd_cents"],
        "credited_pesewas": quote["payout_pesewas"],
        "settlement": "instant",
        "state": "settled",
    }


def write_receipt(ctx: ToolContext) -> dict[str, Any]:
    """Assemble the final artifact from the cached step outputs."""
    simulate_latency()
    intent = ctx.need(StepName.plan)
    recipient = ctx.need(StepName.validate_recipient)
    quote = ctx.need(StepName.lock_quote)
    transfer = ctx.need(StepName.execute_transfer)

    # Reconciliation, in integers: debit = sendable + fee, exactly.
    assert quote["sendable_usd_cents"] + quote["fee_usd_cents"] == quote["debit_usd_cents"]

    return {
        "transfer_id": transfer["transfer_id"],
        "recipient": {
            "name": recipient["recipient_name"],
            "msisdn": recipient["masked_msisdn"],
            "network": recipient["payout_network"],
            "country": recipient["country"],
        },
        "sent": {
            "usd_cents": quote["debit_usd_cents"],
            "display": fmt_usd(quote["debit_usd_cents"]),
        },
        "fee": {
            "usd_cents": quote["fee_usd_cents"],
            "display": fmt_usd(quote["fee_usd_cents"]),
        },
        "received": {
            "pesewas": transfer["credited_pesewas"],
            "display": fmt_ghs(transfer["credited_pesewas"]),
            "currency": intent["dest_currency"],
        },
        "rate": {
            "bps": quote["rate_bps"],
            "display": f"1 USD = {fmt_rate(quote['rate_bps'])} GHS",
            "provider": quote["provider"],
        },
        "state": transfer["state"],
    }


TOOLS: dict[StepName, Callable[[ToolContext], dict[str, Any]]] = {
    StepName.plan: plan,
    StepName.validate_recipient: validate_recipient,
    StepName.fetch_fx_rates: fetch_fx_rates,
    StepName.select_best_rate: select_best_rate,
    StepName.lock_quote: lock_quote,
    StepName.execute_transfer: execute_transfer,
    StepName.write_receipt: write_receipt,
}

assert set(TOOLS) == set(StepName), "every step must have an implementation"
