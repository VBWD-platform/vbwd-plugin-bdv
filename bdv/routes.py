"""BizDevVibes HTTP surface.

Admin routes are RBAC-gated; player routes require ``bdv.play``. Two invariants
run through every mutating handler:

* the acting seat comes from the AUTHENTICATED user, never the request body;
* prices are recomputed server-side — a client-supplied price is ignored.
"""
import re
from typing import Dict

from flask import Blueprint, current_app, g, jsonify, request

from vbwd.extensions import db
from vbwd.middleware.auth import (
    require_auth,
    require_permission,
    require_user_permission,
)
from vbwd.utils.pagination import paginate

from .core.effects import op_descriptors, validate_effect
from .core.engine import ActionType
from .core.fees import available_fee_policies
from .models.board import BOARD_STATUS_DRAFT, BOARD_STATUS_PUBLISHED, BdvBoard
from .models.match import BdvAgentProfile
from .models.match import (
    FILL_AGENTS_NOW,
    SEAT_KIND_BASELINE,
    SEAT_KIND_HUMAN,
    SEAT_KIND_LLM,
    SEAT_KIND_OPEN,
)
from .repositories.board_repository import BoardRepository
from .repositories.match_repository import (
    AgentProfileRepository,
    MatchRepository,
    OfferRepository,
)
from .services.board_spec_factory import BoardSpecFactory
from .services.match_service import MatchError, MatchService, StaleStateError
from .services.service_factory import build_match_service, plugin_config

bdv_bp = Blueprint("bdv", __name__)

ADMIN = "/api/v1/admin/bdv"
PLAY = "/api/v1/bdv"


# ------------------------------------------------------------------ plumbing


def _boards() -> BoardRepository:
    return BoardRepository(db.session)


def _matches() -> MatchRepository:
    return MatchRepository(db.session)


def _match_service() -> MatchService:
    return build_match_service()


def _page_args():
    try:
        page = max(1, int(request.args.get("page", 1)))
        per_page = min(100, max(1, int(request.args.get("per_page", 20))))
    except (TypeError, ValueError):
        page, per_page = 1, 20
    return page, per_page


def _current_user_id():
    """The authenticated user's id.

    ``require_auth`` sets ``g.user`` — NOT ``g.current_user``. Reading the wrong
    attribute silently yields None, which would create human seats with a NULL
    ``user_id`` (violating ck_bdv_seat_one_occupant) and make seat authorisation
    never match.
    """
    user = getattr(g, "user", None)
    return getattr(user, "id", None) if user else None


# --------------------------------------------------------------------- meta


@bdv_bp.route(f"{PLAY}/meta", methods=["GET"])
def bdv_meta():
    """Proves the plugin is ENABLED, not merely mounted."""
    display_name = "BizDevVibes"
    try:
        manager = getattr(current_app, "plugin_manager", None)
        plugin = manager.get_plugin("bdv") if manager else None
        if plugin is not None:
            display_name = plugin.get_config("game_display_name", display_name)
    except Exception:
        pass
    return (
        jsonify(
            {
                "plugin": "bdv",
                "version": "26.7.0",
                "display_name": display_name,
                "board_count": _boards().count(),
                "fee_policies": list(available_fee_policies()),
            }
        ),
        200,
    )


# ------------------------------------------------------------- admin: boards


@bdv_bp.route(f"{ADMIN}/boards", methods=["GET"])
@require_auth
@require_permission("bdv.boards.view")
def list_boards():
    page, per_page = _page_args()
    rows, total = _boards().list_catalogue(
        page=page,
        per_page=per_page,
        query=request.args.get("q"),
        status=request.args.get("status"),
        sort=request.args.get("sort", "updated_at"),
        order=request.args.get("order", "desc"),
    )
    return jsonify(paginate([r.to_dict() for r in rows], total, page, per_page)), 200


@bdv_bp.route(f"{ADMIN}/boards/<board_id>", methods=["GET"])
@require_auth
@require_permission("bdv.boards.view")
def get_board(board_id):
    board = _boards().find_by_id(board_id)
    if not board:
        return jsonify({"error": "board not found"}), 404
    payload = board.to_dict(include_children=True)
    payload["validation_errors"] = BoardSpecFactory.validate(board)
    return jsonify(payload), 200


@bdv_bp.route(f"{ADMIN}/boards", methods=["POST"])
@require_auth
@require_permission("bdv.boards.manage")
def create_board():
    data = request.get_json(silent=True) or {}
    if not data.get("name") or not data.get("slug"):
        return jsonify({"error": "name and slug are required"}), 422
    if _boards().find_by_slug(data["slug"]):
        return jsonify({"error": "slug already exists"}), 422

    board = BdvBoard(
        name=data["name"],
        slug=data["slug"],
        description=data.get("description"),
        status=BOARD_STATUS_DRAFT,
    )
    for field in (
        "game_display_name",
        "currency_label",
        "starting_cash",
        "go_salary",
        "jail_fine",
        "jail_penalty_ev",
        "k_price",
        "k_acquire",
        "cap_pct",
        "fee_policy",
        "min_seats",
        "max_seats",
        "default_seats",
        "turn_timeout_seconds",
        "negotiation_window_seconds",
        "max_houses",
    ):
        if field in data:
            setattr(board, field, data[field])
    db.session.add(board)
    db.session.commit()
    return jsonify(board.to_dict()), 201


@bdv_bp.route(f"{ADMIN}/boards/<board_id>", methods=["PUT"])
@require_auth
@require_permission("bdv.boards.manage")
def update_board(board_id):
    board = _boards().find_by_id(board_id)
    if not board:
        return jsonify({"error": "board not found"}), 404
    data = request.get_json(silent=True) or {}
    for field in (
        "name",
        "description",
        "game_display_name",
        "currency_label",
        "starting_cash",
        "go_salary",
        "jail_fine",
        "jail_penalty_ev",
        "k_price",
        "k_acquire",
        "cap_pct",
        "fee_policy",
        "min_seats",
        "max_seats",
        "default_seats",
        "turn_timeout_seconds",
        "negotiation_window_seconds",
        "max_houses",
    ):
        if field in data:
            setattr(board, field, data[field])
    db.session.commit()
    return jsonify(board.to_dict()), 200


@bdv_bp.route(f"{ADMIN}/boards/<board_id>/publish", methods=["POST"])
@require_auth
@require_permission("bdv.boards.manage")
def publish_board(board_id):
    board = _boards().find_by_id(board_id)
    if not board:
        return jsonify({"error": "board not found"}), 404
    errors = BoardSpecFactory.validate(board)
    if errors:
        return jsonify({"error": "board is not valid", "errors": errors}), 422
    board.status = BOARD_STATUS_PUBLISHED
    db.session.commit()
    return jsonify(board.to_dict()), 200


@bdv_bp.route(f"{ADMIN}/boards/<board_id>/unpublish", methods=["POST"])
@require_auth
@require_permission("bdv.boards.manage")
def unpublish_board(board_id):
    board = _boards().find_by_id(board_id)
    if not board:
        return jsonify({"error": "board not found"}), 404
    board.status = BOARD_STATUS_DRAFT
    db.session.commit()
    return jsonify(board.to_dict()), 200


@bdv_bp.route(f"{ADMIN}/boards/<board_id>/squares", methods=["PUT"])
@require_auth
@require_permission("bdv.boards.manage")
def replace_squares(board_id):
    """Whole-tab save from the Streets / Services tabs."""
    board = _boards().find_by_id(board_id)
    if not board:
        return jsonify({"error": "board not found"}), 404
    rows = (request.get_json(silent=True) or {}).get("squares")
    if not isinstance(rows, list):
        return jsonify({"error": "squares must be a list"}), 422
    _boards().replace_squares(board, rows)
    errors = BoardSpecFactory.validate(board)
    db.session.commit()
    return (
        jsonify(
            {
                "squares": [s.to_dict() for s in board.squares],
                "validation_errors": errors,
            }
        ),
        200,
    )


@bdv_bp.route(f"{ADMIN}/boards/bulk/<operation>", methods=["POST"])
@require_auth
@require_permission("bdv.boards.manage")
def bulk_boards(operation):
    if operation not in {"publish", "unpublish", "duplicate", "delete"}:
        return jsonify({"error": "unknown bulk operation"}), 422
    ids = (request.get_json(silent=True) or {}).get("board_ids") or []
    repository = _boards()
    updated, skipped = [], []

    for board_id in ids:
        board = repository.find_by_id(board_id)
        if not board:
            skipped.append({"id": board_id, "reason": "not_found"})
            continue
        if operation == "publish":
            errors = BoardSpecFactory.validate(board)
            if errors:
                skipped.append({"id": board_id, "reason": "invalid_board"})
                continue
            board.status = BOARD_STATUS_PUBLISHED
        elif operation == "unpublish":
            board.status = BOARD_STATUS_DRAFT
        elif operation == "delete":
            db.session.delete(board)
        elif operation == "duplicate":
            _duplicate_board(board, repository)
        updated.append(board_id)

    db.session.commit()
    return jsonify({"updated": updated, "skipped": skipped}), 200


def _duplicate_board(board, repository):
    from .models.board import BdvCard, BdvSquare

    suffix, candidate = 2, f"{board.slug}-copy"
    while repository.find_by_slug(candidate):
        candidate = f"{board.slug}-copy-{suffix}"
        suffix += 1

    clone = BdvBoard(
        name=f"{board.name} (copy)",
        slug=candidate,
        description=board.description,
        status=BOARD_STATUS_DRAFT,
        game_display_name=board.game_display_name,
        currency_label=board.currency_label,
        starting_cash=board.starting_cash,
        go_salary=board.go_salary,
        jail_fine=board.jail_fine,
        jail_penalty_ev=board.jail_penalty_ev,
        k_price=board.k_price,
        k_acquire=board.k_acquire,
        cap_pct=board.cap_pct,
        fee_policy=board.fee_policy,
        min_seats=board.min_seats,
        max_seats=board.max_seats,
        default_seats=board.default_seats,
        turn_timeout_seconds=board.turn_timeout_seconds,
        negotiation_window_seconds=board.negotiation_window_seconds,
        max_houses=board.max_houses,
    )
    db.session.add(clone)
    db.session.flush()
    for square in board.squares:
        db.session.add(
            BdvSquare(
                board_id=clone.id,
                index=square.index,
                kind=square.kind,
                name=square.name,
                stage=square.stage,
                price=square.price,
                rent_table=square.rent_table,
                service_multipliers=square.service_multipliers,
                house_cost=square.house_cost,
                mortgage_value=square.mortgage_value,
                tax_amount=square.tax_amount,
            )
        )
    for card in board.cards:
        db.session.add(
            BdvCard(
                board_id=clone.id,
                deck=card.deck,
                title=card.title,
                flavor_text=card.flavor_text,
                effect=card.effect,
                weight=card.weight,
                is_active=card.is_active,
                sort_order=card.sort_order,
            )
        )
    return clone


# -------------------------------------------------------------- admin: cards


@bdv_bp.route(f"{ADMIN}/effect-ops", methods=["GET"])
@require_auth
@require_permission("bdv.boards.view")
def list_effect_ops():
    """Feeds the schema-driven card-rule builder.

    This exists as an API (rather than static admin-config) precisely so a new
    effect op appears in the admin form with ZERO fe-admin code.
    """
    return jsonify({"items": op_descriptors()}), 200


@bdv_bp.route(f"{ADMIN}/boards/<board_id>/cards", methods=["POST"])
@require_auth
@require_permission("bdv.boards.manage")
def create_card(board_id):
    board = _boards().find_by_id(board_id)
    if not board:
        return jsonify({"error": "board not found"}), 404
    data = request.get_json(silent=True) or {}
    problems = validate_effect(data.get("effect") or {})
    if problems:
        return jsonify({"error": "invalid effect", "errors": problems}), 422
    card = _boards().add_card(board, data)
    db.session.commit()
    return jsonify(card.to_dict()), 201


@bdv_bp.route(f"{ADMIN}/cards/<card_id>", methods=["PUT", "DELETE"])
@require_auth
@require_permission("bdv.boards.manage")
def modify_card(card_id):
    card = _boards().find_card(card_id)
    if not card:
        return jsonify({"error": "card not found"}), 404
    if request.method == "DELETE":
        db.session.delete(card)
        db.session.commit()
        return jsonify({"deleted": True}), 200

    data = request.get_json(silent=True) or {}
    if "effect" in data:
        problems = validate_effect(data["effect"])
        if problems:
            return jsonify({"error": "invalid effect", "errors": problems}), 422
    for field in (
        "deck",
        "title",
        "flavor_text",
        "effect",
        "weight",
        "is_active",
        "sort_order",
    ):
        if field in data:
            setattr(card, field, data[field])
    db.session.commit()
    return jsonify(card.to_dict()), 200


# ------------------------------------------------------------- admin: agents


def _agents() -> AgentProfileRepository:
    return AgentProfileRepository(db.session)


def _slugify(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return cleaned or "agent"


def _unique_slug(candidate: str) -> str:
    repository = _agents()
    base, suffix = _slugify(candidate), 2
    slug = base
    while repository.find_by_slug(slug):
        slug = f"{base}-{suffix}"
        suffix += 1
    return slug


def _connection_slugs(profiles) -> Dict[str, str]:
    """Adapter slug per profile, resolved in ONE query.

    The roster shows which core LLM connection an agent speaks through; looking
    that up per row would be an N+1 on a page that already aggregates.
    """
    from vbwd.models.llm_connection import LlmConnection

    wanted = {p.llm_connection_id for p in profiles if p.llm_connection_id}
    if not wanted:
        return {}
    rows = (
        db.session.query(LlmConnection.id, LlmConnection.slug)
        .filter(LlmConnection.id.in_(wanted))
        .all()
    )
    return {str(row_id): slug for row_id, slug in rows}


def _agent_payload(profiles) -> list:
    """Roster rows: the profile, its adapter, and its lifetime record."""
    stats = _agents().lifetime_stats([p.id for p in profiles])
    slugs = _connection_slugs(profiles)
    items = []
    for profile in profiles:
        row = profile.to_dict()
        row["llm_adapter_slug"] = slugs.get(str(profile.llm_connection_id))
        row.update(
            stats.get(
                str(profile.id),
                {"games_played": 0, "net_capital": 0, "games_won": 0},
            )
        )
        items.append(row)
    return items


#: Lifetime columns are aggregates, not table columns — sorting them in SQL
#: would need a second aggregate in the ORDER BY, so they sort on the page.
_COMPUTED_SORTS = {"games_played", "net_capital", "games_won", "llm_adapter_slug"}


@bdv_bp.route(f"{ADMIN}/agents", methods=["GET"])
@require_auth
@require_permission("bdv.agents.view")
def list_agents():
    page, per_page = _page_args()
    sort = request.args.get("sort", "name")
    order = request.args.get("order", "asc")
    active = request.args.get("is_active")
    rows, total = _agents().list_catalogue(
        page=page,
        per_page=per_page,
        query=request.args.get("q"),
        is_active=None if active in (None, "") else active == "true",
        sort="name" if sort in _COMPUTED_SORTS else sort,
        order=order,
    )
    items = _agent_payload(rows)
    if sort in _COMPUTED_SORTS:
        items.sort(
            key=lambda r: (r.get(sort) is None, r.get(sort)), reverse=order == "desc"
        )
    return jsonify(paginate(items, total, page, per_page)), 200


@bdv_bp.route(f"{ADMIN}/agents", methods=["POST"])
@require_auth
@require_permission("bdv.agents.manage")
def create_agent():
    data = request.get_json(silent=True) or {}
    if not data.get("name"):
        return jsonify({"error": "name is required"}), 422
    if _agents().find_by_name(data["name"]):
        return jsonify({"error": "an agent with that name already exists"}), 422

    profile = BdvAgentProfile(
        name=data["name"],
        slug=_unique_slug(data.get("slug") or data["name"]),
    )
    _apply_agent_fields(profile, data)
    db.session.add(profile)
    db.session.commit()
    return jsonify(profile.to_dict()), 201


@bdv_bp.route(f"{ADMIN}/agents/<agent_id>", methods=["GET"])
@require_auth
@require_permission("bdv.agents.view")
def get_agent(agent_id):
    profile = _agents().find_by_id(agent_id)
    if not profile:
        return jsonify({"error": "agent not found"}), 404
    return jsonify(_agent_payload([profile])[0]), 200


@bdv_bp.route(f"{ADMIN}/agents/<agent_id>", methods=["PUT", "DELETE"])
@require_auth
@require_permission("bdv.agents.manage")
def modify_agent(agent_id):
    profile = _agents().find_by_id(agent_id)
    if not profile:
        return jsonify({"error": "agent not found"}), 404
    if request.method == "DELETE":
        db.session.delete(profile)
        db.session.commit()
        return jsonify({"deleted": True}), 200

    data = request.get_json(silent=True) or {}
    if data.get("slug") and data["slug"] != profile.slug:
        profile.slug = _unique_slug(data["slug"])
    _apply_agent_fields(profile, data)
    db.session.commit()
    return jsonify(profile.to_dict()), 200


def _apply_agent_fields(profile: BdvAgentProfile, data: Dict) -> None:
    for field in (
        "name",
        "persona",
        "system_prompt",
        "temperature",
        "max_tokens_per_match",
        "risk_bias",
        "is_active",
    ):
        if field in data:
            setattr(profile, field, data[field])
    if "llm_connection_id" in data:
        profile.llm_connection_id = data["llm_connection_id"] or None


@bdv_bp.route(f"{ADMIN}/agents/bulk/<operation>", methods=["POST"])
@require_auth
@require_permission("bdv.agents.manage")
def bulk_agents(operation):
    if operation not in {"copy", "delete", "deactivate", "activate", "export"}:
        return jsonify({"error": "unknown bulk operation"}), 422
    ids = (request.get_json(silent=True) or {}).get("agent_ids") or []
    repository = _agents()
    updated, skipped, exported = [], [], []

    for agent_id in ids:
        profile = repository.find_by_id(agent_id)
        if not profile:
            skipped.append({"id": agent_id, "reason": "not_found"})
            continue
        if operation == "delete":
            db.session.delete(profile)
        elif operation == "deactivate":
            profile.is_active = False
        elif operation == "activate":
            profile.is_active = True
        elif operation == "copy":
            _copy_agent(profile)
        elif operation == "export":
            # Export is READ-ONLY and deliberately drops the connection binding:
            # a connection id is meaningless on another installation, and the
            # slug is what a human matches on when importing.
            payload = profile.to_dict()
            payload.pop("llm_connection_id", None)
            payload.pop("id", None)
            exported.append(payload)
        updated.append(agent_id)

    db.session.commit()
    body = {"updated": updated, "skipped": skipped}
    if operation == "export":
        body["export"] = exported
    return jsonify(body), 200


def _copy_agent(profile: BdvAgentProfile) -> BdvAgentProfile:
    base, suffix = f"{profile.name} (copy)", 2
    candidate = base
    while _agents().find_by_name(candidate):
        candidate = f"{profile.name} (copy {suffix})"
        suffix += 1
    clone = BdvAgentProfile(
        name=candidate,
        slug=_unique_slug(profile.slug),
        llm_connection_id=profile.llm_connection_id,
        persona=profile.persona,
        system_prompt=profile.system_prompt,
        temperature=profile.temperature,
        max_tokens_per_match=profile.max_tokens_per_match,
        risk_bias=profile.risk_bias,
        is_active=profile.is_active,
    )
    db.session.add(clone)
    db.session.flush()
    return clone


@bdv_bp.route(f"{ADMIN}/llm-connections", methods=["GET"])
@require_auth
@require_permission("bdv.agents.view")
def list_llm_connections():
    """The core adapters an agent can be bound to.

    Served from here rather than read from admin-config because select options
    in admin-config are static — a connection added in core must appear without
    a redeploy.
    """
    from vbwd.models.llm_connection import LlmConnection

    rows = (
        db.session.query(LlmConnection)
        .filter(LlmConnection.is_active.is_(True))
        .order_by(LlmConnection.slug.asc())
        .all()
    )
    return (
        jsonify(
            {
                "items": [
                    {
                        "id": str(row.id),
                        "slug": row.slug,
                        "name": row.connection_name,
                        "model": row.model,
                    }
                    for row in rows
                ]
            }
        ),
        200,
    )


# ------------------------------------------------------------ admin: matches


@bdv_bp.route(f"{ADMIN}/matches", methods=["GET"])
@require_auth
@require_permission("bdv.matches.view")
def admin_list_matches():
    page, per_page = _page_args()
    rows, total = _matches().list_all(
        page=page, per_page=per_page, status=request.args.get("status")
    )
    return jsonify(paginate([r.to_dict() for r in rows], total, page, per_page)), 200


@bdv_bp.route(f"{ADMIN}/matches/<match_id>", methods=["GET"])
@require_auth
@require_permission("bdv.matches.view")
def admin_match_detail(match_id):
    match = _matches().find_by_id(match_id)
    if not match:
        return jsonify({"error": "match not found"}), 404
    service = _match_service()
    payload = match.to_dict(include_state=True)
    # The audit surface: every priced move with its derivation.
    payload["actions"] = service.events_since(match)
    return jsonify(payload), 200


# ------------------------------------------------------------- player routes


@bdv_bp.route(f"{PLAY}/boards", methods=["GET"])
@require_auth
@require_user_permission("bdv.play")
def playable_boards():
    return jsonify({"items": [b.to_dict() for b in _boards().published()]}), 200


@bdv_bp.route(f"{PLAY}/matches", methods=["GET"])
@require_auth
@require_user_permission("bdv.play")
def my_matches():
    page, per_page = _page_args()
    rows, total = _matches().list_for_user(
        _current_user_id(), page=page, per_page=per_page
    )
    return jsonify(paginate([r.to_dict() for r in rows], total, page, per_page)), 200


@bdv_bp.route(f"{PLAY}/matches", methods=["POST"])
@require_auth
@require_user_permission("bdv.play")
def create_match():
    data = request.get_json(silent=True) or {}
    board = _boards().find_by_slug(data.get("board_slug", "")) or _boards().find_by_id(
        data.get("board_id")
    )
    if not board:
        return jsonify({"error": "board not found"}), 404

    user_id = _current_user_id()
    seats = [
        {
            "kind": SEAT_KIND_HUMAN,
            "user_id": user_id,
            "display_name": data.get("display_name") or "You",
        }
    ]
    for opponent in data.get("opponents") or []:
        kind = opponent.get("kind", SEAT_KIND_BASELINE)
        seats.append(
            {
                "kind": kind
                if kind in {SEAT_KIND_LLM, SEAT_KIND_BASELINE}
                else SEAT_KIND_BASELINE,
                "agent_profile_id": opponent.get("agent_profile_id"),
                "display_name": opponent.get("display_name") or "Agent",
            }
        )
    # Seats the creator did not fill: agents right away, or held OPEN for humans,
    # depending on the chosen policy.
    fill_policy = data.get("fill_policy") or FILL_AGENTS_NOW
    fills_now = fill_policy == FILL_AGENTS_NOW
    while len(seats) < (data.get("seats") or board.default_seats):
        position = len(seats)
        seats.append(
            {"kind": SEAT_KIND_BASELINE, "display_name": f"Agent {position}"}
            if fills_now
            else {"kind": SEAT_KIND_OPEN, "display_name": f"Open seat {position}"}
        )

    service = _match_service()
    try:
        match = service.create(
            board,
            created_by=user_id,
            seats=seats,
            fill_policy=fill_policy,
            wait_minutes=data.get("wait_minutes"),
            slug=(data.get("slug") or "").strip() or None,
        )
        # If the table is already full, the agents take their turns at once.
        service.advance_agents(match)
    except MatchError as rejected:
        return jsonify({"error": str(rejected)}), 422
    db.session.commit()
    return jsonify(match.to_dict(include_state=True)), 201


@bdv_bp.route(f"{PLAY}/agents", methods=["GET"])
@require_auth
@require_user_permission("bdv.play")
def playable_agents():
    """The roster a viewer picks a fight from.

    Deliberately NOT the admin payload: a system prompt is the agent's edge and
    a connection id is infrastructure. What a viewer needs is the name, the
    personality and the record.
    """
    rows, _ = _agents().list_catalogue(per_page=100, is_active=True, sort="name")
    stats = _agents().lifetime_stats([row.id for row in rows])
    return (
        jsonify(
            {
                "items": [
                    {
                        "id": str(row.id),
                        "slug": row.slug,
                        "name": row.name,
                        "persona": row.persona,
                        **stats.get(
                            str(row.id),
                            {"games_played": 0, "net_capital": 0, "games_won": 0},
                        ),
                    }
                    for row in rows
                ],
                "price": plugin_config("agent_match_token_cost", 10),
                "max_agents": plugin_config("agent_match_max_seats", 4),
                "balance": _token_balance(_current_user_id()),
            }
        ),
        200,
    )


def _token_balance(user_id) -> int:
    from vbwd.repositories.token_repository import TokenBalanceRepository

    if user_id is None:
        return 0
    balance = TokenBalanceRepository(db.session).find_by_user_id(user_id)
    return balance.balance if balance else 0


@bdv_bp.route(f"{PLAY}/agent-matches", methods=["POST"])
@require_auth
@require_user_permission("bdv.play")
def create_agent_match():
    """Pay to watch a table of agents play it out.

    The charge happens BEFORE the match is created and in the same transaction:
    a fight that exists without a debit is free, and a debit without a fight is
    theft. Tokens buy the RUN, never in-game credits — those are created at the
    start of a match and destroyed at the end, and remain uncashable.
    """
    from vbwd.models.enums import TokenTransactionType

    data = request.get_json(silent=True) or {}
    board = _boards().find_by_slug(data.get("board_slug", "")) or _boards().find_by_id(
        data.get("board_id")
    )
    if not board:
        return jsonify({"error": "board not found"}), 404
    if board.status != BOARD_STATUS_PUBLISHED:
        return jsonify({"error": "that board is not published"}), 422

    agent_ids = data.get("agent_ids") or []
    maximum = int(plugin_config("agent_match_max_seats", 4))
    if not 2 <= len(agent_ids) <= maximum:
        return jsonify({"error": f"pick between 2 and {maximum} agents"}), 422

    profiles = []
    for agent_id in agent_ids:
        profile = _agents().find_by_id(agent_id)
        if profile is None or not profile.is_active:
            return jsonify({"error": "unknown or inactive agent"}), 422
        profiles.append(profile)

    user_id = _current_user_id()
    price = int(plugin_config("agent_match_token_cost", 10))
    if price > 0:
        try:
            _token_service().debit_tokens(
                user_id,
                price,
                TokenTransactionType.USAGE,
                description="BizDevVibes agent fight",
            )
        except ValueError as refused:
            return (
                jsonify({"error": str(refused), "balance": _token_balance(user_id)}),
                402,
            )

    service = _match_service()
    try:
        match = service.create(
            board,
            created_by=user_id,
            seats=[
                {
                    "kind": SEAT_KIND_LLM,
                    "agent_profile_id": profile.id,
                    "display_name": profile.name,
                }
                for profile in profiles
            ],
            fill_policy=FILL_AGENTS_NOW,
            slug=(data.get("slug") or "").strip() or None,
        )
        service.advance_agents(match)
    except MatchError as rejected:
        # The debit and the match share this transaction, so a rollback here
        # takes the charge with it — nobody pays for a fight that never ran.
        db.session.rollback()
        return jsonify({"error": str(rejected)}), 422

    db.session.commit()
    payload = match.to_dict(include_state=True)
    payload["charged"] = price
    payload["balance"] = _token_balance(user_id)
    return jsonify(payload), 201


def _token_service():
    from vbwd.repositories.token_bundle_purchase_repository import (
        TokenBundlePurchaseRepository,
    )
    from vbwd.repositories.token_repository import (
        TokenBalanceRepository,
        TokenTransactionRepository,
    )
    from vbwd.services.token_service import TokenService

    return TokenService(
        TokenBalanceRepository(db.session),
        TokenTransactionRepository(db.session),
        TokenBundlePurchaseRepository(db.session),
    )


@bdv_bp.route(f"{PLAY}/matches/by-slug/<slug>", methods=["GET"])
@require_auth
@require_user_permission("bdv.play")
def find_by_slug(slug):
    """Find a table by its shareable handle.

    Deliberately readable by ANY authenticated player, not just a seated one —
    that is the point of a slug: you were given it so you could come and join.
    It returns the summary only (no board spec, no state), so it leaks nothing
    about a game in progress.
    """
    from .services import slug as slug_service

    match = _matches().find_by_slug(slug_service.normalise(slug))
    if not match:
        return jsonify({"error": "no game with that slug"}), 404

    service = _match_service()
    service.resolve_lobby(match)
    db.session.commit()

    payload = match.to_dict()
    payload["your_seat"] = _authorised_seat(match)
    payload["can_join"] = (
        match.status == "lobby"
        and payload["open_seats"] > 0
        and payload["your_seat"] is None
    )
    return jsonify(payload), 200


@bdv_bp.route(f"{PLAY}/matches/open", methods=["GET"])
@require_auth
@require_user_permission("bdv.play")
def open_matches():
    """Tables still waiting for players — the join list."""
    page, per_page = _page_args()
    rows, total = _matches().list_open(page=page, per_page=per_page)
    mine = _current_user_id()
    items = [
        row.to_dict()
        for row in rows
        if not any(s.user_id and str(s.user_id) == str(mine) for s in row.seats)
    ]
    return jsonify(paginate(items, total, page, per_page)), 200


@bdv_bp.route(f"{PLAY}/matches/<match_id>/join", methods=["POST"])
@require_auth
@require_user_permission("bdv.play")
def join_match(match_id):
    match = _matches().find_by_id(match_id)
    if not match:
        return jsonify({"error": "match not found"}), 404
    data = request.get_json(silent=True) or {}
    service = _match_service()
    try:
        service.resolve_lobby(match)
        service.join(
            match,
            user_id=_current_user_id(),
            display_name=data.get("display_name") or "Player",
        )
        service.advance_agents(match)
    except MatchError as rejected:
        return jsonify({"error": str(rejected)}), 422
    db.session.commit()
    return jsonify(match.to_dict(include_state=True)), 200


@bdv_bp.route(f"{PLAY}/matches/<match_id>/start-now", methods=["POST"])
@require_auth
@require_user_permission("bdv.play")
def start_now(match_id):
    """Stop waiting: fill the open seats with agents and begin."""
    match = _matches().find_by_id(match_id)
    if not match:
        return jsonify({"error": "match not found"}), 404
    if _authorised_seat(match) is None:
        return jsonify({"error": "not a seat in this match"}), 403
    service = _match_service()
    try:
        service.fill_open_seats_with_agents(match)
        service.start(match)
        service.advance_agents(match)
    except MatchError as rejected:
        return jsonify({"error": str(rejected)}), 422
    db.session.commit()
    return jsonify(match.to_dict(include_state=True)), 200


def _authorised_seat(match):
    seat = _matches().seat_for_user(match, _current_user_id())
    return seat.seat_index if seat else None


def _may_watch(match) -> bool:
    """Seats may act; the person who PAID for an agent fight may watch.

    Without this an agents-only match 403s for its own buyer, because they hold
    no seat in it — which is the entire point of the format.
    """
    if _authorised_seat(match) is not None:
        return True
    user_id = _current_user_id()
    return bool(user_id and match.created_by and str(match.created_by) == str(user_id))


@bdv_bp.route(f"{PLAY}/matches/<match_id>", methods=["GET"])
@require_auth
@require_user_permission("bdv.play")
def match_detail(match_id):
    match = _matches().find_by_id(match_id)
    if not match:
        return jsonify({"error": "match not found"}), 404
    if not _may_watch(match):
        return jsonify({"error": "not a seat in this match"}), 403
    # Reads are where the lazy lobby deadline is applied and where agent turns
    # get played — a browser can never move an agent seat itself.
    service = _match_service()
    service.resolve_lobby(match)
    # The 60-second rent auto-agree is evaluated lazily on read, like the lobby
    # deadline — no scheduler, and it records an explicit action so replay stays
    # exact.
    service.resolve_rent_timeout(match)
    service.resolve_turn_timeout(match)
    service.maybe_open_trading(match)
    service.resolve_trading_window(match)
    service.advance_agents(match)
    db.session.commit()

    payload = match.to_dict(include_state=True)
    payload["spec"] = match.spec_snapshot
    seat_index = _authorised_seat(match)
    payload["your_seat"] = seat_index
    # Whether "Buy this square" should be offered at all — computed server-side
    # so the button can never be shown for a move that would be refused.
    payload["purchase_offer"] = (
        service.purchase_offer(match, seat_index) if seat_index is not None else None
    )
    # Whether the seat is finished, or merely short. The client must never work
    # that out for itself — see purchase_offer.
    payload["settlement"] = (
        service.settlement(match, seat_index) if seat_index is not None else None
    )
    rent_deadline = service.rent_deadline(match)
    payload["rent_deadline_at"] = rent_deadline.isoformat() if rent_deadline else None
    turn_deadline = service.turn_deadline(match)
    payload["turn_deadline_at"] = turn_deadline.isoformat() if turn_deadline else None

    # What each seat still needs to complete a stage — the hint that turns a
    # trade screen from a spreadsheet into a negotiation.
    if service.state_for(match).phase.value == "trading":
        from .core import economy

        state = service.state_for(match)
        spec = service.spec_for(match)
        payload["stage_needs"] = {
            str(seat.index): economy.stage_needs(state, spec, seat.index)
            for seat in state.seats
            if not seat.bankrupt
        }
    return jsonify(payload), 200


@bdv_bp.route(f"{PLAY}/matches/<match_id>/options", methods=["GET"])
@require_auth
@require_user_permission("bdv.play")
def match_options(match_id):
    match = _matches().find_by_id(match_id)
    if not match:
        return jsonify({"error": "match not found"}), 404
    seat_index = _authorised_seat(match)
    if seat_index is None:
        return jsonify({"error": "not a seat in this match"}), 403
    quotes = _match_service().options_for(match, seat_index)
    return (
        jsonify(
            {
                "state_seq": match.state_seq,
                "items": [
                    {
                        "steps": q.steps,
                        "target_index": q.target_index,
                        "target_name": q.target_name,
                        "ev": q.ev,
                        "ev_delta": q.ev_delta,
                        "price": q.price,
                        "affordable": q.affordable,
                        "is_sum": q.is_sum,
                        "reason": q.reason,
                        "reason_params": q.reason_params,
                    }
                    for q in quotes
                ],
            }
        ),
        200,
    )


@bdv_bp.route(f"{PLAY}/matches/<match_id>/actions", methods=["POST"])
@require_auth
@require_user_permission("bdv.play")
def submit_action(match_id):
    match = _matches().find_by_id(match_id)
    if not match:
        return jsonify({"error": "match not found"}), 404
    seat_index = _authorised_seat(match)
    if seat_index is None:
        return jsonify({"error": "not a seat in this match"}), 403

    data = request.get_json(silent=True) or {}
    action_type = data.get("type")
    if action_type not in vars(ActionType).values():
        return jsonify({"error": "unknown action type"}), 422

    # The price is NEVER taken from the client.
    payload = {k: v for k, v in (data.get("payload") or {}).items() if k != "price"}

    service = _match_service()
    try:
        state, events = service.submit(
            match,
            seat_index=seat_index,
            action_type=action_type,
            payload=payload,
            expected_seq=data.get("state_seq"),
        )
    except StaleStateError as stale:
        return (
            jsonify(
                {
                    "error": str(stale),
                    "state_seq": match.state_seq,
                    "state": match.state_snapshot,
                }
            ),
            409,
        )
    except MatchError as rejected:
        return jsonify({"error": str(rejected)}), 422

    # Buying the last free square opens the trading window before anyone moves.
    service.maybe_open_trading(match)
    # The agents answer in the SAME request. Without this the match sits on
    # "waiting for Agent 1" forever: a browser can never move an agent seat,
    # because the player API only lets you act on your own seat.
    service.advance_agents(match)
    db.session.commit()

    state = service.state_for(match)
    return (
        jsonify(
            {
                "state_seq": state.seq,
                "state": state.to_dict(),
                "events": events,
                # Recomputed AFTER the action: the client must never keep showing
                # a buy button for a square it has already moved off, or for one
                # it can no longer afford.
                "purchase_offer": service.purchase_offer(match, seat_index),
                "settlement": service.settlement(match, seat_index),
                # Same reason: a client that keeps a stale deadline shows no
                # countdown on a demand raised by this very action.
                "rent_deadline_at": (
                    service.rent_deadline(match).isoformat()
                    if service.rent_deadline(match)
                    else None
                ),
            }
        ),
        200,
    )


@bdv_bp.route(f"{PLAY}/matches/<match_id>/events", methods=["GET"])
@require_auth
@require_user_permission("bdv.play")
def match_events(match_id):
    match = _matches().find_by_id(match_id)
    if not match:
        return jsonify({"error": "match not found"}), 404
    if not _may_watch(match):
        return jsonify({"error": "not a seat in this match"}), 403
    try:
        since = int(request.args.get("since", -1))
    except (TypeError, ValueError):
        since = -1
    return (
        jsonify(
            {
                "state_seq": match.state_seq,
                "items": _match_service().events_since(match, since),
            }
        ),
        200,
    )


@bdv_bp.route(f"{PLAY}/matches/<match_id>/messages", methods=["GET", "POST"])
@require_auth
@require_user_permission("bdv.play")
def match_messages(match_id):
    """Table chat. Only seats may read or post — this is a private table."""
    from .models.match import BdvMessage

    match = _matches().find_by_id(match_id)
    if not match:
        return jsonify({"error": "match not found"}), 404
    seat_index = _authorised_seat(match)
    if seat_index is None:
        return jsonify({"error": "not a seat in this match"}), 403

    if request.method == "GET":
        rows = (
            db.session.query(BdvMessage)
            .filter(BdvMessage.match_id == match.id)
            .order_by(BdvMessage.created_at.asc())
            .limit(200)
            .all()
        )
        return jsonify({"items": [row.to_dict() for row in rows]}), 200

    body = ((request.get_json(silent=True) or {}).get("body") or "").strip()
    if not body:
        return jsonify({"error": "message is empty"}), 422
    message = BdvMessage(
        match_id=match.id,
        seat_index=seat_index,
        user_id=_current_user_id(),
        body=body[:500],
    )
    db.session.add(message)
    db.session.commit()
    return jsonify(message.to_dict()), 201


@bdv_bp.route(f"{PLAY}/matches/<match_id>/offers", methods=["GET", "POST"])
@require_auth
@require_user_permission("bdv.play")
def match_offers(match_id):
    match = _matches().find_by_id(match_id)
    if not match:
        return jsonify({"error": "match not found"}), 404
    seat_index = _authorised_seat(match)
    if seat_index is None:
        return jsonify({"error": "not a seat in this match"}), 403

    offers = OfferRepository(db.session)
    if request.method == "GET":
        return (
            jsonify({"items": [o.to_dict() for o in offers.open_for_match(match.id)]}),
            200,
        )

    data = request.get_json(silent=True) or {}
    try:
        offer = _match_service().offer_bribe(
            match,
            from_seat=seat_index,
            to_seat=int(data["to_seat"]),
            amount=int(data["amount"]),
        )
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "to_seat and amount are required"}), 422
    except MatchError as rejected:
        return jsonify({"error": str(rejected)}), 422
    db.session.commit()
    return jsonify(offer.to_dict()), 201


@bdv_bp.route(f"{PLAY}/offers/<offer_id>/<decision>", methods=["POST"])
@require_auth
@require_user_permission("bdv.play")
def resolve_offer(offer_id, decision):
    if decision not in {"accept", "decline"}:
        return jsonify({"error": "unknown decision"}), 422
    offers = OfferRepository(db.session)
    offer = offers.find_by_id(offer_id)
    if not offer:
        return jsonify({"error": "offer not found"}), 404
    match = _matches().find_by_id(offer.match_id)
    seat_index = _authorised_seat(match)
    if seat_index is None:
        return jsonify({"error": "not a seat in this match"}), 403

    service = _match_service()
    try:
        if decision == "accept":
            state, events = service.accept_offer(match, offer, seat_index)
            db.session.commit()
            return jsonify({"state_seq": state.seq, "events": events}), 200
        service.decline_offer(match, offer, seat_index)
    except MatchError as rejected:
        return jsonify({"error": str(rejected)}), 422
    db.session.commit()
    return jsonify(offer.to_dict()), 200


@bdv_bp.route(f"{PLAY}/agent-profiles", methods=["GET"])
@require_auth
@require_user_permission("bdv.play")
def list_agent_profiles():
    rows = AgentProfileRepository(db.session).active()
    return (
        jsonify(
            {
                "items": [
                    {"id": str(r.id), "name": r.name, "persona": r.persona}
                    for r in rows
                ]
            }
        ),
        200,
    )
