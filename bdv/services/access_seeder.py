"""Grant ``bdv.play`` to the access levels that should be able to play.

Why this exists: the player routes are RBAC-gated (the route-exposure oracle
requires a permission on every mutation), but core's shipped default access
levels are a **core** data file and must not name a plugin's vocabulary. So the
plugin grants its own permission, additively, from its own seeder.

Additive and idempotent: it only ever ADDS ``bdv.play`` to an existing level,
never removes anything and never creates a level. An operator who wants to gate
the game behind a paid tier simply removes it from ``logged-in``.
"""
from typing import List, Tuple

#: Levels that can play out of the box. Anyone logged in — the game is free;
#: the permission exists so an operator CAN gate it, not because it is gated.
DEFAULT_PLAYABLE_LEVELS: Tuple[str, ...] = (
    "logged-in",
    "subscribed-basic",
    "subscribed-pro",
)

PLAY_PERMISSION = "bdv.play"


def grant_play_permission(
    session, levels: Tuple[str, ...] = DEFAULT_PLAYABLE_LEVELS
) -> List[str]:
    """Ensure ``bdv.play`` exists and is attached to the given access levels.

    Returns the slugs actually modified (empty on a re-run — it is idempotent).
    """
    from vbwd.models.role import Permission
    from vbwd.models.user_access_level import AccessLevel

    permission = (
        session.query(Permission).filter(Permission.name == PLAY_PERMISSION).first()
    )
    if permission is None:
        permission = Permission(
            name=PLAY_PERMISSION,
            description="Play BizDevVibes matches",
            resource="bdv",
            action="play",
        )
        session.add(permission)
        session.flush()

    changed: List[str] = []
    for slug in levels:
        level = session.query(AccessLevel).filter(AccessLevel.slug == slug).first()
        if level is None:
            continue
        if any(p.name == PLAY_PERMISSION for p in level.permissions):
            continue
        level.permissions.append(permission)
        changed.append(slug)

    session.flush()
    return changed
