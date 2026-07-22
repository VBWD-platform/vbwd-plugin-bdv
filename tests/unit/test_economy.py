"""Rent demands, solvency, buildings and transfers (S146-9 … S146-12)."""
import dataclasses

import pytest

from plugins.bdv.bdv.core import economy
from plugins.bdv.bdv.core.engine import (
    Action,
    ActionType,
    IllegalActionError,
    MatchConfig,
    apply,
    new_match,
)
from plugins.bdv.bdv.core.state import Phase, RentDemand, SeatState


@pytest.fixture
def config():
    return MatchConfig(seed="econ", seat_count=3)


@pytest.fixture
def owned(tiny_spec, config):
    """Seat 0 landed on seat 1's square with a demand outstanding."""
    state = new_match(tiny_spec, config).with_ownership(3, 1)
    state = dataclasses.replace(state, pending_roll=(1, 2), phase=Phase.AWAIT_CHOICE)
    return apply(
        state, tiny_spec, config, Action(ActionType.CHOOSE_OPTION, 0, {"steps": 3})
    ).state


# ------------------------------------------------------------------ S146-9


class TestRentDemand:
    def test_a_demand_is_raised_and_nothing_is_debited(self, owned):
        assert owned.pending_demand is not None
        assert owned.phase == Phase.AWAIT_RENT
        assert owned.seat(0).cash == 2000

    def test_the_turn_cannot_end_while_a_demand_stands(self, owned, tiny_spec, config):
        with pytest.raises(IllegalActionError, match="settle the rent"):
            apply(owned, tiny_spec, config, Action(ActionType.END_TURN, 0))

    def test_agreeing_pays_the_full_rent(self, owned, tiny_spec, config):
        due = owned.pending_demand.amount
        result = apply(owned, tiny_spec, config, Action(ActionType.AGREE_TO_PAY, 0))
        assert result.state.seat(0).cash == 2000 - due
        assert result.state.seat(1).cash == 2000 + due
        assert result.state.pending_demand is None

    def test_auto_agree_is_identical_and_flagged(self, owned, tiny_spec, config):
        manual = apply(owned, tiny_spec, config, Action(ActionType.AGREE_TO_PAY, 0))
        auto = apply(owned, tiny_spec, config, Action(ActionType.RENT_AUTO_AGREED, 0))
        assert auto.state.state_hash() == manual.state.state_hash()
        assert auto.events[0]["auto"] is True

    def test_only_the_debtor_may_agree(self, owned, tiny_spec, config):
        with pytest.raises(IllegalActionError, match="only the debtor"):
            apply(owned, tiny_spec, config, Action(ActionType.AGREE_TO_PAY, 1))

    @pytest.mark.parametrize("offer", [0, -5])
    def test_a_counter_must_be_above_zero(self, owned, tiny_spec, config, offer):
        with pytest.raises(IllegalActionError):
            apply(
                owned,
                tiny_spec,
                config,
                Action(ActionType.OFFER_RENT, 0, {"amount": offer}),
            )

    def test_a_counter_must_be_below_the_rent(self, owned, tiny_spec, config):
        full = owned.pending_demand.amount
        with pytest.raises(IllegalActionError):
            apply(
                owned,
                tiny_spec,
                config,
                Action(ActionType.OFFER_RENT, 0, {"amount": full}),
            )

    def test_one_counter_per_demand(self, owned, tiny_spec, config):
        state = apply(
            owned, tiny_spec, config, Action(ActionType.OFFER_RENT, 0, {"amount": 5})
        ).state
        with pytest.raises(IllegalActionError, match="already made a counter"):
            apply(
                state,
                tiny_spec,
                config,
                Action(ActionType.OFFER_RENT, 0, {"amount": 6}),
            )

    def test_the_owner_can_accept_the_counter(self, owned, tiny_spec, config):
        state = apply(
            owned, tiny_spec, config, Action(ActionType.OFFER_RENT, 0, {"amount": 5})
        ).state
        result = apply(
            state, tiny_spec, config, Action(ActionType.ACCEPT_RENT_OFFER, 1)
        )
        assert result.state.seat(0).cash == 2000 - 5
        assert result.state.seat(1).cash == 2000 + 5
        assert result.events[0]["negotiated"] is True

    def test_the_owner_can_insist_and_the_answer_is_final(
        self, owned, tiny_spec, config
    ):
        full = owned.pending_demand.amount
        state = apply(
            owned, tiny_spec, config, Action(ActionType.OFFER_RENT, 0, {"amount": 5})
        ).state
        state = apply(
            state, tiny_spec, config, Action(ActionType.INSIST_ON_FULL_RENT, 1)
        ).state
        assert state.pending_demand.due == full
        with pytest.raises(IllegalActionError, match="already made a counter"):
            apply(
                state,
                tiny_spec,
                config,
                Action(ActionType.OFFER_RENT, 0, {"amount": 7}),
            )

    def test_only_the_owner_may_accept_or_insist(self, owned, tiny_spec, config):
        state = apply(
            owned, tiny_spec, config, Action(ActionType.OFFER_RENT, 0, {"amount": 5})
        ).state
        with pytest.raises(IllegalActionError, match="only the owner"):
            apply(state, tiny_spec, config, Action(ActionType.ACCEPT_RENT_OFFER, 0))

    def test_a_debtor_who_cannot_pay_is_told_to_raise_cash(self, tiny_spec, config):
        state = new_match(tiny_spec, config).with_ownership(3, 1)
        state = state.with_seat(SeatState(index=0, cash=1, position=0))
        state = state.with_houses(3, 4)
        state = dataclasses.replace(
            state, pending_roll=(1, 2), phase=Phase.AWAIT_CHOICE
        )
        state = apply(
            state, tiny_spec, config, Action(ActionType.CHOOSE_OPTION, 0, {"steps": 3})
        ).state
        with pytest.raises(IllegalActionError, match="short"):
            apply(state, tiny_spec, config, Action(ActionType.AGREE_TO_PAY, 0))

    def test_a_demand_never_outlives_its_turn(self, owned, tiny_spec, config):
        """A later seat must never inherit a debt it did not incur."""
        state = apply(
            owned, tiny_spec, config, Action(ActionType.DECLARE_BANKRUPT, 0)
        ).state
        assert state.pending_demand is None


# ----------------------------------------------------------------- S146-10


class TestSellingAndBorrowing:
    def _owner(self, tiny_spec, config):
        state = new_match(tiny_spec, config)
        return state.with_ownership(1, 0).with_ownership(3, 0)

    def test_selling_a_square_returns_its_mortgage_value_and_unowns_it(
        self, tiny_spec, config
    ):
        state = self._owner(tiny_spec, config)
        before = state.seat(0).cash
        result = apply(
            state, tiny_spec, config, Action(ActionType.SELL_SQUARE, 0, {"square": 1})
        )
        assert result.state.owner_of(1) is None
        assert result.state.seat(0).cash == before + economy.square_value(tiny_spec, 1)

    def test_borrowing_advances_cash_and_locks_the_collateral(self, tiny_spec, config):
        state = self._owner(tiny_spec, config)
        before = state.seat(0).cash
        result = apply(
            state,
            tiny_spec,
            config,
            Action(ActionType.BORROW, 0, {"squares": [1], "amount": 100}),
        )
        assert result.state.seat(0).cash == before + 100
        assert result.state.debt_of(0) == 100
        assert 1 in result.state.pledged_squares(0)

    def test_a_pledged_square_cannot_be_sold(self, tiny_spec, config):
        state = self._owner(tiny_spec, config)
        state = apply(
            state,
            tiny_spec,
            config,
            Action(ActionType.BORROW, 0, {"squares": [1], "amount": 100}),
        ).state
        with pytest.raises(economy.EconomyError, match="pledged"):
            apply(
                state,
                tiny_spec,
                config,
                Action(ActionType.SELL_SQUARE, 0, {"square": 1}),
            )

    def test_the_advance_is_capped_by_loan_to_value(self, tiny_spec, config):
        state = self._owner(tiny_spec, config)
        ceiling = economy.square_value(tiny_spec, 1) * config.loan_to_value // 10_000
        with pytest.raises(economy.EconomyError, match="cap"):
            apply(
                state,
                tiny_spec,
                config,
                Action(ActionType.BORROW, 0, {"squares": [1], "amount": ceiling + 1}),
            )

    def test_you_cannot_pledge_what_you_do_not_own(self, tiny_spec, config):
        state = self._owner(tiny_spec, config)
        with pytest.raises(economy.EconomyError, match="only pledge"):
            apply(
                state,
                tiny_spec,
                config,
                Action(ActionType.BORROW, 0, {"squares": [6], "amount": 10}),
            )

    def test_repaying_reduces_and_then_clears_the_loan(self, tiny_spec, config):
        state = self._owner(tiny_spec, config)
        state = apply(
            state,
            tiny_spec,
            config,
            Action(ActionType.BORROW, 0, {"squares": [1], "amount": 100}),
        ).state
        loan_id = state.loans[0].loan_id
        state = apply(
            state,
            tiny_spec,
            config,
            Action(ActionType.REPAY_LOAN, 0, {"loan_id": loan_id, "amount": 40}),
        ).state
        assert state.debt_of(0) == 60
        state = apply(
            state,
            tiny_spec,
            config,
            Action(ActionType.REPAY_LOAN, 0, {"loan_id": loan_id, "amount": 60}),
        ).state
        assert state.debt_of(0) == 0 and state.loans == ()

    def test_overpaying_is_rejected(self, tiny_spec, config):
        state = self._owner(tiny_spec, config)
        state = apply(
            state,
            tiny_spec,
            config,
            Action(ActionType.BORROW, 0, {"squares": [1], "amount": 100}),
        ).state
        with pytest.raises(economy.EconomyError, match="exceeds"):
            apply(
                state,
                tiny_spec,
                config,
                Action(
                    ActionType.REPAY_LOAN,
                    0,
                    {"loan_id": state.loans[0].loan_id, "amount": 101},
                ),
            )


class TestInterestIsOneLap:
    def test_interest_is_charged_on_passing_go(self, tiny_spec, config):
        state = new_match(tiny_spec, config).with_ownership(1, 0)
        state = apply(
            state,
            tiny_spec,
            config,
            Action(ActionType.BORROW, 0, {"squares": [1], "amount": 100}),
        ).state
        # Park near the end of the board so the next move passes GO.
        state = state.with_seat(dataclasses.replace(state.seat(0), position=8))
        state = dataclasses.replace(
            state, pending_roll=(1, 2), phase=Phase.AWAIT_CHOICE
        )

        before = state.seat(0).cash
        result = apply(
            state, tiny_spec, config, Action(ActionType.CHOOSE_OPTION, 0, {"steps": 3})
        )
        interest = next(e for e in result.events if e["type"] == "interest_charged")
        assert interest["amount"] == 10, "10% of 100, rounded up"
        assert result.state.seat(0).cash == before + tiny_spec.go_salary - 10

    def test_no_interest_without_debt(self, tiny_spec, config):
        state = new_match(tiny_spec, config)
        state = state.with_seat(dataclasses.replace(state.seat(0), position=8))
        state = dataclasses.replace(
            state, pending_roll=(1, 2), phase=Phase.AWAIT_CHOICE
        )
        result = apply(
            state, tiny_spec, config, Action(ActionType.CHOOSE_OPTION, 0, {"steps": 3})
        )
        assert not [e for e in result.events if e["type"] == "interest_charged"]

    def test_interest_rounds_up_so_a_tiny_debt_still_costs(self):
        from plugins.bdv.bdv.core.state import Loan, MatchState

        state = MatchState(
            seats=(SeatState(index=0, cash=100),),
            ownership={},
            houses={},
            loans=(Loan(1, 0, 1, 1, (), 1000),),
        )
        charged, events = economy.charge_interest(state, 0)
        assert events[0]["amount"] == 1
        assert charged.seat(0).cash == 99


# ----------------------------------------------------------------- S146-11


class TestBuildings:
    def _full_stage(self, tiny_spec, config):
        """Seat 0 owns the whole lead_gen stage (squares 1 and 3)."""
        state = new_match(tiny_spec, config)
        return state.with_ownership(1, 0).with_ownership(3, 0)

    def test_building_needs_the_whole_stage(self, tiny_spec, config):
        state = new_match(tiny_spec, config).with_ownership(1, 0)
        with pytest.raises(economy.EconomyError, match="whole funnel stage"):
            apply(
                state,
                tiny_spec,
                config,
                Action(ActionType.BUILD_HOUSE, 0, {"square": 1}),
            )

    def test_building_on_a_full_stage_works(self, tiny_spec, config):
        state = self._full_stage(tiny_spec, config)
        before = state.seat(0).cash
        result = apply(
            state, tiny_spec, config, Action(ActionType.BUILD_HOUSE, 0, {"square": 1})
        )
        assert result.state.houses_on(1) == 1
        assert result.state.seat(0).cash == before - tiny_spec.square(1).house_cost

    def test_even_building_is_enforced(self, tiny_spec, config):
        state = self._full_stage(tiny_spec, config)
        state = apply(
            state, tiny_spec, config, Action(ActionType.BUILD_HOUSE, 0, {"square": 1})
        ).state
        with pytest.raises(economy.EconomyError, match="evenly"):
            apply(
                state,
                tiny_spec,
                config,
                Action(ActionType.BUILD_HOUSE, 0, {"square": 1}),
            )

    def test_even_selling_is_enforced(self, tiny_spec, config):
        state = self._full_stage(tiny_spec, config)
        for square in (1, 3):
            state = apply(
                state,
                tiny_spec,
                config,
                Action(ActionType.BUILD_HOUSE, 0, {"square": square}),
            ).state
        state = apply(
            state, tiny_spec, config, Action(ActionType.BUILD_HOUSE, 0, {"square": 1})
        ).state  # 1 now has 2, 3 has 1
        with pytest.raises(economy.EconomyError, match="evenly"):
            apply(
                state,
                tiny_spec,
                config,
                Action(ActionType.SELL_HOUSE, 0, {"square": 3}),
            )

    def test_selling_a_house_refunds_half(self, tiny_spec, config):
        state = self._full_stage(tiny_spec, config)
        state = apply(
            state, tiny_spec, config, Action(ActionType.BUILD_HOUSE, 0, {"square": 1})
        ).state
        before = state.seat(0).cash
        result = apply(
            state, tiny_spec, config, Action(ActionType.SELL_HOUSE, 0, {"square": 1})
        )
        assert result.state.seat(0).cash == before + tiny_spec.square(1).house_cost // 2

    def test_a_square_with_buildings_cannot_be_sold(self, tiny_spec, config):
        state = self._full_stage(tiny_spec, config)
        state = apply(
            state, tiny_spec, config, Action(ActionType.BUILD_HOUSE, 0, {"square": 1})
        ).state
        with pytest.raises(economy.EconomyError, match="buildings first"):
            apply(
                state,
                tiny_spec,
                config,
                Action(ActionType.SELL_SQUARE, 0, {"square": 1}),
            )

    def test_building_is_only_legal_before_the_roll(self, tiny_spec, config):
        """Building after the roll would be a hedge, not a bet."""
        state = self._full_stage(tiny_spec, config)
        state = dataclasses.replace(
            state, phase=Phase.AWAIT_CHOICE, pending_roll=(1, 2)
        )
        with pytest.raises(IllegalActionError, match="not allowed in phase"):
            apply(
                state,
                tiny_spec,
                config,
                Action(ActionType.BUILD_HOUSE, 0, {"square": 1}),
            )

    def test_rent_responds_to_buildings(self, tiny_spec, config):
        from plugins.bdv.bdv.core.pricing import rent_due

        state = self._full_stage(tiny_spec, config)
        bare = rent_due(state, tiny_spec, 1, die_used=3)
        built = apply(
            state, tiny_spec, config, Action(ActionType.BUILD_HOUSE, 0, {"square": 1})
        ).state
        assert rent_due(built, tiny_spec, 1, die_used=3) > bare


# ----------------------------------------------------------------- S146-12


class TestTransfers:
    def test_a_transfer_is_zero_sum(self, tiny_spec, config):
        state = new_match(tiny_spec, config)
        total = sum(s.cash for s in state.seats)
        result = apply(
            state,
            tiny_spec,
            config,
            Action(ActionType.TRANSFER_CREDITS, 0, {"to_seat": 1, "amount": 250}),
        )
        assert result.state.seat(0).cash == 2000 - 250
        assert result.state.seat(1).cash == 2000 + 250
        assert sum(s.cash for s in result.state.seats) == total

    @pytest.mark.parametrize(
        "payload,message",
        [
            ({"to_seat": 1, "amount": 0}, "positive"),
            ({"to_seat": 1, "amount": -5}, "positive"),
            ({"to_seat": 0, "amount": 10}, "yourself"),
            ({"to_seat": 9, "amount": 10}, "no such seat"),
            ({"to_seat": 1, "amount": 999999}, "not enough cash"),
        ],
    )
    def test_guards(self, tiny_spec, config, payload, message):
        state = new_match(tiny_spec, config)
        with pytest.raises(IllegalActionError, match=message):
            apply(
                state,
                tiny_spec,
                config,
                Action(ActionType.TRANSFER_CREDITS, 0, payload),
            )

    def test_you_cannot_pay_a_seat_that_is_out(self, tiny_spec, config):
        state = new_match(tiny_spec, config)
        state = state.with_seat(dataclasses.replace(state.seat(1), bankrupt=True))
        with pytest.raises(IllegalActionError, match="out of the match"):
            apply(
                state,
                tiny_spec,
                config,
                Action(ActionType.TRANSFER_CREDITS, 0, {"to_seat": 1, "amount": 10}),
            )


class TestStillDeterministic:
    def test_a_match_with_the_new_economy_still_replays(self, tiny_spec):
        from plugins.bdv.bdv.agents.baseline import play_match
        from plugins.bdv.bdv.core.replay import Replay, replay

        cfg = MatchConfig(seed="econ-replay", seat_count=3)
        state, actions = play_match(tiny_spec, cfg)
        recording = Replay(
            spec_hash=tiny_spec.spec_hash(),
            seed=cfg.seed,
            seat_count=cfg.seat_count,
            actions=actions,
        )
        assert replay(tiny_spec, recording).state_hash() == state.state_hash()

    def test_state_round_trips_with_demands_and_loans(self, tiny_spec, config):
        from plugins.bdv.bdv.core.state import MatchState

        state = new_match(tiny_spec, config).with_ownership(1, 0)
        state = apply(
            state,
            tiny_spec,
            config,
            Action(ActionType.BORROW, 0, {"squares": [1], "amount": 50}),
        ).state
        state = state.with_demand(RentDemand(0, 1, 3, 120, offered=40, countered=True))
        assert MatchState.from_dict(state.to_dict()).state_hash() == state.state_hash()
