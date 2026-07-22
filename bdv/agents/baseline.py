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
from ..core.state import ConstraintKind, MatchState, Phase

#: Keep at least this much cash after buying a property.
CASH_RESERVE = 500

#: What completing a stage is worth beyond the square's own value. Building is
#: the only route to serious rent, so the completing square carries a premium.
STAGE_PREMIUM = 1200

#: An agent bids this multiple of face value for a square it needs.
TRADE_PREMIUM_PCT = 1.4

#: Cash an agent keeps back after building. Higher than the buying reserve
#: because a house is worthless if the next rent bill bankrupts you.
BUILD_RESERVE = 1500


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

    # --------------------------------------------------------------- trading

    def trade_value(
        self, state: MatchState, spec: BoardSpec, seat_index: int, offer
    ) -> int:
        """What accepting this offer is worth to ``seat_index``, in credits.

        Face value plus what the swap does to STAGES, because that is the only
        reason to trade at all: a square is worth its mortgage value, but the
        square that completes a stage is worth the right to build on the whole
        stage. An agent that valued trades at face value would refuse every
        sensible deal and the window would be theatre.
        """
        if seat_index == offer.to_seat:
            incoming, outgoing = offer.give_squares, offer.want_squares
            cash_in, cash_out = offer.give_credits, offer.want_credits
        else:
            incoming, outgoing = offer.want_squares, offer.give_squares
            cash_in, cash_out = offer.want_credits, offer.give_credits

        counterparty = offer.from_seat if seat_index == offer.to_seat else offer.to_seat
        after = state
        for square_index in incoming:
            after = after.with_ownership(square_index, seat_index)
        for square_index in outgoing:
            after = after.with_ownership(square_index, counterparty)

        face = (
            sum(economy.square_value(spec, i) for i in incoming)
            - sum(economy.square_value(spec, i) for i in outgoing)
            + cash_in
            - cash_out
        )
        stage_delta = economy.completed_stages(
            after, spec, seat_index
        ) - economy.completed_stages(state, spec, seat_index)
        # A stage the counterparty completes is a rent bill this seat will meet
        # later, so it counts against the deal.
        their_delta = economy.completed_stages(
            after, spec, counterparty
        ) - economy.completed_stages(state, spec, counterparty)
        return face + STAGE_PREMIUM * stage_delta - STAGE_PREMIUM * their_delta

    def trade_response(
        self, state: MatchState, spec: BoardSpec, seat_index: int, offer
    ) -> Action:
        """Accept only what is worth accepting — otherwise say no.

        An agent that accepts everything is a free ATM; one that refuses
        everything makes the window pointless. The line is simply: does this
        leave me better off?
        """
        if self.trade_value(state, spec, seat_index, offer) > 0:
            return Action(ActionType.ACCEPT_TRADE, seat_index, {"offer_id": offer.id})
        return Action(ActionType.DECLINE_TRADE, seat_index, {"offer_id": offer.id})

    def trade_proposal(
        self, state: MatchState, spec: BoardSpec, seat_index: int
    ) -> Optional[Action]:
        """Bid cash for the one square that would complete a stage.

        Deliberately narrow: agents chase completion, not portfolios. Ties break
        on the lowest square index so the harness stays reproducible.
        """
        needs = economy.stage_needs(state, spec, seat_index)
        wanted = sorted({index for missing in needs.values() for index in missing})
        cash = state.seat(seat_index).cash
        for square_index in wanted:
            owner = state.owner_of(square_index)
            if owner is None or owner == seat_index:
                continue
            if economy.can_trade_square(state, spec, owner, square_index) is not None:
                continue
            # Pay over the odds — completion is worth more than the square is.
            price = int(economy.square_value(spec, square_index) * TRADE_PREMIUM_PCT)
            if price <= 0 or cash - price < self.cash_reserve:
                continue
            return Action(
                ActionType.PROPOSE_TRADE,
                seat_index,
                {
                    "to_seat": owner,
                    "give_credits": price,
                    "want_squares": [square_index],
                    "note": "completing a stage",
                },
            )
        return None

    def build_action(
        self, state: MatchState, spec: BoardSpec, seat_index: int
    ) -> Optional[Action]:
        """Build on the completed stage, cheapest square first.

        Without this, completing a stage is a trophy: the agents traded their
        way to whole stages and then rolled past them, so rent never grew and
        the trading window changed nothing measurable.
        """
        if state.phase != Phase.AWAIT_ROLL:
            return None
        cash = state.seat(seat_index).cash
        candidates = [
            index
            for index in state.owned_by(seat_index)
            if economy.can_build(state, spec, seat_index, index) is None
            and cash - spec.square(index).house_cost >= BUILD_RESERVE
        ]
        if not candidates:
            return None
        cheapest = min(candidates, key=lambda i: (spec.square(i).house_cost, i))
        return Action(ActionType.BUILD_HOUSE, seat_index, {"square": cheapest})

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
        # A rent demand outranks everything: the turn cannot end while it stands.
        demand = state.pending_demand
        if demand is not None:
            if seat_index == demand.owner_seat and (
                demand.offered is not None or demand.offered_square is not None
            ):
                # Deterministic, but not a pushover: a square offered in lieu is
                # taken when it is worth at least the rent, and a cash counter
                # only when it clears half. Otherwise insist — an owner who
                # always caves makes the negotiation pointless.
                if demand.offered_square is not None:
                    value = economy.square_value(spec, demand.offered_square)
                    return Action(
                        ActionType.ACCEPT_RENT_OFFER
                        if value >= demand.amount
                        else ActionType.INSIST_ON_FULL_RENT,
                        seat_index,
                    )
                return Action(
                    ActionType.ACCEPT_RENT_OFFER
                    if demand.offered * 2 >= demand.amount
                    else ActionType.INSIST_ON_FULL_RENT,
                    seat_index,
                )
            if seat_index == demand.debtor_seat:
                shortfall = demand.due - state.seat(seat_index).cash
                if shortfall <= 0:
                    return Action(ActionType.AGREE_TO_PAY, seat_index)
                raising = self.raise_cash_action(state, spec, seat_index, shortfall)
                return raising or Action(ActionType.DECLARE_BANKRUPT, seat_index)

        if state.phase == Phase.AWAIT_ROLL:
            # Assets are managed BEFORE the dice, like a human would.
            building = self.build_action(state, spec, seat_index)
            return building or Action(ActionType.ROLL, seat_index)
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

    agent = agent or BaselineSeat()
    state = new_match(spec, config)
    actions = []
    for _ in range(max_actions):
        if state.phase == Phase.FINISHED:
            break
        # The privatisation window is opened by the SERVICE in a live match, on
        # a wall clock this harness does not have. Playing it here anyway is the
        # difference between measuring the shipped game and measuring a game
        # without trading, in which no stage ever completes and nothing is built.
        if state.phase != Phase.TRADING and not state.trading_done:
            if economy.board_is_privatised(state, spec):
                state, _ = _run(
                    state, spec, config, actions, ActionType.OPEN_TRADING, 0
                )
        if state.phase == Phase.TRADING:
            state = _trade_round(state, spec, config, actions, agent)
            continue
        action = agent.next_action(state, spec, state.turn_seat)
        state = apply(state, spec, config, action).state
        actions.append(action)
    return state, tuple(actions)


def _run(state, spec, config, actions, action_type, seat_index, payload=None):
    """Apply one action, recording it. Refusals are swallowed — the harness must
    not die because a seat changed its mind between deciding and acting."""
    from ..core.engine import EngineError, apply

    action = Action(action_type, seat_index, payload or {})
    try:
        state = apply(state, spec, config, action).state
    except EngineError:
        return state, False
    actions.append(action)
    return state, True


def _trade_round(state, spec, config, actions, agent):
    """One full pass of the window: answer, bid, close.

    Bounded by construction — every seat bids at most once — so the harness can
    never spin here.
    """
    for seat in [s.index for s in state.seats if not s.bankrupt]:
        for offer in [o for o in state.trade_offers if o.to_seat == seat]:
            answer = agent.trade_response(state, spec, seat, offer)
            state, _ = _run(
                state, spec, config, actions, answer.type, seat, answer.payload
            )
        proposal = agent.trade_proposal(state, spec, seat)
        if proposal is not None:
            state, _ = _run(
                state, spec, config, actions, proposal.type, seat, proposal.payload
            )
    # Answer whatever the last bids put on the table, then close.
    for seat in [s.index for s in state.seats if not s.bankrupt]:
        for offer in [o for o in state.trade_offers if o.to_seat == seat]:
            answer = agent.trade_response(state, spec, seat, offer)
            state, _ = _run(
                state, spec, config, actions, answer.type, seat, answer.payload
            )
    state, _ = _run(
        state, spec, config, actions, ActionType.CLOSE_TRADING, state.turn_seat
    )
    return state
