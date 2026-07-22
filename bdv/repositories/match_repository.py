"""Match, action, offer and agent-profile data access."""
from typing import List, Optional, Tuple

from sqlalchemy import case, func, or_

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

    def find_by_slug(self, slug: str) -> Optional[BdvMatch]:
        return self._session.query(BdvMatch).filter(BdvMatch.slug == slug).first()

    def slug_taken(self, slug: str) -> bool:
        return self.find_by_slug(slug) is not None

    def list_for_user(
        self, user_id, *, page: int = 1, per_page: int = 20
    ) -> Tuple[List[BdvMatch], int]:
        """Matches the user holds a seat in.

        The id set is resolved FIRST, then the rows are fetched. A ``DISTINCT``
        over the whole entity puts ``spec_snapshot`` / ``state_snapshot`` in the
        SELECT list, and Postgres has no equality operator for ``json`` — so the
        obvious ``query(BdvMatch).join(...).distinct()`` raises UndefinedFunction
        the moment a single match exists.
        """
        seated = (
            self._session.query(BdvSeat.match_id)
            .filter(BdvSeat.user_id == user_id)
            .distinct()
        )
        total = (
            self._session.query(func.count(BdvMatch.id))
            .filter(BdvMatch.id.in_(seated))
            .scalar()
            or 0
        )
        rows = (
            self._session.query(BdvMatch)
            .filter(BdvMatch.id.in_(seated))
            .order_by(BdvMatch.created_at.desc())
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

    def list_open(
        self, *, page: int = 1, per_page: int = 20
    ) -> Tuple[List[BdvMatch], int]:
        """Matches still in the lobby with at least one seat open."""
        waiting = (
            self._session.query(BdvSeat.match_id)
            .filter(BdvSeat.kind == "open")
            .distinct()
        )
        base = self._session.query(BdvMatch).filter(
            BdvMatch.status == "lobby", BdvMatch.id.in_(waiting)
        )
        total = (
            self._session.query(func.count(BdvMatch.id))
            .filter(BdvMatch.status == "lobby", BdvMatch.id.in_(waiting))
            .scalar()
            or 0
        )
        rows = (
            base.order_by(BdvMatch.created_at.desc())
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

    def log(
        self,
        match_id,
        seq: int,
        seat_index: int,
        type_: str,
        payload,
        events,
        reasoning: Optional[str] = None,
    ) -> BdvAction:
        row = BdvAction(
            match_id=match_id,
            seq=seq,
            seat_index=seat_index,
            type=type_,
            payload=payload or {},
            events=list(events or []),
            reasoning=reasoning,
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

    def find_by_slug(self, slug: str) -> Optional[BdvAgentProfile]:
        return (
            self._session.query(BdvAgentProfile)
            .filter(BdvAgentProfile.slug == slug)
            .first()
        )

    def lifetime_stats(self, profile_ids: List) -> dict:
        """Games played and net capital per profile, in one query.

        Net capital is the sum of what the agent WALKED AWAY WITH, so a career
        of defeats totals zero without needing to be clamped: a bankrupt seat's
        recorded result is zero by definition.
        """
        if not profile_ids:
            return {}
        rows = (
            self._session.query(
                BdvSeat.agent_profile_id,
                func.count(BdvSeat.id),
                func.coalesce(func.sum(BdvSeat.final_cash), 0),
                func.coalesce(
                    func.sum(case((BdvSeat.is_winner.is_(True), 1), else_=0)), 0
                ),
            )
            .filter(BdvSeat.agent_profile_id.in_(profile_ids))
            .group_by(BdvSeat.agent_profile_id)
            .all()
        )
        return {
            str(profile_id): {
                "games_played": int(played),
                "net_capital": int(capital),
                "games_won": int(won),
            }
            for profile_id, played, capital, won in rows
        }

    def list_catalogue(
        self,
        *,
        page: int = 1,
        per_page: int = 20,
        query: Optional[str] = None,
        is_active: Optional[bool] = None,
        sort: str = "name",
        order: str = "asc",
    ):
        """The admin roster. Sorting on the lifetime columns happens in the
        route, where the statistics are already joined on — sorting them here
        would mean a second aggregate in the ORDER BY."""
        queryset = self._session.query(BdvAgentProfile)
        if query:
            like = f"%{query.strip()}%"
            queryset = queryset.filter(
                or_(
                    BdvAgentProfile.name.ilike(like),
                    BdvAgentProfile.slug.ilike(like),
                    BdvAgentProfile.persona.ilike(like),
                )
            )
        if is_active is not None:
            queryset = queryset.filter(BdvAgentProfile.is_active.is_(is_active))

        column = {
            "name": BdvAgentProfile.name,
            "slug": BdvAgentProfile.slug,
            "birthday": BdvAgentProfile.created_at,
            "is_active": BdvAgentProfile.is_active,
        }.get(sort, BdvAgentProfile.name)
        queryset = queryset.order_by(column.desc() if order == "desc" else column.asc())

        total = queryset.count()
        rows = queryset.limit(per_page).offset((page - 1) * per_page).all()
        return rows, total
