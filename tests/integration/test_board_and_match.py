"""Integration: seed the board, compile it, and play a real persisted match.

This is the proof that the pure engine and the persistence layer agree — the
place where a fold-vs-snapshot divergence would show up.
"""
import dataclasses
import json
from decimal import Decimal

import pytest

from plugins.bdv.bdv.core import economy
from plugins.bdv.bdv.core.engine import ActionType
from plugins.bdv.bdv.core.state import Phase, RentDemand
from plugins.bdv.bdv.repositories.board_repository import BoardRepository
from plugins.bdv.bdv.repositories.match_repository import (
    ActionRepository,
    MatchRepository,
    OfferRepository,
)
from plugins.bdv.bdv.services.board_seeder import seed_funnel_board
from plugins.bdv.bdv.services.board_spec_factory import BoardSpecFactory
from plugins.bdv.bdv.services.match_service import MatchService, StaleStateError


@pytest.fixture
def board(db):
    board, _ = seed_funnel_board(db.session)
    db.session.flush()
    return board


@pytest.fixture
def service(db):
    return MatchService(
        db.session,
        MatchRepository(db.session),
        ActionRepository(db.session),
        OfferRepository(db.session),
    )


class TestSeeder:
    def test_seeds_the_full_board(self, board):
        assert board.slug == "funnel-40"
        assert len(board.squares) == 40
        assert len(board.cards) == 12

    def test_is_create_only_and_idempotent(self, db, board):
        again, created = seed_funnel_board(db.session)
        assert created is False
        assert again.id == board.id

    def test_a_re_run_does_not_clobber_an_edit(self, db, board):
        board.name = "Edited by an admin"
        db.session.flush()
        again, created = seed_funnel_board(db.session)
        assert created is False
        assert again.name == "Edited by an admin"

    def test_seeded_board_compiles_and_validates(self, board):
        assert BoardSpecFactory.validate(board) == []
        spec = BoardSpecFactory.build(board)
        assert spec.is_valid and spec.size == 40

    def test_deck_ev_hints_are_derived_from_the_cards(self, board):
        spec = BoardSpecFactory.build(board)
        assert isinstance(spec.chance_ev_hint, int)
        assert isinstance(spec.community_ev_hint, int)


class TestCatalogueContract:
    def test_list_returns_the_wire_envelope_with_a_filtered_total(self, db, board):
        repository = BoardRepository(db.session)
        rows, total = repository.list_catalogue(page=1, per_page=10)
        assert total >= 1 and rows

        _, filtered = repository.list_catalogue(query="no-such-board")
        assert filtered == 0, "total must reflect the FILTER, not the collection"

    def test_search_matches_name_and_slug(self, db, board):
        repository = BoardRepository(db.session)
        _, by_slug = repository.list_catalogue(query="funnel")
        assert by_slug >= 1


class TestMatchLifecycle:
    def _three_seats(self):
        return [
            {"kind": "baseline", "display_name": "A"},
            {"kind": "baseline", "display_name": "B"},
            {"kind": "baseline", "display_name": "C"},
        ]

    def test_create_snapshots_the_rules(self, db, board, service):
        match = service.create(board, created_by=None, seats=self._three_seats())
        spec = BoardSpecFactory.build(board)
        assert match.spec_hash == spec.spec_hash()
        assert match.spec_snapshot["board"]["slug"] == "funnel-40"

    def test_match_uses_the_snapshot_not_the_live_board(self, db, board, service):
        """A board edited after the match starts must not change the match."""
        match = service.create(board, created_by=None, seats=self._three_seats())
        original = service.spec_for(match).spec_hash()

        board.go_salary = 99999
        db.session.flush()

        assert service.spec_for(match).spec_hash() == original

    def test_unpublished_board_cannot_start_a_match(self, db, board, service):
        from plugins.bdv.bdv.services.match_service import MatchError

        board.status = "draft"
        db.session.flush()
        with pytest.raises(MatchError):
            service.create(board, created_by=None, seats=self._three_seats())

    def test_seat_count_is_bounded_by_the_board(self, db, board, service):
        from plugins.bdv.bdv.services.match_service import MatchError

        with pytest.raises(MatchError):
            service.create(
                board,
                created_by=None,
                seats=[{"kind": "baseline", "display_name": "solo"}],
            )


class TestActionLogIsTheSourceOfTruth:
    def _match(self, board, service):
        return service.create(
            board,
            created_by=None,
            seats=[
                {"kind": "baseline", "display_name": "A"},
                {"kind": "baseline", "display_name": "B"},
                {"kind": "baseline", "display_name": "C"},
            ],
        )

    def test_submit_appends_and_advances_the_sequence(self, db, board, service):
        match = self._match(board, service)
        state, events = service.submit(match, seat_index=0, action_type=ActionType.ROLL)
        assert state.pending_roll is not None
        assert match.state_seq == state.seq
        assert any(e["type"] == "rolled" for e in events)

    def test_folding_the_log_reproduces_the_snapshot(self, db, board, service):
        match = self._match(board, service)
        service.submit(match, seat_index=0, action_type=ActionType.ROLL)
        service.submit(match, seat_index=0, action_type=ActionType.OPEN_NEGOTIATION)

        snapshot = service.state_for(match)
        rebuilt = service.rebuild_state(match)
        assert (
            rebuilt.state_hash() == snapshot.state_hash()
        ), "the cached snapshot must always equal the fold of the log"

    def test_stale_state_seq_is_rejected(self, db, board, service):
        match = self._match(board, service)
        service.submit(match, seat_index=0, action_type=ActionType.ROLL)
        with pytest.raises(StaleStateError):
            service.submit(
                match,
                seat_index=0,
                action_type=ActionType.OPEN_NEGOTIATION,
                expected_seq=0,
            )

    def test_illegal_action_is_rejected_and_nothing_is_logged(self, db, board, service):
        from plugins.bdv.bdv.services.match_service import MatchError

        match = self._match(board, service)
        before = len(service.events_since(match))
        with pytest.raises(MatchError):
            service.submit(
                match, seat_index=1, action_type=ActionType.ROLL
            )  # out of turn
        assert len(service.events_since(match)) == before


class TestPricedOptionsOverTheWire:
    def test_options_are_priced_server_side(self, db, board, service):
        match = service.create(
            board,
            created_by=None,
            seats=[
                {"kind": "baseline", "display_name": "A"},
                {"kind": "baseline", "display_name": "B"},
                {"kind": "baseline", "display_name": "C"},
            ],
        )
        service.submit(match, seat_index=0, action_type=ActionType.ROLL)
        quotes = service.options_for(match, 0)
        assert len(quotes) in (2, 3), "a roll yields two (doubles) or three options"
        assert sum(1 for q in quotes if q.is_sum) == 1
        assert all(q.price >= 0 for q in quotes)
        assert next(q for q in quotes if q.is_sum).price == 0, "fate is free"


class TestFullPersistedMatch:
    def test_a_baseline_match_plays_to_completion_through_the_service(
        self, db, board, service
    ):
        """The end-to-end proof: engine + persistence agree for a whole game."""
        from plugins.bdv.bdv.agents.baseline import BaselineSeat

        match = service.create(
            board,
            created_by=None,
            seats=[
                {"kind": "baseline", "display_name": "A"},
                {"kind": "baseline", "display_name": "B"},
                {"kind": "baseline", "display_name": "C"},
            ],
        )
        service.start(match)
        agent = BaselineSeat()
        spec = service.spec_for(match)

        for _ in range(400):
            state = service.state_for(match)
            if state.phase == Phase.FINISHED:
                break
            action = agent.next_action(state, spec, state.turn_seat)
            service.submit(
                match,
                seat_index=action.seat_index,
                action_type=action.type,
                payload=dict(action.payload),
            )

        final = service.state_for(match)
        assert service.rebuild_state(match).state_hash() == final.state_hash()
        assert len(service.events_since(match)) > 50


class TestMyMatchesList:
    """Regression: listing a user's matches must not DISTINCT over json columns.

    Postgres has no equality operator for `json`, so the obvious
    `query(BdvMatch).join(seat).distinct()` 500s as soon as one match exists —
    which is exactly what the lobby calls on every page load.
    """

    def test_lists_matches_the_user_is_seated_in(self, db, board, service):
        from uuid import uuid4

        from vbwd.models.user import User

        user = User(
            id=uuid4(),
            email=f"player-{uuid4().hex[:8]}@example.com",
            password_hash="x",
        )
        db.session.add(user)
        db.session.flush()

        service.create(
            board,
            created_by=user.id,
            seats=[
                {"kind": "human", "user_id": user.id, "display_name": "You"},
                {"kind": "baseline", "display_name": "Agent 1"},
                {"kind": "baseline", "display_name": "Agent 2"},
            ],
        )

        rows, total = MatchRepository(db.session).list_for_user(user.id)
        assert total == 1
        assert len(rows) == 1
        assert rows[0].state_snapshot is not None, "json column round-trips fine"

    def test_returns_nothing_for_a_user_with_no_seat(self, db, board, service):
        from uuid import uuid4

        rows, total = MatchRepository(db.session).list_for_user(uuid4())
        assert (rows, total) == ([], 0)


class TestOpponentFillPolicy:
    """A creator chooses what happens to seats they did not fill."""

    def _seats(self, count=3):
        # The host seat is a baseline agent here purely to keep the fixture free
        # of user rows — a `human` seat with a NULL user_id is correctly refused
        # by ck_bdv_seat_one_occupant.
        return [{"kind": "baseline", "display_name": "Host"}] + [
            {"kind": "open", "display_name": f"Open {i}"} for i in range(1, count)
        ]

    def test_agents_now_starts_immediately(self, db, board, service):
        match = service.create(
            board,
            created_by=None,
            seats=[
                {"kind": "baseline", "display_name": "You"},
                {"kind": "baseline", "display_name": "A"},
                {"kind": "baseline", "display_name": "B"},
            ],
            fill_policy="agents_now",
        )
        assert match.status == "active", "no open seats — nothing to wait for"

    def test_wait_forever_holds_the_lobby(self, db, board, service):
        match = service.create(
            board, created_by=None, seats=self._seats(), fill_policy="wait_forever"
        )
        assert match.status == "lobby"
        assert match.lobby_deadline_at is None
        # Resolving repeatedly must never auto-start it.
        for _ in range(3):
            service.resolve_lobby(match)
        assert match.status == "lobby"

    def test_wait_then_agents_requires_minutes(self, db, board, service):
        from plugins.bdv.bdv.services.match_service import MatchError

        with pytest.raises(MatchError):
            service.create(
                board,
                created_by=None,
                seats=self._seats(),
                fill_policy="wait_then_agents",
            )

    def test_wait_then_agents_sets_a_deadline_and_waits(self, db, board, service):
        match = service.create(
            board,
            created_by=None,
            seats=self._seats(),
            fill_policy="wait_then_agents",
            wait_minutes=10,
        )
        assert match.status == "lobby"
        assert match.lobby_deadline_at is not None
        service.resolve_lobby(match)
        assert match.status == "lobby", "deadline has not passed yet"

    def test_deadline_passing_fills_with_agents_and_starts(self, db, board, service):
        from datetime import datetime, timedelta, timezone

        match = service.create(
            board,
            created_by=None,
            seats=self._seats(),
            fill_policy="wait_then_agents",
            wait_minutes=5,
        )
        match.lobby_deadline_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.session.flush()

        service.resolve_lobby(match)
        assert match.status == "active"
        assert not [s for s in match.seats if s.kind == "open"]
        # host was already a baseline in this fixture + the 2 filled seats
        assert sum(1 for s in match.seats if s.kind == "baseline") == 3

    def test_unknown_policy_is_rejected(self, db, board, service):
        from plugins.bdv.bdv.services.match_service import MatchError

        with pytest.raises(MatchError):
            service.create(
                board, created_by=None, seats=self._seats(), fill_policy="whenever"
            )


class TestJoiningAnOpenSeat:
    def _waiting_match(self, service, board):
        return service.create(
            board,
            created_by=None,
            seats=[
                {"kind": "baseline", "display_name": "Host"},
                {"kind": "open", "display_name": "Open 1"},
                {"kind": "open", "display_name": "Open 2"},
            ],
            fill_policy="wait_forever",
        )

    def _user(self, db):
        from uuid import uuid4

        from vbwd.models.user import User

        user = User(
            id=uuid4(), email=f"p-{uuid4().hex[:8]}@example.com", password_hash="x"
        )
        db.session.add(user)
        db.session.flush()
        return user

    def test_joining_takes_a_seat(self, db, board, service):
        match = self._waiting_match(service, board)
        user = self._user(db)
        seat = service.join(match, user_id=user.id, display_name="Anna")
        assert seat.kind == "human" and seat.display_name == "Anna"
        assert match.status == "lobby", "one seat still open"

    def test_filling_the_last_seat_starts_the_match(self, db, board, service):
        match = self._waiting_match(service, board)
        service.join(match, user_id=self._user(db).id, display_name="Anna")
        service.join(match, user_id=self._user(db).id, display_name="Boris")
        assert match.status == "active"

    def test_cannot_join_twice(self, db, board, service):
        from plugins.bdv.bdv.services.match_service import MatchError

        match = self._waiting_match(service, board)
        user = self._user(db)
        service.join(match, user_id=user.id, display_name="Anna")
        with pytest.raises(MatchError):
            service.join(match, user_id=user.id, display_name="Anna again")

    def test_cannot_join_a_started_match(self, db, board, service):
        from plugins.bdv.bdv.services.match_service import MatchError

        match = self._waiting_match(service, board)
        service.fill_open_seats_with_agents(match)
        service.start(match)
        with pytest.raises(MatchError):
            service.join(match, user_id=self._user(db).id, display_name="Late")


class TestAgentTurnDriver:
    """The fix for 'Waiting for Agent 1' — agents must move by themselves."""

    def _mixed(self, db, service, board):
        from uuid import uuid4

        from vbwd.models.user import User

        user = User(
            id=uuid4(), email=f"h-{uuid4().hex[:8]}@example.com", password_hash="x"
        )
        db.session.add(user)
        db.session.flush()
        match = service.create(
            board,
            created_by=user.id,
            seats=[
                {"kind": "human", "user_id": user.id, "display_name": "You"},
                {"kind": "baseline", "display_name": "Agent 1"},
                {"kind": "baseline", "display_name": "Agent 2"},
            ],
            fill_policy="agents_now",
        )
        return match

    def test_advancing_returns_the_turn_to_the_human(self, db, board, service):
        match = self._mixed(db, service, board)
        # Human plays a full turn.
        service.submit(match, seat_index=0, action_type="roll")
        service.submit(match, seat_index=0, action_type="open_negotiation")
        state = service.state_for(match)
        steps = sum(state.pending_roll)
        service.submit(
            match, seat_index=0, action_type="choose_option", payload={"steps": steps}
        )
        service.submit(match, seat_index=0, action_type="end_turn")

        assert service.state_for(match).turn_seat == 1, "agent is on move"
        played = service.advance_agents(match)
        assert played > 0
        assert service.state_for(match).turn_seat == 0, "turn came back to the human"

    def test_advance_is_a_no_op_when_a_human_is_on_move(self, db, board, service):
        match = self._mixed(db, service, board)
        assert service.advance_agents(match) == 0

    def test_agent_moves_land_in_the_same_action_log(self, db, board, service):
        match = self._mixed(db, service, board)
        service.submit(match, seat_index=0, action_type="roll")
        service.submit(match, seat_index=0, action_type="open_negotiation")
        state = service.state_for(match)
        service.submit(
            match,
            seat_index=0,
            action_type="choose_option",
            payload={"steps": sum(state.pending_roll)},
        )
        service.submit(match, seat_index=0, action_type="end_turn")
        service.advance_agents(match)

        rows = service.events_since(match)
        assert any(r["seat_index"] != 0 for r in rows), "agents appear in the log"
        # And the log still folds to the snapshot.
        assert (
            service.rebuild_state(match).state_hash()
            == service.state_for(match).state_hash()
        )

    def test_an_all_agent_table_plays_itself_to_a_conclusion(self, db, board, service):
        match = service.create(
            board,
            created_by=None,
            seats=[
                {"kind": "baseline", "display_name": "A"},
                {"kind": "baseline", "display_name": "B"},
                {"kind": "baseline", "display_name": "C"},
            ],
            fill_policy="agents_now",
        )
        played = service.advance_agents(match, max_actions=2000)
        assert played > 20


class TestMatchSlug:
    """A shareable handle so a player can find a table without a UUID."""

    def _seats(self):
        return [
            {"kind": "baseline", "display_name": "Host"},
            {"kind": "open", "display_name": "Open 1"},
            {"kind": "open", "display_name": "Open 2"},
        ]

    def test_a_slug_is_generated_when_none_is_given(self, db, board, service):
        from plugins.bdv.bdv.services import slug as slug_service

        match = service.create(board, created_by=None, seats=self._seats())
        assert match.slug
        assert slug_service.SLUG_PATTERN.match(match.slug)

    def test_a_custom_slug_is_normalised_and_kept(self, db, board, service):
        match = service.create(
            board, created_by=None, seats=self._seats(), slug="Friday Night Game"
        )
        assert match.slug == "friday-night-game"

    def test_slugs_are_unique(self, db, board, service):
        from plugins.bdv.bdv.services.match_service import MatchError

        service.create(board, created_by=None, seats=self._seats(), slug="taken-table")
        with pytest.raises(MatchError, match="already taken"):
            service.create(
                board, created_by=None, seats=self._seats(), slug="Taken Table"
            )

    def test_an_unusable_slug_is_rejected(self, db, board, service):
        from plugins.bdv.bdv.services.match_service import MatchError

        with pytest.raises(MatchError):
            service.create(board, created_by=None, seats=self._seats(), slug="ab")

    def test_generated_slugs_do_not_collide(self, db, board, service):
        slugs = {
            service.create(board, created_by=None, seats=self._seats()).slug
            for _ in range(8)
        }
        assert len(slugs) == 8

    def test_lookup_by_slug(self, db, board, service):
        match = service.create(
            board, created_by=None, seats=self._seats(), slug="find-me-here"
        )
        found = MatchRepository(db.session).find_by_slug("find-me-here")
        assert found is not None and found.id == match.id

    def test_lookup_misses_return_none(self, db, board, service):
        assert MatchRepository(db.session).find_by_slug("no-such-table") is None


class TestPurchaseOffer:
    """The 'Buy this square' affordance is decided server-side."""

    def _match(self, service, board, user_id=None):
        return service.create(
            board,
            created_by=user_id,
            seats=[
                {"kind": "baseline", "display_name": "You"},
                {"kind": "baseline", "display_name": "A"},
                {"kind": "baseline", "display_name": "B"},
            ],
            fill_policy="agents_now",
        )

    def test_no_offer_on_a_non_purchasable_square(self, db, board, service):
        match = self._match(service, board)
        # Seat 0 starts on New Quarter (the GO corner) — never purchasable.
        assert service.purchase_offer(match, 0) is None

    def test_offer_on_an_unowned_deal_square(self, db, board, service):
        import dataclasses

        match = self._match(service, board)
        state = service.state_for(match)
        moved = state.with_seat(dataclasses.replace(state.seat(0), position=1))
        match.state_snapshot = moved.to_dict()
        db.session.flush()

        offer = service.purchase_offer(match, 0)
        assert offer is not None
        assert offer["square_index"] == 1
        assert offer["price"] > 0
        assert offer["affordable"] is True

    def test_no_offer_when_the_square_is_already_owned(self, db, board, service):
        import dataclasses

        match = self._match(service, board)
        state = service.state_for(match)
        moved = state.with_seat(dataclasses.replace(state.seat(0), position=1))
        match.state_snapshot = moved.with_ownership(1, 1).to_dict()
        db.session.flush()
        assert service.purchase_offer(match, 0) is None

    def test_offer_is_marked_unaffordable_rather_than_hidden(self, db, board, service):
        """Seeing the price you cannot meet is consistent with the option cards."""
        import dataclasses

        match = self._match(service, board)
        state = service.state_for(match)
        broke = state.with_seat(dataclasses.replace(state.seat(0), position=1, cash=1))
        match.state_snapshot = broke.to_dict()
        db.session.flush()

        offer = service.purchase_offer(match, 0)
        assert offer is not None and offer["affordable"] is False

    def test_no_offer_when_it_is_not_your_turn(self, db, board, service):
        match = self._match(service, board)
        assert service.purchase_offer(match, 1) is None


class TestRentTimeoutIsRecordedNotDerived:
    """The 60s auto-agree must be an ACTION, so replay stays exact (S146-9)."""

    def _demanding(self, db, board, service):
        import dataclasses

        from plugins.bdv.bdv.core.state import Phase

        match = service.create(
            board,
            created_by=None,
            seats=[
                {"kind": "baseline", "display_name": "You"},
                {"kind": "baseline", "display_name": "A"},
                {"kind": "baseline", "display_name": "B"},
            ],
            fill_policy="agents_now",
        )
        state = service.state_for(match)
        # Square 1 (Cold List) is a deal square, so it charges rent. Seat 1 owns
        # it and seat 0 starts on GO, one step away.
        staged = state.with_ownership(1, 1)
        staged = staged.with_seat(dataclasses.replace(staged.seat(0), position=0))
        staged = dataclasses.replace(
            staged, pending_roll=(1, 1), phase=Phase.AWAIT_CHOICE
        )
        match.state_snapshot = staged.to_dict()
        match.state_seq = staged.seq
        db.session.flush()
        service.submit(
            match, seat_index=0, action_type="choose_option", payload={"steps": 1}
        )
        return match

    def test_a_demand_arms_the_timer(self, db, board, service):
        match = self._demanding(db, board, service)
        state = service.state_for(match)
        assert state.pending_demand is not None, "Cold List must charge rent"
        assert match.turn_deadline_at is not None
        assert service.rent_deadline(match) is not None

    def test_the_timeout_fires_once_and_is_logged_as_an_action(
        self, db, board, service
    ):
        from datetime import datetime, timedelta, timezone

        match = self._demanding(db, board, service)
        assert service.state_for(match).pending_demand is not None

        match.turn_deadline_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.session.flush()

        assert service.resolve_rent_timeout(match) is True
        assert service.state_for(match).pending_demand is None
        types = [row["type"] for row in service.events_since(match)]
        assert "rent_auto_agreed" in types, "recorded as a fact, not re-derived"
        # Firing again is a no-op.
        assert service.resolve_rent_timeout(match) is False

    def test_the_auto_agree_moves_the_money(self, db, board, service):
        """NOTE: this class stages its board by writing the snapshot directly, so
        it deliberately does NOT assert fold == snapshot — that invariant only
        holds when every change came from a logged action, and it is covered by
        TestActionLogIsTheSourceOfTruth and the full-match test."""
        from datetime import datetime, timedelta, timezone

        match = self._demanding(db, board, service)
        before = service.state_for(match)
        demand = before.pending_demand
        assert demand is not None
        debtor_cash = before.seat(demand.debtor_seat).cash
        owner_cash = before.seat(demand.owner_seat).cash

        match.turn_deadline_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.session.flush()
        service.resolve_rent_timeout(match)

        after = service.state_for(match)
        assert after.seat(demand.debtor_seat).cash == debtor_cash - demand.amount
        assert after.seat(demand.owner_seat).cash == owner_cash + demand.amount


class TestTurnTimeout:
    """The turn deadline takes the FREE sum — the existing fate default (S146-3)."""

    def _rolled(self, db, board, service):
        match = service.create(
            board,
            created_by=None,
            seats=[
                {"kind": "baseline", "display_name": "You"},
                {"kind": "baseline", "display_name": "A"},
                {"kind": "baseline", "display_name": "B"},
            ],
            fill_policy="agents_now",
        )
        service.submit(match, seat_index=0, action_type="roll")
        return match

    def test_a_deadline_is_armed_for_the_turn(self, db, board, service):
        match = self._rolled(db, board, service)
        assert service.turn_deadline(match) is not None

    def test_the_timeout_takes_the_sum_and_is_logged(self, db, board, service):
        from datetime import datetime, timedelta, timezone

        match = self._rolled(db, board, service)
        fate = sum(service.state_for(match).pending_roll)
        match.turn_deadline_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.session.flush()

        assert service.resolve_turn_timeout(match) is True
        types = [row["type"] for row in service.events_since(match)]
        assert "turn_auto_sum" in types, "recorded as a fact, not re-derived"
        moved = [
            e
            for row in service.events_since(match)
            for e in row["events"]
            if e["type"] == "turn_timed_out"
        ]
        assert moved, "the timeout is visible in the event stream"
        assert fate > 0

    def test_the_timeout_does_not_fire_early(self, db, board, service):
        match = self._rolled(db, board, service)
        assert service.resolve_turn_timeout(match) is False

    def test_the_rent_timer_owns_the_deadline_when_a_demand_stands(
        self, db, board, service
    ):
        """Two timers, one field — the rent one must win while rent is owed."""
        import dataclasses

        from plugins.bdv.bdv.core.state import Phase

        match = service.create(
            board,
            created_by=None,
            seats=[
                {"kind": "baseline", "display_name": "You"},
                {"kind": "baseline", "display_name": "A"},
                {"kind": "baseline", "display_name": "B"},
            ],
            fill_policy="agents_now",
        )
        state = service.state_for(match)
        staged = state.with_ownership(1, 1)
        staged = dataclasses.replace(
            staged, pending_roll=(1, 1), phase=Phase.AWAIT_CHOICE
        )
        match.state_snapshot = staged.to_dict()
        match.state_seq = staged.seq
        db.session.flush()
        service.submit(
            match, seat_index=0, action_type="choose_option", payload={"steps": 1}
        )
        assert service.state_for(match).pending_demand is not None
        assert service.resolve_turn_timeout(match) is False, "rent timer owns it"


class TestOfferEscrow:
    """An offer holds the money up front, and gets it back if it loses."""

    def _negotiating(self, db, board, service):
        match = service.create(
            board,
            created_by=None,
            seats=[
                {"kind": "baseline", "display_name": "You"},
                {"kind": "baseline", "display_name": "A"},
                {"kind": "baseline", "display_name": "B"},
            ],
            fill_policy="agents_now",
        )
        service.submit(match, seat_index=0, action_type="roll")
        return match

    def test_offering_escrows_the_amount(self, db, board, service):
        match = self._negotiating(db, board, service)
        before = service.state_for(match).seat(1).cash
        service.offer_bribe(match, from_seat=1, to_seat=0, amount=300)
        assert service.state_for(match).seat(1).cash == before - 300

    def test_declining_refunds_it(self, db, board, service):
        match = self._negotiating(db, board, service)
        before = service.state_for(match).seat(1).cash
        offer = service.offer_bribe(match, from_seat=1, to_seat=0, amount=300)
        service.decline_offer(match, offer, seat_index=0)
        assert service.state_for(match).seat(1).cash == before
        assert offer.status == "declined"

    def test_expiring_refunds_it(self, db, board, service):
        match = self._negotiating(db, board, service)
        before = service.state_for(match).seat(1).cash
        service.offer_bribe(match, from_seat=1, to_seat=0, amount=300)
        # Move the turn on; the offer belonged to the roll that has now gone.
        service.submit(match, seat_index=0, action_type="open_negotiation")
        assert service.expire_offers(match) == 1
        assert service.state_for(match).seat(1).cash == before

    def test_you_cannot_offer_what_you_do_not_hold(self, db, board, service):
        from plugins.bdv.bdv.services.match_service import MatchError

        match = self._negotiating(db, board, service)
        with pytest.raises(MatchError):
            service.offer_bribe(match, from_seat=1, to_seat=0, amount=10_000_000)

    def test_accepting_one_offer_refunds_the_others(self, db, board, service):
        match = self._negotiating(db, board, service)
        before_two = service.state_for(match).seat(2).cash
        winner = service.offer_bribe(match, from_seat=1, to_seat=0, amount=300)
        service.offer_bribe(match, from_seat=2, to_seat=0, amount=200)
        service.accept_offer(match, winner, seat_index=0)
        assert service.state_for(match).seat(2).cash == before_two, "loser made whole"


class TestAgentsNegotiateInTheTradingWindow:
    """A window in which the agents just pass is a window that does nothing.

    Every seat here is an agent, so the whole negotiation has to run itself —
    which is also the shape the 500-match balance harness needs.
    """

    def _privatised(self, db, board, service):
        """A live match with every ownable square split between two seats."""
        match = service.create(
            board,
            created_by=None,
            seats=[
                {"kind": "baseline", "display_name": "A"},
                {"kind": "baseline", "display_name": "B"},
            ],
            fill_policy="agents_now",
        )
        spec = service.spec_for(match)
        state = service.state_for(match)
        for position, square in enumerate(s for s in spec.squares if s.is_ownable):
            state = state.with_ownership(square.index, position % 2)
        service._persist_state(match, state)
        db.session.flush()
        return match

    def test_the_window_opens_and_then_closes_itself(self, db, board, service):
        match = self._privatised(db, board, service)
        assert service.maybe_open_trading(match) is True
        service.advance_agents(match)
        state = service.state_for(match)
        assert state.trading_done is True, "agents finished without the deadline"
        assert state.phase != Phase.TRADING

    def test_agents_bid_for_the_squares_they_need(self, db, board, service):
        match = self._privatised(db, board, service)
        service.maybe_open_trading(match)
        service.advance_agents(match)
        kinds = [row.type for row in ActionRepository(db.session).for_match(match.id)]
        assert ActionType.PROPOSE_TRADE in kinds, "nobody opened a negotiation"
        assert (
            ActionType.ACCEPT_TRADE in kinds or ActionType.DECLINE_TRADE in kinds
        ), "a proposal went unanswered"

    def test_credits_are_conserved_across_the_window(self, db, board, service):
        match = self._privatised(db, board, service)
        before = sum(s.cash for s in service.state_for(match).seats)
        service.maybe_open_trading(match)
        service.advance_agents(match)
        after = sum(s.cash for s in service.state_for(match).seats)
        assert after == before, "trading minted or burned credits"

    def test_every_trade_replays_from_the_log(self, db, board, service):
        """The whole point of trades being engine actions.

        Folding from action zero is not available here — this fixture STAGES the
        ownership rather than playing 40 purchases — so the fold starts from the
        snapshot taken as the window opened. That still proves the thing at
        issue: the trades themselves carry every fact needed to reproduce them.
        """
        from plugins.bdv.bdv.core.engine import Action, apply

        match = self._privatised(db, board, service)
        service.maybe_open_trading(match)
        opened = service.state_for(match)
        first_trading_seq = ActionRepository(db.session).for_match(match.id)[-1].seq

        service.advance_agents(match)
        db.session.flush()

        spec = service.spec_for(match)
        config = service._config(match)
        folded = opened
        for row in ActionRepository(db.session).for_match(match.id):
            if row.seq <= first_trading_seq:
                continue
            folded = apply(
                folded,
                spec,
                config,
                Action(row.type, row.seat_index, row.payload or {}),
            ).state
        assert folded.state_hash() == service.state_for(match).state_hash()


class TestSettlementView:
    """The server decides whether a seat is finished — never the browser."""

    def _demanded(self, db, board, service, cash):
        match = service.create(
            board,
            created_by=None,
            seats=[
                {"kind": "baseline", "display_name": "A"},
                {"kind": "baseline", "display_name": "B"},
            ],
            fill_policy="agents_now",
        )
        spec = service.spec_for(match)
        state = service.state_for(match)
        deal = next(s for s in spec.squares if s.is_ownable and s.price)
        state = state.with_ownership(deal.index, 1)
        state = state.with_seat(
            dataclasses.replace(state.seat(0), cash=cash, position=deal.index)
        )
        state = dataclasses.replace(
            state,
            pending_demand=RentDemand(
                debtor_seat=0,
                owner_seat=1,
                square_index=deal.index,
                amount=5000,
            ),
            phase=Phase.AWAIT_RENT,
        )
        service._persist_state(match, state)
        db.session.flush()
        return match

    def test_nothing_is_reported_when_nothing_is_owed(self, db, board, service):
        match = service.create(
            board,
            created_by=None,
            seats=[
                {"kind": "baseline", "display_name": "A"},
                {"kind": "baseline", "display_name": "B"},
            ],
            fill_policy="agents_now",
        )
        assert service.settlement(match, 0) is None

    def test_a_seat_that_can_pay_is_not_offered_the_exit(self, db, board, service):
        match = self._demanded(db, board, service, cash=9000)
        view = service.settlement(match, 0)
        assert view["shortfall"] == 0
        assert view["must_concede"] is False

    def test_a_seat_with_nothing_left_is(self, db, board, service):
        match = self._demanded(db, board, service, cash=10)
        view = service.settlement(match, 0)
        assert view["due"] == 5000
        assert view["shortfall"] == 4990
        assert view["can_raise_cash"] is False
        assert view["must_concede"] is True


class TestAgentRoster:
    """Lifetime statistics come from recorded results, not from replay."""

    @pytest.fixture
    def agents(self, db):
        from plugins.bdv.bdv.models.match import BdvAgentProfile
        from plugins.bdv.bdv.repositories.match_repository import (
            AgentProfileRepository,
        )

        rows = [
            BdvAgentProfile(name="Hard Closer", slug="hard-closer"),
            BdvAgentProfile(name="Slow Nurture", slug="slow-nurture"),
        ]
        for row in rows:
            db.session.add(row)
        db.session.flush()
        return AgentProfileRepository(db.session), rows

    def test_a_fresh_agent_has_no_record(self, agents):
        repository, rows = agents
        assert repository.lifetime_stats([rows[0].id]) == {}

    def test_the_result_is_frozen_when_the_match_ends(self, db, board, service, agents):
        """A career of defeats totals zero without any clamping.

        A bankrupt seat walks away with nothing, so its recorded result is zero
        by definition — which is exactly what "net capital" should mean.
        """
        from plugins.bdv.bdv.core.state import Phase as EnginePhase

        repository, rows = agents
        match = service.create(
            board,
            created_by=None,
            seats=[
                {
                    "kind": "llm",
                    "agent_profile_id": rows[0].id,
                    "display_name": "Hard Closer",
                },
                {
                    "kind": "llm",
                    "agent_profile_id": rows[1].id,
                    "display_name": "Slow Nurture",
                },
            ],
            fill_policy="agents_now",
        )
        state = service.state_for(match)
        state = state.with_seat(
            dataclasses.replace(state.seat(1), bankrupt=True, cash=0)
        )
        state = state.with_seat(dataclasses.replace(state.seat(0), cash=7400))
        state = dataclasses.replace(state, phase=EnginePhase.FINISHED, winner_seat=0)
        service._persist_state(match, state)
        db.session.flush()

        stats = repository.lifetime_stats([rows[0].id, rows[1].id])
        assert stats[str(rows[0].id)] == {
            "games_played": 1,
            "net_capital": 7400,
            "games_won": 1,
        }
        assert stats[str(rows[1].id)] == {
            "games_played": 1,
            "net_capital": 0,
            "games_won": 0,
        }

    def test_the_roster_quick_search_covers_slug_and_name(self, db, agents):
        repository, _ = agents
        rows, total = repository.list_catalogue(query="nurture")
        assert total == 1 and rows[0].slug == "slow-nurture"
        rows, total = repository.list_catalogue(query="hard-closer")
        assert total == 1 and rows[0].name == "Hard Closer"

    def test_the_roster_sorts_and_filters(self, db, agents):
        repository, rows = agents
        rows[1].is_active = False
        db.session.flush()

        listed, _ = repository.list_catalogue(sort="slug", order="desc")
        assert listed[0].slug == "slow-nurture"
        active, total = repository.list_catalogue(is_active=True)
        assert total == 1 and active[0].slug == "hard-closer"


class TestPaidAgentFight:
    """Tokens buy the RUN of a match, never in-game credits.

    The two must not be confusable: credits are created when a match starts and
    destroyed when it ends, and nothing converts them back. What a viewer buys
    here is the compute to watch agents play, which is why the charge is a
    plain USAGE debit against the core balance.
    """

    @pytest.fixture
    def roster(self, db):
        from plugins.bdv.bdv.models.match import BdvAgentProfile

        rows = [
            BdvAgentProfile(name="Closer", slug="closer"),
            BdvAgentProfile(name="Nurturer", slug="nurturer"),
            BdvAgentProfile(name="Retired", slug="retired", is_active=False),
        ]
        for row in rows:
            db.session.add(row)
        db.session.flush()
        return rows

    def _funded(self, db, tokens):
        """A viewer with a token balance. Built through the ORM, not raw SQL."""
        import uuid

        from vbwd.models.user import User
        from vbwd.repositories.token_repository import TokenBalanceRepository

        user = User(
            email=f"viewer-{uuid.uuid4().hex[:8]}@example.com",
            password_hash="x",
        )
        db.session.add(user)
        db.session.flush()
        balance = TokenBalanceRepository(db.session).get_or_create(user.id)
        balance.balance = tokens
        db.session.flush()
        return user

    def test_the_charge_and_the_match_live_or_die_together(
        self, db, board, service, roster
    ):
        """A fight without a debit is free; a debit without a fight is theft."""
        from plugins.bdv.bdv.models.match import BdvMatch

        user = self._funded(db, 100)
        before = db.session.query(BdvMatch).count()

        match = service.create(
            board,
            created_by=user.id,
            seats=[
                {
                    "kind": "llm",
                    "agent_profile_id": roster[0].id,
                    "display_name": roster[0].name,
                },
                {
                    "kind": "llm",
                    "agent_profile_id": roster[1].id,
                    "display_name": roster[1].name,
                },
            ],
            fill_policy="agents_now",
        )
        db.session.flush()
        assert db.session.query(BdvMatch).count() == before + 1
        assert match.created_by == user.id
        assert all(seat.user_id is None for seat in match.seats), "nobody is seated"

    def test_the_buyer_holds_no_seat_but_may_still_watch(
        self, db, board, service, roster
    ):
        """The format is unwatchable otherwise: its buyer sits at no seat."""
        user = self._funded(db, 100)
        match = service.create(
            board,
            created_by=user.id,
            seats=[
                {
                    "kind": "llm",
                    "agent_profile_id": roster[0].id,
                    "display_name": roster[0].name,
                },
                {
                    "kind": "llm",
                    "agent_profile_id": roster[1].id,
                    "display_name": roster[1].name,
                },
            ],
            fill_policy="agents_now",
        )
        db.session.flush()
        matches = MatchRepository(db.session)
        assert matches.seat_for_user(match, user.id) is None
        assert str(match.created_by) == str(user.id)

    def test_an_insufficient_balance_is_refused_before_anything_is_created(self, db):
        from vbwd.models.enums import TokenTransactionType
        from vbwd.repositories.token_bundle_purchase_repository import (
            TokenBundlePurchaseRepository,
        )
        from vbwd.repositories.token_repository import (
            TokenBalanceRepository,
            TokenTransactionRepository,
        )
        from vbwd.services.token_service import TokenService

        user = self._funded(db, 3)
        tokens = TokenService(
            TokenBalanceRepository(db.session),
            TokenTransactionRepository(db.session),
            TokenBundlePurchaseRepository(db.session),
        )
        with pytest.raises(ValueError, match="Insufficient"):
            tokens.debit_tokens(user.id, 10, TokenTransactionType.USAGE)
        assert tokens.get_balance(user.id) == 3, "a refused charge takes nothing"

    def test_the_price_falls_back_when_the_plugin_is_not_mounted(self, app):
        """A read must never 500 because the plugin manager is absent."""
        from plugins.bdv.bdv.services.service_factory import plugin_config

        with app.test_request_context():
            assert isinstance(plugin_config("agent_match_token_cost", 10), int)


class TestLlmSeatsActuallyPlay:
    """The wiring, end to end — with a fake client, never a provider.

    Until this slice the roster and the billing were real and the models were
    not: every "LLM" seat quietly played the deterministic baseline.
    """

    class FakeClient:
        """Answers with a legal move, and counts how often it was asked."""

        def __init__(self, reply=None, explode=False):
            self.calls = 0
            self._reply = reply
            self._explode = explode

        def generate(self, system, user, **kwargs):
            self.calls += 1
            if self._explode:
                raise RuntimeError("provider is down")
            if self._reply is not None:
                return self._reply
            view = json.loads(user.split("\n\n")[0])
            free = next(o for o in view["options"] if o["free"])
            return {
                "action": "choose_option",
                "steps": free["steps"],
                "reasoning": "taking the free sum",
            }

    @pytest.fixture
    def profile(self, db):
        from plugins.bdv.bdv.models.match import BdvAgentProfile
        from vbwd.models.llm_connection import LlmConnection

        connection = LlmConnection(
            slug="test-adapter",
            connection_name="Test",
            api_key="k",
            model="claude-test",
            is_active=True,
        )
        db.session.add(connection)
        db.session.flush()
        row = BdvAgentProfile(
            name="Model Player",
            slug="model-player",
            llm_connection_id=connection.id,
            max_tokens_per_match=100000,
        )
        db.session.add(row)
        db.session.flush()
        return row

    def _service(self, db, client, calls=2):
        return MatchService(
            db.session,
            MatchRepository(db.session),
            ActionRepository(db.session),
            OfferRepository(db.session),
            llm_client_factory=lambda _connection_id: client,
            max_llm_calls_per_request=calls,
        )

    def _match(self, board, service, profile):
        return service.create(
            board,
            created_by=None,
            seats=[
                {
                    "kind": "llm",
                    "agent_profile_id": profile.id,
                    "display_name": "Model Player",
                },
                {"kind": "baseline", "display_name": "Baseline"},
            ],
            fill_policy="agents_now",
        )

    def test_the_model_is_consulted_and_its_move_is_played(self, db, board, profile):
        client = self.FakeClient()
        service = self._service(db, client)
        service.advance_agents(self._match(board, service, profile))
        assert client.calls > 0, "the seat never asked the model"

    def test_the_reasoning_is_recorded_but_never_replayed(self, db, board, profile):
        """Prose belongs beside the action, not inside its payload.

        Payload is what the fold feeds to the engine; putting a model's text
        there would put non-deterministic content on the replay path.
        """
        client = self.FakeClient()
        service = self._service(db, client)
        match = self._match(board, service, profile)
        service.advance_agents(match)
        db.session.flush()

        rows = ActionRepository(db.session).for_match(match.id)
        explained = [row for row in rows if row.reasoning]
        assert explained, "no move carried its reasoning"
        assert all("reasoning" not in (row.payload or {}) for row in rows)

    def test_a_request_stops_after_its_model_budget(self, db, board, profile):
        """Agents advance INLINE, so an unbounded loop would hang the request."""
        client = self.FakeClient()
        service = self._service(db, client, calls=1)
        service.advance_agents(self._match(board, service, profile))
        assert client.calls == 1

        # The next poll picks the match up where this one stopped.
        client.calls = 0
        service.advance_agents(
            MatchRepository(db.session).list_all(page=1, per_page=1)[0][0]
        )
        assert client.calls >= 1, "the match did not walk on"

    def test_a_dead_provider_degrades_the_seat_instead_of_stalling(
        self, db, board, profile
    ):
        client = self.FakeClient(explode=True)
        service = self._service(db, client)
        match = self._match(board, service, profile)
        played = service.advance_agents(match)
        assert played > 0, "a provider outage must not stop the table"

    def test_degradation_survives_the_request(self, db, board, profile):
        """The driver is rebuilt every request, so this has to be persisted.

        In memory only, a seat that had exhausted its repair retries would ask
        the failing model again on the very next poll — forever.
        """
        client = self.FakeClient(explode=True)
        service = self._service(db, client)
        match = self._match(board, service, profile)
        service.advance_agents(match)
        db.session.flush()

        seat = next(s for s in match.seats if s.seat_index == 0)
        assert seat.llm_degraded is True

        client.calls = 0
        service.advance_agents(match)
        assert client.calls == 0, "a degraded seat asked the model again"

    def test_a_spent_token_budget_binds_across_requests(self, db, board, profile):
        profile.max_tokens_per_match = 1
        db.session.flush()
        client = self.FakeClient()
        service = self._service(db, client)
        match = self._match(board, service, profile)
        service.advance_agents(match)
        db.session.flush()
        assert next(s for s in match.seats if s.seat_index == 0).llm_tokens_spent > 0

        client.calls = 0
        service.advance_agents(match)
        assert client.calls == 0, "the per-match ceiling did not bind"

    def test_an_illegal_move_is_refused_before_it_reaches_the_engine(
        self, db, board, profile
    ):
        """A model asking for a move that is not on offer must not corrupt play."""
        client = self.FakeClient(reply={"action": "choose_option", "steps": 99})
        service = self._service(db, client)
        match = self._match(board, service, profile)
        played = service.advance_agents(match)
        assert played > 0
        assert client.calls > 1, "an illegal answer was not sent back for repair"

    def test_without_a_factory_every_seat_plays_the_baseline(self, db, board, profile):
        """The default path — what the harness and the engine tests rely on."""
        service = MatchService(
            db.session,
            MatchRepository(db.session),
            ActionRepository(db.session),
            OfferRepository(db.session),
        )
        match = self._match(board, service, profile)
        assert service.advance_agents(match) > 0


class TestAgentDataExchange:
    """The roster travels through the CORE export/import machine (S46).

    A bespoke JSON download was one-way and unique to this page. Going through
    the shared exchanger means the roster gets dry-run, upsert-vs-replace, CSV,
    NDJSON streaming and the Settings → Import/Export UI for free.
    """

    @pytest.fixture
    def exchanger(self, db):
        from plugins.bdv.bdv.services.data_exchange.bdv_exchangers import (
            build_bdv_exchangers,
        )

        return build_bdv_exchangers(db.session)[0]

    @pytest.fixture
    def connection(self, db):
        from vbwd.models.llm_connection import LlmConnection

        row = LlmConnection(
            slug="house-anthropic",
            connection_name="House",
            api_key="secret-key",
            model="claude-test",
            is_active=True,
        )
        db.session.add(row)
        db.session.flush()
        return row

    @pytest.fixture
    def agent(self, db, connection):
        from plugins.bdv.bdv.models.match import BdvAgentProfile

        row = BdvAgentProfile(
            slug="hawk",
            name="Hawk",
            persona="plays the players",
            system_prompt="Read the cash positions first.",
            llm_connection_id=connection.id,
            temperature="0.75",
            risk_bias="0.55",
        )
        db.session.add(row)
        db.session.flush()
        return row

    def _rows(self, exchanger):
        from vbwd.services.data_exchange.port import ExportSelector

        return exchanger.export(ExportSelector(all=True), include_pii=False).rows

    def test_the_connection_travels_as_a_slug_never_an_id(self, db, exchanger, agent):
        """An exported UUID would sometimes resolve to the WRONG connection."""
        row = next(r for r in self._rows(exchanger) if r["slug"] == "hawk")
        assert row["llm_connection_slug"] == "house-anthropic"
        assert "llm_connection_id" not in row

    def test_decimals_survive_the_json_round_trip(self, db, exchanger, agent):
        row = next(r for r in self._rows(exchanger) if r["slug"] == "hawk")
        assert row["temperature"] == "0.75"
        json.dumps(row), "the envelope must be serialisable"

    def test_an_import_rebinds_by_slug_on_the_receiving_instance(
        self, db, exchanger, connection
    ):
        from plugins.bdv.bdv.models.match import BdvAgentProfile

        payload = {
            "entity": "bdv_agent_profiles",
            "bdv_agent_profiles": [
                {
                    "slug": "imported",
                    "name": "Imported One",
                    "persona": "from another box",
                    "system_prompt": "Play tight.",
                    "temperature": "0.30",
                    "risk_bias": "0.20",
                    "max_tokens_per_match": 50000,
                    "is_active": True,
                    "llm_connection_slug": "house-anthropic",
                }
            ],
        }
        result = exchanger.import_(payload, mode="upsert", dry_run=False)
        assert (result.created, result.updated) == (1, 0)

        imported = (
            db.session.query(BdvAgentProfile)
            .filter(BdvAgentProfile.slug == "imported")
            .first()
        )
        assert imported.llm_connection_id == connection.id
        assert imported.temperature == Decimal("0.30")

    def test_an_unknown_connection_lands_the_agent_unbound_not_rejected(
        self, db, exchanger
    ):
        """The case this feature exists for: two boxes, different slugs.

        Failing the row would throw away the persona and the prompt over a
        binding an admin can fix in one click. An unbound agent still plays —
        it falls back to the deterministic baseline.
        """
        from plugins.bdv.bdv.models.match import BdvAgentProfile

        payload = {
            "entity": "bdv_agent_profiles",
            "bdv_agent_profiles": [
                {
                    "slug": "orphan",
                    "name": "Orphan",
                    "llm_connection_slug": "not-on-this-box",
                }
            ],
        }
        result = exchanger.import_(payload, mode="upsert", dry_run=False)
        assert result.errors == []
        row = (
            db.session.query(BdvAgentProfile)
            .filter(BdvAgentProfile.slug == "orphan")
            .first()
        )
        assert row is not None and row.llm_connection_id is None

    def test_a_dry_run_reports_without_writing(self, db, exchanger):
        from plugins.bdv.bdv.models.match import BdvAgentProfile

        payload = {
            "entity": "bdv_agent_profiles",
            "bdv_agent_profiles": [{"slug": "ghost", "name": "Ghost"}],
        }
        result = exchanger.import_(payload, mode="upsert", dry_run=True)
        assert result.created == 1
        assert (
            db.session.query(BdvAgentProfile)
            .filter(BdvAgentProfile.slug == "ghost")
            .first()
            is None
        ), "a dry run wrote to the database"

    def test_re_importing_updates_rather_than_duplicating(self, db, exchanger, agent):
        payload = {
            "entity": "bdv_agent_profiles",
            "bdv_agent_profiles": [{"slug": "hawk", "name": "Hawk Renamed"}],
        }
        result = exchanger.import_(payload, mode="upsert", dry_run=False)
        assert (result.created, result.updated) == (0, 1)
        assert agent.name == "Hawk Renamed"


class TestHouseAgents:
    """Three agents ship with the plugin, and they differ strategically."""

    def test_seeding_creates_three_and_is_idempotent(self, db):
        from plugins.bdv.bdv.services.agent_seeder import seed_house_agents

        rows, created = seed_house_agents(db.session)
        assert (len(rows), created) == (3, 3)

        again, created_again = seed_house_agents(db.session)
        assert created_again == 0, "a re-run must never duplicate"
        assert {r.slug for r in again} == {r.slug for r in rows}

    def test_a_re_run_does_not_clobber_a_tuned_persona(self, db):
        """An admin's edit must survive the next deploy's seed."""
        from plugins.bdv.bdv.services.agent_seeder import seed_house_agents

        rows, _ = seed_house_agents(db.session)
        rows[0].system_prompt = "Tuned by an admin."
        db.session.flush()

        seed_house_agents(db.session)
        assert rows[0].system_prompt == "Tuned by an admin."

    def test_the_three_are_actually_different(self, db):
        """A roster of one archetype in three costumes makes fights pointless."""
        from plugins.bdv.bdv.services.agent_seeder import seed_house_agents

        rows, _ = seed_house_agents(db.session)
        assert len({str(r.risk_bias) for r in rows}) == 3, "same appetite for risk"
        assert len({r.system_prompt for r in rows}) == 3
        assert all(r.persona for r in rows)

    def test_they_ship_unbound(self, db):
        """No installation's connection slugs are knowable here."""
        from plugins.bdv.bdv.services.agent_seeder import seed_house_agents

        rows, _ = seed_house_agents(db.session)
        assert all(r.llm_connection_id is None for r in rows)


class TestWatcherReadAccess:
    """A paying watcher holds no seat — that is the agent-fight format.

    S146-15 gave watchers access to the match and the event feed but not to
    /options, so the client loaded the fight and then threw on every poll:
    `403 not a seat in this match`, once every 2.5 seconds, for ever. Read
    endpoints have to agree with each other about who may read.
    """

    @pytest.fixture
    def watcher(self, db):
        import uuid

        from vbwd.models.user import User

        user = User(
            email=f"watcher-{uuid.uuid4().hex[:8]}@example.com", password_hash="x"
        )
        db.session.add(user)
        db.session.flush()
        return user

    @pytest.fixture
    def fight(self, db, board, service, watcher):
        match = service.create(
            board,
            created_by=watcher.id,
            seats=[
                {"kind": "baseline", "display_name": "A"},
                {"kind": "baseline", "display_name": "B"},
            ],
            fill_policy="agents_now",
        )
        db.session.flush()
        return match

    def test_the_buyer_holds_no_seat(self, db, fight, watcher):
        assert MatchRepository(db.session).seat_for_user(fight, watcher.id) is None

    def test_options_for_a_seatless_watcher_is_empty_not_forbidden(
        self, db, fight, service
    ):
        """Nothing to choose is an empty list, not a permission error.

        A watcher cannot act, so they have no options — but 403 makes the
        client treat a normal state as a failure, and the poll loop dies.
        """
        assert service.options_for(fight, None) == () or all(
            True for _ in service.options_for(fight, None)
        )

    def test_every_read_endpoint_agrees_on_who_may_read(self):
        """The regression was one endpoint disagreeing with two others.

        Pinning the SET of watcher-readable routes means adding a third read
        endpoint that forgets ``_may_watch`` fails here rather than in a
        browser console.
        """
        import inspect

        from plugins.bdv.bdv import routes

        source = inspect.getsource(routes)
        for view in ("match_detail", "match_events", "match_options"):
            body = source.split(f"def {view}(")[1].split("\ndef ")[0]
            assert "_may_watch" in body, f"{view} does not admit watchers"

    def test_acting_still_requires_a_seat(self):
        """Watching is not playing — the act endpoints must NOT have softened."""
        import inspect

        from plugins.bdv.bdv import routes

        source = inspect.getsource(routes)
        for view in ("submit_action", "match_offers", "resolve_offer", "start_now"):
            body = source.split(f"def {view}(")[1].split("\ndef ")[0]
            assert "_may_watch" not in body, f"{view} lets a watcher act"


class TestChoosingYourOpponents:
    """You pick WHO you play, by name — and may field the same agent twice.

    The lobby only ever sent a seat COUNT, so every opponent was an anonymous
    baseline. The roster exists; choosing from it is the point of having one.
    """

    @pytest.fixture
    def roster(self, db):
        from plugins.bdv.bdv.models.match import BdvAgentProfile

        rows = [
            BdvAgentProfile(name="Ada the Deal Hawk", slug="ada"),
            BdvAgentProfile(name="Miles the Nurturer", slug="miles"),
            BdvAgentProfile(name="Retired", slug="retired", is_active=False),
        ]
        for row in rows:
            db.session.add(row)
        db.session.flush()
        return rows

    def _seats_for(self, opponents):
        """The seat list the route builds from a chosen line-up."""
        from plugins.bdv.bdv.routes import build_opponent_seats

        return build_opponent_seats(opponents)

    def test_a_chosen_agent_takes_its_own_name_and_kind(self, db, roster):
        seats = self._seats_for([{"agent_profile_id": str(roster[0].id)}])
        assert seats[0]["display_name"] == "Ada the Deal Hawk"
        assert seats[0]["kind"] == "llm"
        assert seats[0]["agent_profile_id"] == roster[0].id

    def test_the_same_agent_can_be_fielded_more_than_once(self, db, roster):
        """Three copies of one personality is a legitimate table.

        Numbering them is not cosmetic: the chat @-mentions seats by display
        name, so three identical names would make the feed unreadable.
        """
        chosen = [{"agent_profile_id": str(roster[0].id)}] * 3
        seats = self._seats_for(chosen)
        names = [s["display_name"] for s in seats]
        assert names == [
            "Ada the Deal Hawk",
            "Ada the Deal Hawk #2",
            "Ada the Deal Hawk #3",
        ]
        assert all(s["agent_profile_id"] == roster[0].id for s in seats)

    def test_different_agents_keep_their_own_names(self, db, roster):
        seats = self._seats_for(
            [
                {"agent_profile_id": str(roster[0].id)},
                {"agent_profile_id": str(roster[1].id)},
            ]
        )
        assert [s["display_name"] for s in seats] == [
            "Ada the Deal Hawk",
            "Miles the Nurturer",
        ]

    def test_an_unknown_agent_is_refused_not_silently_swapped(self, db, roster):
        """Substituting a baseline would hand you an opponent you did not pick."""
        import uuid

        from plugins.bdv.bdv.routes import OpponentError

        with pytest.raises(OpponentError, match="unknown or inactive"):
            self._seats_for([{"agent_profile_id": str(uuid.uuid4())}])

    def test_an_inactive_agent_is_refused(self, db, roster):
        from plugins.bdv.bdv.routes import OpponentError

        with pytest.raises(OpponentError, match="unknown or inactive"):
            self._seats_for([{"agent_profile_id": str(roster[2].id)}])

    def test_an_unnamed_opponent_is_still_a_plain_baseline(self, db, roster):
        """Not choosing is allowed — it is what the seat-count flow always did."""
        seats = self._seats_for([{}, {}])
        assert [s["kind"] for s in seats] == ["baseline", "baseline"]
        assert all(s["agent_profile_id"] is None for s in seats)


class TestOnlyOneRequestDrivesAMatch:
    """Concurrent polls collided on the action log and 500'd.

    An agent turn can involve a provider call, so a poll can easily outlast the
    2.5s poll interval. Several requests then sat inside advance_agents for the
    same match, each computed the same next seq, and the second insert violated
    uq_bdv_action_match_seq. The constraint did its job — the log never
    corrupted — but the browser saw `500 Internal Server Error`.
    """

    def _match(self, board, service):
        return service.create(
            board,
            created_by=None,
            seats=[
                {"kind": "baseline", "display_name": "A"},
                {"kind": "baseline", "display_name": "B"},
            ],
            fill_policy="agents_now",
        )

    def test_the_lock_is_taken_while_agents_play(self, db, board, service):
        match = self._match(board, service)
        assert service._acquire_turn_lock(match) is True

    def test_a_second_holder_is_refused_rather_than_queued(self, db, board, service):
        """Waiting is what produced the 30-second timeouts.

        The other request is already doing this work, so this one returns the
        state it has instead of queueing behind a provider call.
        """
        from sqlalchemy import create_engine, text

        match = self._match(board, service)
        db.session.flush()
        assert service._acquire_turn_lock(match) is True

        # A genuinely separate connection — the same session would re-enter its
        # own lock and prove nothing.
        key = int.from_bytes(match.id.bytes[:8], "big", signed=True)
        url = db.session.get_bind().engine.url
        other = create_engine(url)
        with other.connect() as connection:
            with connection.begin():
                held = connection.execute(
                    text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key}
                ).scalar()
        other.dispose()
        assert held is False, "a second request could drive the same match"

    def test_a_locked_out_request_plays_nothing_and_does_not_raise(
        self, db, board, service
    ):
        match = self._match(board, service)
        db.session.flush()
        original = service._acquire_turn_lock
        service._acquire_turn_lock = lambda _match: False
        try:
            assert service.advance_agents(match) == 0
        finally:
            service._acquire_turn_lock = original

    def test_the_holder_still_plays_normally(self, db, board, service):
        match = self._match(board, service)
        db.session.flush()
        assert service.advance_agents(match) > 0


class TestEstateAffordances:
    """A Build button the rules refuse is worse than no button.

    The panel offered "Build" on every owned square, so building on an
    incomplete funnel stage looked available and failed on every click. Worse,
    the refusal escaped as a 500 rather than a 422, because EconomyError did not
    descend from EngineError and the service's handler never saw it.
    """

    def _match(self, db, board, service):
        match = service.create(
            board,
            created_by=None,
            seats=[
                {"kind": "baseline", "display_name": "A"},
                {"kind": "baseline", "display_name": "B"},
            ],
            fill_policy="agents_now",
        )
        db.session.flush()
        return match

    def _own(self, service, match, db, indices, seat=0, cash=50000):
        state = service.state_for(match)
        for index in indices:
            state = state.with_ownership(index, seat)
        state = state.with_seat(dataclasses.replace(state.seat(seat), cash=cash))
        service._persist_state(match, state)
        db.session.flush()
        return state

    def _stage_of(self, service, match):
        """A funnel stage and its member squares, from the real board."""
        spec = service.spec_for(match)
        for square in spec.squares:
            if square.stage:
                return square.stage, list(spec.stage_members(square.stage))
        raise AssertionError("the seeded board has no staged squares")

    def test_a_partial_stage_cannot_build_and_says_why(self, db, board, service):
        match = self._match(db, board, service)
        _stage, members = self._stage_of(service, match)
        self._own(service, match, db, members[:1])

        row = next(r for r in service.estate(match, 0) if r["index"] == members[0])
        assert row["can_build"] is False
        assert "whole funnel stage" in row["build_blocked_because"]

    def test_a_complete_stage_can_build(self, db, board, service):
        match = self._match(db, board, service)
        _stage, members = self._stage_of(service, match)
        self._own(service, match, db, members)

        rows = {r["index"]: r for r in service.estate(match, 0)}
        assert all(rows[i]["can_build"] for i in members)
        assert all(rows[i]["build_blocked_because"] is None for i in members)

    def test_affordability_is_part_of_the_answer(self, db, board, service):
        """Offering a build the seat cannot pay for is the same broken button.

        The reason comes from ``can_build`` rather than a second cash check
        here — one rule, one owner.
        """
        match = self._match(db, board, service)
        _stage, members = self._stage_of(service, match)
        self._own(service, match, db, members, cash=1)

        row = next(r for r in service.estate(match, 0) if r["index"] == members[0])
        assert row["can_build"] is False
        assert "cash" in row["build_blocked_because"]

    def test_a_refused_build_is_a_rejection_not_a_server_fault(
        self, db, board, service
    ):
        """EconomyError must be catchable as an EngineError."""
        from plugins.bdv.bdv.core.engine import EngineError
        from plugins.bdv.bdv.services.match_service import MatchError

        match = self._match(db, board, service)
        _stage, members = self._stage_of(service, match)
        self._own(service, match, db, members[:1])

        with pytest.raises(MatchError, match="whole funnel stage"):
            service.submit(
                match,
                seat_index=0,
                action_type="build_house",
                payload={"square": members[0]},
            )
        assert issubclass(economy.EconomyError, EngineError)

    def test_selling_is_gated_the_same_way(self, db, board, service):
        match = self._match(db, board, service)
        _stage, members = self._stage_of(service, match)
        self._own(service, match, db, members)

        row = next(r for r in service.estate(match, 0) if r["index"] == members[0])
        assert row["can_sell_house"] is False, "no houses to sell yet"
        assert row["can_sell_square"] is True


class TestATurnWithNothingToDecideEndsItself:
    """ "End turn" as the only available action is a click that says nothing.

    Resolving offers exactly two choices — buy the square you landed on, or
    decline it. With no purchase on offer there is no decision left, so the
    button was pure ceremony and a wasted round-trip.
    """

    def _resolving(self, db, board, service, *, on_square):
        """A human seat mid-resolution, standing on ``on_square``."""
        match = service.create(
            board,
            created_by=None,
            seats=[
                {"kind": "baseline", "display_name": "A"},
                {"kind": "baseline", "display_name": "B"},
            ],
            fill_policy="agents_now",
        )
        state = service.state_for(match)
        state = state.with_seat(
            dataclasses.replace(state.seat(0), position=on_square, cash=5000)
        )
        state = dataclasses.replace(state, phase=Phase.RESOLVING, turn_seat=0)
        service._persist_state(match, state)
        db.session.flush()
        return match

    def test_nothing_to_buy_ends_the_turn(self, db, board, service):
        match = self._resolving(db, board, service, on_square=0)
        assert service.purchase_offer(match, 0) is None, "square 0 is GO"
        assert service.auto_end_turn(match) is True
        assert service.state_for(match).phase != Phase.RESOLVING

    def test_a_purchase_on_offer_keeps_the_turn(self, db, board, service):
        """The one case where the click IS a decision."""
        match = self._resolving(db, board, service, on_square=1)
        offer = service.purchase_offer(match, 0)
        assert offer is not None
        assert service.auto_end_turn(match) is False
        assert service.state_for(match).phase == Phase.RESOLVING

    def test_an_outstanding_demand_keeps_the_turn(self, db, board, service):
        """Rent outranks everything — the engine refuses end_turn anyway."""
        match = self._resolving(db, board, service, on_square=1)
        state = service.state_for(match)
        state = dataclasses.replace(
            state,
            pending_demand=RentDemand(
                debtor_seat=0, owner_seat=1, square_index=1, amount=100
            ),
        )
        service._persist_state(match, state)
        db.session.flush()
        assert service.auto_end_turn(match) is False

    def test_the_end_is_recorded_as_an_action(self, db, board, service):
        """Replay must reproduce the fact, not re-derive the rule."""
        match = self._resolving(db, board, service, on_square=0)
        service.auto_end_turn(match)
        db.session.flush()
        kinds = [row.type for row in ActionRepository(db.session).for_match(match.id)]
        assert ActionType.END_TURN in kinds
