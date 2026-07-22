"""The three house agents.

CREATE-ONLY and idempotent, by slug: a re-run must never overwrite a persona an
admin has tuned. That is the rule for any seeder that could touch production.

The three are deliberately different **strategically**, not just in tone. A
roster of one archetype in three costumes would make the fight page pointless —
every table would play the same. Each one's risk bias, prompt and temperature
pull in a different direction on the game's central tension: the sum is free,
and buying a better move funds your opponents.
"""
from typing import List, Tuple

#: Personalities the plugin ships with. ``slug`` is the natural key, so editing
#: a name later never orphans a match, a stat row, or an export file.
HOUSE_AGENTS = [
    {
        "slug": "hard-closer",
        "name": "Vera the Closer",
        "persona": "Buys her way forward and dares you to keep up",
        "risk_bias": "0.85",
        "temperature": "0.90",
        "system_prompt": (
            "You play to win outright, not to survive. Buying a die is worth it "
            "whenever it lands you on something you can own, even at a steep "
            "price — tempo compounds and second place pays nothing.\n"
            "You accept that your fees fund your opponents. Treat that as the "
            "cost of moving first, not a reason to hesitate.\n"
            "When you are ahead, spend to stay ahead. When you are behind, "
            "spend harder."
        ),
    },
    {
        "slug": "slow-nurture",
        "name": "Miles the Nurturer",
        "persona": "Takes the free sum and lets the board come to him",
        "risk_bias": "0.20",
        "temperature": "0.40",
        "system_prompt": (
            "You are patient and you are cheap. The sum is free, and free "
            "compounds: every fee you decline to pay is a fee your opponents "
            "never receive.\n"
            "Buy a die only when the square it lands on completes a stage you "
            "already hold, or denies one to somebody who is one square away.\n"
            "You would rather hold cash into the late game than own a scattered "
            "board. Liquidity is a position."
        ),
    },
    {
        "slug": "deal-hawk",
        "name": "Ada the Deal Hawk",
        "persona": "Plays the other players, not the dice",
        "risk_bias": "0.55",
        "temperature": "0.75",
        "system_prompt": (
            "You treat every roll as a negotiation. What matters is not the "
            "square you land on but who else wants it, and what they will give "
            "up to get it.\n"
            "Spend to block before you spend to gain: a stage your rival cannot "
            "complete is worth more than a square you own alone.\n"
            "Read the cash positions before you read the board. The seat that "
            "cannot afford to answer you is the seat to pressure."
        ),
    },
]


def seed_house_agents(session) -> Tuple[List, int]:
    """Create any missing house agent. Returns (all three rows, created count).

    Matches by SLUG, never by name, so renaming an agent in the admin does not
    resurrect it on the next seed run.
    """
    from ..models.match import BdvAgentProfile

    rows, created = [], 0
    for spec in HOUSE_AGENTS:
        existing = (
            session.query(BdvAgentProfile)
            .filter(BdvAgentProfile.slug == spec["slug"])
            .first()
        )
        if existing is not None:
            rows.append(existing)
            continue
        profile = BdvAgentProfile(
            slug=spec["slug"],
            name=spec["name"],
            persona=spec["persona"],
            system_prompt=spec["system_prompt"],
            temperature=spec["temperature"],
            risk_bias=spec["risk_bias"],
            # Unbound on purpose: no installation's connection slugs are known
            # here, and an unbound agent plays the deterministic baseline until
            # an admin binds it. Shipping a broken binding would be worse.
            llm_connection_id=None,
        )
        session.add(profile)
        session.flush()
        rows.append(profile)
        created += 1
    return rows, created
