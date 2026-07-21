"""Regressions found by playing the real seeded board.

Both bugs below were invisible on the small fixture board and only appeared once
rents were large enough to bankrupt a seat mid-resolution. They are pinned here
so a future refactor cannot quietly reintroduce them.
"""
import dataclasses

import pytest

from plugins.bdv.bdv.core.engine import (
    Action,
    ActionType,
    IllegalActionError,
    MatchConfig,
    apply,
    new_match,
)
from plugins.bdv.bdv.core.state import Phase, SeatState


class TestBankruptcyDuringResolution:
    """A seat that busts while resolving its own move must not keep the turn."""

    def test_turn_advances_off_a_seat_that_busts_on_rent(self, tiny_spec):
        config = MatchConfig(seed="bust", seat_count=3)
        state = new_match(tiny_spec, config)
        state = dataclasses.replace(
            state,
            seats=(
                SeatState(index=0, cash=5, position=0),
                SeatState(index=1, cash=5000, position=0),
                SeatState(index=2, cash=5000, position=0),
            ),
            ownership={3: 1},
            houses={3: 5},
            pending_roll=(1, 2),
            phase=Phase.AWAIT_CHOICE,
        )
        result = apply(
            state, tiny_spec, config, Action(ActionType.CHOOSE_OPTION, 0, {"steps": 3})
        )
        assert result.state.seat(0).bankrupt is True
        assert result.state.turn_seat != 0, "the busted seat must not still be on turn"
        assert result.state.phase == Phase.AWAIT_ROLL

    def test_the_next_seat_can_actually_act(self, tiny_spec):
        config = MatchConfig(seed="bust2", seat_count=3)
        state = new_match(tiny_spec, config)
        state = dataclasses.replace(
            state,
            seats=(
                SeatState(index=0, cash=5, position=0),
                SeatState(index=1, cash=5000, position=0),
                SeatState(index=2, cash=5000, position=0),
            ),
            ownership={3: 1},
            houses={3: 5},
            pending_roll=(1, 2),
            phase=Phase.AWAIT_CHOICE,
        )
        state = apply(
            state, tiny_spec, config, Action(ActionType.CHOOSE_OPTION, 0, {"steps": 3})
        ).state
        # Must not raise "seat is bankrupt" / "out of turn".
        follow_up = apply(
            state, tiny_spec, config, Action(ActionType.ROLL, state.turn_seat)
        )
        assert follow_up.state.pending_roll is not None


class TestMatchEndIsNeverOverwritten:
    """Resolution can finish the match; the phase must survive it."""

    def test_finishing_on_rent_leaves_the_match_finished(self, tiny_spec):
        config = MatchConfig(seed="end", seat_count=2)
        state = new_match(tiny_spec, config)
        state = dataclasses.replace(
            state,
            seats=(
                SeatState(index=0, cash=5, position=0),
                SeatState(index=1, cash=5000, position=0),
            ),
            ownership={3: 1},
            houses={3: 5},
            pending_roll=(1, 2),
            phase=Phase.AWAIT_CHOICE,
        )
        result = apply(
            state, tiny_spec, config, Action(ActionType.CHOOSE_OPTION, 0, {"steps": 3})
        )
        assert (
            result.state.phase == Phase.FINISHED
        ), "phase=RESOLVING must never clobber a finished match"
        assert result.state.winner_seat == 1
        assert any(e["type"] == "match_finished" for e in result.events)

    def test_no_further_actions_are_accepted(self, tiny_spec):
        config = MatchConfig(seed="end2", seat_count=2)
        state = new_match(tiny_spec, config)
        state = dataclasses.replace(
            state,
            seats=(
                SeatState(index=0, cash=5, position=0),
                SeatState(index=1, cash=5000, position=0),
            ),
            ownership={3: 1},
            houses={3: 5},
            pending_roll=(1, 2),
            phase=Phase.AWAIT_CHOICE,
        )
        state = apply(
            state, tiny_spec, config, Action(ActionType.CHOOSE_OPTION, 0, {"steps": 3})
        ).state
        with pytest.raises(IllegalActionError):
            apply(state, tiny_spec, config, Action(ActionType.ROLL, 1))
