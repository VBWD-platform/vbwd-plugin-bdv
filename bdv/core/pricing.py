"""EV pricing — what it costs to buy your way out of fate.

    options(roll{a,b}) = dedup([a, b, a+b])
    ev(option)         = immediate value delta of landing on the resulting square
    price(option)      = max(0, round(k_price x (ev(option) - ev(sum))))
    price(sum)         = 0                              # fate is always free
    affordable         = price <= floor(cap_pct x cash)

Every number here is a pure function of the state, so any quote can be
re-derived from the action log months later. That is the "auditable prices"
product claim, and it is why this module has no I/O.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Dict, Optional, Tuple

from .board import BoardSpec, SquareKind, SquareSpec
from .options import legal_options, sum_option
from .state import MatchState


def _round_half_up(value: Decimal) -> int:
    """Deterministic rounding. Python's round() is banker's rounding, which
    would make 0.5-boundary prices depend on parity — surprising for money."""
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class OptionQuote:
    """One priced move offered to the mover."""

    steps: int
    target_index: int
    target_name: str
    ev: int
    ev_delta: int
    price: int
    affordable: bool
    is_sum: bool
    reason: str
    reason_params: Dict = field(default_factory=dict)


def rent_due(
    state: MatchState, spec: BoardSpec, square_index: int, die_used: int
) -> int:
    """Rent payable by a visitor. Services multiply by the die ACTUALLY used —
    the one place where which die you bought changes what you owe."""
    square = spec.square(square_index)
    owner = state.owner_of(square_index)
    if owner is None or not square.is_ownable:
        return 0

    if square.kind == SquareKind.DEAL:
        houses = state.houses_on(square_index)
        table = square.rent_table
        if not table:
            return 0
        base = table[min(houses, len(table) - 1)]
        if (
            houses == 0
            and square.stage
            and _owns_whole_stage(state, spec, square, owner)
        ):
            return base * 2
        return base

    owned_services = sum(
        1
        for index in spec.indices_of_kind(SquareKind.SERVICE)
        if state.owner_of(index) == owner
    )
    multipliers = square.service_multipliers
    if not multipliers or owned_services == 0:
        return 0
    multiplier = multipliers[min(owned_services, len(multipliers)) - 1]
    return multiplier * die_used


def _owns_whole_stage(
    state: MatchState, spec: BoardSpec, square: SquareSpec, seat_index: int
) -> bool:
    if not square.stage:
        return False
    members = spec.stage_members(square.stage)
    return bool(members) and all(
        state.owner_of(index) == seat_index for index in members
    )


def _would_complete_stage(
    state: MatchState, spec: BoardSpec, square: SquareSpec, seat_index: int
) -> bool:
    """True when buying this square gives the seat the whole funnel stage."""
    if square.kind != SquareKind.DEAL or not square.stage:
        return False
    members = spec.stage_members(square.stage)
    others = [index for index in members if index != square.index]
    return bool(others) and all(state.owner_of(index) == seat_index for index in others)


def evaluate_square(
    state: MatchState,
    spec: BoardSpec,
    seat_index: int,
    square_index: int,
    die_used: int,
) -> Tuple[int, str, Dict]:
    """The immediate value delta of landing here, plus an i18n reason key.

    ``reason`` is a key with params — never a rendered sentence. The engine
    stays presentation-free; the FE and the chat each localise it.
    """
    square = spec.square(square_index)
    seat = state.seat(seat_index)

    if square.kind in (SquareKind.DEAL, SquareKind.SERVICE):
        owner = state.owner_of(square_index)
        if owner is None:
            bonus = _round_half_up(Decimal(square.price) * spec.k_acquire)
            if _would_complete_stage(state, spec, square, seat_index):
                return (
                    bonus * 2,
                    "completes_stage",
                    {"stage": square.stage, "name": square.name},
                )
            return bonus, "unowned", {"name": square.name}
        if owner == seat_index:
            return 0, "own_square", {"name": square.name}
        rent = rent_due(state, spec, square_index, die_used)
        return -rent, "pays_rent", {"name": square.name, "rent": rent}

    if square.kind == SquareKind.TAX:
        return (
            -square.tax_amount,
            "tax",
            {"name": square.name, "amount": square.tax_amount},
        )

    if square.kind == SquareKind.GO:
        return spec.go_salary, "go", {"name": square.name}

    if square.kind == SquareKind.GOTO_JAIL:
        return -spec.jail_penalty_ev, "goto_jail", {"name": square.name}

    if square.kind in (SquareKind.CHANCE, SquareKind.COMMUNITY):
        return (
            spec.deck_ev_hint(square.kind),
            "draw_card",
            {"name": square.name},
        )

    if square.kind == SquareKind.JAIL and seat.in_jail:
        return 0, "jail_visiting", {"name": square.name}

    return 0, "neutral", {"name": square.name}


def target_index(
    state: MatchState, spec: BoardSpec, seat_index: int, steps: int
) -> int:
    return (state.seat(seat_index).position + steps) % spec.size


def affordability_cap(spec: BoardSpec, cash: int) -> int:
    """Most a seat may spend on move-buying this turn."""
    if cash <= 0:
        return 0
    return int((Decimal(cash) * spec.cap_pct).to_integral_value(rounding="ROUND_FLOOR"))


def price_for(ev_option: int, ev_sum: int, spec: BoardSpec) -> int:
    """You never pay to move worse — but you may move worse for free."""
    delta = ev_option - ev_sum
    if delta <= 0:
        return 0
    return max(0, _round_half_up(Decimal(delta) * spec.k_price))


def evaluate_options(
    state: MatchState,
    spec: BoardSpec,
    roll: Tuple[int, int],
    seat_index: Optional[int] = None,
) -> Tuple[OptionQuote, ...]:
    """Quote every legal move for the current (or given) seat."""
    seat_index = state.turn_seat if seat_index is None else seat_index
    seat = state.seat(seat_index)
    fate = sum_option(roll)

    sum_target = target_index(state, spec, seat_index, fate)
    ev_sum, _, _ = evaluate_square(state, spec, seat_index, sum_target, fate)

    cap = affordability_cap(spec, seat.cash)
    quotes = []
    for steps in legal_options(roll):
        landing = target_index(state, spec, seat_index, steps)
        ev_value, reason, params = evaluate_square(
            state, spec, seat_index, landing, steps
        )
        is_sum = steps == fate
        price = 0 if is_sum else price_for(ev_value, ev_sum, spec)
        quotes.append(
            OptionQuote(
                steps=steps,
                target_index=landing,
                target_name=spec.square(landing).name,
                ev=ev_value,
                ev_delta=ev_value - ev_sum,
                price=price,
                affordable=price <= cap,
                is_sum=is_sum,
                reason=reason,
                reason_params=params,
            )
        )
    return tuple(quotes)


def quote_for_steps(
    state: MatchState, spec: BoardSpec, roll: Tuple[int, int], steps: int
) -> Optional[OptionQuote]:
    for quote in evaluate_options(state, spec, roll):
        if quote.steps == steps:
            return quote
    return None
