"""Agent-profile exchanger for the core data-exchange seam (S46).

An agent is a personality — a persona, a system prompt, a risk bias — and that
is exactly the kind of thing you want to carry between instances: build a roster
on a staging box, ship it to production, share one with another operator. This
puts the roster on the generic Settings → Import/Export page instead of the
one-way JSON download the Agents page used to offer.

Two things need care:

* **The LLM connection cannot travel.** It is an instance-local row holding an
  API key. The export names the connection by SLUG, and the import resolves that
  slug against the receiving instance; a slug that does not exist there lands the
  agent UNBOUND rather than failing the row (see ``_resolve_connection``).
* **``Decimal`` is not JSON.** Temperature and risk bias are ``Numeric`` columns,
  so they are stringified on the way out — the same shape ``to_dict`` already
  emits — and coerced back on the way in.
"""
from decimal import Decimal, InvalidOperation
from typing import Any, List, Optional

from vbwd.services.data_exchange.base_model_exchanger import BaseModelExchanger
from vbwd.services.data_exchange.port import CLUSTER_SETTINGS, EntityExchanger
from vbwd.services.data_exchange.registry import data_exchange_registry

ENTITY_KEY_BDV_AGENT = "bdv_agent_profiles"
NATURAL_KEY = "slug"

#: The connection travels as a slug under its own key, never as the raw column —
#: writing an instance-local UUID into an import file would be worse than
#: useless, because it would sometimes resolve to the WRONG connection.
CONNECTION_FIELD = "llm_connection_slug"

_DECIMAL_FIELDS = ("temperature", "risk_bias")


class _SessionModelRepository:
    """Narrow model repo satisfying the ``BaseModelExchanger`` contract (ISP).

    Mirrors the adapter core, CMS and bot_meinchat each use: the domain
    repository exposes finders, not the four flat methods the base needs.
    """

    def __init__(self, session: Any, model_class: type, natural_key: str) -> None:
        self._session = session
        self._model_class = model_class
        self._natural_key = natural_key

    def find_all(self) -> List[Any]:
        return self._session.query(self._model_class).all()

    def find_by_natural_key(self, value: Any) -> Optional[Any]:
        column = getattr(self._model_class, self._natural_key)
        return self._session.query(self._model_class).filter(column == value).first()

    def add(self, instance: Any) -> None:
        self._session.add(instance)

    def delete_all(self) -> None:
        self._session.query(self._model_class).delete()


class BdvAgentProfileExchanger(BaseModelExchanger):
    """Agent profiles, with the LLM binding carried as a portable slug."""

    def _serialise_row(self, row: Any, *, include_pii: bool) -> dict:
        serialised = super()._serialise_row(row, include_pii=include_pii)
        for field_name in _DECIMAL_FIELDS:
            value = serialised.get(field_name)
            if isinstance(value, Decimal):
                serialised[field_name] = str(value)
        return serialised

    def _import_row(self, row: dict, index: int, result, *, dry_run: bool) -> None:
        return super()._import_row(
            self._coerce(dict(row)), index, result, dry_run=dry_run
        )

    def _coerce(self, row: dict) -> dict:
        row = self._resolve_connection(row)
        for field_name in _DECIMAL_FIELDS:
            if field_name in row and row[field_name] is not None:
                try:
                    row[field_name] = Decimal(str(row[field_name]))
                except (InvalidOperation, ValueError):
                    row.pop(field_name)
        return row

    def _resolve_connection(self, row: dict) -> dict:
        """Translate the exported slug into this instance's connection id.

        An unknown slug leaves the agent unbound instead of rejecting the row.
        That is the useful behaviour for the case this feature exists to serve —
        moving personalities between instances whose connections are named
        differently — and an unbound agent is not broken: it plays the
        deterministic baseline until an admin binds it.
        """
        from vbwd.models.llm_connection import LlmConnection

        slug = row.pop(CONNECTION_FIELD, None)
        if not slug:
            return row
        connection = (
            self._session.query(LlmConnection)
            .filter(LlmConnection.slug == slug)
            .first()
        )
        row["llm_connection_id"] = connection.id if connection else None
        return row


def _connection_slug(profile) -> Optional[str]:
    """The bound connection's slug, resolved lazily at export time."""
    from vbwd.models.llm_connection import LlmConnection
    from vbwd.extensions import db

    if not profile.llm_connection_id:
        return None
    connection = db.session.get(LlmConnection, profile.llm_connection_id)
    return connection.slug if connection else None


def build_bdv_exchangers(session: Any) -> List[EntityExchanger]:
    """Construct the bdv exchangers bound to ``session``."""
    from plugins.bdv.bdv.models.match import BdvAgentProfile

    return [
        BdvAgentProfileExchanger(
            entity_key=ENTITY_KEY_BDV_AGENT,
            label="BizDevVibes Agent",
            cluster=CLUSTER_SETTINGS,
            natural_key=NATURAL_KEY,
            model_class=BdvAgentProfile,
            repository=_SessionModelRepository(session, BdvAgentProfile, NATURAL_KEY),
            session=session,
            public_fields=[
                "slug",
                "name",
                "persona",
                "system_prompt",
                "temperature",
                "risk_bias",
                "max_tokens_per_match",
                "is_active",
            ],
            fk_natural_key_map={CONNECTION_FIELD: _connection_slug},
            supported_formats=frozenset({"json", "csv"}),
        ),
    ]


def register_bdv_exchangers(session: Any) -> None:
    """Register the bdv exchangers (idempotent — re-registering replaces)."""
    for exchanger in build_bdv_exchangers(session):
        data_exchange_registry.register(exchanger)
