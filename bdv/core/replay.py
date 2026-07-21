"""Replay — the audit guarantee.

Same spec + same seed + same actions => byte-identical state, forever.
A replay against a different ``spec_hash`` is rejected loudly: a match must
always be re-derivable under the exact rules it was played under.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from .board import BoardSpec
from .engine import Action, MatchConfig, apply, new_match
from .state import MatchState


class SpecMismatchError(RuntimeError):
    """The recorded board rules are not the rules being replayed."""


@dataclass(frozen=True)
class Replay:
    spec_hash: str
    seed: str
    seat_count: int
    actions: Tuple[Action, ...] = ()
    chance_deck_size: int = 0
    community_deck_size: int = 0

    def config(self) -> MatchConfig:
        return MatchConfig(
            seed=self.seed,
            seat_count=self.seat_count,
            chance_deck_size=self.chance_deck_size,
            community_deck_size=self.community_deck_size,
        )


def replay(spec: BoardSpec, recording: Replay) -> MatchState:
    """Fold the action log back into a state."""
    if spec.spec_hash() != recording.spec_hash:
        raise SpecMismatchError(
            "board spec does not match the recording "
            f"(expected {recording.spec_hash[:12]}…, got {spec.spec_hash()[:12]}…)"
        )
    config = recording.config()
    state = new_match(spec, config)
    for action in recording.actions:
        state = apply(state, spec, config, action).state
    return state


def fold(
    spec: BoardSpec, config: MatchConfig, actions: Tuple[Action, ...]
) -> MatchState:
    """Fold without the spec-hash guard — for rebuilding a live snapshot."""
    state = new_match(spec, config)
    for action in actions:
        state = apply(state, spec, config, action).state
    return state
