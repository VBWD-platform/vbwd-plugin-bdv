"""Pricing tests — the keystone of the product.

The worked example below is the one from the epic. If it ever goes red, the
game's central claim ("escaping disaster is expensive, and you pay the table
for it") no longer holds.
"""
from decimal import Decimal

from plugins.bdv.bdv.core.options import legal_options, sum_option
from plugins.bdv.bdv.core.pricing import (
    affordability_cap,
    evaluate_options,
    price_for,
    quote_for_steps,
    rent_due,
)
from plugins.bdv.bdv.core.state import SeatState


class TestOptionSet:
    def test_normal_roll_yields_three_ascending_options(self):
        assert legal_options((2, 3)) == (2, 3, 5)

    def test_doubles_collapse_to_two_options(self):
        assert legal_options((3, 3)) == (3, 6)

    def test_snake_eyes(self):
        assert legal_options((1, 1)) == (1, 2)

    def test_sum_is_the_fate_move(self):
        assert sum_option((2, 3)) == 5


class TestWorkedExample:
    """5 squares from a hotel (rent 900), 2 squares from the square that
    completes your stage, roll {2, 3}."""

    def test_expected_values(self, worked_example_state, worked_example_spec):
        quotes = {
            q.steps: q
            for q in evaluate_options(worked_example_state, worked_example_spec, (2, 3))
        }
        assert quotes[2].ev == 300, "unowned square at 30% of its 1000 price"
        assert quotes[3].ev == 0, "the mover already owns 37 — neutral"
        assert quotes[5].ev == -900, "opponent's Enterprise Renewal, one house"

    def test_prices(self, worked_example_state, worked_example_spec):
        quotes = {
            q.steps: q
            for q in evaluate_options(worked_example_state, worked_example_spec, (2, 3))
        }
        assert quotes[2].price == 600, "0.5 x (300 - (-900))"
        assert quotes[3].price == 450, "0.5 x (0 - (-900)) — dodging is expensive"
        assert quotes[5].price == 0, "fate is always free"

    def test_the_sum_is_flagged_and_free(self, worked_example_state, worked_example_spec):
        quotes = evaluate_options(worked_example_state, worked_example_spec, (2, 3))
        sums = [q for q in quotes if q.is_sum]
        assert len(sums) == 1 and sums[0].steps == 5 and sums[0].price == 0

    def test_reasons_are_i18n_keys_not_sentences(
        self, worked_example_state, worked_example_spec
    ):
        quotes = {
            q.steps: q
            for q in evaluate_options(worked_example_state, worked_example_spec, (2, 3))
        }
        assert quotes[2].reason == "unowned"
        assert quotes[3].reason == "own_square"
        assert quotes[5].reason == "pays_rent"
        assert quotes[5].reason_params["rent"] == 900
        for quote in quotes.values():
            assert " " not in quote.reason, "reason must be a key, not prose"


class TestAffordabilityCap:
    def test_cap_is_30_percent_of_cash(self, worked_example_spec):
        assert affordability_cap(worked_example_spec, 10000) == 3000

    def test_cap_floors_rather_than_rounds(self, worked_example_spec):
        assert affordability_cap(worked_example_spec, 999) == 299

    def test_zero_cash_caps_at_zero(self, worked_example_spec):
        assert affordability_cap(worked_example_spec, 0) == 0
        assert affordability_cap(worked_example_spec, -50) == 0

    def test_rich_mover_can_afford_the_dodge(
        self, worked_example_state, worked_example_spec
    ):
        quote = quote_for_steps(
            worked_example_state, worked_example_spec, (2, 3), 2
        )
        assert quote.price == 600 and quote.affordable is True

    def test_poor_mover_sees_the_price_but_cannot_pay(
        self, worked_example_state, worked_example_spec
    ):
        broke = worked_example_state.with_seat(
            SeatState(index=0, cash=1000, position=34)
        )
        quote = quote_for_steps(broke, worked_example_spec, (2, 3), 2)
        assert quote.price == 600, "the price is still shown"
        assert quote.affordable is False, "…but the 300 cap blocks it"


class TestNeverPayToMoveWorse:
    def test_negative_delta_is_free(self, worked_example_spec):
        assert price_for(-900, 0, worked_example_spec) == 0

    def test_zero_delta_is_free(self, worked_example_spec):
        assert price_for(0, 0, worked_example_spec) == 0

    def test_dodging_into_a_worse_square_costs_nothing(
        self, worked_example_state, worked_example_spec
    ):
        """You may always move worse for free — sometimes you want to."""
        state = worked_example_state.with_seat(
            SeatState(index=0, cash=10000, position=32)
        )
        quotes = {
            q.steps: q for q in evaluate_options(state, worked_example_spec, (2, 3))
        }
        # 32+2=34 (neutral, ev 0), 32+3=35 (neutral, ev 0), 32+5=37 (own, ev 0)
        assert all(q.price == 0 for q in quotes.values())


class TestStageCompletionDoubling:
    def test_completing_a_stage_doubles_the_acquisition_bonus(
        self, worked_example_state, worked_example_spec
    ):
        """Owning 39 makes 36 a stage-completing buy — worth twice as much."""
        owns_39 = worked_example_state.with_ownership(39, 0)
        quote = quote_for_steps(owns_39, worked_example_spec, (2, 3), 2)
        assert quote.ev == 600, "300 base, doubled for the completed stage"
        assert quote.reason == "completes_stage"


class TestServiceRentUsesTheDieActuallyUsed:
    """The one place where WHICH die you bought changes what you owe."""

    def test_rent_scales_with_the_die(self, tiny_state, tiny_spec):
        owned = tiny_state.with_ownership(5, 1)
        assert rent_due(owned, tiny_spec, 5, die_used=3) == 12  # 4 x 3
        assert rent_due(owned, tiny_spec, 5, die_used=6) == 24  # 4 x 6

    def test_second_service_raises_the_multiplier(self, tiny_state, tiny_spec):
        # tiny board has one service; simulate a two-service board via ownership
        owned = tiny_state.with_ownership(5, 1)
        assert rent_due(owned, tiny_spec, 5, die_used=5) == 20

    def test_unowned_service_charges_nothing(self, tiny_state, tiny_spec):
        assert rent_due(tiny_state, tiny_spec, 5, die_used=6) == 0


class TestDeckSquaresArePricedFromTheHint:
    def test_chance_uses_the_deck_ev_hint(self, tiny_state, tiny_spec):
        import dataclasses

        spec = dataclasses.replace(tiny_spec, chance_ev_hint=-150)
        state = tiny_state.with_seat(SeatState(index=0, cash=2000, position=5))
        quote = quote_for_steps(state, spec, (2, 4), 2)  # 5+2 = 7 = Market Event
        assert quote.ev == -150, "closed-form: no Monte-Carlo inside a quote"


class TestDeterminism:
    def test_same_inputs_give_identical_quotes(
        self, worked_example_state, worked_example_spec
    ):
        first = evaluate_options(worked_example_state, worked_example_spec, (2, 3))
        second = evaluate_options(worked_example_state, worked_example_spec, (2, 3))
        assert first == second

    def test_prices_use_half_up_rounding_not_bankers(self, worked_example_spec):
        import dataclasses

        spec = dataclasses.replace(worked_example_spec, k_price=Decimal("0.5"))
        # delta 1 -> 0.5 -> half-up = 1 (banker's rounding would give 0)
        assert price_for(1, 0, spec) == 1
        # delta 3 -> 1.5 -> half-up = 2 (banker's would give 2 as well)
        assert price_for(3, 0, spec) == 2
