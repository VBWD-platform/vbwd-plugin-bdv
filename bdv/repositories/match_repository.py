"""Match, action, offer and agent-profile data access."""
from typing import List, Optional, Tuple

from sqlalchemy import func

from vbwd.repositories.base import BaseRepository

from ..models.match import (
    BdvAction,
    BdvAgentProfile,
    BdvMatch,
    BdvOffer,
    BdvSeat,
    OFFER_STATUS_OPEN,
)


class MatchRepository(BaseRepository[BdvMatch]):
    def __init__(self, session):
        super().__init__(session, BdvMatch)

    def list_for_user(
        self, user_id, *, page: int = 1, per_page: int = 20
    ) -> Tuple[List[BdvMatch], int]:
        statement = (
            self._session.query(BdvMatch)
            .join(BdvSeat, BdvSeat.match_id == BdvMatch.id)
            .filter(BdvSeat.user_id == user_id)
            .distinct()
        )
        total = statement.with_entities(func.count(func.distinct(BdvMatch.id))).scalar() or 0
        rows = (
            statement.order_by(BdvMatch.created_at.desc())
            .limit(per_page)
            .offset((page - 1) * per_page)
            .all()
        )
        return rows, total

    def list_all(
        self, *, page: int = 1, per_page: int = 20, status: Optional[str] = None
    ) -> Tuple[List[BdvMatch], int]:
        statement = self._session.query(BdvMatch)
        if status:
            statement = statement.filter(BdvMatch.status == status)
        total = statement.with_entities(func.count(BdvMatch.id)).scalar() or 0
        rows = (
            statement.order_by(BdvMatch.created_at.desc())
            .limit(per_page)
            .offset((page - 1) * per_page)
            .all()
        )
        return rows, total

    def seat_for_user(self, match: BdvMatch, user_id) -> Optional[BdvSeat]:
        for seat in match.seats or []:
            if seat.user_id and str(seat.user_id) == str(user_id):
                return seat
        return None

    def add_seat(self, match: BdvMatch, **kwargs) -> BdvSeat:
        seat = BdvSeat(match_id=match.id, **kwargs)
        self._session.add(seat)
        self._session.flush()
        return seat


class ActionRepository(BaseRepository[BdvAction]):
    """Append-only. There is deliberately no update or delete here."""

    def __init__(self, session):
        super().__init__(session, BdvAction)

    def log(self, match_id, seq: int, seat_index: int, type_: str, payload, events) -> BdvAction:
        row = BdvAction(
            match_id=match_id,
            seq=seq,
            seat_index=seat_index,
            type=type_,
            payload=payload or {},
            events=list(events or []),
        )
        self._session.add(row)
        self._session.flush()
        return row

    def for_match(self, match_id, since: int = -1) -> List[BdvAction]:
        return (
            self._session.query(BdvAction)
            .filter(BdvAction.match_id == match_id, BdvAction.seq > since)
            .order_by(BdvAction.seq.asc())
            .all()
        )

    def next_seq(self, match_id) -> int:
        highest = (
            self._session.query(func.max(BdvAction.seq))
            .filter(BdvAction.match_id == match_id)
            .scalar()
        )
        return 0 if highest is None else highest + 1


class OfferRepository(BaseRepository[BdvOffer]):
    def __init__(self, session):
        super().__init__(session, BdvOffer)

    def open_for_match(self, match_id) -> List[BdvOffer]:
        return (
            self._session.query(BdvOffer)
            .filter(BdvOffer.match_id == match_id, BdvOffer.status == OFFER_STATUS_OPEN)
            .order_by(BdvOffer.created_at.asc())
            .all()
        )

    def create(self, **kwargs) -> BdvOffer:
        offer = BdvOffer(**kwargs)
        self._session.add(offer)
        self._session.flush()
        return offer


class AgentProfileRepository(BaseRepository[BdvAgentProfile]):
    def __init__(self, session):
        super().__init__(session, BdvAgentProfile)

    def active(self) -> List[BdvAgentProfile]:
        return (
            self._session.query(BdvAgentProfile)
            .filter(BdvAgentProfile.is_active.is_(True))
            .order_by(BdvAgentProfile.name.asc())
            .all()
        )

    def find_by_name(self, name: str) -> Optional[BdvAgentProfile]:
        return (
            self._session.query(BdvAgentProfile)
            .filter(BdvAgentProfile.name == name)
            .first()
        )
