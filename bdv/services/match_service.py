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
    def __init__(self, session, match_repository, action_repository, offer_repository):
        self._session = session
        self._matches = match_repository
        self._actions = action_repository
        self._offers = offer_repository

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

        agent_kinds = {SEAT_KIND_BASELINE, "llm"}
        agent_seats = {s.seat_index for s in match.seats if s.kind in agent_kinds}
        if not agent_seats:
            return 0

        spec = self.spec_for(match)
        agent = BaselineSeat()
        played = 0
        for _ in range(max_actions):
            state = self.state_for(match)
            if state.phase == Phase.FINISHED or state.turn_seat not in agent_seats:
                break
            action = agent.next_action(state, spec, state.turn_seat)
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
        return played

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
            match.id, seq, seat_index, action_type, payload or {}, result.events
        )
        self._persist_state(match, result.state)
        return result.state, list(result.events)

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
        for other in self._offers.open_for_match(match.id):
            other.status = OFFER_STATUS_DECLINED
        self._session.flush()
        return state, events

    def decline_offer(self, offer, seat_index: int):
        if offer.status != OFFER_STATUS_OPEN:
            raise MatchError("offer is no longer open")
        if offer.to_seat != seat_index:
            raise MatchError("this offer is not addressed to you")
        offer.status = OFFER_STATUS_DECLINED
        self._session.flush()
        return offer

    # -------------------------------------------------------------- helpers

    def _config(self, match: BdvMatch) -> MatchConfig:
        return MatchConfig(
            seed=match.seed,
            seat_count=len(match.seats or []) or 2,
            chance_deck_size=match.chance_deck_size,
            community_deck_size=match.community_deck_size,
        )

    def _persist_state(self, match: BdvMatch, state: MatchState) -> None:
        match.state_snapshot = state.to_dict()
        match.state_seq = state.seq
        if state.phase == Phase.FINISHED:
            match.status = MATCH_STATUS_FINISHED
            match.winner_seat_index = state.winner_seat
        self._session.flush()

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
