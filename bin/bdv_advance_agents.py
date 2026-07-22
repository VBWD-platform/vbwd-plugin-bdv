#!/usr/bin/env python3
"""Advance every non-human seat of a match using the deterministic baseline agent.

Why this exists: the player API only ever lets the AUTHENTICATED user act on
their own seat — that is the seat-authorisation rule, and it is correct. So agent
seats cannot be driven over HTTP by a client, and a match with agent opponents
stalls until something server-side takes their turns.

Until `LlmSeat` and the scheduled turn-driver land (S146-5), this is that
something: a small operator/dev tool that plays baseline turns through the real
MatchService, so every move is appended to the same action log and replays like
any other. It invents nothing.

Usage:
    python plugins/bdv/bin/bdv_advance_agents.py <match_id> [--max-turns 12]
"""
import argparse
import os
import sys

sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
)


def advance(match_id: str, max_turns: int = 12) -> int:
    from vbwd.app import create_app
    from vbwd.extensions import db

    from plugins.bdv.bdv.agents.baseline import BaselineSeat
    from plugins.bdv.bdv.core.state import Phase
    from plugins.bdv.bdv.repositories.match_repository import (
        ActionRepository,
        MatchRepository,
        OfferRepository,
    )
    from plugins.bdv.bdv.services.match_service import MatchError, MatchService

    app = create_app()
    with app.app_context():
        matches = MatchRepository(db.session)
        service = MatchService(
            db.session,
            matches,
            ActionRepository(db.session),
            OfferRepository(db.session),
        )
        match = matches.find_by_id(match_id)
        if match is None:
            print(f"[bdv] no such match: {match_id}")
            return 1

        agent_seats = {
            seat.seat_index for seat in match.seats if seat.kind in ("baseline", "llm")
        }
        spec = service.spec_for(match)
        agent = BaselineSeat()
        played = 0

        for _ in range(max_turns * 8):
            state = service.state_for(match)
            if state.phase == Phase.FINISHED:
                break
            if state.turn_seat not in agent_seats:
                break  # it is a human's move again — stop and hand back

            action = agent.next_action(state, spec, state.turn_seat)
            try:
                service.submit(
                    match,
                    seat_index=action.seat_index,
                    action_type=action.type,
                    payload=dict(action.payload),
                )
            except MatchError as rejected:
                print(f"[bdv] stopped: {rejected}")
                break
            played += 1

        db.session.commit()
        final = service.state_for(match)
        print(
            f"[bdv] advanced {played} agent action(s); "
            f"turn_seat={final.turn_seat} phase={final.phase.value} seq={final.seq}"
        )
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("match_id")
    parser.add_argument("--max-turns", type=int, default=12)
    args = parser.parse_args()
    return advance(args.match_id, args.max_turns)


if __name__ == "__main__":
    raise SystemExit(main())
