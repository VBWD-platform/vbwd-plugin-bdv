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
    MATCH_STATUS_ACTIVE,
    MATCH_STATUS_FINISHED,
    MATCH_STATUS_LOBBY,
    OFFER_STATUS_ACCEPTED,
    OFFER_STATUS_DECLINED,
    OFFER_STATUS_OPEN,
    SEAT_KIND_HUMAN,
    BdvMatch,
)
from .board_spec_factory import BoardSpecFactory


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
    ) -> BdvMatch:
        if board.status != "published":
            raise MatchError("board is not published")
        if not (board.min_seats <= len(seats) <= board.max_seats):
            raise MatchError(
                f"seat count must be between {board.min_seats} and {board.max_seats}"
            )

        spec = BoardSpecFactory.build(board)
        if not spec.is_valid:
            raise MatchError("board does not compile to a valid spec")

        match = BdvMatch(
            board_id=board.id,
            status=MATCH_STATUS_LOBBY,
            seed=seed or secrets.token_hex(16),
            spec_snapshot=self._spec_payload(board),
            spec_hash=spec.spec_hash(),
            state_seq=0,
            chance_deck_size=len(BoardSpecFactory.cards_for(board, "chance")),
            community_deck_size=len(BoardSpecFactory.cards_for(board, "community")),
            created_by=created_by,
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
        return match

    def start(self, match: BdvMatch) -> BdvMatch:
        if match.status != MATCH_STATUS_LOBBY:
            raise MatchError("match already started")
        match.status = MATCH_STATUS_ACTIVE
        self._session.flush()
        return match

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
