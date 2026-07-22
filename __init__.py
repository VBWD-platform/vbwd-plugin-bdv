"""BizDevVibes (bdv) — a dice-market board game as a vbwd plugin.

The rules twist: a roll of {a, b} yields exactly three legal moves — a, b or
a+b. The sum is always FREE (fate, classic play); choosing a single die is a
PURCHASE, priced deterministically from the game state. The fee is not paid to
the bank — it goes to the opponents, so escaping fate funds your rivals.

Core is agnostic: this plugin adds itself only through seams core already
exposes (blueprint, container, permission catalog, bot command provider).
"""
from typing import Any, Dict, Optional, TYPE_CHECKING

from flask import current_app

from vbwd.plugins.base import BasePlugin, PluginMetadata, PublicRouteDeclaration

if TYPE_CHECKING:
    from flask import Blueprint


DEFAULT_CONFIG: Dict[str, Any] = {
    "debug_mode": False,
    # Display name of the game. Config-driven so a public rename stays a config
    # change rather than a refactor.
    "game_display_name": "BizDevVibes",
    # Turn pacing. A timeout auto-takes the FREE sum — the existing "fate
    # default" — so a disconnect degrades to classic play instead of stalling.
    "turn_timeout_seconds": 120,
    "negotiation_window_seconds": 30,
    # Per-match ceiling for LLM seats. Crossing it degrades the seat to the
    # deterministic baseline agent for the rest of the match.
    "agent_max_tokens_per_match": 60000,
    "agent_max_repair_retries": 2,
    # Lobby bounds (the engine supports 2..6).
    "min_seats": 2,
    "max_seats": 4,
    "default_seats": 3,
}


class BdvPlugin(BasePlugin):
    """The BizDevVibes game plugin."""

    @property
    def metadata(self) -> PluginMetadata:
        return PluginMetadata(
            name="bdv",
            version="26.7.0",
            author="VBWD Team",
            description=(
                "BizDevVibes — a dice-market board game where the sum is free, "
                "buying a single die is priced from the game state, and the fee "
                "goes to your opponents rather than the bank."
            ),
            # The chat surface rides the provider-neutral bot bridge. The import
            # stays lazy (optional-bridge): the REST/board path plays fine with
            # the bridge absent.
            dependencies=["bot-base"],
        )

    def initialize(self, config: Optional[Dict[str, Any]] = None) -> None:
        merged = {**DEFAULT_CONFIG}
        if config:
            merged.update(config)
        super().initialize(merged)

    def declare_public_routes(self) -> PublicRouteDeclaration:
        """One unauthenticated route: the capability probe.

        It carries no personal data and no match state — just the plugin id,
        version, display name, the board count and the available fee policies.
        It exists so an installer/walkthrough can prove the plugin is ENABLED
        rather than merely mounted (a disabled plugin whose blueprint registers
        without its DI providers 500s instead of 404ing).
        """
        return PublicRouteDeclaration(
            read={
                "/api/v1/bdv/meta": (
                    "Capability probe for install verification — no personal "
                    "data, no match state."
                ),
            },
        )

    def get_blueprint(self) -> Optional["Blueprint"]:
        from plugins.bdv.bdv.routes import bdv_bp

        return bdv_bp

    def get_url_prefix(self) -> Optional[str]:
        return ""

    @property
    def user_permissions(self):
        """Player-facing. The game is FREE for any logged-in user — this exists
        so an operator *can* gate it (e.g. behind a paid tier) by removing it
        from an access level, not because it is gated by default."""
        return [
            {
                "key": "bdv.play",
                "label": "Play BizDevVibes",
                "group": "BizDevVibes",
            },
        ]

    @property
    def admin_permissions(self):
        return [
            {
                "key": "bdv.boards.view",
                "label": "View BizDevVibes boards",
                "group": "BizDevVibes",
            },
            {
                "key": "bdv.boards.manage",
                "label": "Manage BizDevVibes boards and agents",
                "group": "BizDevVibes",
            },
            {
                "key": "bdv.matches.view",
                "label": "View BizDevVibes matches",
                "group": "BizDevVibes",
            },
        ]

    def on_enable(self) -> None:
        """Register repositories + services into the container.

        A plugin whose blueprint mounts without its DI providers 500s instead of
        404ing — that is the failure this method exists to prevent.
        """
        from vbwd.plugins.di_helpers import register_repositories
        from plugins.bdv.bdv.repositories.board_repository import BoardRepository
        from plugins.bdv.bdv.repositories.match_repository import (
            ActionRepository,
            AgentProfileRepository,
            MatchRepository,
            OfferRepository,
        )

        container = getattr(current_app, "container", None)
        if container is None:
            return

        register_repositories(
            container,
            {
                "bdv_board_repository": BoardRepository,
                "bdv_match_repository": MatchRepository,
                "bdv_action_repository": ActionRepository,
                "bdv_offer_repository": OfferRepository,
                "bdv_agent_profile_repository": AgentProfileRepository,
            },
        )

    def on_disable(self) -> None:
        from vbwd.plugins.di_helpers import unregister_repositories

        container = getattr(current_app, "container", None)
        if container is None:
            return
        unregister_repositories(
            container,
            [
                "bdv_board_repository",
                "bdv_match_repository",
                "bdv_action_repository",
                "bdv_offer_repository",
                "bdv_agent_profile_repository",
            ],
        )
