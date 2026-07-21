"""BizDevVibes HTTP surface.

Admin routes are RBAC-gated; player routes require ``bdv.play``. Two invariants
run through every mutating handler:

* the acting seat comes from the AUTHENTICATED user, never the request body;
* prices are recomputed server-side — a client-supplied price is ignored.
"""
from flask import Blueprint, current_app, g, jsonify, request

from vbwd.extensions import db
from vbwd.middleware.auth import require_auth, require_permission
from vbwd.utils.pagination import paginate

from .core.effects import op_descriptors, validate_effect
from .core.engine import ActionType
from .core.fees import available_fee_policies
from .models.board import BOARD_STATUS_DRAFT, BOARD_STATUS_PUBLISHED, BdvBoard
from .models.match import SEAT_KIND_BASELINE, SEAT_KIND_HUMAN, SEAT_KIND_LLM
from .repositories.board_repository import BoardRepository
from .repositories.match_repository import (
    ActionRepository,
    AgentProfileRepository,
    MatchRepository,
    OfferRepository,
)
from .services.board_spec_factory import BoardSpecFactory
from .services.match_service import MatchError, MatchService, StaleStateError

bdv_bp = Blueprint("bdv", __name__)

ADMIN = "/api/v1/admin/bdv"
PLAY = "/api/v1/bdv"


# ------------------------------------------------------------------ plumbing


def _boards() -> BoardRepository:
    return BoardRepository(db.session)


def _matches() -> MatchRepository:
    return MatchRepository(db.session)


def _match_service() -> MatchService:
    return MatchService(
        db.session,
        MatchRepository(db.session),
        ActionRepository(db.session),
        OfferRepository(db.session),
    )


def _page_args():
    try:
        page = max(1, int(request.args.get("page", 1)))
        per_page = min(100, max(1, int(request.args.get("per_page", 20))))
    except (TypeError, ValueError):
        page, per_page = 1, 20
    return page, per_page


def _current_user_id():
    user = getattr(g, "current_user", None)
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
@require_permission("bdv.play")
def playable_boards():
    return jsonify({"items": [b.to_dict() for b in _boards().published()]}), 200


@bdv_bp.route(f"{PLAY}/matches", methods=["GET"])
@require_auth
@require_permission("bdv.play")
def my_matches():
    page, per_page = _page_args()
    rows, total = _matches().list_for_user(
        _current_user_id(), page=page, per_page=per_page
    )
    return jsonify(paginate([r.to_dict() for r in rows], total, page, per_page)), 200


@bdv_bp.route(f"{PLAY}/matches", methods=["POST"])
@require_auth
@require_permission("bdv.play")
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
    while len(seats) < (data.get("seats") or board.default_seats):
        seats.append(
            {"kind": SEAT_KIND_BASELINE, "display_name": f"Agent {len(seats)}"}
        )

    try:
        match = _match_service().create(board, created_by=user_id, seats=seats)
        _match_service().start(match)
    except MatchError as rejected:
        return jsonify({"error": str(rejected)}), 422
    db.session.commit()
    return jsonify(match.to_dict(include_state=True)), 201


def _authorised_seat(match):
    seat = _matches().seat_for_user(match, _current_user_id())
    return seat.seat_index if seat else None


@bdv_bp.route(f"{PLAY}/matches/<match_id>", methods=["GET"])
@require_auth
@require_permission("bdv.play")
def match_detail(match_id):
    match = _matches().find_by_id(match_id)
    if not match:
        return jsonify({"error": "match not found"}), 404
    if _authorised_seat(match) is None:
        return jsonify({"error": "not a seat in this match"}), 403
    payload = match.to_dict(include_state=True)
    payload["spec"] = match.spec_snapshot
    payload["your_seat"] = _authorised_seat(match)
    return jsonify(payload), 200


@bdv_bp.route(f"{PLAY}/matches/<match_id>/options", methods=["GET"])
@require_auth
@require_permission("bdv.play")
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
@require_permission("bdv.play")
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

    try:
        state, events = _match_service().submit(
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

    db.session.commit()
    return (
        jsonify({"state_seq": state.seq, "state": state.to_dict(), "events": events}),
        200,
    )


@bdv_bp.route(f"{PLAY}/matches/<match_id>/events", methods=["GET"])
@require_auth
@require_permission("bdv.play")
def match_events(match_id):
    match = _matches().find_by_id(match_id)
    if not match:
        return jsonify({"error": "match not found"}), 404
    if _authorised_seat(match) is None:
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


@bdv_bp.route(f"{PLAY}/matches/<match_id>/offers", methods=["GET", "POST"])
@require_auth
@require_permission("bdv.play")
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
@require_permission("bdv.play")
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
        service.decline_offer(offer, seat_index)
    except MatchError as rejected:
        return jsonify({"error": str(rejected)}), 422
    db.session.commit()
    return jsonify(offer.to_dict()), 200


@bdv_bp.route(f"{PLAY}/agent-profiles", methods=["GET"])
@require_auth
@require_permission("bdv.play")
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
