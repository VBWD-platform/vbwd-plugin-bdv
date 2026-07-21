"""Card rules as declarative descriptors, resolved by a registry.

An admin authors ``{"ops": [{"op": "collect_from_each_player", "params": {...}}]}``.
No stored code, no ``eval``. Each op supplies four things:

* ``apply``          — pure state transition
* ``describe``       — an i18n key + params, so the human-readable text is
                       GENERATED from the ops and cannot drift from behaviour
* ``ev_hint``        — signed credits, so a draw square can be priced without
                       simulating the deck
* ``params_schema``  — JSON Schema, which drives the admin form generically

Adding an op is additive: one class here, zero fe-admin code.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Dict, List, Mapping, Protocol, Tuple

from .board import BoardSpec, SquareKind
from .state import MatchState


@dataclass(frozen=True)
class Description:
    """i18n key + params — never a rendered sentence."""

    key: str
    params: Dict = field(default_factory=dict)


@dataclass(frozen=True)
class EffectContext:
    """Who drew the card, and under which board."""

    seat_index: int


@dataclass(frozen=True)
class EffectResult:
    state: MatchState
    events: Tuple[Dict, ...] = ()


class CardExecutionError(RuntimeError):
    """A card could not be applied — the whole card is rolled back."""


class UnknownEffectOpError(LookupError):
    """An op id that is not registered. Loud at save time AND at draw time."""


class EffectOp(Protocol):
    op_id: str
    params_schema: Dict

    def apply(
        self, state: MatchState, spec: BoardSpec, ctx: EffectContext, params: Mapping
    ) -> EffectResult:
        ...

    def describe(self, params: Mapping, spec: BoardSpec) -> Description:
        ...

    def ev_hint(self, params: Mapping, spec: BoardSpec) -> int:
        ...


_REGISTRY: Dict[str, EffectOp] = {}


def register_op(op_class):
    """Class decorator. Registers a single INSTANCE — ops are stateless, and
    storing the class would leave every method unbound at call time."""
    instance = op_class()
    _REGISTRY[instance.op_id] = instance
    return op_class


def resolve_op(op_id: str) -> EffectOp:
    try:
        return _REGISTRY[op_id]
    except KeyError as missing:
        raise UnknownEffectOpError(f"Unknown effect op: {op_id!r}") from missing


def registered_ops() -> Tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


def op_descriptors() -> List[Dict]:
    """What the admin card-rule builder renders its form from."""
    return [
        {
            "op_id": op_id,
            "label_key": f"bdv.effect.{op_id}.label",
            "description_key": f"bdv.effect.{op_id}",
            "params_schema": _REGISTRY[op_id].params_schema,
        }
        for op_id in sorted(_REGISTRY)
    ]


# --------------------------------------------------------------------- helpers

_AMOUNT_SCHEMA = {
    "type": "object",
    "properties": {"amount": {"type": "integer", "minimum": 0}},
    "required": ["amount"],
}


def _amount(params: Mapping) -> int:
    try:
        return int(params["amount"])
    except (KeyError, TypeError, ValueError) as bad:
        raise CardExecutionError("effect requires an integer 'amount'") from bad


def _solvent_others(state: MatchState, seat_index: int) -> Tuple[int, ...]:
    return tuple(
        s.index for s in state.seats if s.index != seat_index and not s.bankrupt
    )


def _move_to(
    state: MatchState,
    spec: BoardSpec,
    seat_index: int,
    destination: int,
    collect_go: bool,
) -> Tuple[MatchState, Tuple[Dict, ...]]:
    seat = state.seat(seat_index)
    passed_go = collect_go and destination < seat.position
    next_state = state.with_seat(replace(seat, position=destination % spec.size))
    events: List[Dict] = [
        {"type": "moved", "seat": seat_index, "to": destination % spec.size}
    ]
    if passed_go:
        next_state = next_state.with_cash_delta(seat_index, spec.go_salary)
        events.append(
            {"type": "go_salary", "seat": seat_index, "amount": spec.go_salary}
        )
    return next_state, tuple(events)


# ------------------------------------------------------------------- the ops


@register_op
class PayBank:
    op_id = "pay_bank"
    params_schema = _AMOUNT_SCHEMA

    def apply(self, state, spec, ctx, params) -> EffectResult:
        amount = _amount(params)
        return EffectResult(
            state.with_cash_delta(ctx.seat_index, -amount),
            ({"type": "paid_bank", "seat": ctx.seat_index, "amount": amount},),
        )

    def describe(self, params, spec) -> Description:
        return Description("bdv.effect.pay_bank", {"amount": _amount(params)})

    def ev_hint(self, params, spec) -> int:
        return -_amount(params)


@register_op
class CollectBank:
    op_id = "collect_bank"
    params_schema = _AMOUNT_SCHEMA

    def apply(self, state, spec, ctx, params) -> EffectResult:
        amount = _amount(params)
        return EffectResult(
            state.with_cash_delta(ctx.seat_index, amount),
            ({"type": "collected_bank", "seat": ctx.seat_index, "amount": amount},),
        )

    def describe(self, params, spec) -> Description:
        return Description("bdv.effect.collect_bank", {"amount": _amount(params)})

    def ev_hint(self, params, spec) -> int:
        return _amount(params)


@register_op
class CollectFromEachPlayer:
    op_id = "collect_from_each_player"
    params_schema = _AMOUNT_SCHEMA

    def apply(self, state, spec, ctx, params) -> EffectResult:
        amount = _amount(params)
        others = _solvent_others(state, ctx.seat_index)
        next_state = state
        for other in others:
            next_state = next_state.with_cash_delta(other, -amount)
        next_state = next_state.with_cash_delta(ctx.seat_index, amount * len(others))
        return EffectResult(
            next_state,
            (
                {
                    "type": "collected_from_players",
                    "seat": ctx.seat_index,
                    "amount": amount,
                    "from": list(others),
                    "total": amount * len(others),
                },
            ),
        )

    def describe(self, params, spec) -> Description:
        return Description(
            "bdv.effect.collect_from_each_player", {"amount": _amount(params)}
        )

    def ev_hint(self, params, spec) -> int:
        return _amount(params)


@register_op
class PayEachPlayer:
    op_id = "pay_each_player"
    params_schema = _AMOUNT_SCHEMA

    def apply(self, state, spec, ctx, params) -> EffectResult:
        amount = _amount(params)
        others = _solvent_others(state, ctx.seat_index)
        next_state = state
        for other in others:
            next_state = next_state.with_cash_delta(other, amount)
        next_state = next_state.with_cash_delta(ctx.seat_index, -amount * len(others))
        return EffectResult(
            next_state,
            (
                {
                    "type": "paid_players",
                    "seat": ctx.seat_index,
                    "amount": amount,
                    "to": list(others),
                    "total": amount * len(others),
                },
            ),
        )

    def describe(self, params, spec) -> Description:
        return Description("bdv.effect.pay_each_player", {"amount": _amount(params)})

    def ev_hint(self, params, spec) -> int:
        return -_amount(params)


@register_op
class MoveToSquare:
    op_id = "move_to_square"
    params_schema = {
        "type": "object",
        "properties": {
            "index": {"type": "integer", "minimum": 0, "format": "square-index"},
            "collect_go_salary": {"type": "boolean", "default": True},
        },
        "required": ["index"],
    }

    def apply(self, state, spec, ctx, params) -> EffectResult:
        destination = int(params["index"]) % spec.size
        collect = bool(params.get("collect_go_salary", True))
        next_state, events = _move_to(state, spec, ctx.seat_index, destination, collect)
        return EffectResult(next_state, events)

    def describe(self, params, spec) -> Description:
        index = int(params["index"]) % spec.size
        return Description(
            "bdv.effect.move_to_square",
            {"index": index, "name": spec.square(index).name},
        )

    def ev_hint(self, params, spec) -> int:
        index = int(params["index"]) % spec.size
        square = spec.square(index)
        if square.kind == SquareKind.GO:
            return spec.go_salary
        if square.kind == SquareKind.TAX:
            return -square.tax_amount
        return 0


@register_op
class MoveRelative:
    op_id = "move_relative"
    params_schema = {
        "type": "object",
        "properties": {"steps": {"type": "integer"}},
        "required": ["steps"],
    }

    def apply(self, state, spec, ctx, params) -> EffectResult:
        steps = int(params["steps"])
        seat = state.seat(ctx.seat_index)
        destination = (seat.position + steps) % spec.size
        next_state = state.with_seat(replace(seat, position=destination))
        return EffectResult(
            next_state,
            ({"type": "moved", "seat": ctx.seat_index, "to": destination},),
        )

    def describe(self, params, spec) -> Description:
        return Description("bdv.effect.move_relative", {"steps": int(params["steps"])})

    def ev_hint(self, params, spec) -> int:
        return 0


@register_op
class GoToJail:
    op_id = "go_to_jail"
    params_schema = {"type": "object", "properties": {}}

    def apply(self, state, spec, ctx, params) -> EffectResult:
        jail_squares = spec.indices_of_kind(SquareKind.JAIL)
        destination = (
            jail_squares[0] if jail_squares else state.seat(ctx.seat_index).position
        )
        seat = state.seat(ctx.seat_index)
        next_state = state.with_seat(
            replace(seat, position=destination, in_jail=True, jail_turns=0)
        )
        return EffectResult(next_state, ({"type": "jailed", "seat": ctx.seat_index},))

    def describe(self, params, spec) -> Description:
        return Description("bdv.effect.go_to_jail", {})

    def ev_hint(self, params, spec) -> int:
        return -spec.jail_penalty_ev


@register_op
class GetOutOfJailCard:
    op_id = "get_out_of_jail_card"
    params_schema = {"type": "object", "properties": {}}

    def apply(self, state, spec, ctx, params) -> EffectResult:
        seat = state.seat(ctx.seat_index)
        next_state = state.with_seat(
            replace(seat, get_out_of_jail_cards=seat.get_out_of_jail_cards + 1)
        )
        return EffectResult(
            next_state,
            ({"type": "received_jail_card", "seat": ctx.seat_index},),
        )

    def describe(self, params, spec) -> Description:
        return Description("bdv.effect.get_out_of_jail_card", {})

    def ev_hint(self, params, spec) -> int:
        return spec.jail_fine


@register_op
class AdvanceToNearestKind:
    op_id = "advance_to_nearest_kind"
    params_schema = {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": [SquareKind.SERVICE.value, SquareKind.DEAL.value],
            },
            "rent_multiplier": {"type": "integer", "minimum": 1, "default": 1},
        },
        "required": ["kind"],
    }

    def apply(self, state, spec, ctx, params) -> EffectResult:
        kind = SquareKind(params["kind"])
        seat = state.seat(ctx.seat_index)
        candidates = spec.indices_of_kind(kind)
        if not candidates:
            raise CardExecutionError(f"board has no square of kind {kind.value}")
        distances = {
            (index - seat.position) % spec.size: index
            for index in sorted(candidates, reverse=True)
        }
        forward = (
            min(d for d in distances if d > 0) if any(d > 0 for d in distances) else 0
        )
        destination = distances[forward]
        next_state, events = _move_to(state, spec, ctx.seat_index, destination, True)
        return EffectResult(next_state, events)

    def describe(self, params, spec) -> Description:
        return Description(
            "bdv.effect.advance_to_nearest_kind",
            {
                "kind": params["kind"],
                "rent_multiplier": int(params.get("rent_multiplier", 1)),
            },
        )

    def ev_hint(self, params, spec) -> int:
        return 0


@register_op
class PayPerBuilding:
    op_id = "pay_per_building"
    params_schema = {
        "type": "object",
        "properties": {
            "per_house": {"type": "integer", "minimum": 0},
            "per_hotel": {"type": "integer", "minimum": 0},
        },
        "required": ["per_house", "per_hotel"],
    }

    def apply(self, state, spec, ctx, params) -> EffectResult:
        per_house = int(params["per_house"])
        per_hotel = int(params["per_hotel"])
        owned = state.owned_by(ctx.seat_index)
        total = 0
        for index in owned:
            count = state.houses_on(index)
            if count >= spec.max_houses:
                total += per_hotel
            else:
                total += per_house * count
        return EffectResult(
            state.with_cash_delta(ctx.seat_index, -total),
            (
                {
                    "type": "paid_maintenance",
                    "seat": ctx.seat_index,
                    "amount": total,
                },
            ),
        )

    def describe(self, params, spec) -> Description:
        return Description(
            "bdv.effect.pay_per_building",
            {
                "per_house": int(params["per_house"]),
                "per_hotel": int(params["per_hotel"]),
            },
        )

    def ev_hint(self, params, spec) -> int:
        # Approximation, documented: the true cost depends on holdings, which a
        # price quote cannot know for a card not yet drawn. Deterministic is
        # what matters here, not exact.
        return -int(params["per_house"]) * 2


@register_op
class SkipNextTurn:
    op_id = "skip_next_turn"
    params_schema = {"type": "object", "properties": {}}

    def apply(self, state, spec, ctx, params) -> EffectResult:
        seat = state.seat(ctx.seat_index)
        return EffectResult(
            state.with_seat(replace(seat, skip_next_turn=True)),
            ({"type": "turn_skipped", "seat": ctx.seat_index},),
        )

    def describe(self, params, spec) -> Description:
        return Description("bdv.effect.skip_next_turn", {})

    def ev_hint(self, params, spec) -> int:
        return 0


# ------------------------------------------------------------- card execution


def validate_effect(effect: Mapping) -> List[str]:
    """Errors an admin should see at save time. Empty list == valid."""
    errors: List[str] = []
    ops = effect.get("ops") if isinstance(effect, Mapping) else None
    if not isinstance(ops, list) or not ops:
        return ["effect must contain a non-empty 'ops' list"]
    for position, entry in enumerate(ops):
        if not isinstance(entry, Mapping) or "op" not in entry:
            errors.append(f"ops[{position}]: missing 'op'")
            continue
        try:
            resolve_op(entry["op"])
        except UnknownEffectOpError as unknown:
            errors.append(f"ops[{position}]: {unknown}")
    return errors


def describe_effect(effect: Mapping, spec: BoardSpec) -> Tuple[Description, ...]:
    """The single source of truth for a card's human-readable text."""
    return tuple(
        resolve_op(entry["op"]).describe(entry.get("params", {}), spec)
        for entry in effect.get("ops", [])
    )


def effect_ev_hint(effect: Mapping, spec: BoardSpec) -> int:
    return sum(
        resolve_op(entry["op"]).ev_hint(entry.get("params", {}), spec)
        for entry in effect.get("ops", [])
    )


def apply_effect(
    state: MatchState, spec: BoardSpec, seat_index: int, effect: Mapping
) -> EffectResult:
    """Apply every op in order, atomically.

    If any op raises, nothing is applied — a half-executed card would corrupt
    both the match and its replay.
    """
    ctx = EffectContext(seat_index=seat_index)
    working = state
    events: List[Dict] = []
    for entry in effect.get("ops", []):
        op = resolve_op(entry["op"])
        result = op.apply(working, spec, ctx, entry.get("params", {}))
        working = result.state
        events.extend(result.events)
    return EffectResult(working, tuple(events))
