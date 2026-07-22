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
    #: A rent demand is outstanding — the debtor must agree, counter, or raise cash.
    AWAIT_RENT = "await_rent"
    #: Every ownable square is owned — the table trades before play resumes.
    TRADING = "trading"
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
class RentDemand:
    """Rent owed but not yet settled.

    Rent is a DEMAND, not an instant debit: the debtor may agree, or counter
    once. The owner then accepts or insists, and their answer is final —
    otherwise a table can haggle forever and only the clock decides.
    """

    debtor_seat: int
    owner_seat: int
    square_index: int
    amount: int
    offered: Optional[int] = None
    #: A square offered IN LIEU of cash. The owner may take it instead.
    offered_square: Optional[int] = None
    #: True once a counter has been made — one per demand.
    countered: bool = False

    @property
    def due(self) -> int:
        """What is actually payable right now."""
        return self.amount if self.offered is None else self.offered


@dataclass(frozen=True)
class Loan:
    """Credit advanced against pledged squares.

    Interest is charged on passing GO — one lap, one cycle. That hook is
    deterministic (no clock) and ties the cost of debt to tempo, which is a real
    tension with the dice market: buying bigger moves gets you round faster.
    """

    loan_id: int
    seat: int
    principal: int
    outstanding: int
    collateral: Tuple[int, ...]
    rate_bp: int


@dataclass(frozen=True)
class TradeOffer:
    """Terms one seat has put to another, awaiting an answer.

    Offers live in engine state rather than a side table on purpose. A trade is
    the only move that transfers assets between two seats, so its CONSENT must
    replay exactly like every other fact; a proposal row in the database would
    be a second source of truth that the fold could not see, and an accept could
    then execute terms the log never recorded.
    """

    id: int
    from_seat: int
    to_seat: int
    give_squares: Tuple[int, ...] = ()
    give_credits: int = 0
    want_squares: Tuple[int, ...] = ()
    want_credits: int = 0
    note: str = ""

    def touches(self, square_index: int) -> bool:
        return square_index in self.give_squares or square_index in self.want_squares

    def involves(self, seat_index: int) -> bool:
        return seat_index in (self.from_seat, self.to_seat)


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
    pending_demand: Optional[RentDemand] = None
    loans: Tuple[Loan, ...] = ()
    next_loan_id: int = 1
    #: The privatisation trading window fires ONCE per match.
    trading_done: bool = False
    #: Seats that have marked themselves ready to close the window early.
    trading_ready: Tuple[int, ...] = ()
    #: Trades put to the table and not yet answered.
    trade_offers: Tuple[TradeOffer, ...] = ()
    next_offer_id: int = 1

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
        return tuple(sorted(k for k, v in self.ownership.items() if v == seat_index))

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

    def with_ownership(
        self, square_index: int, seat_index: Optional[int]
    ) -> "MatchState":
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

    # ------------------------------------------------------------- economy

    def loans_of(self, seat_index: int) -> Tuple[Loan, ...]:
        return tuple(loan for loan in self.loans if loan.seat == seat_index)

    def debt_of(self, seat_index: int) -> int:
        return sum(loan.outstanding for loan in self.loans_of(seat_index))

    def pledged_squares(self, seat_index: Optional[int] = None) -> Tuple[int, ...]:
        """Squares locked as collateral — they cannot be sold while pledged."""
        pledged: list = []
        for loan in self.loans:
            if seat_index is None or loan.seat == seat_index:
                pledged.extend(loan.collateral)
        return tuple(sorted(set(pledged)))

    def with_loans(self, loans: Tuple[Loan, ...]) -> "MatchState":
        return replace(self, loans=loans)

    def with_demand(self, demand: Optional[RentDemand]) -> "MatchState":
        return replace(self, pending_demand=demand)

    # --------------------------------------------------------------- trading

    def offer(self, offer_id: int) -> Optional[TradeOffer]:
        for candidate in self.trade_offers:
            if candidate.id == offer_id:
                return candidate
        return None

    def offers_for(self, seat_index: int) -> Tuple[TradeOffer, ...]:
        return tuple(o for o in self.trade_offers if o.involves(seat_index))

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
            "pending_demand": _demand_payload(self.pending_demand),
            "loans": [_loan_payload(loan) for loan in self.loans],
            "next_loan_id": self.next_loan_id,
            "trading_done": self.trading_done,
            "trading_ready": list(self.trading_ready),
            "trade_offers": [_offer_payload(o) for o in self.trade_offers],
            "next_offer_id": self.next_offer_id,
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
                    "pending_demand": _demand_payload(self.pending_demand),
                    "loans": [_loan_payload(loan) for loan in self.loans],
                    "next_loan_id": self.next_loan_id,
                    "trading_done": self.trading_done,
                    "trading_ready": list(self.trading_ready),
                    "trade_offers": [_offer_payload(o) for o in self.trade_offers],
                    "next_offer_id": self.next_offer_id,
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
            pending_demand=_demand_from(payload.get("pending_demand")),
            loans=tuple(_loan_from(row) for row in payload.get("loans", [])),
            next_loan_id=payload.get("next_loan_id", 1),
            trading_done=payload.get("trading_done", False),
            trading_ready=tuple(payload.get("trading_ready", [])),
            trade_offers=tuple(
                _offer_from(row) for row in payload.get("trade_offers", [])
            ),
            next_offer_id=payload.get("next_offer_id", 1),
        )


def _demand_payload(demand: Optional[RentDemand]) -> Optional[Dict]:
    if demand is None:
        return None
    return {
        "debtor_seat": demand.debtor_seat,
        "owner_seat": demand.owner_seat,
        "square_index": demand.square_index,
        "amount": demand.amount,
        "offered": demand.offered,
        "offered_square": demand.offered_square,
        "countered": demand.countered,
        "due": demand.due,
    }


def _demand_from(payload: Optional[Dict]) -> Optional[RentDemand]:
    if not payload:
        return None
    return RentDemand(
        debtor_seat=payload["debtor_seat"],
        owner_seat=payload["owner_seat"],
        square_index=payload["square_index"],
        amount=payload["amount"],
        offered=payload.get("offered"),
        offered_square=payload.get("offered_square"),
        countered=payload.get("countered", False),
    )


def _offer_payload(offer: TradeOffer) -> Dict:
    return {
        "id": offer.id,
        "from_seat": offer.from_seat,
        "to_seat": offer.to_seat,
        "give_squares": list(offer.give_squares),
        "give_credits": offer.give_credits,
        "want_squares": list(offer.want_squares),
        "want_credits": offer.want_credits,
        "note": offer.note,
    }


def _offer_from(payload: Dict) -> TradeOffer:
    return TradeOffer(
        id=payload["id"],
        from_seat=payload["from_seat"],
        to_seat=payload["to_seat"],
        give_squares=tuple(payload.get("give_squares", [])),
        give_credits=payload.get("give_credits", 0),
        want_squares=tuple(payload.get("want_squares", [])),
        want_credits=payload.get("want_credits", 0),
        note=payload.get("note", ""),
    )


def _loan_payload(loan: Loan) -> Dict:
    return {
        "loan_id": loan.loan_id,
        "seat": loan.seat,
        "principal": loan.principal,
        "outstanding": loan.outstanding,
        "collateral": list(loan.collateral),
        "rate_bp": loan.rate_bp,
    }


def _loan_from(payload: Dict) -> Loan:
    return Loan(
        loan_id=payload["loan_id"],
        seat=payload["seat"],
        principal=payload["principal"],
        outstanding=payload["outstanding"],
        collateral=tuple(payload["collateral"]),
        rate_bp=payload["rate_bp"],
    )


def initial_state(seat_count: int, starting_cash: int) -> MatchState:
    """A fresh match: every seat identical, nothing owned."""
    return MatchState(
        seats=tuple(SeatState(index=i, cash=starting_cash) for i in range(seat_count)),
        ownership={},
        houses={},
        deck_cursors={},
    )
