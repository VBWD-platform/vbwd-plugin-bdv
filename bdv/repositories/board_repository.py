"""Board data access — catalogue-contract list + the queries the admin needs."""
from typing import List, Optional, Tuple

from sqlalchemy import func, or_

from vbwd.repositories.base import BaseRepository

from ..models.board import BdvBoard, BdvCard, BdvSquare

SORTABLE = {
    "name": BdvBoard.name,
    "slug": BdvBoard.slug,
    "status": BdvBoard.status,
    "updated_at": BdvBoard.updated_at,
    "created_at": BdvBoard.created_at,
}


class BoardRepository(BaseRepository[BdvBoard]):
    def __init__(self, session):
        super().__init__(session, BdvBoard)

    def find_by_slug(self, slug: str) -> Optional[BdvBoard]:
        return self._session.query(BdvBoard).filter(BdvBoard.slug == slug).first()

    def list_catalogue(
        self,
        *,
        page: int = 1,
        per_page: int = 20,
        query: Optional[str] = None,
        status: Optional[str] = None,
        sort: str = "updated_at",
        order: str = "desc",
    ) -> Tuple[List[BdvBoard], int]:
        """Returns (page slice, FILTERED total).

        The total must reflect the effective filter, not the whole collection —
        a filtered list showing an unfiltered count is the exact bug the
        catalogue contract exists to prevent.
        """
        statement = self._session.query(BdvBoard)
        if query:
            pattern = f"%{query.strip()}%"
            statement = statement.filter(
                or_(BdvBoard.name.ilike(pattern), BdvBoard.slug.ilike(pattern))
            )
        if status:
            statement = statement.filter(BdvBoard.status == status)

        total = statement.with_entities(func.count(BdvBoard.id)).scalar() or 0

        column = SORTABLE.get(sort, BdvBoard.updated_at)
        statement = statement.order_by(
            column.desc() if order == "desc" else column.asc()
        )
        rows = statement.limit(per_page).offset((page - 1) * per_page).all()
        return rows, total

    def published(self) -> List[BdvBoard]:
        return (
            self._session.query(BdvBoard)
            .filter(BdvBoard.status == "published")
            .order_by(BdvBoard.name.asc())
            .all()
        )

    def replace_squares(self, board: BdvBoard, rows: List[dict]) -> None:
        """Whole-tab save: validate contiguity once, then swap atomically.

        One round-trip beats 40 PATCHes, and it is the only way the contiguity
        rule can be enforced at all.
        """
        for square in list(board.squares or []):
            self._session.delete(square)
        self._session.flush()
        for row in sorted(rows, key=lambda r: r["index"]):
            self._session.add(
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
        self._session.flush()
        self._session.refresh(board)

    def add_card(self, board: BdvBoard, payload: dict) -> BdvCard:
        card = BdvCard(
            board_id=board.id,
            deck=payload["deck"],
            title=payload["title"],
            flavor_text=payload.get("flavor_text"),
            effect=payload["effect"],
            weight=payload.get("weight", 1),
            is_active=payload.get("is_active", True),
            sort_order=payload.get("sort_order", 0),
        )
        self._session.add(card)
        self._session.flush()
        return card

    def find_card(self, card_id) -> Optional[BdvCard]:
        return self._session.get(BdvCard, card_id)
