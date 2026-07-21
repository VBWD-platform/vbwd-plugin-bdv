"""BizDevVibes seed data — the canonical ``funnel-40`` board.

CREATE-ONLY and idempotent: re-running must never overwrite a board an admin has
edited. That is the rule for any seeder that could ever touch production.
"""
from vbwd.extensions import db


def populate(app=None):
    from plugins.bdv.bdv.services.board_seeder import seed_funnel_board

    board, created = seed_funnel_board(db.session)
    db.session.commit()
    print(
        f"[bdv] {'created' if created else 'already present'}: "
        f"{board.slug} ({len(board.squares)} squares, {len(board.cards)} cards)"
    )
    return board


if __name__ == "__main__":
    from vbwd.app import create_app

    app = create_app()
    with app.app_context():
        populate(app)
