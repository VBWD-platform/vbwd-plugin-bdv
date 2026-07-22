"""The one base for every refused action.

``EconomyError`` used to descend from ``RuntimeError`` while ``EngineError`` had
its own hierarchy, so the service's ``except EngineError`` missed it and a
perfectly ordinary refusal — "you must own the whole funnel stage first" —
escaped as a 500 instead of a 422. Two parallel bases for the same idea is one
too many; this module is the shared root, and it lives apart from both so
``economy`` and ``engine`` can each import it without a cycle.
"""


class EngineError(RuntimeError):
    """Base for every rejected action."""
