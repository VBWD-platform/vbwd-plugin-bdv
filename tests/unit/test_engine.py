"""Turn-loop state machine: phases, guards, fee flow, bribes, bankruptcy."""
import dataclasses

import pytest

from plugins.bdv.bdv.core.engine import (
    Action,
    ActionType,
    ConstraintViolationError,
    IllegalActionError,
    MatchConfig,
    apply,
    new_match,
    options_for,
)
from plugins.bdv.bdv.core.state import ConstraintKind, Phase, SeatState


@pytest.fixture
def config():
    return MatchConfig(seed="seed-1", seat_count=3)


def roll(state, spec, config, seat=0):
    return apply(state, spec, config, Action(ActionType.ROLL, seat)).state


class TestPhases:
    def test_new_match_awaits_a_roll(self, tiny_spec, config):
        state = new_match(tiny_spec, config)
        assert state.phase == Phase.AWAIT_ROLL
        assert all(s.cash == tiny_spec.starting_cash for s in state.seats)

    def test_roll_moves_to_negotiation(self, tiny_spec, config):
        state = roll(new_match(tiny_spec, config), tiny_spec, config)
        assert state.phase == Phase.NEGOTIATE
        assert state.pending_roll is not None

    def test_closing_negotiation_moves_to_choice(self, tiny_spec, config):
        state = roll(new_match(tiny_spec, config), tiny_spec, config)
        state = apply(
            state, tiny_spec, config, Action(ActionType.OPEN_NEGOTIATION, 0)
        ).state
        assert state.phase == Phase.AWAIT_CHOICE

    def test_cannot_choose_before_rolling(self, tiny_spec, config):
        state = new_match(tiny_spec, config)
        with pytest.raises(IllegalActionError):
            apply(
                state,
                tiny_spec,
                config,
                Action(ActionType.CHOOSE_OPTION, 0, {"steps": 3}),
            )

    def test_seat_cannot_act_out_of_turn(self, tiny_spec, config):
        state = new_match(tiny_spec, config)
        with pytest.raises(IllegalActionError):
            apply(state, tiny_spec, config, Action(ActionType.ROLL, 1))

    def test_unknown_action_type_is_rejected(self, tiny_spec, config):
        state = new_match(tiny_spec, config)
        with pytest.raises(IllegalActionError):
            apply(state, tiny_spec, config, Action("teleport", 0))


class TestChoosingAnOption:
    def test_illegal_step_count_is_rejected(self, tiny_spec, config):
        state = roll(new_match(tiny_spec, config), tiny_spec, config)
        with pytest.raises(IllegalActionError):
            apply(
                state,
                tiny_spec,
                config,
                Action(ActionType.CHOOSE_OPTION, 0, {"steps": 99}),
            )

    def test_taking_the_sum_is_always_free(self, tiny_spec, config):
        state = roll(new_match(tiny_spec, config), tiny_spec, config)
        fate = sum(state.pending_roll)
        before = state.seat(0).cash
        result = apply(
            state,
            tiny_spec,
            config,
            Action(ActionType.CHOOSE_OPTION, 0, {"steps": fate}),
        )
        purchase_events = [e for e in result.events if e["type"] == "option_purchased"]
        assert not purchase_events
        assert result.state.seat(0).cash >= before, "no fee charged for fate"

    def test_over_cap_purchase_is_rejected(self, worked_example_spec):
        cfg = MatchConfig(seed="s", seat_count=3)
        state = new_match(worked_example_spec, cfg)
        state = dataclasses.replace(
            state,
            seats=(
                SeatState(index=0, cash=1000, position=34),
                SeatState(index=1, cash=8000, position=5),
                SeatState(index=2, cash=3000, position=12),
            ),
            ownership={39: 1, 37: 0},
            houses={39: 1},
            pending_roll=(2, 3),
            phase=Phase.AWAIT_CHOICE,
        )
        with pytest.raises(IllegalActionError, match="cap"):
            apply(
                state,
                worked_example_spec,
                cfg,
                Action(ActionType.CHOOSE_OPTION, 0, {"steps": 2}),
            )


class TestFeeFlowEndToEnd:
    """The mechanic that defines the game: the fee reaches an opponent, and the
    bank gets nothing."""

    def _positioned(self, spec, cfg, policy_state=None):
        state = new_match(spec, cfg)
        return dataclasses.replace(
            state,
            seats=(
                SeatState(index=0, cash=10000, position=34),
                SeatState(index=1, cash=8000, position=5),
                SeatState(index=2, cash=3000, position=12),
            ),
            ownership={39: 1, 37: 0},
            houses={39: 1},
            pending_roll=(2, 3),
            phase=Phase.AWAIT_CHOICE,
        )

    def test_fee_goes_to_the_poorest_opponent(self, worked_example_spec):
        cfg = MatchConfig(seed="s", seat_count=3)
        state = self._positioned(worked_example_spec, cfg)
        total_before = sum(s.cash for s in state.seats)

        result = apply(
            state,
            worked_example_spec,
            cfg,
            Action(ActionType.CHOOSE_OPTION, 0, {"steps": 2}),
        )
        after = result.state

        assert after.seat(0).cash == 10000 - 600, "mover paid 600"
        assert after.seat(2).cash == 3000 + 600, "poorest received all of it"
        assert after.seat(1).cash == 8000, "the middle seat is untouched"
        assert (
            sum(s.cash for s in after.seats) == total_before
        ), "the fee is a TRANSFER — the bank creates and destroys nothing"

    def test_split_policy_divides_between_opponents(self, worked_example_spec):
        spec = dataclasses.replace(
            worked_example_spec, fee_policy="split_among_opponents"
        )
        cfg = MatchConfig(seed="s", seat_count=3)
        state = self._positioned(spec, cfg)
        result = apply(
            state, spec, cfg, Action(ActionType.CHOOSE_OPTION, 0, {"steps": 2})
        )
        assert result.state.seat(1).cash == 8000 + 300
        assert result.state.seat(2).cash == 3000 + 300

    def test_purchase_event_records_the_derivation(self, worked_example_spec):
        cfg = MatchConfig(seed="s", seat_count=3)
        state = self._positioned(worked_example_spec, cfg)
        result = apply(
            state,
            worked_example_spec,
            cfg,
            Action(ActionType.CHOOSE_OPTION, 0, {"steps": 2}),
        )
        event = next(e for e in result.events if e["type"] == "option_purchased")
        assert event["price"] == 600
        assert event["ev_delta"] == 1200, "price = k_price x ev_delta, auditable"
        assert event["policy"] == "all_to_poorest"


class TestBribeToFate:
    def _negotiating(self, spec, cfg):
        state = new_match(spec, cfg)
        state = dataclasses.replace(
            state,
            seats=(
                SeatState(index=0, cash=10000, position=34),
                SeatState(index=1, cash=8000, position=5),
                SeatState(index=2, cash=3000, position=12),
            ),
            ownership={39: 1, 37: 0},
            houses={39: 1},
            pending_roll=(2, 3),
            phase=Phase.NEGOTIATE,
        )
        return state

    def test_escrow_then_accept_moves_the_money_and_binds_the_turn(
        self, worked_example_spec
    ):
        """The offer escrows up front (S146-3), so accept only credits the mover."""
        cfg = MatchConfig(seed="s", seat_count=3)
        state = self._negotiating(worked_example_spec, cfg)
        state = apply(
            state,
            worked_example_spec,
            cfg,
            Action(ActionType.ESCROW_BRIBE, 1, {"amount": 250}),
        ).state
        assert state.seat(1).cash == 7750, "held out of play at offer time"

        result = apply(
            state,
            worked_example_spec,
            cfg,
            Action(ActionType.ACCEPT_BRIBE, 0, {"from_seat": 1, "amount": 250}),
        )
        assert result.state.seat(0).cash == 10250
        assert result.state.seat(1).cash == 7750, "not debited twice"
        assert result.state.has_constraint(ConstraintKind.FORCED_SUM, 0)

    def test_a_declined_offer_refunds_the_escrow(self, worked_example_spec):
        cfg = MatchConfig(seed="s", seat_count=3)
        state = self._negotiating(worked_example_spec, cfg)
        state = apply(
            state,
            worked_example_spec,
            cfg,
            Action(ActionType.ESCROW_BRIBE, 1, {"amount": 250}),
        ).state
        result = apply(
            state,
            worked_example_spec,
            cfg,
            Action(ActionType.REFUND_BRIBE, 1, {"amount": 250}),
        )
        assert result.state.seat(1).cash == 8000, "made whole again"

    def test_the_engine_enforces_the_contract_not_a_route_guard(
        self, worked_example_spec
    ):
        cfg = MatchConfig(seed="s", seat_count=3)
        state = self._negotiating(worked_example_spec, cfg)
        state = apply(
            state,
            worked_example_spec,
            cfg,
            Action(ActionType.ACCEPT_BRIBE, 0, {"from_seat": 1, "amount": 250}),
        ).state
        with pytest.raises(ConstraintViolationError):
            apply(
                state,
                worked_example_spec,
                cfg,
                Action(ActionType.CHOOSE_OPTION, 0, {"steps": 2}),
            )

    def test_the_bound_sum_is_still_playable(self, worked_example_spec):
        cfg = MatchConfig(seed="s", seat_count=3)
        state = self._negotiating(worked_example_spec, cfg)
        state = apply(
            state,
            worked_example_spec,
            cfg,
            Action(ActionType.ACCEPT_BRIBE, 0, {"from_seat": 1, "amount": 250}),
        ).state
        result = apply(
            state,
            worked_example_spec,
            cfg,
            Action(ActionType.CHOOSE_OPTION, 0, {"steps": 5}),
        )
        assert result.state.seat(0).position == 39

    def test_cannot_accept_two_bribes(self, worked_example_spec):
        cfg = MatchConfig(seed="s", seat_count=3)
        state = self._negotiating(worked_example_spec, cfg)
        state = apply(
            state,
            worked_example_spec,
            cfg,
            Action(ActionType.ACCEPT_BRIBE, 0, {"from_seat": 1, "amount": 250}),
        ).state
        with pytest.raises(IllegalActionError):
            apply(
                state,
                worked_example_spec,
                cfg,
                Action(ActionType.ACCEPT_BRIBE, 0, {"from_seat": 2, "amount": 100}),
            )

    def test_you_cannot_escrow_what_you_do_not_have(self, worked_example_spec):
        """Affordability is now checked at OFFER time — that is the point of the
        escrow: an offer cannot evaporate before it is answered."""
        cfg = MatchConfig(seed="s", seat_count=3)
        state = self._negotiating(worked_example_spec, cfg)
        with pytest.raises(IllegalActionError, match="cannot afford"):
            apply(
                state,
                worked_example_spec,
                cfg,
                Action(ActionType.ESCROW_BRIBE, 2, {"amount": 99999}),
            )

    def test_a_seat_cannot_bribe_itself(self, worked_example_spec):
        cfg = MatchConfig(seed="s", seat_count=3)
        state = self._negotiating(worked_example_spec, cfg)
        with pytest.raises(IllegalActionError):
            apply(
                state,
                worked_example_spec,
                cfg,
                Action(ActionType.ACCEPT_BRIBE, 0, {"from_seat": 0, "amount": 10}),
            )

    def test_constraints_clear_at_end_of_turn(self, worked_example_spec):
        cfg = MatchConfig(seed="s", seat_count=3)
        state = self._negotiating(worked_example_spec, cfg)
        state = apply(
            state,
            worked_example_spec,
            cfg,
            Action(ActionType.ACCEPT_BRIBE, 0, {"from_seat": 1, "amount": 250}),
        ).state
        state = apply(
            state,
            worked_example_spec,
            cfg,
            Action(ActionType.CHOOSE_OPTION, 0, {"steps": 5}),
        ).state
        # +5 lands on seat 1's Enterprise Renewal, which now raises a rent
        # demand (S146-9) — settle it before the turn can end.
        if state.pending_demand is not None:
            state = apply(
                state, worked_example_spec, cfg, Action(ActionType.AGREE_TO_PAY, 0)
            ).state
        state = apply(
            state, worked_example_spec, cfg, Action(ActionType.END_TURN, 0)
        ).state
        assert state.constraints == ()
        assert state.turn_seat == 1


class TestPropertyAndRent:
    def test_landing_on_an_unowned_square_offers_a_purchase(self, tiny_spec, config):
        state = new_match(tiny_spec, config)
        state = dataclasses.replace(
            state, pending_roll=(1, 2), phase=Phase.AWAIT_CHOICE
        )
        result = apply(
            state, tiny_spec, config, Action(ActionType.CHOOSE_OPTION, 0, {"steps": 1})
        )
        assert any(e["type"] == "purchase_offered" for e in result.events)

    def test_buying_transfers_cash_and_ownership(self, tiny_spec, config):
        state = new_match(tiny_spec, config)
        state = state.with_seat(SeatState(index=0, cash=2000, position=1))
        result = apply(state, tiny_spec, config, Action(ActionType.BUY_PROPERTY, 0))
        assert result.state.owner_of(1) == 0
        assert result.state.seat(0).cash == 1400

    def test_cannot_buy_an_owned_square(self, tiny_spec, config):
        state = new_match(tiny_spec, config).with_ownership(1, 1)
        state = state.with_seat(SeatState(index=0, cash=2000, position=1))
        with pytest.raises(IllegalActionError):
            apply(state, tiny_spec, config, Action(ActionType.BUY_PROPERTY, 0))

    def test_landing_on_an_owned_square_raises_a_demand(self, tiny_spec, config):
        """Rent is a DEMAND now — it must not debit silently (S146-9)."""
        state = new_match(tiny_spec, config).with_ownership(3, 1)
        state = dataclasses.replace(
            state, pending_roll=(1, 2), phase=Phase.AWAIT_CHOICE
        )
        before = [s.cash for s in state.seats]
        result = apply(
            state, tiny_spec, config, Action(ActionType.CHOOSE_OPTION, 0, {"steps": 3})
        )
        assert any(e["type"] == "rent_demanded" for e in result.events)
        assert result.state.pending_demand is not None
        assert result.state.phase == Phase.AWAIT_RENT
        assert [s.cash for s in result.state.seats] == before, "nothing debited yet"

    def test_agreeing_settles_the_demand_and_is_zero_sum(self, tiny_spec, config):
        state = new_match(tiny_spec, config).with_ownership(3, 1)
        state = dataclasses.replace(
            state, pending_roll=(1, 2), phase=Phase.AWAIT_CHOICE
        )
        total_before = sum(s.cash for s in state.seats)
        state = apply(
            state, tiny_spec, config, Action(ActionType.CHOOSE_OPTION, 0, {"steps": 3})
        ).state
        result = apply(state, tiny_spec, config, Action(ActionType.AGREE_TO_PAY, 0))
        rent_event = next(e for e in result.events if e["type"] == "paid_rent")
        assert rent_event["to"] == 1
        assert sum(s.cash for s in result.state.seats) == total_before
        assert result.state.pending_demand is None


class TestTaxJailAndSalary:
    def test_tax_square_charges_the_board_amount(self, tiny_spec, config):
        state = new_match(tiny_spec, config)
        state = dataclasses.replace(
            state, pending_roll=(2, 2), phase=Phase.AWAIT_CHOICE
        )
        result = apply(
            state, tiny_spec, config, Action(ActionType.CHOOSE_OPTION, 0, {"steps": 4})
        )
        assert result.state.seat(0).cash == tiny_spec.starting_cash - 200

    def test_goto_jail_sends_the_seat_to_jail(self, tiny_spec, config):
        state = new_match(tiny_spec, config)
        state = dataclasses.replace(
            state, pending_roll=(4, 5), phase=Phase.AWAIT_CHOICE
        )
        result = apply(
            state, tiny_spec, config, Action(ActionType.CHOOSE_OPTION, 0, {"steps": 9})
        )
        assert result.state.seat(0).in_jail is True
        assert result.state.seat(0).position == 8

    def test_passing_go_pays_the_salary(self, tiny_spec, config):
        state = new_match(tiny_spec, config)
        state = state.with_seat(SeatState(index=0, cash=2000, position=8))
        state = dataclasses.replace(
            state, pending_roll=(1, 2), phase=Phase.AWAIT_CHOICE
        )
        result = apply(
            state, tiny_spec, config, Action(ActionType.CHOOSE_OPTION, 0, {"steps": 3})
        )
        assert result.state.seat(0).position == 1
        assert result.state.seat(0).cash == 2000 + tiny_spec.go_salary


class TestBankruptcyAndMatchEnd:
    def test_a_seat_with_nothing_left_can_declare_bankruptcy_on_a_demand(
        self, tiny_spec, config
    ):
        """Bankruptcy is now a CHOICE at the end of the solvency ladder, not an
        automatic consequence of landing on an expensive square (S146-10)."""
        state = new_match(tiny_spec, config).with_ownership(3, 1)
        state = state.with_seat(SeatState(index=0, cash=5, position=0))
        state = state.with_houses(3, 4)
        state = dataclasses.replace(
            state, pending_roll=(1, 2), phase=Phase.AWAIT_CHOICE
        )
        state = apply(
            state, tiny_spec, config, Action(ActionType.CHOOSE_OPTION, 0, {"steps": 3})
        ).state
        assert state.pending_demand is not None, "a demand, not an instant bust"
        # Seat 0 owns nothing, so there is nothing to sell or pledge.
        result = apply(state, tiny_spec, config, Action(ActionType.DECLARE_BANKRUPT, 0))
        assert result.state.seat(0).bankrupt is True

    def test_last_solvent_seat_wins(self, tiny_spec, config):
        state = new_match(tiny_spec, config)
        state = apply(
            state, tiny_spec, config, Action(ActionType.DECLARE_BANKRUPT, 0)
        ).state
        assert state.phase != Phase.FINISHED, "two seats still solvent"
        state = apply(
            state, tiny_spec, config, Action(ActionType.DECLARE_BANKRUPT, 1)
        ).state
        assert state.phase == Phase.FINISHED
        assert state.winner_seat == 2

    def test_no_actions_accepted_after_the_match_ends(self, tiny_spec, config):
        state = new_match(tiny_spec, config)
        state = apply(
            state, tiny_spec, config, Action(ActionType.DECLARE_BANKRUPT, 0)
        ).state
        state = apply(
            state, tiny_spec, config, Action(ActionType.DECLARE_BANKRUPT, 1)
        ).state
        with pytest.raises(IllegalActionError):
            apply(state, tiny_spec, config, Action(ActionType.ROLL, 2))


class TestTurnOrder:
    def test_turn_advances_past_bankrupt_seats(self, tiny_spec, config):
        state = new_match(tiny_spec, config)
        state = state.with_seat(dataclasses.replace(state.seat(1), bankrupt=True))
        state = apply(state, tiny_spec, config, Action(ActionType.END_TURN, 0)).state
        assert state.turn_seat == 2

    def test_skip_next_turn_is_consumed(self, tiny_spec, config):
        state = new_match(tiny_spec, config)
        state = state.with_seat(dataclasses.replace(state.seat(0), skip_next_turn=True))
        result = apply(state, tiny_spec, config, Action(ActionType.ROLL, 0))
        assert result.state.seat(0).skip_next_turn is False
        assert result.state.turn_seat == 1


class TestImmutability:
    def test_apply_never_mutates_the_input_state(self, tiny_spec, config):
        state = new_match(tiny_spec, config)
        before = state.state_hash()
        apply(state, tiny_spec, config, Action(ActionType.ROLL, 0))
        assert state.state_hash() == before

    def test_options_are_empty_without_a_pending_roll(self, tiny_spec, config):
        assert options_for(new_match(tiny_spec, config), tiny_spec) == ()
