#!/usr/bin/env python3
"""Balance harness — does the fee-to-opponents rule actually damp the snowball?

The product rests on one empirical claim: because the move-purchase fee is a
TRANSFER to opponents rather than a sink to the bank, the leader finances the
underdogs and the classic runaway-leader problem becomes self-damping.

That claim is either true or it is not, and no amount of design discussion
settles it. Because the engine is pure and seeded, N matches cost seconds and
zero API tokens, so we measure it.

Arms compared:
    to_bank               -- the CLASSIC control. Without it there is nothing to
                             measure damping against.
    split_among_opponents -- even split
    all_to_poorest        -- the seeded default

Usage:
    python plugins/bdv/bin/bdv_balance.py --matches 500 --seats 3
"""
import argparse
import json
import os
import statistics
import sys
from decimal import Decimal
from typing import Dict, List

sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
)

from plugins.bdv.bdv.agents.baseline import BaselineSeat  # noqa: E402
from plugins.bdv.bdv.core.board import BoardSpec, SquareKind, SquareSpec  # noqa: E402
from plugins.bdv.bdv.core.engine import MatchConfig, apply, new_match  # noqa: E402
from plugins.bdv.bdv.core.state import Phase  # noqa: E402
from plugins.bdv.bdv.services.seed_board import (  # noqa: E402
    seed_board_payload,
    seed_squares,
)


def build_spec(fee_policy: str, k_price: str = "0.5", cap_pct: str = "0.30") -> BoardSpec:
    payload = seed_board_payload()
    squares = tuple(
        SquareSpec(
            index=row["index"],
            kind=SquareKind(row["kind"]),
            name=row["name"],
            stage=row.get("stage"),
            price=row.get("price", 0),
            rent_table=tuple(row.get("rent_table", ())),
            service_multipliers=tuple(row.get("service_multipliers", ())),
            house_cost=row.get("house_cost", 0),
            mortgage_value=row.get("mortgage_value", 0),
            tax_amount=row.get("tax_amount", 0),
        )
        for row in seed_squares()
    )
    return BoardSpec(
        squares=squares,
        starting_cash=payload["starting_cash"],
        go_salary=payload["go_salary"],
        jail_fine=payload["jail_fine"],
        jail_penalty_ev=payload["jail_penalty_ev"],
        k_price=Decimal(k_price),
        k_acquire=Decimal(payload["k_acquire"]),
        cap_pct=Decimal(cap_pct),
        fee_policy=fee_policy,
        max_houses=payload["max_houses"],
    )


def gini(values: List[int]) -> float:
    """0 = perfect equality, 1 = one seat holds everything."""
    positive = [max(0, v) for v in values]
    total = sum(positive)
    if total <= 0 or len(positive) < 2:
        return 0.0
    ordered = sorted(positive)
    n = len(ordered)
    weighted = sum((index + 1) * value for index, value in enumerate(ordered))
    return (2 * weighted) / (n * total) - (n + 1) / n


def run_match(spec: BoardSpec, seed: str, seat_count: int, max_actions: int = 6000) -> Dict:
    agent = BaselineSeat()
    config = MatchConfig(seed=seed, seat_count=seat_count)
    state = new_match(spec, config)

    gini_track: List[float] = []
    purchases = 0
    blocked_by_cap = 0
    lead_changes = 0
    previous_leader = None
    turns = 0

    for _ in range(max_actions):
        if state.phase == Phase.FINISHED:
            break
        if state.phase == Phase.AWAIT_ROLL:
            turns += 1
            gini_track.append(gini([s.cash for s in state.seats]))
            leader = max(range(len(state.seats)), key=lambda i: state.seats[i].cash)
            if previous_leader is not None and leader != previous_leader:
                lead_changes += 1
            previous_leader = leader

        if state.phase == Phase.AWAIT_CHOICE and state.pending_roll:
            from plugins.bdv.bdv.core.pricing import evaluate_options

            quotes = evaluate_options(state, spec, state.pending_roll, state.turn_seat)
            if any(q.price > 0 and not q.affordable for q in quotes):
                blocked_by_cap += 1

        action = agent.next_action(state, spec, state.turn_seat)
        result = apply(state, spec, config, action)
        purchases += sum(1 for e in result.events if e["type"] == "option_purchased")
        state = result.state

    return {
        "turns": turns,
        "finished": state.phase == Phase.FINISHED,
        "winner": state.winner_seat,
        "purchases": purchases,
        "blocked_by_cap": blocked_by_cap,
        "lead_changes": lead_changes,
        "gini_start": statistics.fmean(gini_track[:5]) if len(gini_track) >= 5 else 0.0,
        "gini_mid": statistics.fmean(
            gini_track[len(gini_track) // 3 : 2 * len(gini_track) // 3]
        )
        if len(gini_track) >= 6
        else 0.0,
        "gini_end": statistics.fmean(gini_track[-5:]) if len(gini_track) >= 5 else 0.0,
        "gini_peak": max(gini_track) if gini_track else 0.0,
    }


def run_arm(fee_policy: str, matches: int, seats: int, **spec_kwargs) -> Dict:
    spec = build_spec(fee_policy, **spec_kwargs)
    results = [
        run_match(spec, f"{fee_policy}-{index}", seats) for index in range(matches)
    ]
    finished = [r for r in results if r["finished"]]
    seat_wins = [0] * seats
    for r in finished:
        if r["winner"] is not None:
            seat_wins[r["winner"]] += 1

    return {
        "fee_policy": fee_policy,
        "matches": matches,
        "seats": seats,
        "finished_pct": round(100 * len(finished) / matches, 1),
        "median_turns": statistics.median([r["turns"] for r in results]),
        "mean_purchases": round(statistics.fmean([r["purchases"] for r in results]), 1),
        "mean_blocked_by_cap": round(
            statistics.fmean([r["blocked_by_cap"] for r in results]), 1
        ),
        "mean_lead_changes": round(
            statistics.fmean([r["lead_changes"] for r in results]), 2
        ),
        "gini_start": round(statistics.fmean([r["gini_start"] for r in results]), 4),
        "gini_mid": round(statistics.fmean([r["gini_mid"] for r in results]), 4),
        "gini_end": round(statistics.fmean([r["gini_end"] for r in results]), 4),
        "gini_peak": round(statistics.fmean([r["gini_peak"] for r in results]), 4),
        "win_rate_by_seat": [
            round(100 * w / max(1, len(finished)), 1) for w in seat_wins
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matches", type=int, default=200)
    parser.add_argument("--seats", type=int, default=3)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    arms = [
        run_arm(policy, args.matches, args.seats)
        for policy in ("to_bank", "split_among_opponents", "all_to_poorest")
    ]

    if args.json:
        print(json.dumps(arms, indent=2))
        return 0

    print(f"\nBizDevVibes balance harness — {args.matches} matches/arm, {args.seats} seats\n")
    header = (
        f"{'fee policy':<24}{'gini start':>11}{'gini mid':>10}{'gini end':>10}"
        f"{'peak':>8}{'turns':>8}{'buys':>7}{'capped':>8}{'leadΔ':>7}"
    )
    print(header)
    print("-" * len(header))
    for arm in arms:
        print(
            f"{arm['fee_policy']:<24}{arm['gini_start']:>11.4f}{arm['gini_mid']:>10.4f}"
            f"{arm['gini_end']:>10.4f}{arm['gini_peak']:>8.4f}"
            f"{arm['median_turns']:>8.0f}{arm['mean_purchases']:>7.1f}"
            f"{arm['mean_blocked_by_cap']:>8.1f}{arm['mean_lead_changes']:>7.2f}"
        )

    control = next(a for a in arms if a["fee_policy"] == "to_bank")
    print("\nVERDICT (vs the classic to_bank control):")
    for arm in arms:
        if arm["fee_policy"] == "to_bank":
            continue
        delta = arm["gini_end"] - control["gini_end"]
        direction = "DAMPS" if delta < 0 else "does NOT damp"
        print(
            f"  {arm['fee_policy']:<24} end-gini {arm['gini_end']:.4f} "
            f"vs {control['gini_end']:.4f}  ({delta:+.4f}) -> {direction} the snowball"
        )
    print("\nWin rate by seat order (fairness check — should be roughly flat):")
    for arm in arms:
        print(f"  {arm['fee_policy']:<24} {arm['win_rate_by_seat']}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
