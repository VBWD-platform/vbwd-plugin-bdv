"""Integration: seed the board, compile it, and play a real persisted match.

This is the proof that the pure engine and the persistence layer agree — the
place where a fold-vs-snapshot divergence would show up.
"""
import pytest

from plugins.bdv.bdv.core.engine import ActionType
from plugins.bdv.bdv.core.state import Phase
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
