"""Match persistence.

``BdvAction`` is append-only and is the SOURCE OF TRUTH: current state is the
fold of ``engine.apply()`` over it. ``BdvMatch.state_snapshot`` is a rebuildable
cache for reads, which is why replay, audit and the balance harness come for
free rather than as three retrofitted features.
"""
from vbwd.extensions import db
from vbwd.models.base import BaseModel

MATCH_STATUS_LOBBY = "lobby"
MATCH_STATUS_ACTIVE = "active"
MATCH_STATUS_FINISHED = "finished"
MATCH_STATUS_ABANDONED = "abandoned"

SEAT_KIND_HUMAN = "human"
SEAT_KIND_LLM = "llm"
SEAT_KIND_BASELINE = "baseline"
#: A seat held open for a human who has not joined yet.
SEAT_KIND_OPEN = "open"

#: How the remaining seats get filled when a match is created.
FILL_AGENTS_NOW = "agents_now"  # start immediately against agents
FILL_WAIT_FOREVER = "wait_forever"  # hold seats open indefinitely
FILL_WAIT_THEN_AGENTS = "wait_then_agents"  # hold open until a deadline, then agents
FILL_POLICIES = (FILL_AGENTS_NOW, FILL_WAIT_FOREVER, FILL_WAIT_THEN_AGENTS)

OFFER_STATUS_OPEN = "open"
OFFER_STATUS_ACCEPTED = "accepted"
OFFER_STATUS_DECLINED = "declined"
OFFER_STATUS_EXPIRED = "expired"


class BdvMatch(BaseModel):
    __tablename__ = "bdv_match"

    board_id = db.Column(
        db.UUID(as_uuid=True),
        db.ForeignKey("bdv_board.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    status = db.Column(
        db.String(20), nullable=False, server_default=MATCH_STATUS_LOBBY, index=True
    )
    # Human-shareable handle for the table: how a player finds a game without a
    # UUID. Unique so it can be typed into "find a game".
    slug = db.Column(db.String(60), nullable=False, unique=True, index=True)
    seed = db.Column(db.String(64), nullable=False)
    # The rules the match is played under, frozen at start: a published board may
    # later be duplicated or cosmetically edited, and a match must always replay
    # against its own rules.
    spec_snapshot = db.Column(db.JSON, nullable=False)
    spec_hash = db.Column(db.String(64), nullable=False, index=True)
    state_snapshot = db.Column(db.JSON, nullable=True)
    state_seq = db.Column(db.Integer, nullable=False, server_default="0")
    chance_deck_size = db.Column(db.Integer, nullable=False, server_default="0")
    community_deck_size = db.Column(db.Integer, nullable=False, server_default="0")
    created_by = db.Column(
        db.UUID(as_uuid=True),
        db.ForeignKey("vbwd_user.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    chat_room_id = db.Column(db.UUID(as_uuid=True), nullable=True)
    winner_seat_index = db.Column(db.Integer, nullable=True)
    turn_deadline_at = db.Column(db.DateTime(timezone=True), nullable=True)
    # How open seats get filled, and (for wait_then_agents) when the wait ends.
    fill_policy = db.Column(
        db.String(30), nullable=False, server_default=FILL_AGENTS_NOW
    )
    lobby_deadline_at = db.Column(db.DateTime(timezone=True), nullable=True)
    finished_at = db.Column(db.DateTime(timezone=True), nullable=True)

    seats = db.relationship(
        "BdvSeat",
        backref="match",
        lazy="selectin",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="BdvSeat.seat_index",
    )

    def to_dict(self, include_state: bool = False) -> dict:
        payload = {
            "id": str(self.id),
            "slug": self.slug,
            "board_id": str(self.board_id),
            "status": self.status,
            "state_seq": self.state_seq,
            "spec_hash": self.spec_hash,
            "winner_seat_index": self.winner_seat_index,
            "fill_policy": self.fill_policy,
            "lobby_deadline_at": self.lobby_deadline_at.isoformat()
            if self.lobby_deadline_at
            else None,
            "open_seats": sum(
                1 for seat in (self.seats or []) if seat.kind == SEAT_KIND_OPEN
            ),
            "chat_room_id": str(self.chat_room_id) if self.chat_room_id else None,
            "seats": [seat.to_dict() for seat in self.seats],
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }
        if include_state:
            payload["state"] = self.state_snapshot
        return payload


class BdvSeat(BaseModel):
    __tablename__ = "bdv_seat"
    __table_args__ = (
        db.UniqueConstraint("match_id", "seat_index", name="uq_bdv_seat_match_index"),
        # A seat is EITHER a human or an agent — never both, never neither.
        # A seat is EITHER a human, OR an agent, OR unoccupied — the last case
        # covering a baseline agent (no profile row) and a seat still held open
        # for a human who has not joined.
        db.CheckConstraint(
            "(user_id IS NULL) <> (agent_profile_id IS NULL)"
            " OR (user_id IS NULL AND agent_profile_id IS NULL"
            "     AND kind IN ('baseline', 'open'))",
            name="ck_bdv_seat_one_occupant",
        ),
    )

    match_id = db.Column(
        db.UUID(as_uuid=True),
        db.ForeignKey("bdv_match.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    seat_index = db.Column(db.Integer, nullable=False)
    kind = db.Column(db.String(20), nullable=False, server_default=SEAT_KIND_HUMAN)
    user_id = db.Column(
        db.UUID(as_uuid=True),
        db.ForeignKey("vbwd_user.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    agent_profile_id = db.Column(
        db.UUID(as_uuid=True),
        db.ForeignKey("bdv_agent_profile.id", ondelete="SET NULL"),
        nullable=True,
    )
    display_name = db.Column(db.String(80), nullable=False)

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "seat_index": self.seat_index,
            "kind": self.kind,
            "user_id": str(self.user_id) if self.user_id else None,
            "agent_profile_id": str(self.agent_profile_id)
            if self.agent_profile_id
            else None,
            "display_name": self.display_name,
        }


class BdvAction(BaseModel):
    """Append-only. Never updated, never deleted — it IS the match."""

    __tablename__ = "bdv_action"
    __table_args__ = (
        db.UniqueConstraint("match_id", "seq", name="uq_bdv_action_match_seq"),
    )

    match_id = db.Column(
        db.UUID(as_uuid=True),
        db.ForeignKey("bdv_match.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    seq = db.Column(db.Integer, nullable=False)
    seat_index = db.Column(db.Integer, nullable=False)
    type = db.Column(db.String(40), nullable=False)
    payload = db.Column(db.JSON, nullable=True)
    events = db.Column(db.JSON, nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "seq": self.seq,
            "seat_index": self.seat_index,
            "type": self.type,
            "payload": self.payload or {},
            "events": self.events or [],
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class BdvOffer(BaseModel):
    """A bribe-to-fate offer. Escrowed at creation, refunded on decline/expiry."""

    __tablename__ = "bdv_offer"

    match_id = db.Column(
        db.UUID(as_uuid=True),
        db.ForeignKey("bdv_match.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    turn_seq = db.Column(db.Integer, nullable=False)
    kind = db.Column(db.String(30), nullable=False, server_default="bribe_to_fate")
    from_seat = db.Column(db.Integer, nullable=False)
    to_seat = db.Column(db.Integer, nullable=False)
    amount = db.Column(db.Integer, nullable=False)
    status = db.Column(
        db.String(20), nullable=False, server_default=OFFER_STATUS_OPEN, index=True
    )
    expires_at = db.Column(db.DateTime(timezone=True), nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "match_id": str(self.match_id),
            "turn_seq": self.turn_seq,
            "kind": self.kind,
            "from_seat": self.from_seat,
            "to_seat": self.to_seat,
            "amount": self.amount,
            "status": self.status,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }


class BdvAgentProfile(BaseModel):
    """An LLM opponent. Data, not code — a new personality is a DB row."""

    __tablename__ = "bdv_agent_profile"

    name = db.Column(db.String(80), nullable=False, unique=True)
    llm_connection_id = db.Column(db.UUID(as_uuid=True), nullable=True)
    persona = db.Column(db.String(200), nullable=True)
    system_prompt = db.Column(db.Text, nullable=True)
    temperature = db.Column(db.Numeric(4, 2), nullable=False, server_default="0.70")
    max_tokens_per_match = db.Column(db.Integer, nullable=False, server_default="60000")
    risk_bias = db.Column(db.Numeric(4, 2), nullable=False, server_default="0.50")
    is_active = db.Column(db.Boolean, nullable=False, server_default="true")

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "name": self.name,
            "llm_connection_id": str(self.llm_connection_id)
            if self.llm_connection_id
            else None,
            "persona": self.persona,
            "system_prompt": self.system_prompt,
            "temperature": str(self.temperature),
            "max_tokens_per_match": self.max_tokens_per_match,
            "risk_bias": str(self.risk_bias),
            "is_active": self.is_active,
        }
