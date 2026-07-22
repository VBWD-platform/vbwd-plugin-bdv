"""The one place a live ``MatchService`` is assembled.

Both surfaces — the REST API and the chat bridge — build the service here. When
they each built their own, the LLM wiring reached only the one that had been
updated, and a table played by models over REST played by the baseline over
chat. Same rules, same seats, same driver: one factory.

Everything Flask-shaped stays in this module so the service itself keeps taking
plain collaborators and every test can construct it without an app context.
"""
from __future__ import annotations

from typing import Any, Optional

from flask import current_app

from vbwd.extensions import db

from ..repositories.match_repository import (
    ActionRepository,
    MatchRepository,
    OfferRepository,
)
from .match_service import MatchService

#: Kept in step with ``config.json``.
DEFAULT_LLM_CALLS_PER_REQUEST = 2


def plugin_config(key: str, default: Any) -> Any:
    """Read a plugin config value, tolerating an unmounted plugin.

    A read must never 500 because the plugin manager is absent — that happens in
    tests and during boot, and the correct answer there is the default.
    """
    try:
        manager = getattr(current_app, "plugin_manager", None)
        plugin = manager.get_plugin("bdv") if manager else None
        if plugin is not None:
            return plugin.get_config(key, default)
    except Exception:
        pass
    return default


def llm_client_for_connection(connection_id) -> Any:
    """Resolve a core ``LlmClient`` for an agent profile's bound connection.

    Core's container is keyed by SLUG while a profile stores the connection id,
    so this is the one place that translates. Raising is expected and handled:
    the caller falls back to the deterministic baseline seat rather than letting
    a table stall on an unreachable provider.
    """
    from vbwd.models.llm_connection import LlmConnection

    if not connection_id:
        raise LookupError("agent has no LLM connection bound")
    connection = db.session.get(LlmConnection, connection_id)
    if connection is None or not connection.is_active:
        raise LookupError("that LLM connection is missing or inactive")
    return current_app.container.llm_client(slug=connection.slug)


def build_match_service(matches: Optional[MatchRepository] = None) -> MatchService:
    """A service bound to the live session, with LLM seats wired in."""
    return MatchService(
        db.session,
        matches or MatchRepository(db.session),
        ActionRepository(db.session),
        OfferRepository(db.session),
        llm_client_factory=llm_client_for_connection,
        max_llm_calls_per_request=int(
            plugin_config(
                "agent_max_llm_calls_per_request", DEFAULT_LLM_CALLS_PER_REQUEST
            )
        ),
    )
