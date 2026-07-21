"""The purity oracle.

``bdv/core/`` must import nothing from the framework, the ORM, the wall clock or
a global RNG. This is not style policing: purity is what makes move prices
reproducible and auditable, replay free, and the balance harness runnable at
compute cost. If this test goes red, the product claim goes with it.
"""
import ast
import pathlib

import pytest

CORE = pathlib.Path(__file__).resolve().parents[2] / "bdv" / "core"

#: Import roots that would destroy determinism or couple the engine to infra.
BANNED_IMPORT_ROOTS = {
    "vbwd",
    "flask",
    "sqlalchemy",
    "alembic",
    "redis",
    "requests",
    "httpx",
    "openai",
    "anthropic",
    "plugins",
}

#: Calls that introduce hidden state — a roll must be a function of (seed, cursor).
BANNED_CALLS = {
    ("random", "random"),
    ("random", "randint"),
    ("random", "choice"),
    ("random", "shuffle"),
    ("random", "seed"),
    ("time", "time"),
    ("datetime", "now"),
    ("datetime", "utcnow"),
    ("uuid", "uuid4"),
}


def core_modules():
    return sorted(CORE.glob("*.py"))


def test_core_package_exists():
    assert CORE.is_dir(), "the pure engine package must exist"
    assert core_modules(), "…and contain modules"


@pytest.mark.parametrize("module", core_modules(), ids=lambda p: p.name)
def test_no_infrastructure_imports(module):
    tree = ast.parse(module.read_text(), filename=str(module))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                assert (
                    root not in BANNED_IMPORT_ROOTS
                ), f"{module.name} imports {alias.name!r} — the engine must stay pure"
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import inside core is fine
                continue
            root = (node.module or "").split(".")[0]
            assert (
                root not in BANNED_IMPORT_ROOTS
            ), f"{module.name} imports from {node.module!r} — the engine must stay pure"


@pytest.mark.parametrize("module", core_modules(), ids=lambda p: p.name)
def test_no_module_level_randomness_or_clock(module):
    """``dice.py`` may construct ``random.Random(seed)`` — that is seeded and
    therefore reproducible. What is banned is drawing from the GLOBAL rng or
    reading the wall clock, which no replay could ever reproduce."""
    tree = ast.parse(module.read_text(), filename=str(module))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            pair = (func.value.id, func.attr)
            assert (
                pair not in BANNED_CALLS
            ), f"{module.name} calls {pair[0]}.{pair[1]}() — non-reproducible"


def test_engine_is_importable_without_an_app_context():
    """The strongest proof: import and play with no Flask app, no DB, nothing."""
    from plugins.bdv.bdv.core.board import BoardSpec, SquareKind, SquareSpec
    from plugins.bdv.bdv.core.engine import MatchConfig, new_match

    spec = BoardSpec(
        squares=(
            SquareSpec(index=0, kind=SquareKind.GO, name="New Quarter"),
            SquareSpec(index=1, kind=SquareKind.FREE, name="Offsite"),
        )
    )
    state = new_match(spec, MatchConfig(seed="x", seat_count=2))
    assert len(state.seats) == 2
