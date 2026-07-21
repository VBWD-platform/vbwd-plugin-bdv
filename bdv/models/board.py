"""Board configuration — the admin-editable DATA behind a pure BoardSpec.

The database never defines game rules; it defines game data. Validation and
semantics live in ``core.board.BoardSpec`` and are mapped here, never
re-implemented (DRY — one definition of a valid board).
"""
from vbwd.extensions import db
from vbwd.models.base import BaseModel

BOARD_STATUS_DRAFT = "draft"
BOARD_STATUS_PUBLISHED = "published"


class BdvBoard(BaseModel):
    """One playable board configuration."""

    __tablename__ = "bdv_board"

    name = db.Column(db.String(120), nullable=False)
    slug = db.Column(db.String(140), nullable=False, unique=True, index=True)
    description = db.Column(db.Text, nullable=True)
    status = db.Column(
        db.String(20), nullable=False, server_default=BOARD_STATUS_DRAFT, index=True
    )
    game_display_name = db.Column(
        db.String(120), nullable=False, server_default="BizDevVibes"
    )
    currency_label = db.Column(db.String(20), nullable=False, server_default="cr")

    starting_cash = db.Column(db.Integer, nullable=False, server_default="15000")
    go_salary = db.Column(db.Integer, nullable=False, server_default="2000")
    jail_fine = db.Column(db.Integer, nullable=False, server_default="500")
    jail_penalty_ev = db.Column(db.Integer, nullable=False, server_default="1000")

    # Economy dials — Numeric, never Float: a price that differs in the last bit
    # between two runs is not reproducible, and reproducibility is the product.
    k_price = db.Column(db.Numeric(6, 4), nullable=False, server_default="0.5000")
    k_acquire = db.Column(db.Numeric(6, 4), nullable=False, server_default="0.3000")
    cap_pct = db.Column(db.Numeric(6, 4), nullable=False, server_default="0.3000")
    fee_policy = db.Column(
        db.String(40), nullable=False, server_default="all_to_poorest"
    )

    min_seats = db.Column(db.Integer, nullable=False, server_default="2")
    max_seats = db.Column(db.Integer, nullable=False, server_default="6")
    default_seats = db.Column(db.Integer, nullable=False, server_default="3")
    turn_timeout_seconds = db.Column(db.Integer, nullable=False, server_default="120")
    negotiation_window_seconds = db.Column(
        db.Integer, nullable=False, server_default="30"
    )
    max_houses = db.Column(db.Integer, nullable=False, server_default="5")

    squares = db.relationship(
        "BdvSquare",
        backref="board",
        lazy="selectin",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="BdvSquare.index",
    )
    cards = db.relationship(
        "BdvCard",
        backref="board",
        lazy="selectin",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="BdvCard.sort_order",
    )

    @property
    def is_published(self) -> bool:
        return self.status == BOARD_STATUS_PUBLISHED

    def to_dict(self, include_children: bool = False) -> dict:
        payload = {
            "id": str(self.id),
            "name": self.name,
            "slug": self.slug,
            "description": self.description,
            "status": self.status,
            "game_display_name": self.game_display_name,
            "currency_label": self.currency_label,
            "starting_cash": self.starting_cash,
            "go_salary": self.go_salary,
            "jail_fine": self.jail_fine,
            "jail_penalty_ev": self.jail_penalty_ev,
            "k_price": str(self.k_price),
            "k_acquire": str(self.k_acquire),
            "cap_pct": str(self.cap_pct),
            "fee_policy": self.fee_policy,
            "min_seats": self.min_seats,
            "max_seats": self.max_seats,
            "default_seats": self.default_seats,
            "turn_timeout_seconds": self.turn_timeout_seconds,
            "negotiation_window_seconds": self.negotiation_window_seconds,
            "max_houses": self.max_houses,
            "squares_count": len(self.squares or []),
            "cards_count": len(self.cards or []),
        }
        if include_children:
            payload["squares"] = [s.to_dict() for s in self.squares]
            payload["cards"] = [c.to_dict() for c in self.cards]
        return payload


class BdvSquare(BaseModel):
    """One square. ``stage`` is the funnel stage — the 'colour group'."""

    __tablename__ = "bdv_square"
    __table_args__ = (
        db.UniqueConstraint("board_id", "index", name="uq_bdv_square_board_index"),
    )

    board_id = db.Column(
        db.UUID(as_uuid=True),
        db.ForeignKey("bdv_board.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    index = db.Column(db.Integer, nullable=False)
    kind = db.Column(db.String(20), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    stage = db.Column(db.String(60), nullable=True, index=True)
    price = db.Column(db.Integer, nullable=False, server_default="0")
    rent_table = db.Column(db.JSON, nullable=True)
    service_multipliers = db.Column(db.JSON, nullable=True)
    house_cost = db.Column(db.Integer, nullable=False, server_default="0")
    mortgage_value = db.Column(db.Integer, nullable=False, server_default="0")
    tax_amount = db.Column(db.Integer, nullable=False, server_default="0")

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "index": self.index,
            "kind": self.kind,
            "name": self.name,
            "stage": self.stage,
            "price": self.price,
            "rent_table": list(self.rent_table or []),
            "service_multipliers": list(self.service_multipliers or []),
            "house_cost": self.house_cost,
            "mortgage_value": self.mortgage_value,
            "tax_amount": self.tax_amount,
        }


class BdvCard(BaseModel):
    """A Market Event / Board Memo card.

    ``effect`` is a declarative descriptor — ``{"ops": [{"op": ..., "params": ...}]}``
    — never stored code. The human-readable description is GENERATED from the ops
    by the effect registry, so it cannot drift from what the card does.
    """

    __tablename__ = "bdv_card"

    board_id = db.Column(
        db.UUID(as_uuid=True),
        db.ForeignKey("bdv_board.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    deck = db.Column(db.String(20), nullable=False, index=True)
    title = db.Column(db.String(160), nullable=False)
    flavor_text = db.Column(db.Text, nullable=True)
    effect = db.Column(db.JSON, nullable=False)
    weight = db.Column(db.Integer, nullable=False, server_default="1")
    is_active = db.Column(db.Boolean, nullable=False, server_default="true")
    sort_order = db.Column(db.Integer, nullable=False, server_default="0")

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "deck": self.deck,
            "title": self.title,
            "flavor_text": self.flavor_text,
            "effect": self.effect,
            "weight": self.weight,
            "is_active": self.is_active,
            "sort_order": self.sort_order,
        }
