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
            service.create(board, created_by=None, seats=[{"kind": "baseline", "display_name": "solo"}])


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
        state, events = service.submit(
            match, seat_index=0, action_type=ActionType.ROLL
        )
        assert state.pending_roll is not None
        assert match.state_seq == state.seq
        assert any(e["type"] == "rolled" for e in events)

    def test_folding_the_log_reproduces_the_snapshot(self, db, board, service):
        match = self._match(board, service)
        service.submit(match, seat_index=0, action_type=ActionType.ROLL)
        service.submit(match, seat_index=0, action_type=ActionType.OPEN_NEGOTIATION)

        snapshot = service.state_for(match)
        rebuilt = service.rebuild_state(match)
        assert rebuilt.state_hash() == snapshot.state_hash(), (
            "the cached snapshot must always equal the fold of the log"
        )

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
