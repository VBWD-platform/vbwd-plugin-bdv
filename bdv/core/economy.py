"""Selling, borrowing and interest — the solvency primitives.

PURE, like the rest of ``core/``. Interest is charged on **passing GO**: one lap,
one cycle. That hook is deterministic (no clock), already exists in the move
resolver, and ties the cost of debt to *tempo* — a player buying bigger moves
gets round the board faster and therefore pays interest more often, which is a
real tension with the dice market.

A seat that cannot pay is NOT immediately bankrupt. Bankruptcy is what happens
when liquidating everything still falls short — a computed fact, not a first
resort. Sell-or-borrow turns the worst moment of a match into a decision:
selling raises cash and permanently shrinks your income; borrowing keeps the
asset earning but adds a cost that recurs every lap.
"""
from __future__ import annotations

import math
from dataclasses import replace
from typing import Dict, List, Optional, Sequence, Tuple

from .board import BoardSpec, SquareKind
from .state import Loan, MatchState


class EconomyError(RuntimeError):
    """A refused sale, advance or repayment."""


# ------------------------------------------------------------------ valuation


def house_refund(spec: BoardSpec, square_index: int) -> int:
    """Selling a building returns half its cost — the classic rule."""
    return spec.square(square_index).house_cost // 2


def square_value(spec: BoardSpec, square_index: int) -> int:
    return spec.square(square_index).mortgage_value


def liquidation_value(state: MatchState, spec: BoardSpec, seat_index: int) -> int:
    """Everything this seat could raise by selling out completely.

    Used to decide whether a shortfall is survivable at all — bankruptcy is only
    correct when this still falls short.
    """
    total = 0
    for square_index in state.owned_by(seat_index):
        total += state.houses_on(square_index) * house_refund(spec, square_index)
        total += square_value(spec, square_index)
    return total - state.debt_of(seat_index)


def borrowing_power(
    state: MatchState, spec: BoardSpec, seat_index: int, loan_to_value: int
) -> int:
    """Max advance against everything not already pledged, in basis points."""
    pledged = set(state.pledged_squares(seat_index))
    free = [i for i in state.owned_by(seat_index) if i not in pledged]
    collateral = sum(square_value(spec, i) for i in free)
    return collateral * loan_to_value // 10_000


# ------------------------------------------------------------------- selling


def _stage_house_counts(
    state: MatchState, spec: BoardSpec, square_index: int
) -> Dict[int, int]:
    square = spec.square(square_index)
    if not square.stage:
        return {square_index: state.houses_on(square_index)}
    return {i: state.houses_on(i) for i in spec.stage_members(square.stage)}


def can_build(
    state: MatchState, spec: BoardSpec, seat_index: int, square_index: int
) -> Optional[str]:
    """None when the build is legal, else the reason it is not."""
    square = spec.square(square_index)
    if square.kind != SquareKind.DEAL:
        return "only deal squares take buildings"
    if state.owner_of(square_index) != seat_index:
        return "you do not own that square"
    if not square.stage:
        return "that square has no funnel stage"
    members = spec.stage_members(square.stage)
    if any(state.owner_of(i) != seat_index for i in members):
        return "you must own the whole funnel stage first"
    counts = _stage_house_counts(state, spec, square_index)
    current = counts[square_index]
    if current >= spec.max_houses:
        return "already at the maximum"
    # Even building: never get more than one ahead of the least-built square.
    if current > min(counts.values()):
        return "build evenly across the stage"
    if state.seat(seat_index).cash < square.house_cost:
        return "not enough cash"
    return None


def can_sell_house(
    state: MatchState, spec: BoardSpec, seat_index: int, square_index: int
) -> Optional[str]:
    if state.owner_of(square_index) != seat_index:
        return "you do not own that square"
    counts = _stage_house_counts(state, spec, square_index)
    current = counts[square_index]
    if current <= 0:
        return "nothing to sell there"
    # Even selling, mirroring even building.
    if current < max(counts.values()):
        return "sell evenly across the stage"
    return None


def can_sell_square(
    state: MatchState, spec: BoardSpec, seat_index: int, square_index: int
) -> Optional[str]:
    if state.owner_of(square_index) != seat_index:
        return "you do not own that square"
    if state.houses_on(square_index) > 0:
        return "sell its buildings first"
    if square_index in state.pledged_squares(seat_index):
        return "that square is pledged as collateral"
    return None


def build_house(
    state: MatchState, spec: BoardSpec, seat_index: int, square_index: int
) -> Tuple[MatchState, Dict]:
    problem = can_build(state, spec, seat_index, square_index)
    if problem:
        raise EconomyError(problem)
    cost = spec.square(square_index).house_cost
    working = state.with_cash_delta(seat_index, -cost)
    working = working.with_houses(square_index, state.houses_on(square_index) + 1)
    return working, {
        "type": "house_built",
        "seat": seat_index,
        "square": square_index,
        "cost": cost,
        "houses": working.houses_on(square_index),
    }


def sell_house(
    state: MatchState, spec: BoardSpec, seat_index: int, square_index: int
) -> Tuple[MatchState, Dict]:
    problem = can_sell_house(state, spec, seat_index, square_index)
    if problem:
        raise EconomyError(problem)
    refund = house_refund(spec, square_index)
    working = state.with_cash_delta(seat_index, refund)
    working = working.with_houses(square_index, state.houses_on(square_index) - 1)
    return working, {
        "type": "house_sold",
        "seat": seat_index,
        "square": square_index,
        "refund": refund,
        "houses": working.houses_on(square_index),
    }


def sell_square(
    state: MatchState, spec: BoardSpec, seat_index: int, square_index: int
) -> Tuple[MatchState, Dict]:
    """Back to the bank as UNOWNED — anyone may buy it again.

    Simpler than mortgage-with-buyback and it keeps the board circulating, which
    matters more in a game this short.
    """
    problem = can_sell_square(state, spec, seat_index, square_index)
    if problem:
        raise EconomyError(problem)
    value = square_value(spec, square_index)
    working = state.with_cash_delta(seat_index, value)
    working = working.with_ownership(square_index, None).with_houses(square_index, 0)
    return working, {
        "type": "square_sold",
        "seat": seat_index,
        "square": square_index,
        "amount": value,
    }


# ----------------------------------------------------------------- borrowing


def borrow(
    state: MatchState,
    spec: BoardSpec,
    seat_index: int,
    squares: Sequence[int],
    amount: int,
    *,
    loan_to_value: int,
    rate_bp: int,
) -> Tuple[MatchState, Dict]:
    """Advance ``amount`` against ``squares``, capped at the LTV."""
    if amount <= 0:
        raise EconomyError("advance must be positive")
    pledged = set(state.pledged_squares())
    chosen = list(dict.fromkeys(squares))
    if not chosen:
        raise EconomyError("pledge at least one square")
    for square_index in chosen:
        if state.owner_of(square_index) != seat_index:
            raise EconomyError("you can only pledge squares you own")
        if square_index in pledged:
            raise EconomyError("that square is already pledged")

    ceiling = sum(square_value(spec, i) for i in chosen) * loan_to_value // 10_000
    if amount > ceiling:
        raise EconomyError(
            f"advance exceeds the {loan_to_value / 100:.0f}% cap of {ceiling}"
        )

    loan = Loan(
        loan_id=state.next_loan_id,
        seat=seat_index,
        principal=amount,
        outstanding=amount,
        collateral=tuple(sorted(chosen)),
        rate_bp=rate_bp,
    )
    working = state.with_cash_delta(seat_index, amount).with_loans(
        state.loans + (loan,)
    )
    working = replace(working, next_loan_id=state.next_loan_id + 1)
    return working, {
        "type": "loan_taken",
        "seat": seat_index,
        "loan_id": loan.loan_id,
        "amount": amount,
        "collateral": list(loan.collateral),
        "rate_bp": rate_bp,
    }


def repay_loan(
    state: MatchState, seat_index: int, loan_id: int, amount: int
) -> Tuple[MatchState, Dict]:
    if amount <= 0:
        raise EconomyError("repayment must be positive")
    loan = next((row for row in state.loans if row.loan_id == loan_id), None)
    if loan is None or loan.seat != seat_index:
        raise EconomyError("no such loan")
    if amount > loan.outstanding:
        raise EconomyError("repayment exceeds what is outstanding")
    if state.seat(seat_index).cash < amount:
        raise EconomyError("not enough cash")

    remaining = loan.outstanding - amount
    loans = tuple(row for row in state.loans if row.loan_id != loan_id) + (
        (replace(loan, outstanding=remaining),) if remaining > 0 else ()
    )
    working = state.with_cash_delta(seat_index, -amount).with_loans(loans)
    return working, {
        "type": "loan_repaid",
        "seat": seat_index,
        "loan_id": loan_id,
        "amount": amount,
        "outstanding": remaining,
        "cleared": remaining == 0,
    }


def charge_interest(
    state: MatchState, seat_index: int
) -> Tuple[MatchState, List[Dict]]:
    """One lap = one interest cycle. Called when a seat passes GO."""
    events: List[Dict] = []
    working = state
    for loan in state.loans_of(seat_index):
        interest = math.ceil(loan.outstanding * loan.rate_bp / 10_000)
        if interest <= 0:
            continue
        working = working.with_cash_delta(seat_index, -interest)
        events.append(
            {
                "type": "interest_charged",
                "seat": seat_index,
                "loan_id": loan.loan_id,
                "amount": interest,
                "outstanding": loan.outstanding,
            }
        )
    return working, events


def seize_collateral(
    state: MatchState, seat_index: int
) -> Tuple[MatchState, List[Dict]]:
    """Default: the pledged squares go to the bank and the loans clear."""
    events: List[Dict] = []
    working = state
    for loan in state.loans_of(seat_index):
        for square_index in loan.collateral:
            working = working.with_ownership(square_index, None).with_houses(
                square_index, 0
            )
        events.append(
            {
                "type": "collateral_seized",
                "seat": seat_index,
                "loan_id": loan.loan_id,
                "squares": list(loan.collateral),
            }
        )
    working = working.with_loans(
        tuple(row for row in working.loans if row.seat != seat_index)
    )
    return working, events
