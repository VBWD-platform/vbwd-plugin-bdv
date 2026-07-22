"""The deterministic baseline seat.

Boring on purpose, and it earns its keep three times over:

* it is the degradation target when an LLM seat errors or exhausts its budget,
  so a model failure can never stall a table;
* it makes the 500-match balance harness free to run — no tokens, no network;
* it makes every engine test deterministic.

PURE: imports only from ``core``. No Flask, no DB, no LLM.
"""
from __future__ import annotations

from typing import Optional, Tuple

from ..core import economy
from ..core.board import BoardSpec
from ..core.engine import Action, ActionType
from ..core.pricing import OptionQuote, evaluate_options
from ..core.state import ConstraintKind, MatchState

#: Keep at least this much cash after buying a property.
CASH_RESERVE = 500


class BaselineSeat:
    """Fixed policy: maximise (ev_delta - price), buy when comfortably affordable."""

    seat_kind = "baseline"

    def __init__(
        self, cash_reserve: int = CASH_RESERVE, loan_to_value: int = 5000
    ) -> None:
        self.cash_reserve = cash_reserve
        self.loan_to_value = loan_to_value

    # ------------------------------------------------------------- decisions

    def choose_option(
        self, state: MatchState, spec: BoardSpec, seat_index: int
    ) -> Optional[OptionQuote]:
        if state.pending_roll is None:
            return None
        quotes = evaluate_options(state, spec, state.pending_roll, seat_index)
        if not quotes:
            return None

        if state.has_constraint(ConstraintKind.FORCED_SUM, seat_index):
            return next((q for q in quotes if q.is_sum), quotes[0])

        affordable = [q for q in quotes if q.affordable]
        pool = affordable or [q for q in quotes if q.price == 0]
        if not pool:
            return next((q for q in quotes if q.is_sum), quotes[0])
        # Net gain, ties broken on the cheaper then lower-step option so the
        # policy is fully deterministic.
        return max(pool, key=lambda q: (q.ev_delta - q.price, -q.price, -q.steps))

    def should_buy_property(
        self, state: MatchState, spec: BoardSpec, seat_index: int
    ) -> bool:
        seat = state.seat(seat_index)
        square = spec.square(seat.position)
        if not square.is_ownable or state.owner_of(seat.position) is not None:
            return False
        return seat.cash - square.price >= self.cash_reserve

    def should_accept_bribe(
        self,
        state: MatchState,
        spec: BoardSpec,
        seat_index: int,
        amount: int,
    ) -> bool:
        """Accept when the payment beats what the best non-sum option was worth."""
        if state.pending_roll is None:
            return amount > 0
        quotes = evaluate_options(state, spec, state.pending_roll, seat_index)
        best_alternative = max(
            (q.ev_delta - q.price for q in quotes if not q.is_sum and q.affordable),
            default=0,
        )
        return amount >= best_alternative

    # ------------------------------------------------------------ turn driver

    # -------------------------------------------------------------- solvency

    def raise_cash_action(
        self, state: MatchState, spec: BoardSpec, seat_index: int, needed: int
    ) -> Optional[Action]:
        """One step towards covering ``needed``, or None if nothing is left.

        Deterministic ladder, boring on purpose — this is the control group the
        balance harness measures against: sell buildings, then borrow at the
        cap, then sell squares.
        """
        if needed <= 0:
            return None

        # 1. Buildings first — they are the cheapest thing to give up.
        for square_index in state.owned_by(seat_index):
            if economy.can_sell_house(state, spec, seat_index, square_index) is None:
                return Action(
                    ActionType.SELL_HOUSE, seat_index, {"square": square_index}
                )

        # 2. Borrow against something unpledged — keeps the asset earning.
        pledged = set(state.pledged_squares(seat_index))
        free = [i for i in state.owned_by(seat_index) if i not in pledged]
        if free:
            ceiling = (
                sum(economy.square_value(spec, i) for i in free) * self.loan_to_value
            ) // 10_000
            if ceiling > 0:
                return Action(
                    ActionType.BORROW,
                    seat_index,
                    {"squares": list(free), "amount": min(ceiling, needed)},
                )

        # 3. Sell a square outright — permanent, so it comes last.
        for square_index in state.owned_by(seat_index):
            if economy.can_sell_square(state, spec, seat_index, square_index) is None:
                return Action(
                    ActionType.SELL_SQUARE, seat_index, {"square": square_index}
                )
        return None

    def next_action(
        self, state: MatchState, spec: BoardSpec, seat_index: int
    ) -> Action:
        """The single action this seat would take right now."""
        from ..core.state import Phase

        # A rent demand outranks everything: the turn cannot end while it stands.
        demand = state.pending_demand
        if demand is not None:
            if seat_index == demand.owner_seat and demand.offered is not None:
                # Boring and deterministic: take the offer rather than gamble on
                # a forced sale that might destroy the value.
                return Action(ActionType.ACCEPT_RENT_OFFER, seat_index)
            if seat_index == demand.debtor_seat:
                shortfall = demand.due - state.seat(seat_index).cash
                if shortfall <= 0:
                    return Action(ActionType.AGREE_TO_PAY, seat_index)
                raising = self.raise_cash_action(state, spec, seat_index, shortfall)
                return raising or Action(ActionType.DECLARE_BANKRUPT, seat_index)

        if state.phase == Phase.AWAIT_ROLL:
            return Action(ActionType.ROLL, seat_index)
        if state.phase == Phase.NEGOTIATE:
            return Action(ActionType.OPEN_NEGOTIATION, seat_index)
        if state.phase == Phase.AWAIT_CHOICE:
            quote = self.choose_option(state, spec, seat_index)
            steps = quote.steps if quote else sum(state.pending_roll or (0, 0))
            return Action(ActionType.CHOOSE_OPTION, seat_index, {"steps": steps})
        if self.should_buy_property(state, spec, seat_index):
            return Action(ActionType.BUY_PROPERTY, seat_index)
        return Action(ActionType.END_TURN, seat_index)


def play_match(
    spec: BoardSpec,
    config,
    max_actions: int = 4000,
    agent: Optional[BaselineSeat] = None,
) -> Tuple[MatchState, Tuple[Action, ...]]:
    """Drive a full match with baseline seats. Returns the final state and the
    action log — the exact pair the balance harness and the replay test need."""
    from ..core.engine import apply, new_match
    from ..core.state import Phase

    agent = agent or BaselineSeat()
    state = new_match(spec, config)
    actions = []
    for _ in range(max_actions):
        if state.phase == Phase.FINISHED:
            break
        action = agent.next_action(state, spec, state.turn_seat)
        state = apply(state, spec, config, action).state
        actions.append(action)
    return state, tuple(actions)
