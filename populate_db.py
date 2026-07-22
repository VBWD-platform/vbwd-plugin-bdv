"""BizDevVibes seed data — the canonical ``funnel-40`` board.

CREATE-ONLY and idempotent: re-running must never overwrite a board an admin has
edited. That is the rule for any seeder that could ever touch production.
"""
from vbwd.extensions import db


def populate(app=None):
    from plugins.bdv.bdv.services.access_seeder import grant_play_permission
    from plugins.bdv.bdv.services.agent_seeder import seed_house_agents
    from plugins.bdv.bdv.services.board_seeder import seed_funnel_board

    board, created = seed_funnel_board(db.session)
    agents, agents_created = seed_house_agents(db.session)
    # Player routes are RBAC-gated; grant the permission additively so a fresh
    # install is playable without an operator editing core's access levels.
    granted = grant_play_permission(db.session)
    db.session.commit()
    if granted:
        print(f"[bdv] granted bdv.play to: {', '.join(granted)}")
    print(
        f"[bdv] {'created' if created else 'already present'}: "
        f"{board.slug} ({len(board.squares)} squares, {len(board.cards)} cards)"
    )
    print(
        f"[bdv] house agents: {agents_created} created, "
        f"{len(agents) - agents_created} already present "
        f"({', '.join(a.slug for a in agents)})"
    )
    return board


if __name__ == "__main__":
    from vbwd.app import create_app

    app = create_app()
    with app.app_context():
        populate(app)
