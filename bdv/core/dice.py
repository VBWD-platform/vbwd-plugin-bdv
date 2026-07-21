"""Deterministic dice.

A roll is a pure function of ``(seed, cursor)``. There is no shared RNG and no
hidden state, so replaying an action log reproduces every roll exactly.
"""
from __future__ import annotations

import random
from typing import Tuple


def roll_at(seed: str, cursor: int) -> Tuple[int, int]:
    """The two dice for a given roll index of a given match."""
    generator = random.Random(f"{seed}:{cursor}")
    return generator.randint(1, 6), generator.randint(1, 6)


def shuffled_order(
    seed: str, deck: str, size: int, reshuffle: int = 0
) -> Tuple[int, ...]:
    """A deterministic draw order for a deck, reproducible from the seed alone."""
    if size <= 0:
        return ()
    order = list(range(size))
    random.Random(f"{seed}:{deck}:{reshuffle}").shuffle(order)
    return tuple(order)
