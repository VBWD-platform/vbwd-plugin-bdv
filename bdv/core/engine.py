"""The state machine: roll -> negotiate -> choose -> resolve -> end turn.

Pure. ``apply(state, spec, action)`` returns a NEW state plus the events that
narrate the transition. Illegal actions raise typed errors — never a silent
no-op, which would desynchronise the action log from reality.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Dict, List, Mapping, Optional, Tuple

from . import economy
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
    RentDemand,
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
    # --- rent demand (S146-9)
    AGREE_TO_PAY = "agree_to_pay"
    RENT_AUTO_AGREED = "rent_auto_agreed"
    OFFER_RENT = "offer_rent"
    ACCEPT_RENT_OFFER = "accept_rent_offer"
    INSIST_ON_FULL_RENT = "insist_on_full_rent"
    # --- solvency + assets (S146-10 / S146-11)
    BUILD_HOUSE = "build_house"
    SELL_HOUSE = "sell_house"
    SELL_SQUARE = "sell_square"
    BORROW = "borrow"
    REPAY_LOAN = "repay_loan"
    # --- table economy (S146-12)
    TRANSFER_CREDITS = "transfer_credits"
    OPEN_NEGOTIATION = "open_negotiation"
    ACCEPT_BRIBE = "accept_bribe"
    ESCROW_BRIBE = "escrow_bribe"
    REFUND_BRIBE = "refund_bribe"
    #: The turn timeout, recorded as an action so replay stays exact.
    TURN_AUTO_SUM = "turn_auto_sum"
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
    #: Basis points of the pledged value a seat may borrow (5000 = 50 %).
    loan_to_value: int = 5000
    #: Basis points charged per lap on outstanding debt (1000 = 10 %).
    loan_rate_bp: int = 1000


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


def _handle_escrow_bribe(state, spec, config, action) -> ApplyResult:
    """Hold the offered amount at OFFER time.

    Escrowing up front is what stops a seat offering money it no longer has by
    the time the mover answers. The funds sit out of play until the offer is
    accepted, declined or expires.
    """
    _require_phase(state, Phase.NEGOTIATE)
    amount = int(action.payload["amount"])
    if amount <= 0:
        raise IllegalActionError("offer must be positive")
    if state.seat(action.seat_index).cash < amount:
        raise IllegalActionError("you cannot afford that offer")
    working = state.with_cash_delta(action.seat_index, -amount).bump()
    return ApplyResult(
        working,
        ({"type": "bribe_escrowed", "seat": action.seat_index, "amount": amount},),
    )


def _handle_refund_bribe(state, spec, config, action) -> ApplyResult:
    """Return escrowed funds when an offer is declined or expires."""
    amount = int(action.payload["amount"])
    if amount <= 0:
        raise IllegalActionError("refund must be positive")
    working = state.with_cash_delta(action.seat_index, amount).bump()
    return ApplyResult(
        working,
        ({"type": "bribe_refunded", "seat": action.seat_index, "amount": amount},),
    )


def _handle_accept_bribe(state, spec, config, action) -> ApplyResult:
    """Binding: the mover takes the payment and MUST take the free sum.

    The money was already escrowed when the offer was made, so this only credits
    the mover — debiting again here would take it twice.
    """
    _require_phase(state, Phase.NEGOTIATE)
    _require_turn(state, action.seat_index)

    payer = int(action.payload["from_seat"])
    amount = int(action.payload["amount"])
    if amount < 0:
        raise IllegalActionError("bribe amount must be non-negative")
    if payer == action.seat_index:
        raise IllegalActionError("a seat cannot bribe itself")
    if state.has_constraint(ConstraintKind.FORCED_SUM, action.seat_index):
        raise IllegalActionError("a bribe has already been accepted this turn")

    next_state = state.with_cash_delta(action.seat_index, amount)
    next_state = replace(
        next_state,
        constraints=next_state.constraints
        + (Constraint(kind=ConstraintKind.FORCED_SUM, seat_index=action.seat_index),),
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


def _handle_turn_auto_sum(state, spec, config, action) -> ApplyResult:
    """The turn timeout: take the FREE sum.

    Deliberately the existing "fate default" rather than a new concept — a
    disconnect degrades to classic play instead of stalling the table. Like the
    rent auto-agree, the clock lives in the service and this is the recorded
    fact, so replay reproduces it exactly.
    """
    if state.pending_roll is None:
        raise IllegalActionError("no roll pending")
    fate = sum_option(state.pending_roll)
    result = _handle_choose_option(
        state,
        spec,
        config,
        Action(ActionType.CHOOSE_OPTION, state.turn_seat, {"steps": fate}),
    )
    return ApplyResult(
        result.state,
        result.events + ({"type": "turn_timed_out", "seat": state.turn_seat},),
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

    working, move_events = _move_and_resolve(
        working, spec, config, action.seat_index, steps
    )
    events.extend(move_events)

    if working.phase == Phase.FINISHED:
        # Never overwrite a finished match — resolution can end it (a rent
        # payment that bankrupts the last opponent finishes the game).
        return ApplyResult(working.bump(), tuple(events))

    if working.seat(action.seat_index).bankrupt:
        # A seat that busts during resolution has no turn left to take.
        working = _advance_turn(working, spec)
    elif working.pending_demand is not None:
        working = replace(working, phase=Phase.AWAIT_RENT)
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
        # One lap = one interest cycle. Deterministic, and it ties the cost of
        # debt to tempo — racing round the board pays interest more often.
        working, interest_events = economy.charge_interest(working, seat_index)
        events.extend(interest_events)

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
        working, draw_events = _draw_card(
            working, spec, config, seat_index, square.kind
        )
        events.extend(draw_events)

    elif square.is_ownable:
        owner = working.owner_of(destination)
        if owner is not None and owner != seat_index:
            rent = rent_due(working, spec, destination, steps)
            if rent:
                # Rent is a DEMAND, not a silent debit: the debtor must agree or
                # counter, and may have to raise cash first.
                working = working.with_demand(
                    RentDemand(
                        debtor_seat=seat_index,
                        owner_seat=owner,
                        square_index=destination,
                        amount=rent,
                    )
                )
                events.append(
                    {
                        "type": "rent_demanded",
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

    working, bankruptcy_events = _settle_bankruptcy(working, seat_index, spec)
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
    if state.pending_demand is not None:
        raise IllegalActionError("settle the rent demand before ending your turn")
    return ApplyResult(
        _advance_turn(state, spec).bump(),
        ({"type": "turn_ended", "seat": action.seat_index},),
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


def _require_demand(state: MatchState) -> RentDemand:
    if state.pending_demand is None:
        raise IllegalActionError("there is no rent demand outstanding")
    return state.pending_demand


def _settle_demand(state, spec, demand: RentDemand):
    """Pay what was agreed. The debtor must already be able to cover it."""
    amount = demand.due
    seat = state.seat(demand.debtor_seat)
    if seat.cash < amount:
        raise IllegalActionError(
            f"you are {amount - seat.cash} short — sell or borrow first"
        )
    working = state.with_cash_delta(demand.debtor_seat, -amount)
    working = working.with_cash_delta(demand.owner_seat, amount)
    working = working.with_demand(None)
    working = replace(working, phase=Phase.RESOLVING)
    return working, {
        "type": "paid_rent",
        "seat": demand.debtor_seat,
        "to": demand.owner_seat,
        "amount": amount,
        "square": demand.square_index,
        "negotiated": demand.offered is not None,
    }


def _handle_agree_to_pay(state, spec, config, action) -> ApplyResult:
    demand = _require_demand(state)
    if action.seat_index != demand.debtor_seat:
        raise IllegalActionError("only the debtor may agree to pay")
    working, event = _settle_demand(state, spec, demand)
    return ApplyResult(working.bump(), (event,))


def _handle_rent_auto_agreed(state, spec, config, action) -> ApplyResult:
    """The 60-second timeout, recorded as an ACTION.

    The clock lives in the service layer; the engine only ever sees the recorded
    fact, so a replay reproduces the auto-agreement exactly instead of
    re-evaluating a deadline.
    """
    demand = _require_demand(state)
    working, event = _settle_demand(state, spec, demand)
    event["auto"] = True
    return ApplyResult(working.bump(), (event,))


def _handle_offer_rent(state, spec, config, action) -> ApplyResult:
    demand = _require_demand(state)
    if action.seat_index != demand.debtor_seat:
        raise IllegalActionError("only the debtor may counter")
    if demand.countered:
        raise IllegalActionError("you have already made a counter-offer")
    amount = int(action.payload["amount"])
    if amount <= 0 or amount >= demand.amount:
        raise IllegalActionError("a counter must be above zero and below the rent")
    working = state.with_demand(replace(demand, offered=amount, countered=True)).bump()
    return ApplyResult(
        working,
        (
            {
                "type": "rent_countered",
                "seat": demand.debtor_seat,
                "to": demand.owner_seat,
                "offered": amount,
                "rent": demand.amount,
            },
        ),
    )


def _handle_accept_rent_offer(state, spec, config, action) -> ApplyResult:
    demand = _require_demand(state)
    if action.seat_index != demand.owner_seat:
        raise IllegalActionError("only the owner may accept")
    if demand.offered is None:
        raise IllegalActionError("there is no counter-offer to accept")
    working, event = _settle_demand(state, spec, demand)
    event["accepted_offer"] = True
    return ApplyResult(working.bump(), (event,))


def _handle_insist_on_full_rent(state, spec, config, action) -> ApplyResult:
    """The owner's answer is final — otherwise a table haggles forever."""
    demand = _require_demand(state)
    if action.seat_index != demand.owner_seat:
        raise IllegalActionError("only the owner may insist")
    working = state.with_demand(replace(demand, offered=None)).bump()
    return ApplyResult(
        working,
        (
            {
                "type": "rent_insisted",
                "seat": demand.owner_seat,
                "debtor": demand.debtor_seat,
                "amount": demand.amount,
            },
        ),
    )


# ---------------------------------------------------------- solvency + assets


def _require_own_turn_phase(state, seat_index, *phases):
    _require_turn(state, seat_index)
    if state.phase not in phases:
        raise IllegalActionError(f"not allowed in phase {state.phase.value!r}")


def _handle_build_house(state, spec, config, action) -> ApplyResult:
    # Building is restricted to AWAIT_ROLL so it stays a bet about the board
    # rather than a hedge against a roll you have already seen.
    _require_own_turn_phase(state, action.seat_index, Phase.AWAIT_ROLL)
    working, event = economy.build_house(
        state, spec, action.seat_index, int(action.payload["square"])
    )
    return ApplyResult(working.bump(), (event,))


def _handle_sell_house(state, spec, config, action) -> ApplyResult:
    _require_turn(state, action.seat_index)
    working, event = economy.sell_house(
        state, spec, action.seat_index, int(action.payload["square"])
    )
    return ApplyResult(working.bump(), (event,))


def _handle_sell_square(state, spec, config, action) -> ApplyResult:
    _require_turn(state, action.seat_index)
    working, event = economy.sell_square(
        state, spec, action.seat_index, int(action.payload["square"])
    )
    return ApplyResult(working.bump(), (event,))


def _handle_borrow(state, spec, config, action) -> ApplyResult:
    _require_turn(state, action.seat_index)
    working, event = economy.borrow(
        state,
        spec,
        action.seat_index,
        [int(i) for i in action.payload.get("squares", [])],
        int(action.payload["amount"]),
        loan_to_value=config.loan_to_value,
        rate_bp=config.loan_rate_bp,
    )
    return ApplyResult(working.bump(), (event,))


def _handle_repay_loan(state, spec, config, action) -> ApplyResult:
    _require_turn(state, action.seat_index)
    working, event = economy.repay_loan(
        state,
        action.seat_index,
        int(action.payload["loan_id"]),
        int(action.payload["amount"]),
    )
    return ApplyResult(working.bump(), (event,))


def _handle_transfer_credits(state, spec, config, action) -> ApplyResult:
    """A side payment between seats.

    These are IN-GAME credits: created at match start, destroyed at match end,
    never purchasable and never cashable. A transfer is a move, not a payment.
    """
    sender = action.seat_index
    recipient = int(action.payload["to_seat"])
    amount = int(action.payload["amount"])
    if amount <= 0:
        raise IllegalActionError("amount must be positive")
    if recipient == sender:
        raise IllegalActionError("you cannot pay yourself")
    if not 0 <= recipient < len(state.seats):
        raise IllegalActionError("no such seat")
    if state.seat(recipient).bankrupt:
        raise IllegalActionError("that seat is out of the match")
    if state.seat(sender).cash < amount:
        raise IllegalActionError("not enough cash")

    working = state.with_cash_delta(sender, -amount).with_cash_delta(recipient, amount)
    return ApplyResult(
        working.bump(),
        (
            {
                "type": "credits_transferred",
                "seat": sender,
                "to": recipient,
                "amount": amount,
            },
        ),
    )


def _settle_bankruptcy(
    state: MatchState, seat_index: int, spec: Optional[BoardSpec] = None
) -> Tuple[MatchState, List[Dict]]:
    seat = state.seat(seat_index)
    if seat.bankrupt or seat.cash >= 0:
        return state, []
    # Bankruptcy is what happens when liquidating EVERYTHING still falls short —
    # a computed fact, not a first resort. With assets left the seat keeps the
    # shortfall and must sell or borrow.
    if (
        spec is not None
        and economy.liquidation_value(state, spec, seat_index) + seat.cash >= 0
    ):
        return state, []
    working = _bankrupt_seat(state, seat_index)
    working, events = _check_match_end(working)
    return working, [{"type": "bankrupt", "seat": seat_index}] + events


def _bankrupt_seat(state: MatchState, seat_index: int) -> MatchState:
    seat = state.seat(seat_index)
    working = state.with_seat(replace(seat, bankrupt=True, cash=0))
    # Collateral goes to the bank and the loans clear.
    working, _ = economy.seize_collateral(working, seat_index)
    if working.pending_demand is not None and (
        working.pending_demand.debtor_seat == seat_index
    ):
        working = working.with_demand(None)
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
        # A demand cannot outlive the turn it arose in — otherwise the next seat
        # inherits a debt it never incurred and can never end its turn.
        pending_demand=None,
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
    ActionType.AGREE_TO_PAY: _handle_agree_to_pay,
    ActionType.RENT_AUTO_AGREED: _handle_rent_auto_agreed,
    ActionType.OFFER_RENT: _handle_offer_rent,
    ActionType.ACCEPT_RENT_OFFER: _handle_accept_rent_offer,
    ActionType.INSIST_ON_FULL_RENT: _handle_insist_on_full_rent,
    ActionType.BUILD_HOUSE: _handle_build_house,
    ActionType.SELL_HOUSE: _handle_sell_house,
    ActionType.SELL_SQUARE: _handle_sell_square,
    ActionType.BORROW: _handle_borrow,
    ActionType.REPAY_LOAN: _handle_repay_loan,
    ActionType.TRANSFER_CREDITS: _handle_transfer_credits,
    ActionType.ESCROW_BRIBE: _handle_escrow_bribe,
    ActionType.REFUND_BRIBE: _handle_refund_bribe,
    ActionType.TURN_AUTO_SUM: _handle_turn_auto_sum,
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


def options_for(state: MatchState, spec: BoardSpec, seat_index: Optional[int] = None):
    """Priced options for the pending roll, or () when none is pending."""
    if state.pending_roll is None:
        return ()
    return evaluate_options(state, spec, state.pending_roll, seat_index)
