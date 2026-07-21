"""Seed the canonical board through the model layer — never raw SQL.

Create-only by design: if ``funnel-40`` already exists it is returned untouched,
so a re-run can never clobber an edited board.
"""
from typing import Tuple

from ..models.board import BdvBoard, BdvCard, BdvSquare
from .seed_board import SEED_BOARD_SLUG, seed_board_payload, seed_cards, seed_squares


def seed_funnel_board(session) -> Tuple[BdvBoard, bool]:
    existing = session.query(BdvBoard).filter(BdvBoard.slug == SEED_BOARD_SLUG).first()
    if existing is not None:
        return existing, False

    payload = seed_board_payload()
    board = BdvBoard(**payload)
    session.add(board)
    session.flush()

    for row in seed_squares():
        session.add(
            BdvSquare(
                board_id=board.id,
                index=row["index"],
                kind=row["kind"],
                name=row["name"],
                stage=row.get("stage"),
                price=row.get("price", 0),
                rent_table=row.get("rent_table") or [],
                service_multipliers=row.get("service_multipliers") or [],
                house_cost=row.get("house_cost", 0),
                mortgage_value=row.get("mortgage_value", 0),
                tax_amount=row.get("tax_amount", 0),
            )
        )

    for card in seed_cards():
        session.add(
            BdvCard(
                board_id=board.id,
                deck=card["deck"],
                title=card["title"],
                flavor_text=card.get("flavor_text"),
                effect=card["effect"],
                weight=card.get("weight", 1),
                is_active=True,
                sort_order=card.get("sort_order", 0),
            )
        )

    session.flush()
    session.refresh(board)
    return board, True
