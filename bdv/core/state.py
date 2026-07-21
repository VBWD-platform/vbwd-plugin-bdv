"""Match state — frozen value objects.

Every transition returns a NEW state. Nothing mutates in place, which is what
makes the fold in the persistence layer (state = fold of ``apply`` over the
action log) and the replay check trivially correct.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from enum import Enum
from typing import Dict, Mapping, Optional, Tuple


class Phase(str, Enum):
    AWAIT_ROLL = "await_roll"
    NEGOTIATE = "negotiate"
    AWAIT_CHOICE = "await_choice"
    RESOLVING = "resolving"
    FINISHED = "finished"


class ConstraintKind(str, Enum):
    #: An accepted bribe: the mover MUST take the free sum this turn.
    FORCED_SUM = "forced_sum"


@dataclass(frozen=True)
class Constraint:
    kind: ConstraintKind
    seat_index: int


@dataclass(frozen=True)
class SeatState:
    index: int
    cash: int
    position: int = 0
    in_jail: bool = False
    jail_turns: int = 0
    get_out_of_jail_cards: int = 0
    skip_next_turn: bool = False
    bankrupt: bool = False


@dataclass(frozen=True)
class MatchState:
    seats: Tuple[SeatState, ...]
    ownership: Mapping[int, Optional[int]]
    houses: Mapping[int, int]
    turn_seat: int = 0
    phase: Phase = Phase.AWAIT_ROLL
    pending_roll: Optional[Tuple[int, int]] = None
    constraints: Tuple[Constraint, ...] = ()
    rng_cursor: int = 0
    deck_cursors: Mapping[str, int] = None  # type: ignore[assignment]
    seq: int = 0
    winner_seat: Optional[int] = None

    def __post_init__(self) -> None:
        # Frozen dataclass: use object.__setattr__ to normalise defaults once.
        if self.deck_cursors is None:
            object.__setattr__(self, "deck_cursors", {})

    # ---------------------------------------------------------------- lookups

    def seat(self, index: int) -> SeatState:
        return self.seats[index]

    @property
    def current_seat(self) -> SeatState:
        return self.seats[self.turn_seat]

    @property
    def solvent_seats(self) -> Tuple[SeatState, ...]:
        return tuple(s for s in self.seats if not s.bankrupt)

    def owner_of(self, square_index: int) -> Optional[int]:
        return self.ownership.get(square_index)

    def houses_on(self, square_index: int) -> int:
        return self.houses.get(square_index, 0)

    def owned_by(self, seat_index: int) -> Tuple[int, ...]:
        return tuple(
            sorted(k for k, v in self.ownership.items() if v == seat_index)
        )

    def has_constraint(self, kind: ConstraintKind, seat_index: int) -> bool:
        return any(
            c.kind == kind and c.seat_index == seat_index for c in self.constraints
        )

    # -------------------------------------------------------------- mutation

    def with_seat(self, seat: SeatState) -> "MatchState":
        seats = tuple(seat if s.index == seat.index else s for s in self.seats)
        return replace(self, seats=seats)

    def with_cash_delta(self, seat_index: int, delta: int) -> "MatchState":
        seat = self.seats[seat_index]
        return self.with_seat(replace(seat, cash=seat.cash + delta))

    def with_ownership(self, square_index: int, seat_index: Optional[int]) -> "MatchState":
        ownership = dict(self.ownership)
        ownership[square_index] = seat_index
        return replace(self, ownership=ownership)

    def with_houses(self, square_index: int, count: int) -> "MatchState":
        houses = dict(self.houses)
        houses[square_index] = count
        return replace(self, houses=houses)

    def with_deck_cursor(self, deck: str, cursor: int) -> "MatchState":
        cursors = dict(self.deck_cursors)
        cursors[deck] = cursor
        return replace(self, deck_cursors=cursors)

    def bump(self) -> "MatchState":
        return replace(self, seq=self.seq + 1)

    # -------------------------------------------------------------- identity

    def state_hash(self) -> str:
        """Content hash of everything that matters — the replay equality check."""
        payload = {
            "seats": [
                {
                    "index": s.index,
                    "cash": s.cash,
                    "position": s.position,
                    "in_jail": s.in_jail,
                    "jail_turns": s.jail_turns,
                    "cards": s.get_out_of_jail_cards,
                    "skip": s.skip_next_turn,
                    "bankrupt": s.bankrupt,
                }
                for s in self.seats
            ],
            "ownership": {str(k): v for k, v in sorted(self.ownership.items())},
            "houses": {str(k): v for k, v in sorted(self.houses.items())},
            "turn_seat": self.turn_seat,
            "phase": self.phase.value,
            "pending_roll": list(self.pending_roll) if self.pending_roll else None,
            "constraints": [
                {"kind": c.kind.value, "seat": c.seat_index} for c in self.constraints
            ],
            "rng_cursor": self.rng_cursor,
            "deck_cursors": {k: v for k, v in sorted(self.deck_cursors.items())},
            "seq": self.seq,
            "winner_seat": self.winner_seat,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def to_dict(self) -> Dict:
        return json.loads(
            json.dumps(
                {
                    "seats": [
                        {
                            "index": s.index,
                            "cash": s.cash,
                            "position": s.position,
                            "in_jail": s.in_jail,
                            "jail_turns": s.jail_turns,
                            "get_out_of_jail_cards": s.get_out_of_jail_cards,
                            "skip_next_turn": s.skip_next_turn,
                            "bankrupt": s.bankrupt,
                        }
                        for s in self.seats
                    ],
                    "ownership": {str(k): v for k, v in self.ownership.items()},
                    "houses": {str(k): v for k, v in self.houses.items()},
                    "turn_seat": self.turn_seat,
                    "phase": self.phase.value,
                    "pending_roll": list(self.pending_roll)
                    if self.pending_roll
                    else None,
                    "constraints": [
                        {"kind": c.kind.value, "seat_index": c.seat_index}
                        for c in self.constraints
                    ],
                    "rng_cursor": self.rng_cursor,
                    "deck_cursors": dict(self.deck_cursors),
                    "seq": self.seq,
                    "winner_seat": self.winner_seat,
                }
            )
        )

    @classmethod
    def from_dict(cls, payload: Dict) -> "MatchState":
        return cls(
            seats=tuple(
                SeatState(
                    index=s["index"],
                    cash=s["cash"],
                    position=s["position"],
                    in_jail=s["in_jail"],
                    jail_turns=s["jail_turns"],
                    get_out_of_jail_cards=s["get_out_of_jail_cards"],
                    skip_next_turn=s["skip_next_turn"],
                    bankrupt=s["bankrupt"],
                )
                for s in payload["seats"]
            ),
            ownership={int(k): v for k, v in payload["ownership"].items()},
            houses={int(k): v for k, v in payload["houses"].items()},
            turn_seat=payload["turn_seat"],
            phase=Phase(payload["phase"]),
            pending_roll=tuple(payload["pending_roll"])
            if payload.get("pending_roll")
            else None,
            constraints=tuple(
                Constraint(kind=ConstraintKind(c["kind"]), seat_index=c["seat_index"])
                for c in payload.get("constraints", [])
            ),
            rng_cursor=payload["rng_cursor"],
            deck_cursors=dict(payload.get("deck_cursors", {})),
            seq=payload["seq"],
            winner_seat=payload.get("winner_seat"),
        )


def initial_state(seat_count: int, starting_cash: int) -> MatchState:
    """A fresh match: every seat identical, nothing owned."""
    return MatchState(
        seats=tuple(
            SeatState(index=i, cash=starting_cash) for i in range(seat_count)
        ),
        ownership={},
        houses={},
        deck_cursors={},
    )
