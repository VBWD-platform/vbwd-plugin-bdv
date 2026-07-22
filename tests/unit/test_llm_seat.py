"""LlmSeat — a model plays a seat, and can never stall the table.

Every test injects a stub client. A test that would reach a real provider is a
bug: the suite must stay offline and deterministic.
"""
import dataclasses

import pytest

from plugins.bdv.bdv.agents.llm_seat import MOVE_SCHEMA, LlmSeat
from plugins.bdv.bdv.core.engine import ActionType, MatchConfig, new_match
from plugins.bdv.bdv.core.state import Constraint, ConstraintKind, Phase, SeatState


class StubClient:
    """Returns canned replies and records what it was asked."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def generate(self, system, user, **kwargs):
        self.calls.append({"system": system, "user": user, **kwargs})
        if not self.replies:
            raise RuntimeError("no more canned replies")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


@pytest.fixture
def choosing_state(tiny_spec):
    """A seat facing a real, priced option set."""
    state = new_match(tiny_spec, MatchConfig(seed="llm", seat_count=3))
    return dataclasses.replace(
        state,
        seats=(
            SeatState(index=0, cash=2000, position=0),
            SeatState(index=1, cash=2000, position=0),
            SeatState(index=2, cash=2000, position=0),
        ),
        pending_roll=(2, 3),
        phase=Phase.AWAIT_CHOICE,
    )


class TestSubstitutability:
    def test_it_answers_the_same_call_as_the_baseline_seat(
        self, choosing_state, tiny_spec
    ):
        """The turn loop must not be able to tell the seats apart (Liskov)."""
        seat = LlmSeat(client=StubClient({"action": "choose_option", "steps": 5}))
        action = seat.next_action(choosing_state, tiny_spec, 0)
        assert action.type == ActionType.CHOOSE_OPTION
        assert action.seat_index == 0

    def test_mechanical_phases_never_call_the_model(self, tiny_spec):
        """Rolling has exactly one sensible action — do not burn a token on it."""
        client = StubClient()
        seat = LlmSeat(client=client)
        state = new_match(tiny_spec, MatchConfig(seed="x", seat_count=3))
        action = seat.next_action(state, tiny_spec, 0)
        assert action.type == ActionType.ROLL
        assert client.calls == []


class TestSeatViewIsFair:
    def test_the_view_never_leaks_hidden_state(self, choosing_state, tiny_spec):
        seat = LlmSeat(client=StubClient())
        view = seat.seat_view(choosing_state, tiny_spec, 0)
        blob = str(view).lower()
        for forbidden in ("deck_cursor", "seed", "rng", "reasoning", "spec_hash"):
            assert forbidden not in blob

    def test_the_view_carries_the_engine_prices(self, choosing_state, tiny_spec):
        seat = LlmSeat(client=StubClient())
        view = seat.seat_view(choosing_state, tiny_spec, 0)
        assert view["options"], "the model must be given priced options"
        for option in view["options"]:
            assert {"steps", "price", "free", "affordable"} <= set(option)
        assert sum(1 for o in view["options"] if o["free"]) == 1

    def test_opponents_appear_without_personal_data(self, choosing_state, tiny_spec):
        seat = LlmSeat(client=StubClient())
        view = seat.seat_view(choosing_state, tiny_spec, 0)
        assert len(view["opponents"]) == 2
        for opponent in view["opponents"]:
            assert set(opponent) == {
                "seat",
                "cash",
                "position_name",
                "bankrupt",
                "owns",
            }

    def test_the_schema_is_sent_so_core_can_repair(self, choosing_state, tiny_spec):
        client = StubClient({"action": "choose_option", "steps": 5})
        LlmSeat(client=client).next_action(choosing_state, tiny_spec, 0)
        assert client.calls[0]["json_schema"] is MOVE_SCHEMA


class TestValidation:
    def test_an_illegal_step_count_is_rejected_then_repaired(
        self, choosing_state, tiny_spec
    ):
        client = StubClient(
            {"action": "choose_option", "steps": 99},  # not a legal option
            {"action": "choose_option", "steps": 5},
        )
        seat = LlmSeat(client=client)
        decision = seat.decide(choosing_state, tiny_spec, 0)
        assert decision.action.payload["steps"] == 5
        assert decision.attempts == 2
        assert "rejected" in client.calls[1]["user"], "the repair names the problem"

    def test_an_unaffordable_option_is_rejected(
        self, worked_example_state, worked_example_spec
    ):
        broke = worked_example_state.with_seat(
            SeatState(index=0, cash=1000, position=34)
        )
        broke = dataclasses.replace(
            broke, pending_roll=(2, 3), phase=Phase.AWAIT_CHOICE
        )
        client = StubClient(
            {"action": "choose_option", "steps": 2},  # 600, over the 300 cap
            {"action": "choose_option", "steps": 5},  # the free sum
        )
        decision = LlmSeat(client=client).decide(broke, worked_example_spec, 0)
        assert decision.action.payload["steps"] == 5

    def test_a_bribe_constraint_is_enforced_before_the_engine_sees_it(
        self, choosing_state, tiny_spec
    ):
        bound = dataclasses.replace(
            choosing_state,
            constraints=(Constraint(kind=ConstraintKind.FORCED_SUM, seat_index=0),),
        )
        client = StubClient(
            {"action": "choose_option", "steps": 2},  # not the sum — forbidden
            {"action": "choose_option", "steps": 5},
        )
        decision = LlmSeat(client=client).decide(bound, tiny_spec, 0)
        assert decision.action.payload["steps"] == 5

    def test_an_unknown_action_is_rejected(self, choosing_state, tiny_spec):
        client = StubClient({"action": "flip_the_table"}, {"action": "end_turn"})
        decision = LlmSeat(client=client).decide(choosing_state, tiny_spec, 0)
        assert decision.action.type == ActionType.END_TURN

    def test_non_integer_steps_are_rejected(self, choosing_state, tiny_spec):
        client = StubClient(
            {"action": "choose_option", "steps": "five"},
            {"action": "choose_option", "steps": 5},
        )
        decision = LlmSeat(client=client).decide(choosing_state, tiny_spec, 0)
        assert decision.action.payload["steps"] == 5


class TestTheLadderNeverStallsTheTable:
    def test_repeated_illegal_answers_degrade_to_the_baseline(
        self, choosing_state, tiny_spec
    ):
        client = StubClient(*[{"action": "choose_option", "steps": 99}] * 5)
        seat = LlmSeat(client=client, max_repair_retries=2)
        decision = seat.decide(choosing_state, tiny_spec, 0)
        assert decision.degraded_from == "illegal_or_error"
        assert decision.action.type == ActionType.CHOOSE_OPTION, "still a legal move"
        assert seat.degraded is True

    def test_a_provider_error_degrades_rather_than_raising(
        self, choosing_state, tiny_spec
    ):
        seat = LlmSeat(client=StubClient(RuntimeError("provider exploded")))
        decision = seat.decide(choosing_state, tiny_spec, 0)
        assert decision.degraded_from == "illegal_or_error"
        assert decision.action is not None

    def test_once_degraded_it_stays_degraded_and_stops_calling(
        self, choosing_state, tiny_spec
    ):
        client = StubClient(RuntimeError("boom"))
        seat = LlmSeat(client=client, max_repair_retries=0)
        seat.decide(choosing_state, tiny_spec, 0)
        calls_after_first = len(client.calls)
        seat.decide(choosing_state, tiny_spec, 0)
        assert len(client.calls) == calls_after_first, "no further provider calls"

    def test_an_exhausted_token_budget_degrades(self, choosing_state, tiny_spec):
        client = StubClient({"action": "choose_option", "steps": 5})
        seat = LlmSeat(client=client, token_budget=10, tokens_spent=999)
        decision = seat.decide(choosing_state, tiny_spec, 0)
        assert decision.degraded_from == "budget"
        assert client.calls == [], "budget is checked BEFORE spending more"

    def test_tokens_are_accounted_so_the_budget_can_bite(
        self, choosing_state, tiny_spec
    ):
        seat = LlmSeat(client=StubClient({"action": "choose_option", "steps": 5}))
        before = seat.tokens_spent
        seat.decide(choosing_state, tiny_spec, 0)
        assert seat.tokens_spent > before

    def test_the_fallback_move_is_always_legal(self, choosing_state, tiny_spec):
        """Whatever happens, the engine must accept the result."""
        from plugins.bdv.bdv.core.engine import apply

        seat = LlmSeat(client=StubClient(RuntimeError("nope")))
        action = seat.next_action(choosing_state, tiny_spec, 0)
        result = apply(
            choosing_state, tiny_spec, MatchConfig(seed="llm", seat_count=3), action
        )
        assert result.state is not None


class TestReasoningIsCaptured:
    def test_reasoning_is_kept_for_the_audit_trail(self, choosing_state, tiny_spec):
        client = StubClient(
            {"action": "choose_option", "steps": 5, "reasoning": "fate is free"}
        )
        decision = LlmSeat(client=client).decide(choosing_state, tiny_spec, 0)
        assert decision.reasoning == "fate is free"

    def test_reasoning_is_length_capped(self, choosing_state, tiny_spec):
        client = StubClient(
            {"action": "choose_option", "steps": 5, "reasoning": "x" * 9000}
        )
        decision = LlmSeat(client=client).decide(choosing_state, tiny_spec, 0)
        assert len(decision.reasoning) <= 2000


class TestBuildFromProfile:
    def test_profile_fields_are_honoured(self):
        from plugins.bdv.bdv.agents.llm_seat import build_llm_seat

        profile = dataclasses.make_dataclass(
            "P",
            [
                ("llm_connection_id", object, None),
                ("system_prompt", object, "be ruthless"),
                ("temperature", object, 0.3),
                ("max_tokens_per_match", object, 5000),
            ],
        )()
        seen = {}

        def factory(slug):
            seen["slug"] = slug
            return StubClient()

        seat = build_llm_seat(profile, client_factory=factory)
        assert seat._token_budget == 5000
        assert seat._system_prompt == "be ruthless"
        assert seen["slug"] is None


class TestTheSchemaMatchesWhatCoreExpects:
    """The bug that made every LLM seat silently useless.

    Core's adapter iterates the TOP-LEVEL KEYS of ``json_schema`` and turns each
    into a tool field. A full JSON Schema therefore asked the model to fill in
    fields called ``type``, ``properties`` and ``required`` — and it did,
    returning a schema instead of a move. Every answer was rejected as illegal,
    the retries burned, and the seat degraded on its first real decision. A
    whole match of "LLM agents" was two baseline policies playing each other.
    """

    def test_the_schema_is_a_flat_field_map(self):
        from plugins.bdv.bdv.agents.llm_seat import MOVE_SCHEMA

        assert set(MOVE_SCHEMA) == {"action", "steps", "reasoning"}
        for key in ("type", "properties", "required"):
            assert key not in MOVE_SCHEMA, "a JSON Schema was passed, not a field map"
        assert all(isinstance(v, str) for v in MOVE_SCHEMA.values())

    def test_the_prompt_still_names_the_legal_actions(self):
        """The enum left the schema, so the prompt has to carry it."""
        from plugins.bdv.bdv.agents.llm_seat import LEGAL_ACTIONS, SYSTEM_PROMPT

        for action in LEGAL_ACTIONS:
            assert action in SYSTEM_PROMPT


class TestADegradedSeatSaysWhy:
    """A silent fallback is indistinguishable from a seat that never spoke."""

    class Parrot:
        """Answers with the schema instead of a move — the real failure."""

        def generate(self, system, user, **kwargs):
            return {"type": {"move": "string"}, "properties": "{}", "required": "[]"}

    class Dead:
        def generate(self, system, user, **kwargs):
            raise RuntimeError("connection refused")

    def _seat(self, client):
        from plugins.bdv.bdv.agents.llm_seat import LlmSeat

        return LlmSeat(client=client, max_repair_retries=1)

    def test_an_unusable_answer_is_recorded_in_words(self, choosing_state, tiny_spec):
        seat = self._seat(self.Parrot())
        decision = seat.decide(choosing_state, tiny_spec, 0)
        assert seat.degraded is True
        assert "unknown action" in decision.failure
        assert "degraded to baseline" in decision.reasoning

    def test_a_provider_outage_is_recorded_in_words(self, choosing_state, tiny_spec):
        seat = self._seat(self.Dead())
        decision = seat.decide(choosing_state, tiny_spec, 0)
        assert seat.degraded is True
        assert "connection refused" in decision.failure
        assert decision.action is not None, "it still returns a legal move"
