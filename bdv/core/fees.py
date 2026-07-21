"""Where the move-purchase fee goes — the game's main balance lever.

Classic play sends fees to the bank, which is a sink: the leader's lead
compounds. Here the fee is a *transfer to opponents*, so escaping fate funds
your rivals. Which opponents, and in what proportion, is the dial the product
will iterate on — so it is a strategy resolved by id, never an ``if`` chain.
"""
from __future__ import annotations

from typing import Callable, Dict, Mapping, Protocol, Tuple

from .state import MatchState


class FeeDistributionPolicy(Protocol):
    """Split ``amount`` among the payer's solvent opponents."""

    policy_id: str

    def distribute(
        self, amount: int, state: MatchState, payer: int
    ) -> Mapping[int, int]:
        ...


def _eligible_recipients(state: MatchState, payer: int) -> Tuple[int, ...]:
    return tuple(
        seat.index
        for seat in state.seats
        if seat.index != payer and not seat.bankrupt
    )


class AllToPoorest:
    """100 % to the seat with the least cash — the strongest self-damping rule.

    Ties break on the lowest seat index so the result is deterministic.
    """

    policy_id = "all_to_poorest"

    def distribute(
        self, amount: int, state: MatchState, payer: int
    ) -> Mapping[int, int]:
        recipients = _eligible_recipients(state, payer)
        if amount <= 0 or not recipients:
            return {}
        poorest = min(recipients, key=lambda i: (state.seats[i].cash, i))
        return {poorest: amount}


class SplitAmongOpponents:
    """Even split; the remainder goes to the poorest so totals reconcile exactly."""

    policy_id = "split_among_opponents"

    def distribute(
        self, amount: int, state: MatchState, payer: int
    ) -> Mapping[int, int]:
        recipients = _eligible_recipients(state, payer)
        if amount <= 0 or not recipients:
            return {}
        share, remainder = divmod(amount, len(recipients))
        payout: Dict[int, int] = {index: share for index in recipients}
        if remainder:
            poorest = min(recipients, key=lambda i: (state.seats[i].cash, i))
            payout[poorest] += remainder
        return {index: value for index, value in payout.items() if value}


class ToBank:
    """The classic-game control arm. Present ONLY so the balance harness has a
    baseline to measure damping against — never a seeded default."""

    policy_id = "to_bank"

    def distribute(
        self, amount: int, state: MatchState, payer: int
    ) -> Mapping[int, int]:
        return {}


_POLICIES: Dict[str, Callable[[], FeeDistributionPolicy]] = {
    AllToPoorest.policy_id: AllToPoorest,
    SplitAmongOpponents.policy_id: SplitAmongOpponents,
    ToBank.policy_id: ToBank,
}


class UnknownFeePolicyError(LookupError):
    """Raised for an unregistered policy id — loudly, never a silent default."""


def resolve_fee_policy(policy_id: str) -> FeeDistributionPolicy:
    try:
        return _POLICIES[policy_id]()
    except KeyError as missing:
        raise UnknownFeePolicyError(
            f"Unknown fee distribution policy: {policy_id!r}"
        ) from missing


def available_fee_policies() -> Tuple[str, ...]:
    return tuple(sorted(_POLICIES))
