"""Match orchestration — the thin shell around the pure engine.

Two rules govern everything here:

1. ``bdv_action`` is the SOURCE OF TRUTH; ``state_snapshot`` is a rebuildable
   cache. Any disagreement is resolved by folding the log.
2. The client never supplies a price. Every mutating call re-derives the option
   set for the AUTHENTICATED seat at the CURRENT ``state_seq`` and recomputes
   the price server-side.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from ..core.board import BoardSpec
from ..core.engine import (
    Action,
    ActionType,
    EngineError,
    MatchConfig,
    apply,
    apply_card_effect,
    new_match,
)
from ..core.pricing import evaluate_options
from ..core.state import MatchState, Phase
from ..models.match import (
    OFFER_STATUS_EXPIRED,
    FILL_AGENTS_NOW,
    FILL_POLICIES,
    FILL_WAIT_THEN_AGENTS,
    MATCH_STATUS_ACTIVE,
    MATCH_STATUS_FINISHED,
    MATCH_STATUS_LOBBY,
    OFFER_STATUS_ACCEPTED,
    OFFER_STATUS_DECLINED,
    OFFER_STATUS_OPEN,
    SEAT_KIND_BASELINE,
    SEAT_KIND_HUMAN,
    SEAT_KIND_OPEN,
    BdvMatch,
)
from . import slug as slug_service
from .board_spec_factory import BoardSpecFactory


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    """Postgres may hand back a naive datetime depending on the driver."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class MatchError(RuntimeError):
    """A rejected match operation (maps to 4xx)."""


class StaleStateError(MatchError):
    """The caller's ``state_seq`` is behind — maps to 409 with the fresh state."""


class MatchService:
    def __init__(
        self,
        session,
        match_repository,
        action_repository,
        offer_repository,
        llm_client_factory=None,
        max_llm_calls_per_request: int = 2,
    ):
        """
        ``llm_client_factory`` maps a connection id to a core ``LlmClient``.
        Injected rather than imported so the service never reaches into core's
        container itself, and so every test runs without a provider. Left as
        None, every agent plays the deterministic baseline — which is exactly
        what the balance harness and the engine tests want.

        ``max_llm_calls_per_request`` is the reason agent fights are usable at
        all. ``advance_agents`` runs INLINE in the HTTP request, and a full
        match is hundreds of decisions; unbounded, one poll would sit there
        making provider calls until the request timed out. Instead a request
        plays a couple of thought-out moves and returns — the client is already
        polling, so the match walks forward a few moves at a time.
        """
        self._session = session
        self._matches = match_repository
        self._actions = action_repository
        self._offers = offer_repository
        self._llm_client_factory = llm_client_factory
        self._max_llm_calls_per_request = max(0, int(max_llm_calls_per_request))

    # ------------------------------------------------------------ lifecycle

    def create(
        self,
        board,
        *,
        created_by,
        seats: List[Dict],
        seed: Optional[str] = None,
        fill_policy: str = FILL_AGENTS_NOW,
        wait_minutes: Optional[int] = None,
        slug: Optional[str] = None,
    ) -> BdvMatch:
        """Create a match.

        ``fill_policy`` decides what happens to seats the creator did not fill:

        * ``agents_now``       — agents take them and the match starts at once;
        * ``wait_forever``     — they stay OPEN for humans, indefinitely;
        * ``wait_then_agents`` — open until ``wait_minutes`` elapses, then agents.

        The wait is evaluated LAZILY (``resolve_lobby``) rather than by a
        scheduler: any read or action past the deadline fills the seats. The
        feature therefore needs no extra infrastructure, and a match nobody
        looks at costs nothing.
        """
        if board.status != "published":
            raise MatchError("board is not published")
        if not (board.min_seats <= len(seats) <= board.max_seats):
            raise MatchError(
                f"seat count must be between {board.min_seats} and {board.max_seats}"
            )

        if fill_policy not in FILL_POLICIES:
            raise MatchError(f"unknown fill policy: {fill_policy!r}")
        if fill_policy == FILL_WAIT_THEN_AGENTS and not wait_minutes:
            raise MatchError("wait_then_agents requires wait_minutes")

        spec = BoardSpecFactory.build(board)
        if not spec.is_valid:
            raise MatchError("board does not compile to a valid spec")

        # A shareable handle so a player can find the table without a UUID.
        if slug:
            try:
                slug = slug_service.validate(slug)
            except slug_service.InvalidSlugError as bad:
                raise MatchError(str(bad)) from bad
            if self._matches.slug_taken(slug):
                raise MatchError(f"the slug {slug!r} is already taken")
        else:
            slug = slug_service.generate(self._matches.slug_taken)

        deadline = (
            _now() + timedelta(minutes=int(wait_minutes))
            if fill_policy == FILL_WAIT_THEN_AGENTS
            else None
        )

        match = BdvMatch(
            board_id=board.id,
            slug=slug,
            status=MATCH_STATUS_LOBBY,
            seed=seed or secrets.token_hex(16),
            spec_snapshot=self._spec_payload(board),
            spec_hash=spec.spec_hash(),
            state_seq=0,
            chance_deck_size=len(BoardSpecFactory.cards_for(board, "chance")),
            community_deck_size=len(BoardSpecFactory.cards_for(board, "community")),
            created_by=created_by,
            fill_policy=fill_policy,
            lobby_deadline_at=deadline,
        )
        self._session.add(match)
        self._session.flush()

        for position, seat in enumerate(seats):
            self._matches.add_seat(
                match,
                seat_index=position,
                kind=seat.get("kind", SEAT_KIND_HUMAN),
                user_id=seat.get("user_id"),
                agent_profile_id=seat.get("agent_profile_id"),
                display_name=seat.get("display_name") or f"Seat {position + 1}",
            )

        state = new_match(spec, self._config(match))
        self._persist_state(match, state)

        # A table with nobody left to wait for starts immediately.
        if not self._open_seats(match):
            self.start(match)
        return match

    # -------------------------------------------------------------- the lobby

    @staticmethod
    def _open_seats(match: BdvMatch) -> List:
        return [s for s in (match.seats or []) if s.kind == SEAT_KIND_OPEN]

    def join(self, match: BdvMatch, *, user_id, display_name: str):
        """Take an open seat. Starts the match when the last one is filled."""
        if match.status != MATCH_STATUS_LOBBY:
            raise MatchError("match has already started")
        if any(s.user_id and str(s.user_id) == str(user_id) for s in match.seats):
            raise MatchError("you are already seated in this match")
        open_seats = self._open_seats(match)
        if not open_seats:
            raise MatchError("no open seats")

        seat = open_seats[0]
        seat.kind = SEAT_KIND_HUMAN
        seat.user_id = user_id
        seat.display_name = display_name or seat.display_name
        self._session.flush()

        if not self._open_seats(match):
            self.start(match)
        return seat

    def fill_open_seats_with_agents(self, match: BdvMatch) -> int:
        """Convert every still-open seat into a baseline agent."""
        filled = 0
        for index, seat in enumerate(self._open_seats(match), start=1):
            seat.kind = SEAT_KIND_BASELINE
            seat.user_id = None
            seat.agent_profile_id = None
            seat.display_name = f"Agent {index}"
            filled += 1
        self._session.flush()
        return filled

    def resolve_lobby(self, match: BdvMatch) -> BdvMatch:
        """Lazily apply the fill policy. Safe to call on every read."""
        if match.status != MATCH_STATUS_LOBBY:
            return match
        if not self._open_seats(match):
            self.start(match)
            return match
        deadline = match.lobby_deadline_at
        if deadline is not None and _now() >= _aware(deadline):
            self.fill_open_seats_with_agents(match)
            self.start(match)
        return match

    def start(self, match: BdvMatch) -> BdvMatch:
        if match.status == MATCH_STATUS_ACTIVE:
            return match
        if match.status != MATCH_STATUS_LOBBY:
            raise MatchError("match already started")
        match.status = MATCH_STATUS_ACTIVE
        self._session.flush()
        return match

    # --------------------------------------------------------- the turn driver

    def advance_agents(self, match: BdvMatch, max_actions: int = 400) -> int:
        """Play every consecutive agent turn until a human is on move.

        This is what makes a mixed table playable at all. The player API only
        ever lets the authenticated user act on their OWN seat (the seat rule),
        so nothing in a browser can move an agent — without this the match sits
        forever on "waiting for Agent 1".

        Rather than require a scheduler, the server plays the agents inline
        whenever it is asked to: after any human action, and on read. Every
        agent move goes through ``submit`` like any other, so it lands in the
        same append-only log and replays identically.
        """
        from ..agents.baseline import BaselineSeat

        if match.status != MATCH_STATUS_ACTIVE:
            return 0
        # Only ONE request may drive a given match at a time. Since agent turns
        # can involve provider calls, a poll can outlast the poll interval, so
        # several requests end up inside this method for the same match at once
        # — and they each compute the same next action seq and collide on
        # uq_bdv_action_match_seq. The constraint caught it (the log never
        # corrupted) but the caller saw a 500.
        #
        # The lock is TRY, not wait: whoever holds it is already doing this
        # work, so a second request should return the current state promptly
        # rather than queue behind a provider call and time out.
        if not self._acquire_turn_lock(match):
            return 0

        agent_kinds = {SEAT_KIND_BASELINE, "llm"}
        agent_seats = {s.seat_index for s in match.seats if s.kind in agent_kinds}
        if not agent_seats:
            return 0

        spec = self.spec_for(match)
        agent = BaselineSeat()
        # One driver per seat, built once: an LlmSeat carries budget and
        # degradation state that must survive the whole loop, not be rebuilt
        # per move.
        drivers = self._seat_drivers(match, agent, agent_seats)
        llm_calls = 0
        played = 0
        for _ in range(max_actions):
            state = self.state_for(match)
            if state.phase == Phase.FINISHED:
                break
            if state.phase == Phase.TRADING:
                played += self._advance_agent_trading(match, agent, spec, agent_seats)
                self.resolve_trading_window(match)
                break

            # An agent OWNER must answer a counter-offer even though the turn
            # belongs to the debtor — otherwise a human counter hangs forever.
            demand = state.pending_demand
            if (
                demand is not None
                and demand.offered is not None
                and demand.owner_seat in agent_seats
            ):
                action = agent.next_action(state, spec, demand.owner_seat)
                try:
                    self.submit(
                        match,
                        seat_index=action.seat_index,
                        action_type=action.type,
                        payload=dict(action.payload),
                    )
                except MatchError:
                    break
                played += 1
                continue

            if state.turn_seat not in agent_seats:
                break

            driver = drivers.get(state.turn_seat, agent)
            action, reasoning, used_model = self._decide(
                driver, state, spec, state.turn_seat
            )
            try:
                self.submit(
                    match,
                    seat_index=action.seat_index,
                    action_type=action.type,
                    payload=dict(action.payload),
                    reasoning=reasoning or None,
                )
            except MatchError:
                break
            played += 1
            if used_model:
                llm_calls += 1
                # Stop while the request is still fast. The next poll picks the
                # match up exactly where this one left it — the action log is
                # the state, so nothing is held in memory between requests.
                if llm_calls >= self._max_llm_calls_per_request:
                    self._persist_llm_state(match, drivers)
                    break
        self._persist_llm_state(match, drivers)
        return played

    def _acquire_turn_lock(self, match: BdvMatch) -> bool:
        """Take a non-blocking advisory lock on this match for the transaction.

        Released automatically when the transaction ends, so there is no lock to
        leak on an error path. A backend without advisory locks degrades to the
        previous behaviour rather than refusing to play.
        """
        try:
            from sqlalchemy import text

            key = int.from_bytes(match.id.bytes[:8], "big", signed=True)
            return bool(
                self._session.execute(
                    text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key}
                ).scalar()
            )
        except Exception:
            return True

    def _seat_drivers(self, match: BdvMatch, fallback, agent_seats: set) -> Dict:
        """Resolve each agent seat to the thing that will play it.

        A seat becomes an ``LlmSeat`` only if it is bound to a profile with a
        connection AND a client factory was injected. Everything else — an
        unbound profile, a missing factory, a provider that will not build —
        plays the deterministic baseline. A table must never fail to advance
        because a model is unavailable.
        """
        from ..agents.llm_seat import build_llm_seat

        drivers: Dict[int, object] = {}
        if self._llm_client_factory is None:
            return drivers

        for seat in match.seats:
            if seat.seat_index not in agent_seats or not seat.agent_profile_id:
                continue
            profile = self._agent_profile(seat.agent_profile_id)
            if profile is None or not profile.llm_connection_id:
                continue
            try:
                driver = build_llm_seat(
                    profile,
                    client_factory=self._llm_client_factory,
                    max_repair_retries=2,
                    fallback=fallback,
                )
            except Exception:
                # No connection, no key, provider unavailable — the seat plays
                # baseline rather than the match stalling.
                continue
            driver.tokens_spent = seat.llm_tokens_spent or 0
            driver.degraded = bool(seat.llm_degraded)
            drivers[seat.seat_index] = driver
        return drivers

    def _agent_profile(self, profile_id):
        from ..models.match import BdvAgentProfile

        return self._session.get(BdvAgentProfile, profile_id)

    @staticmethod
    def _decide(driver, state, spec, seat_index) -> Tuple[object, str, bool]:
        """Ask a driver for its move, learning whether the model was consulted.

        ``BaselineSeat`` has no ``decide`` — it is pure and free — so the
        distinction is made here rather than by widening its interface for the
        benefit of one caller.
        """
        decide = getattr(driver, "decide", None)
        if decide is None:
            return driver.next_action(state, spec, seat_index), "", False
        decision = decide(state, spec, seat_index)
        return decision.action, decision.reasoning, decision.attempts > 0

    def _persist_llm_state(self, match: BdvMatch, drivers: Dict) -> None:
        """Carry budget and degradation back to the seat rows.

        Without this the driver's memory dies with the request: the per-match
        token ceiling would never bind, and a seat that had exhausted its repair
        retries would try the model again on the very next poll.
        """
        if not drivers:
            return
        for seat in match.seats:
            driver = drivers.get(seat.seat_index)
            if driver is None:
                continue
            seat.llm_tokens_spent = int(getattr(driver, "tokens_spent", 0))
            seat.llm_degraded = bool(getattr(driver, "degraded", False))
        self._session.flush()

    # ------------------------------------------------------------- reading

    def spec_for(self, match: BdvMatch) -> BoardSpec:
        """Rules the match is played under — from the SNAPSHOT, never the live
        board, which may have been edited or duplicated since."""
        return _spec_from_payload(match.spec_snapshot)

    def state_for(self, match: BdvMatch) -> MatchState:
        if match.state_snapshot:
            return MatchState.from_dict(match.state_snapshot)
        return self.rebuild_state(match)

    def rebuild_state(self, match: BdvMatch) -> MatchState:
        """Fold the action log. The snapshot is only ever a cache of this."""
        spec = self.spec_for(match)
        config = self._config(match)
        state = new_match(spec, config)
        for row in self._actions.for_match(match.id):
            state = apply(
                state,
                spec,
                config,
                Action(row.type, row.seat_index, row.payload or {}),
            ).state
        return state

    def options_for(self, match: BdvMatch, seat_index: Optional[int] = None):
        state = self.state_for(match)
        if state.pending_roll is None:
            return ()
        return evaluate_options(
            state, self.spec_for(match), state.pending_roll, seat_index
        )

    def auto_end_turn(self, match: BdvMatch) -> bool:
        """End a turn that has nothing left to decide.

        Resolving offers exactly two choices: buy the square you landed on, or
        decline it. With no purchase on offer there is no decision, and the
        "End turn" button becomes a mandatory click that conveys nothing and
        costs a round-trip.

        An UNAFFORDABLE offer is deliberately left alone — the seat may still
        sell or borrow and then buy, so ending the turn for them would take a
        real option away.

        Recorded as a normal action, like the turn and rent timeouts, so replay
        reproduces the fact rather than re-deriving the rule.
        """
        state = self.state_for(match)
        if state.phase != Phase.RESOLVING or state.pending_demand is not None:
            return False
        if self.purchase_offer(match, state.turn_seat) is not None:
            return False
        try:
            self.submit(
                match,
                seat_index=state.turn_seat,
                action_type=ActionType.END_TURN,
                payload={},
            )
        except MatchError:
            return False
        return True

    def estate(self, match: BdvMatch, seat_index: int) -> List[Dict]:
        """Every square this seat owns, with what may actually be DONE to it.

        Computed here for the same reason prices are: a "Build" button that the
        rules will refuse is worse than no button. The panel used to offer one
        on every square, so building on an incomplete funnel stage looked
        available and failed on click, every time.

        Each entry carries the refusal REASON as well, because "you cannot" is
        only half an answer — the useful half is "you must own the whole stage
        first".
        """
        from ..core import economy

        state = self.state_for(match)
        spec = self.spec_for(match)
        pledged = set(state.pledged_squares(seat_index))
        rows: List[Dict] = []
        for square_index in state.owned_by(seat_index):
            square = spec.square(square_index)
            # ``can_build`` already covers the stage rule, even building, the
            # house cap AND affordability — re-checking cash here would be a
            # second copy of a rule the engine owns.
            build_problem = economy.can_build(state, spec, seat_index, square_index)
            house_cost = square.house_cost or 0
            rows.append(
                {
                    "index": square_index,
                    "name": square.name,
                    "stage": square.stage,
                    "houses": state.houses_on(square_index),
                    "house_cost": house_cost,
                    "mortgage_value": square.mortgage_value,
                    "pledged": square_index in pledged,
                    "can_build": build_problem is None,
                    "build_blocked_because": build_problem,
                    "can_sell_house": economy.can_sell_house(
                        state, spec, seat_index, square_index
                    )
                    is None,
                    "can_sell_square": economy.can_sell_square(
                        state, spec, seat_index, square_index
                    )
                    is None,
                    "house_refund": economy.house_refund(spec, square_index),
                }
            )
        return rows

    def settlement(self, match: BdvMatch, seat_index: int) -> Optional[Dict]:
        """What this seat owes and whether it can still do anything about it.

        Computed server-side for the same reason prices are: the client must
        never decide whether conceding is legal. The UI only offers "End game"
        when the engine would actually accept it, so the button can never be a
        dead end — and never a way out for a seat that could simply pay.
        """
        from ..core import economy

        state = self.state_for(match)
        if state.phase == Phase.FINISHED or state.seat(seat_index).bankrupt:
            return None
        obligation = economy.obligation_of(state, seat_index)
        if obligation <= 0:
            return None
        spec = self.spec_for(match)
        cash = state.seat(seat_index).cash
        return {
            "due": obligation,
            "cash": cash,
            "shortfall": max(0, obligation - cash),
            "can_raise_cash": economy.can_raise_cash(state, spec, seat_index),
            "liquidation_value": economy.liquidation_value(state, spec, seat_index),
            # The only flag the UI needs: everything else is explanation.
            "must_concede": not economy.can_settle(state, spec, seat_index),
        }

    def purchase_offer(self, match: BdvMatch, seat_index: int) -> Optional[Dict]:
        """The square this seat could buy right now, or None.

        The UI must not offer "Buy this square" when the square is owned, is not
        purchasable, or the seat cannot afford it — a button that always fails is
        worse than no button.
        """
        state = self.state_for(match)
        if state.phase == Phase.FINISHED or state.turn_seat != seat_index:
            return None
        spec = self.spec_for(match)
        seat = state.seat(seat_index)
        square = spec.square(seat.position)
        if not square.is_ownable or not square.price:
            return None
        if state.owner_of(seat.position) is not None:
            return None
        return {
            "square_index": seat.position,
            "name": square.name,
            "price": square.price,
            "affordable": seat.cash >= square.price,
            "stage": square.stage,
        }

    def events_since(self, match: BdvMatch, since: int = -1) -> List[Dict]:
        return [row.to_dict() for row in self._actions.for_match(match.id, since)]

    # ------------------------------------------------------------- mutation

    def submit(
        self,
        match: BdvMatch,
        *,
        seat_index: int,
        action_type: str,
        payload: Optional[Dict] = None,
        expected_seq: Optional[int] = None,
        reasoning: Optional[str] = None,
    ) -> Tuple[MatchState, List[Dict]]:
        """The single mutating entry point. Optimistic concurrency, no locks."""
        if match.status == MATCH_STATUS_FINISHED:
            raise MatchError("match is finished")
        if expected_seq is not None and expected_seq != match.state_seq:
            raise StaleStateError(
                f"stale state_seq {expected_seq} (current {match.state_seq})"
            )

        spec = self.spec_for(match)
        config = self._config(match)
        state = self.state_for(match)

        try:
            result = apply(
                state, spec, config, Action(action_type, seat_index, payload or {})
            )
        except EngineError as rejected:
            raise MatchError(str(rejected)) from rejected

        seq = self._actions.next_seq(match.id)
        self._actions.log(
            match.id,
            seq,
            seat_index,
            action_type,
            payload or {},
            result.events,
            reasoning=reasoning,
        )
        self._persist_state(match, result.state)
        return result.state, list(result.events)

    def submit_and_settle(self, match: BdvMatch, **kwargs):
        """Submit, then run the checks that belong AFTER a state change.

        Kept separate from ``submit`` so the recursive calls inside those checks
        (trading, agents) cannot re-enter it.
        """
        result = self.submit(match, **kwargs)
        self.maybe_open_trading(match)
        return result

    def apply_drawn_card(self, match: BdvMatch, seat_index: int, effect: Dict):
        """Execute a drawn card's ops.

        Kept separate from the draw itself because the card CONTENT lives in the
        database while the deck ORDER is pure — the engine stays free of
        persistence and the draw still replays exactly.
        """
        spec = self.spec_for(match)
        state = self.state_for(match)
        result = apply_card_effect(state, spec, seat_index, effect)
        seq = self._actions.next_seq(match.id)
        self._actions.log(
            match.id, seq, seat_index, "card_effect", {"effect": effect}, result.events
        )
        self._persist_state(match, result.state)
        return result.state, list(result.events)

    # --------------------------------------------------------- rent timeout

    def rent_deadline(self, match: BdvMatch) -> Optional[datetime]:
        """When the outstanding demand auto-agrees, or None."""
        if match.turn_deadline_at is None:
            return None
        state = self.state_for(match)
        return _aware(match.turn_deadline_at) if state.pending_demand else None

    def arm_rent_timer(self, match: BdvMatch, seconds: int = 60) -> None:
        state = self.state_for(match)
        if state.pending_demand is None:
            match.turn_deadline_at = None
        elif match.turn_deadline_at is None:
            match.turn_deadline_at = _now() + timedelta(seconds=seconds)
        self._session.flush()

    def resolve_rent_timeout(self, match: BdvMatch) -> bool:
        """Fire the 60-second auto-agree if it is due.

        The clock lives HERE, never in the engine: this records an explicit
        ``rent_auto_agreed`` action, so a replay reproduces the auto-agreement
        as a fact rather than re-deriving a deadline.
        """
        state = self.state_for(match)
        demand = state.pending_demand
        if demand is None or match.turn_deadline_at is None:
            return False
        if _now() < _aware(match.turn_deadline_at):
            return False
        try:
            self.submit(
                match,
                seat_index=demand.debtor_seat,
                action_type=ActionType.RENT_AUTO_AGREED,
                payload={},
            )
        except MatchError:
            # The debtor cannot cover it — leave the demand for them to resolve
            # by selling or borrowing rather than forcing a bust on a timer.
            return False
        match.turn_deadline_at = None
        self._session.flush()
        return True

    # ------------------------------------------------------- trading window

    def _advance_agent_trading(
        self, match: BdvMatch, agent, spec, agent_seats: set
    ) -> int:
        """Answer what was put to the agents, bid once, then stand ready.

        Answering first matters: a human whose offer is still open when the
        agents mark ready would watch the window close on an unanswered
        proposal. Each agent bids at most once per window, so the pass always
        terminates — an agent that re-proposed a declined trade would spin.
        """
        played = 0
        for seat_index in sorted(agent_seats):
            state = self.state_for(match)
            for offer in [o for o in state.trade_offers if o.to_seat == seat_index]:
                action = agent.trade_response(state, spec, seat_index, offer)
                if self._try_submit(match, action):
                    played += 1
                state = self.state_for(match)

            if seat_index in state.trading_ready:
                continue
            proposal = agent.trade_proposal(state, spec, seat_index)
            if proposal is not None and self._try_submit(match, proposal):
                played += 1
            # Ready means "done initiating" — the agent still answers whatever
            # arrives next, so a human keeps a live counterparty either way.
            if self._try_submit(
                match, Action(ActionType.TRADING_READY, seat_index, {})
            ):
                played += 1
        return played

    def _try_submit(self, match: BdvMatch, action) -> bool:
        """Submit an agent action, treating a refusal as "it changed its mind".

        The board can move under an agent between deciding and acting — another
        seat may take the square it was about to bid for. That is a normal race,
        not an error worth failing the whole request over.
        """
        try:
            self.submit(
                match,
                seat_index=action.seat_index,
                action_type=action.type,
                payload=action.payload,
            )
            return True
        except MatchError:
            return False

    def maybe_open_trading(self, match: BdvMatch) -> bool:
        """Open the window the moment the last ownable square is bought.

        Fires once per match: re-opening it every time a square changes hands
        would stall the game. Buildings need a complete stage, and stages do not
        complete by luck — without this the endgame is decided by who happened
        to land where.
        """
        from ..core import economy

        state = self.state_for(match)
        if state.trading_done or state.phase in (Phase.TRADING, Phase.FINISHED):
            return False
        if not economy.board_is_privatised(state, self.spec_for(match)):
            return False
        try:
            self.submit(
                match,
                seat_index=state.turn_seat,
                action_type=ActionType.OPEN_TRADING,
                payload={},
            )
        except MatchError:
            return False
        match.turn_deadline_at = _now() + timedelta(
            seconds=self._trading_seconds(match)
        )
        self._session.flush()
        return True

    def resolve_trading_window(self, match: BdvMatch) -> bool:
        """Close on the deadline, or as soon as every solvent seat is ready."""
        state = self.state_for(match)
        if state.phase != Phase.TRADING:
            return False

        solvent = {s.index for s in state.seats if not s.bankrupt}
        everyone_ready = solvent and solvent <= set(state.trading_ready)
        expired = match.turn_deadline_at is not None and _now() >= _aware(
            match.turn_deadline_at
        )
        if not (everyone_ready or expired):
            return False

        self.submit(
            match,
            seat_index=state.turn_seat,
            action_type=ActionType.CLOSE_TRADING,
            payload={},
        )
        self._session.flush()
        return True

    @staticmethod
    def _trading_seconds(match: BdvMatch) -> int:
        try:
            return int(match.spec_snapshot["board"]["trading_window_seconds"])
        except Exception:
            return 300

    # --------------------------------------------------------- turn timeout

    def resolve_turn_timeout(self, match: BdvMatch) -> bool:
        """Fire the turn deadline: take the FREE sum, the existing fate default.

        Like the rent timer, the clock lives HERE and the outcome is a recorded
        action, so a replay reproduces it rather than re-deriving a deadline.
        """
        if match.status != MATCH_STATUS_ACTIVE or match.turn_deadline_at is None:
            return False
        state = self.state_for(match)
        if state.pending_demand is not None:
            return False  # the rent timer owns this deadline
        if _now() < _aware(match.turn_deadline_at):
            return False

        seat_index = state.turn_seat
        try:
            if state.phase == Phase.AWAIT_ROLL:
                self.submit(match, seat_index=seat_index, action_type=ActionType.ROLL)
            elif state.phase in (Phase.NEGOTIATE, Phase.AWAIT_CHOICE):
                self.expire_offers(match)
                self.submit(
                    match, seat_index=seat_index, action_type=ActionType.TURN_AUTO_SUM
                )
            else:
                self.submit(
                    match, seat_index=seat_index, action_type=ActionType.END_TURN
                )
        except MatchError:
            return False
        self._session.flush()
        return True

    def turn_deadline(self, match: BdvMatch) -> Optional[datetime]:
        if match.turn_deadline_at is None:
            return None
        return _aware(match.turn_deadline_at)

    # -------------------------------------------------------------- offers

    def offer_bribe(
        self, match: BdvMatch, *, from_seat: int, to_seat: int, amount: int
    ):
        state = self.state_for(match)
        if state.phase != Phase.NEGOTIATE:
            raise MatchError("the negotiation window is not open")
        if from_seat == to_seat:
            raise MatchError("a seat cannot bribe itself")
        if amount <= 0:
            raise MatchError("offer amount must be positive")
        if state.seat(from_seat).cash < amount:
            raise MatchError("insufficient cash to make this offer")

        # Escrow up front so the offer cannot evaporate before it is answered.
        self.submit(
            match,
            seat_index=from_seat,
            action_type=ActionType.ESCROW_BRIBE,
            payload={"amount": amount},
        )
        return self._offers.create(
            match_id=match.id,
            turn_seq=match.state_seq,
            kind="bribe_to_fate",
            from_seat=from_seat,
            to_seat=to_seat,
            amount=amount,
            status=OFFER_STATUS_OPEN,
        )

    def accept_offer(self, match: BdvMatch, offer, seat_index: int):
        if offer.status != OFFER_STATUS_OPEN:
            raise MatchError("offer is no longer open")
        if offer.to_seat != seat_index:
            raise MatchError("this offer is not addressed to you")
        state, events = self.submit(
            match,
            seat_index=seat_index,
            action_type=ActionType.ACCEPT_BRIBE,
            payload={"from_seat": offer.from_seat, "amount": offer.amount},
        )
        offer.status = OFFER_STATUS_ACCEPTED
        # Every other open offer loses — refund what they escrowed.
        for other in self._offers.open_for_match(match.id):
            if other.id == offer.id:
                continue
            self._refund(match, other)
            other.status = OFFER_STATUS_DECLINED
        self._session.flush()
        return state, events

    def decline_offer(self, match: BdvMatch, offer, seat_index: int):
        if offer.status != OFFER_STATUS_OPEN:
            raise MatchError("offer is no longer open")
        if offer.to_seat != seat_index:
            raise MatchError("this offer is not addressed to you")
        self._refund(match, offer)
        offer.status = OFFER_STATUS_DECLINED
        self._session.flush()
        return offer

    def _refund(self, match: BdvMatch, offer) -> None:
        self.submit(
            match,
            seat_index=offer.from_seat,
            action_type=ActionType.REFUND_BRIBE,
            payload={"amount": offer.amount},
        )

    def expire_offers(self, match: BdvMatch) -> int:
        """Refund and close every open offer from a turn that has moved on.

        An offer only makes sense for the roll it was made against; once the
        negotiation window is gone the escrowed money must come back.
        """
        expired = 0
        for offer in self._offers.open_for_match(match.id):
            if (
                offer.turn_seq >= match.state_seq
                and self.state_for(match).phase == Phase.NEGOTIATE
            ):
                continue
            self._refund(match, offer)
            offer.status = OFFER_STATUS_EXPIRED
            expired += 1
        if expired:
            self._session.flush()
        return expired

    # -------------------------------------------------------------- helpers

    @staticmethod
    def _turn_seconds(match: BdvMatch) -> int:
        try:
            return int(match.spec_snapshot["board"]["turn_timeout_seconds"])
        except Exception:
            return 120

    def _config(self, match: BdvMatch) -> MatchConfig:
        return MatchConfig(
            seed=match.seed,
            seat_count=len(match.seats or []) or 2,
            chance_deck_size=match.chance_deck_size,
            community_deck_size=match.community_deck_size,
        )

    def _persist_state(self, match: BdvMatch, state: MatchState) -> None:
        # One deadline field, two meanings: 60s while a rent demand stands,
        # otherwise the board's turn timeout.
        #
        # It is re-armed only when the thing being timed actually CHANGES,
        # compared against the PERSISTED previous snapshot. An in-memory marker
        # would be absent on every fresh request and silently re-arm the clock,
        # so a countdown could never expire.
        previous = match.state_snapshot or {}
        was = (
            previous.get("turn_seat"),
            previous.get("phase"),
            previous.get("pending_demand") is not None,
        )
        now_signature = (
            state.turn_seat,
            state.phase.value,
            state.pending_demand is not None,
        )

        match.state_snapshot = state.to_dict()
        match.state_seq = state.seq

        if state.phase == Phase.FINISHED:
            match.turn_deadline_at = None
        elif was != now_signature or match.turn_deadline_at is None:
            seconds = (
                60 if state.pending_demand is not None else self._turn_seconds(match)
            )
            match.turn_deadline_at = _now() + timedelta(seconds=seconds)

        if state.phase == Phase.FINISHED:
            match.status = MATCH_STATUS_FINISHED
            match.winner_seat_index = state.winner_seat
            self._record_results(match, state)
        self._session.flush()

    @staticmethod
    def _record_results(match: BdvMatch, state: MatchState) -> None:
        """Freeze each seat's result at the moment the match ends.

        Lifetime agent statistics are then one GROUP BY instead of a replay of
        every match a profile ever sat in. A bankrupt seat holds nothing, so its
        zero is a fact rather than a special case.
        """
        for seat in match.seats:
            if seat.seat_index >= len(state.seats):
                continue
            final = state.seat(seat.seat_index)
            seat.final_cash = 0 if final.bankrupt else final.cash
            seat.is_winner = seat.seat_index == state.winner_seat

    @staticmethod
    def _spec_payload(board) -> Dict:
        return {
            "board": board.to_dict(),
            "squares": [square.to_dict() for square in board.squares],
            "cards": [card.to_dict() for card in board.cards],
        }


def _spec_from_payload(payload: Dict) -> BoardSpec:
    """Rebuild a pure spec from the frozen snapshot (no DB access)."""
    import dataclasses
    from decimal import Decimal

    from ..core.board import SquareKind, SquareSpec
    from ..core.effects import effect_ev_hint

    board = payload["board"]
    squares = tuple(
        SquareSpec(
            index=row["index"],
            kind=SquareKind(row["kind"]),
            name=row["name"],
            stage=row.get("stage"),
            price=row.get("price", 0),
            rent_table=tuple(row.get("rent_table") or ()),
            service_multipliers=tuple(row.get("service_multipliers") or ()),
            house_cost=row.get("house_cost", 0),
            mortgage_value=row.get("mortgage_value", 0),
            tax_amount=row.get("tax_amount", 0),
        )
        for row in sorted(payload["squares"], key=lambda r: r["index"])
    )
    spec = BoardSpec(
        squares=squares,
        starting_cash=board["starting_cash"],
        go_salary=board["go_salary"],
        jail_fine=board["jail_fine"],
        jail_penalty_ev=board["jail_penalty_ev"],
        k_price=Decimal(board["k_price"]),
        k_acquire=Decimal(board["k_acquire"]),
        cap_pct=Decimal(board["cap_pct"]),
        fee_policy=board["fee_policy"],
        max_houses=board.get("max_houses", 5),
    )

    def deck_hint(deck: str) -> int:
        cards = [
            c
            for c in payload.get("cards", [])
            if c["deck"] == deck and c.get("is_active", True)
        ]
        if not cards:
            return 0
        weight_total = sum(max(1, c.get("weight", 1)) for c in cards)
        weighted = 0
        for card in cards:
            try:
                weighted += effect_ev_hint(card["effect"], spec) * max(
                    1, card.get("weight", 1)
                )
            except Exception:
                continue
        return int(round(weighted / weight_total)) if weight_total else 0

    return dataclasses.replace(
        spec,
        chance_ev_hint=deck_hint("chance"),
        community_ev_hint=deck_hint("community"),
    )
