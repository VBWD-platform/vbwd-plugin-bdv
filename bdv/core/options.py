"""The option set: a roll of {a, b} yields exactly a, b or a+b."""
from __future__ import annotations

from typing import Tuple


def legal_options(roll: Tuple[int, int]) -> Tuple[int, ...]:
    """Deduped, ascending. Doubles collapse to two options: {3,3} -> (3, 6)."""
    first, second = roll
    return tuple(sorted({first, second, first + second}))


def sum_option(roll: Tuple[int, int]) -> int:
    """The free 'fate' move — always available, always price 0."""
    return roll[0] + roll[1]
