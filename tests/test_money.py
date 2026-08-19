"""Integer money and credit arithmetic. No floats anywhere in these paths."""

import pytest

from app.pricing import PIPELINE, RATE_CARD, format_mc, full_run_cost_mc
from app.tools import (
    BPS_SCALE,
    ToolContext,
    ToolError,
    _parse_amount_cents,
    net_payout_pesewas,
    plan,
    usd_to_ghs,
)


def test_conversion_is_exact_integer_math():
    # $498.01 at 15.2000 GHS/USD
    pesewas, remainder = usd_to_ghs(49_801, 152_000)
    assert (pesewas, remainder) == (756_975, 2_000)
    # the identity holds exactly, with the remainder accounted for
    assert pesewas * BPS_SCALE + remainder == 49_801 * 152_000
    assert isinstance(pesewas, int) and isinstance(remainder, int)


def test_conversion_rounds_down_never_up():
    """A payout must never exceed what the arithmetic supports."""
    for cents in range(1, 400):
        pesewas, remainder = usd_to_ghs(cents, 152_187)
        assert pesewas * BPS_SCALE <= cents * 152_187
        assert 0 <= remainder < BPS_SCALE


def test_amount_parsing_avoids_float_error():
    # float("0.1") * 100 == 10.000000000000002 -- the classic way cents vanish
    assert _parse_amount_cents("Send $0.10 to my mom in Accra") == 10
    assert _parse_amount_cents("Send $1,234.56 to Accra") == 123_456
    assert _parse_amount_cents("send 500 dollars to accra") == 50_000


@pytest.mark.parametrize("bad", ["Send money to Accra", "Send $0 to Accra"])
def test_amount_parsing_rejects_nonsense(bad):
    with pytest.raises(ToolError):
        _parse_amount_cents(bad)


def test_best_rate_is_net_payout_not_headline_rate():
    """The highest quoted rate is not the best deal once fees are applied."""
    amount = 50_000
    akua = net_payout_pesewas(amount, 199, 152_000)     # low rate, low fee
    volta = net_payout_pesewas(amount, 1_200, 154_500)  # top rate, high fee
    assert volta < akua, "a naive max(rate_bps) implementation would pick wrong"


def test_fee_exceeding_amount_is_an_error_not_a_negative_payout():
    with pytest.raises(ToolError):
        net_payout_pesewas(100, 1_200, 152_000)


def test_format_mc_uses_no_floats():
    assert format_mc(5_400) == "5.400"
    assert format_mc(100) == "0.100"
    assert format_mc(0) == "0.000"
    assert format_mc(-250) == "-0.250"
    assert format_mc(1_000_000) == "1000.000"


def test_rate_card_covers_pipeline_and_is_integral():
    assert set(RATE_CARD) == set(PIPELINE)
    assert all(isinstance(v, int) for v in RATE_CARD.values())
    assert full_run_cost_mc() == 5_400
    assert sum(RATE_CARD.values()) == full_run_cost_mc()


def test_tools_are_deterministic_per_run_and_salted_per_tool():
    a = ToolContext(run_id="run-x", goal="Send $500 to my mom in Accra")
    b = ToolContext(run_id="run-x", goal="Send $500 to my mom in Accra")
    assert plan(a) == plan(b)
    # different tools must not draw the identical sequence, or quote_id would
    # equal transfer_id
    assert a.rng("lock_quote").getrandbits(48) != a.rng("execute_transfer").getrandbits(48)


def test_destination_is_not_mistaken_for_a_recipient():
    """'Send $500 to Accra' names no recipient -- the city is not a person."""
    out = plan(ToolContext(run_id="r", goal="Send $500 to Accra"))
    assert out["recipient_name"] is None
    assert out["dest_country"] == "GH"
    # a real recipient still parses
    out2 = plan(ToolContext(run_id="r", goal="Send $500 to my mom in Accra"))
    assert out2["recipient_name"] == "Mom"
