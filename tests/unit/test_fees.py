"""Fee distribution — the balance lever.

The invariant that matters: the distributed amounts sum EXACTLY to the fee, and
the payer never receives any of it. A leak here would silently create or destroy
credits, which would make the whole economy unauditable.
"""
import pytest

from plugins.bdv.bdv.core.fees import (
    UnknownFeePolicyError,
    available_fee_policies,
    resolve_fee_policy,
)
from plugins.bdv.bdv.core.state import MatchState, SeatState


def state_with(*cash_values, bankrupt=()):
    return MatchState(
        seats=tuple(
            SeatState(index=i, cash=c, bankrupt=(i in bankrupt))
            for i, c in enumerate(cash_values)
        ),
        ownership={},
        houses={},
    )


class TestAllToPoorest:
    def test_entire_fee_goes_to_the_poorest_opponent(self):
        policy = resolve_fee_policy("all_to_poorest")
        payout = policy.distribute(600, state_with(10000, 8000, 3000), payer=0)
        assert payout == {2: 600}

    def test_payer_never_receives_even_when_poorest(self):
        policy = resolve_fee_policy("all_to_poorest")
        payout = policy.distribute(600, state_with(100, 8000, 3000), payer=0)
        assert 0 not in payout
        assert payout == {2: 600}

    def test_ties_break_deterministically_on_seat_index(self):
        policy = resolve_fee_policy("all_to_poorest")
        payout = policy.distribute(300, state_with(9000, 500, 500), payer=0)
        assert payout == {1: 300}

    def test_bankrupt_seats_are_not_recipients(self):
        policy = resolve_fee_policy("all_to_poorest")
        payout = policy.distribute(
            300,
            state_with(9000, 100, 4000),
            payer=0,
        )
        assert payout == {1: 300}
        payout = policy.distribute(
            300, state_with(9000, 100, 4000, bankrupt=(1,)), payer=0
        )
        assert payout == {2: 300}


class TestSplitAmongOpponents:
    def test_even_split(self):
        policy = resolve_fee_policy("split_among_opponents")
        payout = policy.distribute(600, state_with(10000, 8000, 3000), payer=0)
        assert payout == {1: 300, 2: 300}

    def test_remainder_goes_to_the_poorest_so_totals_reconcile(self):
        policy = resolve_fee_policy("split_among_opponents")
        payout = policy.distribute(601, state_with(10000, 8000, 3000), payer=0)
        assert payout == {1: 300, 2: 301}
        assert sum(payout.values()) == 601


class TestToBankControlArm:
    def test_bank_policy_pays_nobody(self):
        """The classic-game baseline — present only so the harness can measure
        damping against it. Never a seeded default."""
        policy = resolve_fee_policy("to_bank")
        assert policy.distribute(600, state_with(10000, 8000, 3000), payer=0) == {}


@pytest.mark.parametrize("policy_id", ["all_to_poorest", "split_among_opponents"])
@pytest.mark.parametrize("seat_count", [2, 3, 4, 5, 6])
@pytest.mark.parametrize("amount", [1, 7, 100, 601, 999])
class TestReconciliation:
    def test_distribution_sums_exactly_to_the_fee(self, policy_id, seat_count, amount):
        policy = resolve_fee_policy(policy_id)
        state = state_with(*[1000 * (i + 1) for i in range(seat_count)])
        payout = policy.distribute(amount, state, payer=0)
        assert sum(payout.values()) == amount, "no credits created or destroyed"

    def test_payer_is_never_a_recipient(self, policy_id, seat_count, amount):
        policy = resolve_fee_policy(policy_id)
        state = state_with(*[1000 * (i + 1) for i in range(seat_count)])
        assert 0 not in policy.distribute(amount, state, payer=0)


class TestPolicyResolution:
    def test_unknown_policy_fails_loudly(self):
        with pytest.raises(UnknownFeePolicyError):
            resolve_fee_policy("pay_the_house")

    def test_registry_lists_the_shipped_policies(self):
        assert set(available_fee_policies()) == {
            "all_to_poorest",
            "split_among_opponents",
            "to_bank",
        }

    def test_no_recipients_yields_no_payout(self):
        policy = resolve_fee_policy("all_to_poorest")
        assert policy.distribute(500, state_with(1000), payer=0) == {}

    def test_zero_or_negative_amount_pays_nothing(self):
        policy = resolve_fee_policy("all_to_poorest")
        state = state_with(1000, 2000)
        assert policy.distribute(0, state, payer=0) == {}
        assert policy.distribute(-5, state, payer=0) == {}
