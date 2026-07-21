"""Card rule engine — declarative ops, generated descriptions, EV hints."""
import pytest

from plugins.bdv.bdv.core.effects import (
    CardExecutionError,
    UnknownEffectOpError,
    apply_effect,
    describe_effect,
    effect_ev_hint,
    op_descriptors,
    registered_ops,
    resolve_op,
    validate_effect,
)
from plugins.bdv.bdv.core.state import SeatState

MVP_OPS = {
    "pay_bank",
    "collect_bank",
    "collect_from_each_player",
    "pay_each_player",
    "move_to_square",
    "move_relative",
    "go_to_jail",
    "get_out_of_jail_card",
    "advance_to_nearest_kind",
    "pay_per_building",
    "skip_next_turn",
}


class TestRegistry:
    def test_mvp_op_set_is_registered(self):
        assert set(registered_ops()) == MVP_OPS

    def test_unknown_op_fails_loudly_never_silently(self):
        with pytest.raises(UnknownEffectOpError):
            resolve_op("delete_opponent")

    def test_descriptors_carry_a_schema_for_the_admin_form(self):
        for descriptor in op_descriptors():
            assert descriptor["op_id"] in MVP_OPS
            assert descriptor["params_schema"]["type"] == "object"
            assert descriptor["label_key"].startswith("bdv.effect.")


class TestValidation:
    def test_valid_effect_has_no_errors(self):
        assert validate_effect({"ops": [{"op": "pay_bank", "params": {"amount": 100}}]}) == []

    def test_empty_ops_is_rejected(self):
        assert validate_effect({"ops": []})
        assert validate_effect({})

    def test_unknown_op_is_rejected_at_save_time(self):
        errors = validate_effect({"ops": [{"op": "nope", "params": {}}]})
        assert errors and "nope" in errors[0]


class TestMoneyOps:
    def test_pay_bank(self, tiny_state, tiny_spec):
        result = apply_effect(
            tiny_state, tiny_spec, 0, {"ops": [{"op": "pay_bank", "params": {"amount": 150}}]}
        )
        assert result.state.seat(0).cash == 1850

    def test_collect_bank(self, tiny_state, tiny_spec):
        result = apply_effect(
            tiny_state, tiny_spec, 0, {"ops": [{"op": "collect_bank", "params": {"amount": 150}}]}
        )
        assert result.state.seat(0).cash == 2150

    def test_collect_from_each_player_is_zero_sum(self, tiny_state, tiny_spec):
        before = sum(s.cash for s in tiny_state.seats)
        result = apply_effect(
            tiny_state,
            tiny_spec,
            0,
            {"ops": [{"op": "collect_from_each_player", "params": {"amount": 100}}]},
        )
        assert result.state.seat(0).cash == 2200
        assert result.state.seat(1).cash == 1900
        assert result.state.seat(2).cash == 1900
        assert sum(s.cash for s in result.state.seats) == before

    def test_pay_each_player_is_zero_sum(self, tiny_state, tiny_spec):
        before = sum(s.cash for s in tiny_state.seats)
        result = apply_effect(
            tiny_state,
            tiny_spec,
            0,
            {"ops": [{"op": "pay_each_player", "params": {"amount": 100}}]},
        )
        assert result.state.seat(0).cash == 1800
        assert sum(s.cash for s in result.state.seats) == before

    def test_bankrupt_seats_are_skipped(self, tiny_state, tiny_spec):
        import dataclasses

        state = tiny_state.with_seat(
            dataclasses.replace(tiny_state.seat(2), bankrupt=True)
        )
        result = apply_effect(
            state,
            tiny_spec,
            0,
            {"ops": [{"op": "collect_from_each_player", "params": {"amount": 100}}]},
        )
        assert result.state.seat(0).cash == 2100, "only one solvent opponent paid"


class TestMovementOps:
    def test_move_to_square_collects_go_when_passing(self, tiny_state, tiny_spec):
        state = tiny_state.with_seat(SeatState(index=0, cash=2000, position=7))
        result = apply_effect(
            state,
            tiny_spec,
            0,
            {"ops": [{"op": "move_to_square", "params": {"index": 0}}]},
        )
        assert result.state.seat(0).position == 0
        assert result.state.seat(0).cash == 2200, "passed New Quarter"

    def test_move_to_square_can_skip_the_salary(self, tiny_state, tiny_spec):
        state = tiny_state.with_seat(SeatState(index=0, cash=2000, position=7))
        result = apply_effect(
            state,
            tiny_spec,
            0,
            {
                "ops": [
                    {
                        "op": "move_to_square",
                        "params": {"index": 0, "collect_go_salary": False},
                    }
                ]
            },
        )
        assert result.state.seat(0).cash == 2000

    def test_move_relative_wraps(self, tiny_state, tiny_spec):
        state = tiny_state.with_seat(SeatState(index=0, cash=2000, position=1))
        result = apply_effect(
            state, tiny_spec, 0, {"ops": [{"op": "move_relative", "params": {"steps": -3}}]}
        )
        assert result.state.seat(0).position == 8

    def test_go_to_jail(self, tiny_state, tiny_spec):
        result = apply_effect(tiny_state, tiny_spec, 0, {"ops": [{"op": "go_to_jail"}]})
        assert result.state.seat(0).in_jail is True
        assert result.state.seat(0).position == 8

    def test_advance_to_nearest_service(self, tiny_state, tiny_spec):
        state = tiny_state.with_seat(SeatState(index=0, cash=2000, position=1))
        result = apply_effect(
            state,
            tiny_spec,
            0,
            {"ops": [{"op": "advance_to_nearest_kind", "params": {"kind": "service"}}]},
        )
        assert result.state.seat(0).position == 5


class TestHeldAndStatefulOps:
    def test_get_out_of_jail_card_is_held_not_resolved(self, tiny_state, tiny_spec):
        result = apply_effect(
            tiny_state, tiny_spec, 0, {"ops": [{"op": "get_out_of_jail_card"}]}
        )
        assert result.state.seat(0).get_out_of_jail_cards == 1

    def test_skip_next_turn(self, tiny_state, tiny_spec):
        result = apply_effect(
            tiny_state, tiny_spec, 0, {"ops": [{"op": "skip_next_turn"}]}
        )
        assert result.state.seat(0).skip_next_turn is True

    def test_pay_per_building(self, tiny_state, tiny_spec):
        state = tiny_state.with_ownership(1, 0).with_houses(1, 3)
        result = apply_effect(
            state,
            tiny_spec,
            0,
            {"ops": [{"op": "pay_per_building", "params": {"per_house": 25, "per_hotel": 100}}]},
        )
        assert result.state.seat(0).cash == 2000 - 75


class TestComposition:
    def test_ops_apply_in_order(self, tiny_state, tiny_spec):
        result = apply_effect(
            tiny_state,
            tiny_spec,
            0,
            {
                "ops": [
                    {"op": "collect_bank", "params": {"amount": 500}},
                    {"op": "move_to_square", "params": {"index": 4}},
                ]
            },
        )
        assert result.state.seat(0).cash == 2500
        assert result.state.seat(0).position == 4

    def test_a_failing_op_rolls_back_the_whole_card(self, tiny_state, tiny_spec):
        """No half-applied cards — that would corrupt the match AND the replay."""
        with pytest.raises(CardExecutionError):
            apply_effect(
                tiny_state,
                tiny_spec,
                0,
                {
                    "ops": [
                        {"op": "collect_bank", "params": {"amount": 500}},
                        {"op": "pay_bank", "params": {}},  # missing amount
                    ]
                },
            )

    def test_unknown_op_fails_at_draw_time_too(self, tiny_state, tiny_spec):
        with pytest.raises(UnknownEffectOpError):
            apply_effect(tiny_state, tiny_spec, 0, {"ops": [{"op": "ghost"}]})


class TestGeneratedDescriptions:
    """The description is generated FROM the ops, so it cannot drift from
    behaviour. These are the golden strings that keep admin-visible prose honest."""

    GOLDEN = [
        ({"op": "pay_bank", "params": {"amount": 150}}, "bdv.effect.pay_bank", {"amount": 150}),
        ({"op": "collect_bank", "params": {"amount": 200}}, "bdv.effect.collect_bank", {"amount": 200}),
        (
            {"op": "collect_from_each_player", "params": {"amount": 100}},
            "bdv.effect.collect_from_each_player",
            {"amount": 100},
        ),
        ({"op": "go_to_jail"}, "bdv.effect.go_to_jail", {}),
        ({"op": "skip_next_turn"}, "bdv.effect.skip_next_turn", {}),
    ]

    @pytest.mark.parametrize("entry,key,params", GOLDEN)
    def test_description_key_and_params(self, entry, key, params, tiny_spec):
        described = describe_effect({"ops": [entry]}, tiny_spec)
        assert described[0].key == key
        assert described[0].params == params

    def test_move_to_square_description_names_the_square(self, tiny_spec):
        described = describe_effect(
            {"ops": [{"op": "move_to_square", "params": {"index": 5}}]}, tiny_spec
        )
        assert described[0].params["name"] == "CRM Platform"

    def test_every_op_can_describe_itself(self, tiny_spec):
        """A new op cannot ship without a description — the admin form would
        otherwise render a rule nobody can read."""
        samples = {
            "pay_bank": {"amount": 10},
            "collect_bank": {"amount": 10},
            "collect_from_each_player": {"amount": 10},
            "pay_each_player": {"amount": 10},
            "move_to_square": {"index": 1},
            "move_relative": {"steps": 2},
            "go_to_jail": {},
            "get_out_of_jail_card": {},
            "advance_to_nearest_kind": {"kind": "service"},
            "pay_per_building": {"per_house": 25, "per_hotel": 100},
            "skip_next_turn": {},
        }
        for op_id in registered_ops():
            description = resolve_op(op_id).describe(samples[op_id], tiny_spec)
            assert description.key.startswith("bdv.effect.")


class TestEvHints:
    def test_signs_are_right(self, tiny_spec):
        assert effect_ev_hint({"ops": [{"op": "pay_bank", "params": {"amount": 100}}]}, tiny_spec) == -100
        assert effect_ev_hint({"ops": [{"op": "collect_bank", "params": {"amount": 100}}]}, tiny_spec) == 100

    def test_hints_compose_across_ops(self, tiny_spec):
        effect = {
            "ops": [
                {"op": "collect_bank", "params": {"amount": 300}},
                {"op": "pay_bank", "params": {"amount": 100}},
            ]
        }
        assert effect_ev_hint(effect, tiny_spec) == 200

    def test_go_to_jail_hint_is_the_board_penalty(self, tiny_spec):
        assert effect_ev_hint({"ops": [{"op": "go_to_jail"}]}, tiny_spec) == -100
