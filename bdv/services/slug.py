"""Readable, shareable slugs for a match.

A player should be able to say "join amber-hawk-42" rather than read out a UUID.
The words are deliberately bland and business-flavoured — the slug appears in
chat and on screen, so it must be safe to say out loud.
"""
from __future__ import annotations

import re
import secrets
from typing import Callable, Optional

_ADJECTIVES = (
    "amber",
    "brisk",
    "candid",
    "dapper",
    "eager",
    "frank",
    "golden",
    "hearty",
    "ivory",
    "jolly",
    "keen",
    "lucid",
    "mellow",
    "nimble",
    "olive",
    "prime",
    "quiet",
    "rapid",
    "sharp",
    "tidy",
    "upbeat",
    "vivid",
    "warm",
    "zesty",
)
_NOUNS = (
    "anchor",
    "beacon",
    "cobalt",
    "delta",
    "ember",
    "forum",
    "gadget",
    "harbor",
    "index",
    "jigsaw",
    "kernel",
    "ledger",
    "matrix",
    "nucleus",
    "orbit",
    "pivot",
    "quarry",
    "ridge",
    "summit",
    "tempo",
    "uplink",
    "vector",
    "willow",
    "zenith",
)

SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_SLUG_LENGTH = 60


class InvalidSlugError(ValueError):
    """The caller supplied a slug that is not usable as a shareable handle."""


def normalise(raw: str) -> str:
    """Lowercase, collapse separators, strip anything that is not a-z0-9-."""
    value = re.sub(r"[^a-z0-9]+", "-", (raw or "").strip().lower()).strip("-")
    return value[:MAX_SLUG_LENGTH]


def validate(raw: str) -> str:
    value = normalise(raw)
    if len(value) < 3:
        raise InvalidSlugError("slug must be at least 3 characters")
    if not SLUG_PATTERN.match(value):
        raise InvalidSlugError("slug may contain only letters, digits and dashes")
    return value


def generate(is_taken: Optional[Callable[[str], bool]] = None) -> str:
    """A readable slug, retried until it is free.

    Falls back to a longer random suffix rather than looping forever — a caller
    should always get a slug back.
    """
    for _ in range(12):
        candidate = (
            f"{secrets.choice(_ADJECTIVES)}-"
            f"{secrets.choice(_NOUNS)}-"
            f"{secrets.randbelow(90) + 10}"
        )
        if is_taken is None or not is_taken(candidate):
            return candidate
    return f"table-{secrets.token_hex(4)}"
