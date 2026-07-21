"""The state machine: roll -> negotiate -> choose -> resolve -> end turn.

Pure. ``apply(state, spec, action)`` returns a NEW state plus the events that
narrate the transition. Illegal actions raise typed errors — never a silent
no-op, which would desynchronise the action log from reality.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Dict, List, Mapping, Optional, Tuple

from .board import BoardSpec, SquareKind
from .dice import roll_at, shuffled_order
from .effects import apply_effect
from .fees import resolve_fee_policy
from .options import legal_options, sum_option
from .pricing import (
    affordability_cap,
    evaluate_options,
    quote_for_steps,
    rent_due,
)
from .state import (
    Constraint,
    ConstraintKind,
    MatchState,
    Phase,
    initial_state,
)


class EngineError(RuntimeError):
    """Base for every rejected action."""


class IllegalActionError(EngineError):
    """The action is not legal in the current phase / for this seat."""


class ConstraintViolationError(EngineError):
    """A binding contract (e.g. an accepted bribe) forbids this move."""


class ActionType:
    ROLL = "roll"
    OPEN_NEGOTIATION = "open_negotiation"
    ACCEPT_BRIBE = "accept_bribe"
    CHOOSE_OPTION = "choose_option"
    BUY_PROPERTY = "buy_property"
    DECLINE_PURCHASE = "decline_purchase"
    END_TURN = "end_turn"
    DECLARE_BANKRUPT = "declare_bankrupt"


@dataclass(frozen=True)
class Action:
    type: str
    seat_index: int
    payload: Mapping = field(default_factory=dict)


@dataclass(frozen=True)
class ApplyResult:
    state: MatchState
    events: Tuple[Dict, ...] = ()


@dataclass(frozen=True)
class MatchConfig:
    """Everything a match needs beyond the board rules."""

    seed: str
    seat_count: int
    chance_deck_size: int = 0
    community_deck_size: int = 0


def new_match(spec: BoardSpec, config: MatchConfig) -> MatchState:
    return initial_state(config.seat_count, spec.starting_cash)


# --------------------------------------------------------------------- guards


def _require_phase(state: MatchState, *phases: Phase) -> None:
    if state.phase not in phases:
        raise IllegalActionError(
            f"action not legal in phase {state.phase.value!r}; "
            f"expected one of {[p.value for p in phases]}"
        )


def _require_turn(state: MatchState, seat_index: int) -> None:
    if state.turn_seat != seat_index:
        raise IllegalActionError(
            f"seat {seat_index} acted out of turn (current: {state.turn_seat})"
        )
    if state.seat(seat_index).bankrupt:
        raise IllegalActionError(f"seat {seat_index} is bankrupt")


# ------------------------------------------------------------------- handlers


def _handle_roll(state, spec, config, action) -> ApplyResult:
    _require_phase(state, Phase.AWAIT_ROLL)
    _require_turn(state, action.seat_index)

    seat = state.current_seat
    if seat.skip_next_turn:
        cleared = state.with_seat(replace(seat, skip_next_turn=False))
        return ApplyResult(
            _advance_turn(cleared, spec),
            ({"type": "turn_skipped_consumed", "seat": seat.index},),
        )

    roll = roll_at(config.seed, state.rng_cursor)
    next_state = replace(
        state,
        pending_roll=roll,
        rng_cursor=state.rng_cursor + 1,
        phase=Phase.NEGOTIATE,
    ).bump()
    return ApplyResult(
        next_state,
        (
            {
                "type": "rolled",
                "seat": seat.index,
                "dice": list(roll),
                "options": list(legal_options(roll)),
            },
        ),
    )


def _handle_open_negotiation(state, spec, config, action) -> ApplyResult:
    """Close the negotiation window and move to the choice phase."""
    _require_phase(state, Phase.NEGOTIATE)
    return ApplyResult(
        replace(state, phase=Phase.AWAIT_CHOICE).bump(),
        ({"type": "negotiation_closed"},),
    )


def _handle_accept_bribe(state, spec, config, action) -> ApplyResult:
    """Binding: the mover takes the payment and MUST take the free sum."""
    _require_phase(state, Phase.NEGOTIATE)
    _require_turn(state, action.seat_index)

    payer = int(action.payload["from_seat"])
    amount = int(action.payload["amount"])
    if amount < 0:
        raise IllegalActionError("bribe amount must be non-negative")
    if payer == action.seat_index:
        raise IllegalActionError("a seat cannot bribe itself")
    if state.seat(payer).cash < amount:
        raise IllegalActionError("payer cannot afford the bribe")
    if state.has_constraint(ConstraintKind.FORCED_SUM, action.seat_index):
        raise IllegalActionError("a bribe has already been accepted this turn")

    next_state = state.with_cash_delta(payer, -amount).with_cash_delta(
        action.seat_index, amount
    )
    next_state = replace(
        next_state,
        constraints=next_state.constraints
        + (
            Constraint(
                kind=ConstraintKind.FORCED_SUM, seat_index=action.seat_index
            ),
        ),
        phase=Phase.AWAIT_CHOICE,
    ).bump()
    return ApplyResult(
        next_state,
        (
            {
                "type": "bribe_accepted",
                "seat": action.seat_index,
                "from": payer,
                "amount": amount,
            },
        ),
    )


def _handle_choose_option(state, spec, config, action) -> ApplyResult:
    _require_phase(state, Phase.NEGOTIATE, Phase.AWAIT_CHOICE)
    _require_turn(state, action.seat_index)
    if state.pending_roll is None:
        raise IllegalActionError("no roll pending")

    roll = state.pending_roll
    steps = int(action.payload["steps"])
    if steps not in legal_options(roll):
        raise IllegalActionError(f"{steps} is not a legal option for roll {roll}")

    fate = sum_option(roll)
    if (
        state.has_constraint(ConstraintKind.FORCED_SUM, action.seat_index)
        and steps != fate
    ):
        raise ConstraintViolationError(
            "an accepted bribe binds this turn to the free sum"
        )

    quote = quote_for_steps(state, spec, roll, steps)
    if quote is None:  # pragma: no cover - guarded by the legality check above
        raise IllegalActionError("option could not be quoted")

    events: List[Dict] = []
    working = state

    if quote.price > 0:
        cap = affordability_cap(spec, working.current_seat.cash)
        if quote.price > cap:
            raise IllegalActionError(
                f"price {quote.price} exceeds the affordability cap {cap}"
            )
        working = working.with_cash_delta(action.seat_index, -quote.price)
        policy = resolve_fee_policy(spec.fee_policy)
        payouts = policy.distribute(quote.price, working, action.seat_index)
        for recipient, amount in payouts.items():
            working = working.with_cash_delta(recipient, amount)
        events.append(
            {
                "type": "option_purchased",
                "seat": action.seat_index,
                "steps": steps,
                "price": quote.price,
                "ev_delta": quote.ev_delta,
                "policy": spec.fee_policy,
                "payouts": {str(k): v for k, v in payouts.items()},
            }
        )
    else:
        events.append(
            {
                "type": "option_taken_free",
                "seat": action.seat_index,
                "steps": steps,
                "is_sum": quote.is_sum,
            }
        )

    working, move_events = _move_and_resolve(working, spec, config, action.seat_index, steps)
    events.extend(move_events)

    if working.phase == Phase.FINISHED:
        # Never overwrite a finished match — resolution can end it (a rent
        # payment that bankrupts the last opponent finishes the game).
        return ApplyResult(working.bump(), tuple(events))

    if working.seat(action.seat_index).bankrupt:
        # A seat that busts during resolution has no turn left to take.
        working = _advance_turn(working, spec)
    else:
        working = replace(working, phase=Phase.RESOLVING)
    return ApplyResult(working.bump(), tuple(events))


def _move_and_resolve(
    state: MatchState, spec: BoardSpec, config: MatchConfig, seat_index: int, steps: int
) -> Tuple[MatchState, List[Dict]]:
    seat = state.seat(seat_index)
    destination = (seat.position + steps) % spec.size
    passed_go = destination < seat.position
    working = state.with_seat(replace(seat, position=destination))
    events: List[Dict] = [
        {"type": "moved", "seat": seat_index, "to": destination, "steps": steps}
    ]

    if passed_go:
        working = working.with_cash_delta(seat_index, spec.go_salary)
        events.append(
            {"type": "go_salary", "seat": seat_index, "amount": spec.go_salary}
        )

    square = spec.square(destination)

    if square.kind == SquareKind.TAX and square.tax_amount:
        working = working.with_cash_delta(seat_index, -square.tax_amount)
        events.append(
            {"type": "paid_tax", "seat": seat_index, "amount": square.tax_amount}
        )

    elif square.kind == SquareKind.GOTO_JAIL:
        jail = spec.indices_of_kind(SquareKind.JAIL)
        jail_index = jail[0] if jail else destination
        working = working.with_seat(
            replace(
                working.seat(seat_index),
                position=jail_index,
                in_jail=True,
                jail_turns=0,
            )
        )
        events.append({"type": "jailed", "seat": seat_index})

    elif square.kind in (SquareKind.CHANCE, SquareKind.COMMUNITY):
        working, draw_events = _draw_card(working, spec, config, seat_index, square.kind)
        events.extend(draw_events)

    elif square.is_ownable:
        owner = working.owner_of(destination)
        if owner is not None and owner != seat_index:
            rent = rent_due(working, spec, destination, steps)
            if rent:
                working = working.with_cash_delta(seat_index, -rent)
                working = working.with_cash_delta(owner, rent)
                events.append(
                    {
                        "type": "paid_rent",
                        "seat": seat_index,
                        "to": owner,
                        "amount": rent,
                        "square": destination,
                    }
                )
        elif owner is None and square.price:
            events.append(
                {
                    "type": "purchase_offered",
                    "seat": seat_index,
                    "square": destination,
                    "price": square.price,
                }
            )

    working, bankruptcy_events = _settle_bankruptcy(working, seat_index)
    events.extend(bankruptcy_events)
    return working, events


def _draw_card(
    state: MatchState,
    spec: BoardSpec,
    config: MatchConfig,
    seat_index: int,
    kind: SquareKind,
) -> Tuple[MatchState, List[Dict]]:
    deck = kind.value
    size = (
        config.chance_deck_size
        if kind == SquareKind.CHANCE
        else config.community_deck_size
    )
    if size <= 0:
        return state, []

    cursor = state.deck_cursors.get(deck, 0)
    reshuffle, position = divmod(cursor, size)
    order = shuffled_order(config.seed, deck, size, reshuffle)
    card_index = order[position]
    working = state.with_deck_cursor(deck, cursor + 1)
    events = [
        {
            "type": "card_drawn",
            "seat": seat_index,
            "deck": deck,
            "card_index": card_index,
        }
    ]
    return working, events


def apply_card_effect(
    state: MatchState, spec: BoardSpec, seat_index: int, effect: Mapping
) -> ApplyResult:
    """Execute a drawn card's ops. Kept separate from ``_draw_card`` because the
    card CONTENT lives in the database while the deck ORDER is pure — the engine
    stays free of persistence, and replay still reproduces the draw."""
    result = apply_effect(state, spec, seat_index, effect)
    working, bankruptcy_events = _settle_bankruptcy(result.state, seat_index)
    return ApplyResult(working, result.events + tuple(bankruptcy_events))


def _handle_buy_property(state, spec, config, action) -> ApplyResult:
    _require_turn(state, action.seat_index)
    seat = state.current_seat
    square = spec.square(seat.position)
    if not square.is_ownable:
        raise IllegalActionError("square is not purchasable")
    if state.owner_of(seat.position) is not None:
        raise IllegalActionError("square is already owned")
    if seat.cash < square.price:
        raise IllegalActionError("insufficient cash to purchase")

    working = state.with_cash_delta(action.seat_index, -square.price)
    working = working.with_ownership(seat.position, action.seat_index)
    return ApplyResult(
        working.bump(),
        (
            {
                "type": "property_bought",
                "seat": action.seat_index,
                "square": seat.position,
                "price": square.price,
            },
        ),
    )


def _handle_decline_purchase(state, spec, config, action) -> ApplyResult:
    _require_turn(state, action.seat_index)
    return ApplyResult(
        state.bump(),
        ({"type": "purchase_declined", "seat": action.seat_index},),
    )


def _handle_end_turn(state, spec, config, action) -> ApplyResult:
    _require_turn(state, action.seat_index)
    return ApplyResult(
        _advance_turn(state, spec).bump(), ({"type": "turn_ended", "seat": action.seat_index},)
    )


def _handle_declare_bankrupt(state, spec, config, action) -> ApplyResult:
    working = _bankrupt_seat(state, action.seat_index)
    working, events = _check_match_end(working)
    if working.phase != Phase.FINISHED:
        working = _advance_turn(working, spec)
    return ApplyResult(
        working.bump(),
        ({"type": "bankrupt", "seat": action.seat_index},) + tuple(events),
    )


def _settle_bankruptcy(state: MatchState, seat_index: int) -> Tuple[MatchState, List[Dict]]:
    seat = state.seat(seat_index)
    if seat.bankrupt or seat.cash >= 0:
        return state, []
    working = _bankrupt_seat(state, seat_index)
    working, events = _check_match_end(working)
    return working, [{"type": "bankrupt", "seat": seat_index}] + events


def _bankrupt_seat(state: MatchState, seat_index: int) -> MatchState:
    seat = state.seat(seat_index)
    working = state.with_seat(replace(seat, bankrupt=True, cash=0))
    for square in state.owned_by(seat_index):
        working = working.with_ownership(square, None).with_houses(square, 0)
    return working


def _check_match_end(state: MatchState) -> Tuple[MatchState, List[Dict]]:
    solvent = state.solvent_seats
    if len(solvent) > 1:
        return state, []
    winner = solvent[0].index if solvent else None
    working = replace(state, phase=Phase.FINISHED, winner_seat=winner)
    return working, [{"type": "match_finished", "winner": winner}]


def _advance_turn(state: MatchState, spec: BoardSpec) -> MatchState:
    if state.phase == Phase.FINISHED:
        return state
    count = len(state.seats)
    next_seat = state.turn_seat
    for _ in range(count):
        next_seat = (next_seat + 1) % count
        if not state.seats[next_seat].bankrupt:
            break
    return replace(
        state,
        turn_seat=next_seat,
        phase=Phase.AWAIT_ROLL,
        pending_roll=None,
        constraints=(),
    )


_HANDLERS = {
    ActionType.ROLL: _handle_roll,
    ActionType.OPEN_NEGOTIATION: _handle_open_negotiation,
    ActionType.ACCEPT_BRIBE: _handle_accept_bribe,
    ActionType.CHOOSE_OPTION: _handle_choose_option,
    ActionType.BUY_PROPERTY: _handle_buy_property,
    ActionType.DECLINE_PURCHASE: _handle_decline_purchase,
    ActionType.END_TURN: _handle_end_turn,
    ActionType.DECLARE_BANKRUPT: _handle_declare_bankrupt,
}


def apply(
    state: MatchState, spec: BoardSpec, config: MatchConfig, action: Action
) -> ApplyResult:
    """The single entry point. Unknown action types fail loudly."""
    if state.phase == Phase.FINISHED:
        raise IllegalActionError("the match is finished")
    handler = _HANDLERS.get(action.type)
    if handler is None:
        raise IllegalActionError(f"unknown action type: {action.type!r}")
    return handler(state, spec, config, action)


def options_for(
    state: MatchState, spec: BoardSpec, seat_index: Optional[int] = None
):
    """Priced options for the pending roll, or () when none is pending."""
    if state.pending_roll is None:
        return ()
    return evaluate_options(state, spec, state.pending_roll, seat_index)
