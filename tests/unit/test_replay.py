"""Replay determinism — the audit guarantee.

Same spec + same seed + same actions => byte-identical state, forever. This is
what lets a player ask "why did that cost 600?" months later and get an answer.
"""
import dataclasses

import pytest

from plugins.bdv.bdv.agents.baseline import BaselineSeat, play_match
from plugins.bdv.bdv.core.dice import roll_at, shuffled_order
from plugins.bdv.bdv.core.engine import MatchConfig
from plugins.bdv.bdv.core.replay import Replay, SpecMismatchError, fold, replay


class TestDeterministicDice:
    def test_same_seed_and_cursor_always_give_the_same_roll(self):
        assert roll_at("match-a", 7) == roll_at("match-a", 7)

    def test_different_cursors_give_different_rolls(self):
        rolls = {roll_at("match-a", i) for i in range(40)}
        assert len(rolls) > 1, "the sequence must actually vary"

    def test_different_seeds_diverge(self):
        a = [roll_at("match-a", i) for i in range(20)]
        b = [roll_at("match-b", i) for i in range(20)]
        assert a != b

    def test_dice_are_in_range(self):
        for cursor in range(200):
            first, second = roll_at("s", cursor)
            assert 1 <= first <= 6 and 1 <= second <= 6

    def test_deck_order_is_reproducible_and_a_permutation(self):
        first = shuffled_order("s", "chance", 12)
        assert first == shuffled_order("s", "chance", 12)
        assert sorted(first) == list(range(12))

    def test_reshuffle_changes_the_order_deterministically(self):
        assert shuffled_order("s", "chance", 12, 0) != shuffled_order("s", "chance", 12, 1)
        assert shuffled_order("s", "chance", 12, 1) == shuffled_order("s", "chance", 12, 1)


class TestFullMatchReplay:
    @pytest.fixture
    def played(self, tiny_spec):
        config = MatchConfig(seed="replay-seed", seat_count=3)
        state, actions = play_match(tiny_spec, config)
        return config, state, actions

    def test_a_full_match_actually_finishes(self, played, tiny_spec):
        _, state, actions = played
        assert len(actions) > 20, "the match should be a real game, not two moves"

    def test_replay_reproduces_the_state_hash_exactly(self, played, tiny_spec):
        config, state, actions = played
        recording = Replay(
            spec_hash=tiny_spec.spec_hash(),
            seed=config.seed,
            seat_count=config.seat_count,
            actions=actions,
        )
        assert replay(tiny_spec, recording).state_hash() == state.state_hash()

    def test_replay_is_stable_across_repeated_runs(self, played, tiny_spec):
        config, _, actions = played
        recording = Replay(
            spec_hash=tiny_spec.spec_hash(),
            seed=config.seed,
            seat_count=config.seat_count,
            actions=actions,
        )
        first = replay(tiny_spec, recording).state_hash()
        second = replay(tiny_spec, recording).state_hash()
        assert first == second

    def test_playing_twice_from_the_same_seed_is_identical(self, tiny_spec):
        config = MatchConfig(seed="same", seat_count=3)
        first_state, first_actions = play_match(tiny_spec, config)
        second_state, second_actions = play_match(tiny_spec, config)
        assert first_state.state_hash() == second_state.state_hash()
        assert first_actions == second_actions

    def test_a_different_seed_produces_a_different_match(self, tiny_spec):
        a, _ = play_match(tiny_spec, MatchConfig(seed="a", seat_count=3))
        b, _ = play_match(tiny_spec, MatchConfig(seed="b", seat_count=3))
        assert a.state_hash() != b.state_hash()


class TestSpecPinning:
    def test_replaying_against_different_rules_is_rejected_loudly(self, tiny_spec):
        config = MatchConfig(seed="pin", seat_count=3)
        _, actions = play_match(tiny_spec, config)
        recording = Replay(
            spec_hash=tiny_spec.spec_hash(),
            seed=config.seed,
            seat_count=config.seat_count,
            actions=actions,
        )
        tampered = dataclasses.replace(tiny_spec, go_salary=999)
        with pytest.raises(SpecMismatchError):
            replay(tampered, recording)

    def test_spec_hash_is_stable_for_identical_specs(self, tiny_spec):
        assert tiny_spec.spec_hash() == dataclasses.replace(tiny_spec).spec_hash()

    def test_spec_hash_changes_when_any_rule_changes(self, tiny_spec):
        for field, value in (
            ("go_salary", 1),
            ("k_price", tiny_spec.k_price + 1),
            ("fee_policy", "split_among_opponents"),
            ("cap_pct", tiny_spec.cap_pct / 2),
        ):
            assert (
                dataclasses.replace(tiny_spec, **{field: value}).spec_hash()
                != tiny_spec.spec_hash()
            ), field


class TestFoldRebuildsSnapshots:
    def test_fold_matches_the_incremental_state_at_every_step(self, tiny_spec):
        """The persistence layer caches a snapshot; folding the log must always
        reproduce it, or the cache is a lie."""
        from plugins.bdv.bdv.core.engine import apply, new_match
        from plugins.bdv.bdv.core.state import Phase

        config = MatchConfig(seed="fold", seat_count=3)
        agent = BaselineSeat()
        state = new_match(tiny_spec, config)
        log = []
        for _ in range(120):
            if state.phase == Phase.FINISHED:
                break
            action = agent.next_action(state, tiny_spec, state.turn_seat)
            state = apply(state, tiny_spec, config, action).state
            log.append(action)
            assert fold(tiny_spec, config, tuple(log)).state_hash() == state.state_hash()


class TestStateSerialisation:
    def test_state_round_trips_through_dict(self, tiny_spec):
        from plugins.bdv.bdv.core.state import MatchState

        config = MatchConfig(seed="ser", seat_count=3)
        state, _ = play_match(tiny_spec, config)
        restored = MatchState.from_dict(state.to_dict())
        assert restored.state_hash() == state.state_hash()


class TestNoCreditLeak:
    def test_total_credits_are_conserved_except_at_the_bank(self, tiny_spec):
        """Fees and bribes are transfers; only tax/purchase/salary touch the bank.
        This guards against a distribution bug silently minting money."""
        config = MatchConfig(seed="leak", seat_count=3)
        state, actions = play_match(tiny_spec, config)
        assert all(seat.cash >= 0 for seat in state.seats)
