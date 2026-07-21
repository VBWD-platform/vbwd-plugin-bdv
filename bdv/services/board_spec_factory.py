"""Compile board rows into the pure ``BoardSpec`` the engine consumes.

This module is a faithful LOADER. It maps engine validation errors onto API
field errors; it never re-implements a rule. One definition of a valid board
lives in ``core.board.BoardSpec.validate()``.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Dict, List

from ..core.board import BoardSpec, FieldError, SquareKind, SquareSpec
from ..core.effects import effect_ev_hint, validate_effect


class BoardSpecFactory:
    """Rows -> pure spec, plus publish-time validation."""

    @staticmethod
    def build(board) -> BoardSpec:
        squares = tuple(
            SquareSpec(
                index=row.index,
                kind=SquareKind(row.kind),
                name=row.name,
                stage=row.stage,
                price=row.price or 0,
                rent_table=tuple(row.rent_table or ()),
                service_multipliers=tuple(row.service_multipliers or ()),
                house_cost=row.house_cost or 0,
                mortgage_value=row.mortgage_value or 0,
                tax_amount=row.tax_amount or 0,
            )
            for row in sorted(board.squares or [], key=lambda s: s.index)
        )

        base = BoardSpec(
            squares=squares,
            starting_cash=board.starting_cash,
            go_salary=board.go_salary,
            jail_fine=board.jail_fine,
            jail_penalty_ev=board.jail_penalty_ev,
            k_price=Decimal(str(board.k_price)),
            k_acquire=Decimal(str(board.k_acquire)),
            cap_pct=Decimal(str(board.cap_pct)),
            fee_policy=board.fee_policy,
            max_houses=board.max_houses,
        )
        # Deck hints need the compiled spec (ops read go_salary etc.), so they
        # are folded in on a second pass.
        import dataclasses

        return dataclasses.replace(
            base,
            chance_ev_hint=BoardSpecFactory.deck_ev_hint(board, "chance", base),
            community_ev_hint=BoardSpecFactory.deck_ev_hint(board, "community", base),
        )

    @staticmethod
    def deck_ev_hint(board, deck: str, spec: BoardSpec) -> int:
        """Weight-averaged EV of a deck — what prices a draw square.

        Closed-form on purpose: simulating the deck inside a price quote would be
        both slow and non-deterministic, and the price must be auditable.
        """
        cards = [c for c in (board.cards or []) if c.deck == deck and c.is_active]
        if not cards:
            return 0
        total_weight = sum(max(1, c.weight or 1) for c in cards)
        weighted = 0
        for card in cards:
            try:
                weighted += effect_ev_hint(card.effect or {}, spec) * max(1, card.weight or 1)
            except Exception:  # a broken card contributes 0 rather than exploding a quote
                continue
        return int(round(weighted / total_weight)) if total_weight else 0

    @staticmethod
    def cards_for(board, deck: str) -> List:
        return [
            card
            for card in sorted(board.cards or [], key=lambda c: (c.sort_order, str(c.id)))
            if card.deck == deck and card.is_active
        ]

    @staticmethod
    def validate(board) -> List[Dict]:
        """Field errors the admin UI can pin to a tab, a field and a square."""
        spec = BoardSpecFactory.build(board)
        errors: List[Dict] = [
            BoardSpecFactory._as_payload(error) for error in spec.validate()
        ]
        for card in board.cards or []:
            for message in validate_effect(card.effect or {}):
                errors.append(
                    {
                        "field": "effect",
                        "code": "invalid_effect",
                        "index": None,
                        "params": {"card": card.title, "message": message},
                    }
                )
        if board.default_seats < board.min_seats or board.default_seats > board.max_seats:
            errors.append(
                {
                    "field": "default_seats",
                    "code": "out_of_range",
                    "index": None,
                    "params": {"min": board.min_seats, "max": board.max_seats},
                }
            )
        return errors

    @staticmethod
    def _as_payload(error: FieldError) -> Dict:
        return {
            "field": error.field,
            "code": error.code,
            "index": error.index,
            "params": error.params,
        }
