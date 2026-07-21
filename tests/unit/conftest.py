"""Shared fixtures for the pure-engine tests.

Deliberately builds SMALL boards by hand rather than importing the seeder: an
engine test that depends on seed data is really a seed test, and it breaks
whenever the content team renames a square.
"""
from decimal import Decimal

import pytest

from plugins.bdv.bdv.core.board import BoardSpec, SquareKind, SquareSpec
from plugins.bdv.bdv.core.state import MatchState, SeatState


def deal(index, name, stage, price, rent_table, house_cost=500):
    return SquareSpec(
        index=index,
        kind=SquareKind.DEAL,
        name=name,
        stage=stage,
        price=price,
        rent_table=tuple(rent_table),
        house_cost=house_cost,
        mortgage_value=price // 2,
    )


def service(index, name, price=1500, multipliers=(4, 10)):
    return SquareSpec(
        index=index,
        kind=SquareKind.SERVICE,
        name=name,
        price=price,
        service_multipliers=tuple(multipliers),
    )


@pytest.fixture
def worked_example_spec() -> BoardSpec:
    """A 40-square board positioned exactly like the epic's worked example.

    Seat sits on 34; +2 lands on 36 (completes the Expansion stage with 39),
    +3 on 37 (neutral), +5 on 39 (opponent's, rent 900).
    """
    squares = []
    for index in range(40):
        if index == 0:
            squares.append(SquareSpec(index=0, kind=SquareKind.GO, name="New Quarter"))
        elif index == 10:
            squares.append(
                SquareSpec(index=10, kind=SquareKind.JAIL, name="Compliance Hold")
            )
        elif index == 20:
            squares.append(SquareSpec(index=20, kind=SquareKind.FREE, name="Offsite"))
        elif index == 30:
            squares.append(
                SquareSpec(index=30, kind=SquareKind.GOTO_JAIL, name="Audit Triggered")
            )
        elif index == 36:
            # price 1000 -> k_acquire 0.30 -> 300 base, doubled to 600 when it
            # completes the stage. The epic's ev(2) = +300 uses the UNdoubled
            # bonus because the seat does not yet own 39 in that scenario.
            squares.append(
                deal(36, "Upsell Tier", "expansion", 1000, (100, 300, 900, 1800, 2500))
            )
        elif index == 37:
            squares.append(service(37, "Analytics Stack"))
        elif index == 39:
            squares.append(
                deal(39, "Enterprise Renewal", "expansion", 4000, (500, 900, 1800, 2700, 3500))
            )
        else:
            squares.append(
                SquareSpec(index=index, kind=SquareKind.FREE, name=f"Square {index}")
            )
    return BoardSpec(
        squares=tuple(squares),
        starting_cash=15000,
        go_salary=2000,
        jail_fine=500,
        jail_penalty_ev=1000,
        k_price=Decimal("0.5"),
        k_acquire=Decimal("0.30"),
        cap_pct=Decimal("0.30"),
        fee_policy="all_to_poorest",
    )


@pytest.fixture
def worked_example_state(worked_example_spec) -> MatchState:
    """The epic's scenario, exactly.

    Mover (seat 0) on 34. Seat 1 owns 39 with one house => rent 900.
    The mover already owns 37, which is why landing there is NEUTRAL (ev 0) —
    that is what makes price(3) = 450 rather than a purchase bonus.
    """
    return MatchState(
        seats=(
            SeatState(index=0, cash=10000, position=34),
            SeatState(index=1, cash=8000, position=5),
            SeatState(index=2, cash=3000, position=12),
        ),
        ownership={39: 1, 37: 0},
        houses={39: 1},
        turn_seat=0,
    )


@pytest.fixture
def tiny_spec() -> BoardSpec:
    """A 10-square board — fast to reason about in transition tests."""
    squares = (
        SquareSpec(index=0, kind=SquareKind.GO, name="New Quarter"),
        deal(1, "Cold List", "lead_gen", 600, (20, 100, 300, 900, 1600)),
        SquareSpec(index=2, kind=SquareKind.COMMUNITY, name="Board Memo"),
        deal(3, "Inbound Form", "lead_gen", 600, (40, 200, 600, 1800, 3200)),
        SquareSpec(index=4, kind=SquareKind.TAX, name="Payroll Tax", tax_amount=200),
        service(5, "CRM Platform"),
        deal(6, "Cold Email", "outreach", 1000, (60, 300, 900, 2700, 4000)),
        SquareSpec(index=7, kind=SquareKind.CHANCE, name="Market Event"),
        SquareSpec(index=8, kind=SquareKind.JAIL, name="Compliance Hold"),
        SquareSpec(index=9, kind=SquareKind.GOTO_JAIL, name="Audit Triggered"),
    )
    return BoardSpec(
        squares=squares,
        starting_cash=2000,
        go_salary=200,
        jail_fine=50,
        jail_penalty_ev=100,
        k_price=Decimal("0.5"),
        k_acquire=Decimal("0.30"),
        cap_pct=Decimal("0.30"),
        fee_policy="all_to_poorest",
    )


@pytest.fixture
def tiny_state(tiny_spec) -> MatchState:
    return MatchState(
        seats=(
            SeatState(index=0, cash=2000, position=0),
            SeatState(index=1, cash=2000, position=0),
            SeatState(index=2, cash=2000, position=0),
        ),
        ownership={},
        houses={},
    )
