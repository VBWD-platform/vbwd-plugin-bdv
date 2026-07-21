"""Board specification — the immutable rules a match is played under.

PURE: this module (and every module under ``core/``) imports nothing from
``vbwd``, ``flask``, ``sqlalchemy`` or the plugin's own ``models/``. That is a
product requirement, not a style preference — move prices must be reproducible
and auditable long after the match, and the balance harness replays hundreds of
matches with no infrastructure at all.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field as dataclass_field
from decimal import Decimal
from enum import Enum
from typing import List, Optional, Tuple


class SquareKind(str, Enum):
    """What a square does when a seat lands on it."""

    GO = "go"
    DEAL = "deal"
    SERVICE = "service"
    TAX = "tax"
    JAIL = "jail"
    GOTO_JAIL = "goto_jail"
    FREE = "free"
    CHANCE = "chance"
    COMMUNITY = "community"


#: Squares a seat can own. Everything else is scenery or an event.
OWNABLE_KINDS = (SquareKind.DEAL, SquareKind.SERVICE)


@dataclass(frozen=True)
class FieldError:
    """One validation failure, addressable by the admin UI."""

    field: str
    code: str
    index: Optional[int] = None
    # Aliased import: the ``field`` attribute above would otherwise shadow
    # ``dataclasses.field`` in readers' heads (and in mypy's).
    params: dict = dataclass_field(default_factory=dict)


@dataclass(frozen=True)
class SquareSpec:
    """One square of the board."""

    index: int
    kind: SquareKind
    name: str
    stage: Optional[str] = None
    price: int = 0
    rent_table: Tuple[int, ...] = ()
    service_multipliers: Tuple[int, ...] = ()
    house_cost: int = 0
    mortgage_value: int = 0
    tax_amount: int = 0

    @property
    def is_ownable(self) -> bool:
        return self.kind in OWNABLE_KINDS


@dataclass(frozen=True)
class BoardSpec:
    """The full, validated rule set for a match.

    Economy constants are ``Decimal`` — never float — because a price that
    differs in the last bit between two runs is not reproducible, and
    reproducibility is the product.
    """

    squares: Tuple[SquareSpec, ...]
    starting_cash: int = 15000
    go_salary: int = 2000
    jail_fine: int = 500
    jail_penalty_ev: int = 1000
    k_price: Decimal = Decimal("0.5")
    k_acquire: Decimal = Decimal("0.30")
    cap_pct: Decimal = Decimal("0.30")
    fee_policy: str = "all_to_poorest"
    chance_ev_hint: int = 0
    community_ev_hint: int = 0
    max_houses: int = 5

    # ---------------------------------------------------------------- lookups

    def square(self, index: int) -> SquareSpec:
        return self.squares[index % len(self.squares)]

    @property
    def size(self) -> int:
        return len(self.squares)

    def stage_members(self, stage: str) -> Tuple[int, ...]:
        """Indices of every deal square in a stage (the 'colour group')."""
        return tuple(s.index for s in self.squares if s.stage == stage)

    def indices_of_kind(self, kind: SquareKind) -> Tuple[int, ...]:
        return tuple(s.index for s in self.squares if s.kind == kind)

    def deck_ev_hint(self, kind: SquareKind) -> int:
        if kind == SquareKind.CHANCE:
            return self.chance_ev_hint
        if kind == SquareKind.COMMUNITY:
            return self.community_ev_hint
        return 0

    # ------------------------------------------------------------- validation

    def validate(self) -> List[FieldError]:
        """Every rule that makes a board playable, defined exactly once here.

        The persistence layer maps these onto API field errors; it never
        re-implements them (DRY — one definition of a valid board).
        """
        errors: List[FieldError] = []

        if not self.squares:
            errors.append(FieldError(field="squares", code="board_empty"))
            return errors

        for position, square in enumerate(self.squares):
            if square.index != position:
                errors.append(
                    FieldError(
                        field="squares",
                        code="index_not_contiguous",
                        index=position,
                        params={"expected": position, "actual": square.index},
                    )
                )

        go_squares = self.indices_of_kind(SquareKind.GO)
        if len(go_squares) != 1:
            errors.append(
                FieldError(
                    field="squares",
                    code="go_square_count",
                    params={"expected": 1, "actual": len(go_squares)},
                )
            )

        for square in self.squares:
            if square.kind == SquareKind.DEAL:
                if not square.stage:
                    errors.append(
                        FieldError(
                            field="stage",
                            code="deal_requires_stage",
                            index=square.index,
                        )
                    )
                if not square.rent_table:
                    errors.append(
                        FieldError(
                            field="rent_table",
                            code="deal_requires_rent_table",
                            index=square.index,
                        )
                    )
            if square.kind == SquareKind.SERVICE and not square.service_multipliers:
                errors.append(
                    FieldError(
                        field="service_multipliers",
                        code="service_requires_multipliers",
                        index=square.index,
                    )
                )
            if square.price < 0 or square.tax_amount < 0:
                errors.append(
                    FieldError(
                        field="price", code="negative_amount", index=square.index
                    )
                )

        for name, value in (
            ("starting_cash", self.starting_cash),
            ("go_salary", self.go_salary),
            ("jail_fine", self.jail_fine),
        ):
            if value < 0:
                errors.append(FieldError(field=name, code="negative_amount"))

        if not (Decimal("0") <= self.cap_pct <= Decimal("1")):
            errors.append(FieldError(field="cap_pct", code="out_of_range"))
        if self.k_price < Decimal("0"):
            errors.append(FieldError(field="k_price", code="out_of_range"))
        if self.k_acquire < Decimal("0"):
            errors.append(FieldError(field="k_acquire", code="out_of_range"))

        return errors

    @property
    def is_valid(self) -> bool:
        return not self.validate()

    # -------------------------------------------------------------- identity

    def spec_hash(self) -> str:
        """Stable content hash — pins a replay to the exact rules it was played under."""
        payload = {
            "squares": [
                {
                    "index": s.index,
                    "kind": s.kind.value,
                    "name": s.name,
                    "stage": s.stage,
                    "price": s.price,
                    "rent_table": list(s.rent_table),
                    "service_multipliers": list(s.service_multipliers),
                    "house_cost": s.house_cost,
                    "mortgage_value": s.mortgage_value,
                    "tax_amount": s.tax_amount,
                }
                for s in self.squares
            ],
            "starting_cash": self.starting_cash,
            "go_salary": self.go_salary,
            "jail_fine": self.jail_fine,
            "jail_penalty_ev": self.jail_penalty_ev,
            "k_price": str(self.k_price),
            "k_acquire": str(self.k_acquire),
            "cap_pct": str(self.cap_pct),
            "fee_policy": self.fee_policy,
            "chance_ev_hint": self.chance_ev_hint,
            "community_ev_hint": self.community_ev_hint,
            "max_houses": self.max_houses,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
