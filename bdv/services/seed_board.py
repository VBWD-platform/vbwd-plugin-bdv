"""The seeded ``funnel-40`` board — a bizdev deal pipeline, not a renamed street map.

Colour groups are FUNNEL STAGES, so completing a set means owning a whole stage
of the pipeline. Rents rise along the funnel: Cold List is cheap, Enterprise
Renewal is brutal — which is how bizdev value actually concentrates, and it
makes the endgame dice market sharp.

Every name here is original. No Hasbro-owned names appear anywhere, and
``tests`` asserts that against a denylist.
"""
from __future__ import annotations

from typing import Dict, List

STAGE_LEAD_GEN = "lead_gen"
STAGE_OUTREACH = "outreach"
STAGE_QUALIFICATION = "qualification"
STAGE_PITCH = "pitch"
STAGE_NEGOTIATION = "negotiation"
STAGE_CONTRACT = "contract"
STAGE_DELIVERY = "delivery"
STAGE_EXPANSION = "expansion"

SEED_BOARD_SLUG = "funnel-40"


def _deal(index, name, stage, price, rents, house_cost):
    return {
        "index": index,
        "kind": "deal",
        "name": name,
        "stage": stage,
        "price": price,
        "rent_table": rents,
        "house_cost": house_cost,
        "mortgage_value": price // 2,
    }


def _service(index, name, price, multipliers):
    return {
        "index": index,
        "kind": "service",
        "name": name,
        "price": price,
        "service_multipliers": multipliers,
    }


def _plain(index, kind, name, tax_amount=0):
    return {"index": index, "kind": kind, "name": name, "tax_amount": tax_amount}


def seed_squares() -> List[Dict]:
    """40 squares: 22 deals in 8 stages, 6 services, 2 costs, 4 corners, 6 draws."""
    return [
        _plain(0, "go", "New Quarter"),
        _deal(1, "Cold List", STAGE_LEAD_GEN, 600, [20, 100, 300, 900, 1600, 2500], 500),
        _plain(2, "community", "Board Memo"),
        _deal(3, "Inbound Form", STAGE_LEAD_GEN, 600, [40, 200, 600, 1800, 3200, 4500], 500),
        _plain(4, "tax", "Payroll Tax", tax_amount=2000),
        _service(5, "CRM Platform", 2000, [250, 500, 1000, 2000]),
        _deal(6, "Cold Email", STAGE_OUTREACH, 1000, [60, 300, 900, 2700, 4000, 5500], 500),
        _plain(7, "chance", "Market Event"),
        _deal(8, "LinkedIn DM", STAGE_OUTREACH, 1000, [60, 300, 900, 2700, 4000, 5500], 500),
        _deal(9, "Conference Booth", STAGE_OUTREACH, 1200, [80, 400, 1000, 3000, 4500, 6000], 500),
        _plain(10, "jail", "Compliance Hold"),
        _deal(11, "Discovery Call", STAGE_QUALIFICATION, 1400, [100, 500, 1500, 4500, 6250, 7500], 1000),
        _service(12, "Data Room", 1500, [40, 100]),
        _deal(13, "Needs Analysis", STAGE_QUALIFICATION, 1400, [100, 500, 1500, 4500, 6250, 7500], 1000),
        _deal(14, "Budget Check", STAGE_QUALIFICATION, 1600, [120, 600, 1800, 5000, 7000, 9000], 1000),
        _service(15, "Ad Network", 2000, [250, 500, 1000, 2000]),
        _deal(16, "Demo Day", STAGE_PITCH, 1800, [140, 700, 2000, 5500, 7500, 9500], 1000),
        _plain(17, "community", "Board Memo"),
        _deal(18, "Pilot Proposal", STAGE_PITCH, 1800, [140, 700, 2000, 5500, 7500, 9500], 1000),
        _deal(19, "Reference Call", STAGE_PITCH, 2000, [160, 800, 2200, 6000, 8000, 10000], 1000),
        _plain(20, "free", "Offsite"),
        _deal(21, "Term Sheet", STAGE_NEGOTIATION, 2200, [180, 900, 2500, 7000, 8750, 10500], 1500),
        _plain(22, "chance", "Market Event"),
        _deal(23, "Redlines", STAGE_NEGOTIATION, 2200, [180, 900, 2500, 7000, 8750, 10500], 1500),
        _deal(24, "Procurement Review", STAGE_NEGOTIATION, 2400, [200, 1000, 3000, 7500, 9250, 11000], 1500),
        _service(25, "Partner Channel", 2000, [250, 500, 1000, 2000]),
        _deal(26, "Signed MSA", STAGE_CONTRACT, 2600, [220, 1100, 3300, 8000, 9750, 11500], 1500),
        _deal(27, "Statement of Work", STAGE_CONTRACT, 2600, [220, 1100, 3300, 8000, 9750, 11500], 1500),
        _plain(28, "community", "Board Memo"),
        _deal(29, "Purchase Order", STAGE_CONTRACT, 2800, [240, 1200, 3600, 8500, 10250, 12000], 1500),
        _plain(30, "goto_jail", "Audit Triggered"),
        _deal(31, "Onboarding", STAGE_DELIVERY, 3000, [260, 1300, 3900, 9000, 11000, 12750], 2000),
        _deal(32, "Integration", STAGE_DELIVERY, 3000, [260, 1300, 3900, 9000, 11000, 12750], 2000),
        _plain(33, "chance", "Market Event"),
        _deal(34, "Go-Live", STAGE_DELIVERY, 3200, [280, 1500, 4500, 10000, 12000, 14000], 2000),
        _service(35, "Events Agency", 2000, [250, 500, 1000, 2000]),
        _deal(36, "Upsell Tier", STAGE_EXPANSION, 3500, [350, 1750, 5000, 11000, 13000, 15000], 2000),
        _service(37, "Analytics Stack", 1500, [40, 100]),
        _plain(38, "tax", "Burn Rate", tax_amount=1000),
        _deal(39, "Enterprise Renewal", STAGE_EXPANSION, 4000, [500, 900, 1800, 2700, 3500, 5000], 2000),
    ]


def seed_cards() -> List[Dict]:
    """Market Event (external shock) and Board Memo (internal mandate) decks.

    Every card is a declarative effect. The human-readable description is
    generated from the ops — nothing here restates the mechanics in prose.
    """
    return [
        # ---- Market Event: things the outside world does to you
        {
            "deck": "chance",
            "title": "Category heats up",
            "flavor_text": "An analyst report puts your category on every roadmap.",
            "effect": {"ops": [{"op": "collect_bank", "params": {"amount": 1500}}]},
            "sort_order": 0,
        },
        {
            "deck": "chance",
            "title": "Competitor undercuts you",
            "flavor_text": "A rival ships the same feature at half the price.",
            "effect": {"ops": [{"op": "pay_bank", "params": {"amount": 1000}}]},
            "sort_order": 1,
        },
        {
            "deck": "chance",
            "title": "Regulator opens a review",
            "flavor_text": "Someone filed a complaint. Legal takes the quarter.",
            "effect": {"ops": [{"op": "go_to_jail"}]},
            "sort_order": 2,
        },
        {
            "deck": "chance",
            "title": "Inbound surge",
            "flavor_text": "A viral post fills the top of the funnel.",
            "effect": {
                "ops": [{"op": "move_to_square", "params": {"index": 0}}]
            },
            "sort_order": 3,
        },
        {
            "deck": "chance",
            "title": "Channel partner delivers",
            "flavor_text": "Your partner routes a qualified deal straight to you.",
            "effect": {
                "ops": [
                    {"op": "advance_to_nearest_kind", "params": {"kind": "service"}}
                ]
            },
            "sort_order": 4,
        },
        {
            "deck": "chance",
            "title": "Procurement freeze",
            "flavor_text": "Budgets lock until next quarter. Nothing moves.",
            "effect": {"ops": [{"op": "skip_next_turn"}]},
            "sort_order": 5,
        },
        # ---- Board Memo: things your own org does to you
        {
            "deck": "community",
            "title": "Bonus pool released",
            "flavor_text": "The board signs off on the quarterly bonus.",
            "effect": {"ops": [{"op": "collect_bank", "params": {"amount": 1000}}]},
            "sort_order": 0,
        },
        {
            "deck": "community",
            "title": "Everyone owes you a referral",
            "flavor_text": "You closed the flagship logo. The team pays up.",
            "effect": {
                "ops": [{"op": "collect_from_each_player", "params": {"amount": 500}}]
            },
            "sort_order": 1,
        },
        {
            "deck": "community",
            "title": "Team offsite on you",
            "flavor_text": "You promised dinner if the quarter landed. It landed.",
            "effect": {"ops": [{"op": "pay_each_player", "params": {"amount": 300}}]},
            "sort_order": 2,
        },
        {
            "deck": "community",
            "title": "Infrastructure true-up",
            "flavor_text": "Finance reconciles what your accounts actually cost to serve.",
            "effect": {
                "ops": [
                    {
                        "op": "pay_per_building",
                        "params": {"per_house": 250, "per_hotel": 1000},
                    }
                ]
            },
            "sort_order": 3,
        },
        {
            "deck": "community",
            "title": "Compliance waiver",
            "flavor_text": "Legal pre-clears you. Keep it for when you need it.",
            "effect": {"ops": [{"op": "get_out_of_jail_card"}]},
            "sort_order": 4,
        },
        {
            "deck": "community",
            "title": "Reorg",
            "flavor_text": "Your pod moves under a new VP. Momentum resets.",
            "effect": {"ops": [{"op": "move_relative", "params": {"steps": -3}}]},
            "sort_order": 5,
        },
    ]


def seed_board_payload() -> Dict:
    return {
        "name": "Funnel 40",
        "slug": SEED_BOARD_SLUG,
        "description": (
            "The canonical BizDevVibes board: a 40-square deal pipeline where each "
            "colour group is a funnel stage, so completing a set means owning a "
            "whole stage of the pipeline."
        ),
        "status": "published",
        "game_display_name": "BizDevVibes",
        "currency_label": "cr",
        "starting_cash": 15000,
        "go_salary": 2000,
        "jail_fine": 500,
        "jail_penalty_ev": 1000,
        "k_price": "0.5000",
        "k_acquire": "0.3000",
        "cap_pct": "0.3000",
        "fee_policy": "all_to_poorest",
        "min_seats": 2,
        "max_seats": 6,
        "default_seats": 3,
        "turn_timeout_seconds": 120,
        "negotiation_window_seconds": 30,
        "max_houses": 5,
    }
