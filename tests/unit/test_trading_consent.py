"""Trading is a NEGOTIATION, so both sides must consent (S146-14).

The first cut of the trading window exposed ``execute_trade`` as a submittable
action that read ``from_seat`` straight out of the payload. Nothing checked that
the acting seat owned that side, so any seated player could post a trade in which
someone ELSE gave away their best square for nothing. The route could not catch
it either: it authorises WHICH SEAT you are, not what you put in the body.

The fix is a proposal in engine state — propose, then accept — so consent is a
recorded fact that replays, not a check some caller might forget.
"""
import pytest

from plugins.bdv.bdv.core import economy
from plugins.bdv.bdv.core.engine import (
    Action,
    ActionType,
    IllegalActionError,
    apply,
    new_match,
)
from plugins.bdv.bdv.core.engine import MatchConfig
from plugins.bdv.bdv.core.state import Phase


@pytest.fixture
def config():
    return MatchConfig(seed="trade", seat_count=3)


def _all_owned(spec, config):
    """Every ownable square taken: 1, 3, 6 to seat 0; 5 to seat 1."""
    state = new_match(spec, config)
    for square in spec.squares:
        if square.is_ownable and square.price:
            state = state.with_ownership(square.index, 0 if square.index != 5 else 1)
    return state


def _trading(spec, config):
    state = _all_owned(spec, config)
    return apply(state, spec, config, Action(ActionType.OPEN_TRADING, 0)).state


def _propose(state, spec, config, seat, payload):
    return apply(state, spec, config, Action(ActionType.PROPOSE_TRADE, seat, payload))


class TestNobodyCanTradeOnAnotherSeatsBehalf:
    def test_execute_trade_is_not_a_submittable_action(self):
        """The unsafe verb is GONE, not merely guarded.

        Leaving it in the enum would keep it reachable through the generic
        /actions route, which validates the type against exactly this list.
        """
        assert "execute_trade" not in set(vars(ActionType).values())

    def test_a_proposal_is_always_from_the_acting_seat(self, tiny_spec, config):
        """Seat 0 cannot author a proposal that gives away seat 1's square."""
        state = _trading(tiny_spec, config)
        with pytest.raises(economy.EconomyError, match="not theirs"):
            _propose(
                state,
                tiny_spec,
                config,
                0,
                {"from_seat": 1, "give_squares": [5], "to_seat": 1},
            )

    def test_only_the_counterparty_can_accept(self, tiny_spec, config):
        state = _trading(tiny_spec, config)
        state = _propose(
            state,
            tiny_spec,
            config,
            0,
            {"to_seat": 1, "give_squares": [1], "want_squares": [5]},
        ).state
        offer = state.trade_offers[0]
        for impostor in (0, 2):
            with pytest.raises(IllegalActionError, match="yours to answer|not a seat"):
                apply(
                    state,
                    tiny_spec,
                    config,
                    Action(ActionType.ACCEPT_TRADE, impostor, {"offer_id": offer.id}),
                )

    def test_the_square_only_moves_once_both_sides_have_spoken(self, tiny_spec, config):
        state = _trading(tiny_spec, config)
        state = _propose(
            state,
            tiny_spec,
            config,
            0,
            {"to_seat": 1, "give_squares": [1], "want_squares": [5]},
        ).state
        assert state.owner_of(1) == 0, "proposing moves nothing"
        assert state.owner_of(5) == 1

        offer = state.trade_offers[0]
        state = apply(
            state,
            tiny_spec,
            config,
            Action(ActionType.ACCEPT_TRADE, 1, {"offer_id": offer.id}),
        ).state
        assert state.owner_of(1) == 1
        assert state.owner_of(5) == 0
        assert state.trade_offers == (), "an accepted offer leaves the table"


class TestTheProposalLifecycle:
    def test_credits_and_squares_move_together_on_accept(self, tiny_spec, config):
        state = _trading(tiny_spec, config)
        total = sum(s.cash for s in state.seats)
        state = _propose(
            state, tiny_spec, config, 1, {"to_seat": 0, "give_squares": [5]}
        ).state
        # Seat 1 asks 400 credits for square 5.
        state = _trading_replace_want(state, want_credits=400)
        state = apply(
            state,
            tiny_spec,
            config,
            Action(ActionType.ACCEPT_TRADE, 0, {"offer_id": state.trade_offers[0].id}),
        ).state
        assert state.owner_of(5) == 0
        assert state.seat(1).cash == 2000 + 400
        assert state.seat(0).cash == 2000 - 400
        assert sum(s.cash for s in state.seats) == total, "credits are conserved"

    def test_declining_clears_the_offer_and_moves_nothing(self, tiny_spec, config):
        state = _trading(tiny_spec, config)
        state = _propose(
            state, tiny_spec, config, 0, {"to_seat": 1, "give_squares": [1]}
        ).state
        before = state.owner_of(1)
        result = apply(
            state,
            tiny_spec,
            config,
            Action(ActionType.DECLINE_TRADE, 1, {"offer_id": state.trade_offers[0].id}),
        )
        assert result.state.trade_offers == ()
        assert result.state.owner_of(1) == before
        assert result.events[0]["type"] == "trade_declined"

    def test_the_proposer_can_withdraw(self, tiny_spec, config):
        state = _trading(tiny_spec, config)
        state = _propose(
            state, tiny_spec, config, 0, {"to_seat": 1, "give_squares": [1]}
        ).state
        state = apply(
            state,
            tiny_spec,
            config,
            Action(ActionType.DECLINE_TRADE, 0, {"offer_id": state.trade_offers[0].id}),
        ).state
        assert state.trade_offers == ()

    def test_a_counter_replaces_the_offer_with_the_mirror(self, tiny_spec, config):
        """Counter is one action, not decline-then-propose.

        Two actions would leave a window in which the table shows nothing on
        offer, and a counter that raced a close would silently vanish.
        """
        state = _trading(tiny_spec, config)
        state = _propose(
            state,
            tiny_spec,
            config,
            0,
            {"to_seat": 1, "give_squares": [1], "want_squares": [5]},
        ).state
        original = state.trade_offers[0].id
        state = apply(
            state,
            tiny_spec,
            config,
            Action(
                ActionType.COUNTER_TRADE,
                1,
                {
                    "offer_id": original,
                    "give_squares": [5],
                    "want_squares": [1],
                    "want_credits": 300,
                },
            ),
        ).state
        assert len(state.trade_offers) == 1
        counter = state.trade_offers[0]
        assert counter.id != original, "a counter is a new offer"
        assert (counter.from_seat, counter.to_seat) == (1, 0)
        assert counter.want_credits == 300

    def test_offers_that_the_swap_invalidated_are_dropped(self, tiny_spec, config):
        """Square 1 can only be promised once.

        Without this, seat 0 could promise the same square to two seats and the
        second acceptance would fail deep inside the economy — or worse, succeed
        against a stale ownership read.
        """
        state = _trading(tiny_spec, config)
        state = _propose(
            state, tiny_spec, config, 0, {"to_seat": 1, "give_squares": [1]}
        ).state
        state = _propose(
            state, tiny_spec, config, 0, {"to_seat": 1, "give_squares": [1, 3]}
        ).state
        assert len(state.trade_offers) == 2

        state = apply(
            state,
            tiny_spec,
            config,
            Action(ActionType.ACCEPT_TRADE, 1, {"offer_id": state.trade_offers[0].id}),
        ).state
        assert state.trade_offers == (), "the stale second offer went with it"

    def test_a_seat_cannot_paper_the_table_with_offers(self, tiny_spec, config):
        state = _trading(tiny_spec, config)
        for _ in range(5):
            state = _propose(
                state, tiny_spec, config, 0, {"to_seat": 1, "give_credits": 1}
            ).state
        with pytest.raises(IllegalActionError, match="too many open offers"):
            _propose(state, tiny_spec, config, 0, {"to_seat": 1, "give_credits": 1})

    def test_offers_do_not_survive_the_window(self, tiny_spec, config):
        state = _trading(tiny_spec, config)
        state = _propose(
            state, tiny_spec, config, 0, {"to_seat": 1, "give_squares": [1]}
        ).state
        state = apply(
            state, tiny_spec, config, Action(ActionType.CLOSE_TRADING, 0)
        ).state
        assert state.phase == Phase.AWAIT_ROLL
        assert state.trade_offers == (), "an unanswered offer dies with the window"

    def test_proposing_outside_the_window_is_refused(self, tiny_spec, config):
        state = new_match(tiny_spec, config)
        with pytest.raises(IllegalActionError, match="trading is not open"):
            _propose(state, tiny_spec, config, 0, {"to_seat": 1, "give_credits": 1})

    def test_an_unknown_offer_id_is_refused(self, tiny_spec, config):
        state = _trading(tiny_spec, config)
        with pytest.raises(IllegalActionError, match="no such offer"):
            apply(
                state,
                tiny_spec,
                config,
                Action(ActionType.ACCEPT_TRADE, 1, {"offer_id": 999}),
            )


class TestOffersRoundTrip:
    def test_open_offers_survive_a_snapshot(self, tiny_spec, config):
        """State is persisted as JSON between requests — offers must come back."""
        from plugins.bdv.bdv.core.state import MatchState

        state = _trading(tiny_spec, config)
        state = _propose(
            state,
            tiny_spec,
            config,
            0,
            {"to_seat": 1, "give_squares": [1], "want_credits": 250, "note": "deal?"},
        ).state
        restored = MatchState.from_dict(state.to_dict())
        assert restored.trade_offers == state.trade_offers
        assert restored.state_hash() == state.state_hash()
        assert restored.trade_offers[0].note == "deal?"


def _trading_replace_want(state, *, want_credits):
    """Re-issue the single open offer with different terms (test helper)."""
    from dataclasses import replace

    offer = state.trade_offers[0]
    return replace(state, trade_offers=(replace(offer, want_credits=want_credits),))
