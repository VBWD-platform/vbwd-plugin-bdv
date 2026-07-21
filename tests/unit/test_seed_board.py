"""The seeded funnel-40 board: valid, playable, and trademark-clean.

These run WITHOUT the database — the seed payload is plain data, so it can be
compiled into a pure spec and actually played. A board that seeds but cannot be
played is the failure mode this catches.
"""
from decimal import Decimal

import pytest

from plugins.bdv.bdv.agents.baseline import play_match
from plugins.bdv.bdv.core.board import BoardSpec, SquareKind, SquareSpec
from plugins.bdv.bdv.core.effects import (
    describe_effect,
    effect_ev_hint,
    validate_effect,
)
from plugins.bdv.bdv.core.engine import MatchConfig
from plugins.bdv.bdv.services.seed_board import (
    seed_board_payload,
    seed_cards,
    seed_squares,
)

#: Names owned by Hasbro's board game. None may appear in our content.
DENYLIST = {
    "monopoly",
    "boardwalk",
    "park place",
    "mediterranean",
    "baltic",
    "marvin gardens",
    "st. charles",
    "illinois avenue",
    "reading railroad",
    "b&o railroad",
    "short line",
    "pennsylvania railroad",
    "electric company",
    "water works",
    "community chest",
    "free parking",
    "go to jail",
    "chance",
}


@pytest.fixture
def seeded_spec() -> BoardSpec:
    payload = seed_board_payload()
    squares = tuple(
        SquareSpec(
            index=row["index"],
            kind=SquareKind(row["kind"]),
            name=row["name"],
            stage=row.get("stage"),
            price=row.get("price", 0),
            rent_table=tuple(row.get("rent_table", ())),
            service_multipliers=tuple(row.get("service_multipliers", ())),
            house_cost=row.get("house_cost", 0),
            mortgage_value=row.get("mortgage_value", 0),
            tax_amount=row.get("tax_amount", 0),
        )
        for row in seed_squares()
    )
    return BoardSpec(
        squares=squares,
        starting_cash=payload["starting_cash"],
        go_salary=payload["go_salary"],
        jail_fine=payload["jail_fine"],
        jail_penalty_ev=payload["jail_penalty_ev"],
        k_price=Decimal(payload["k_price"]),
        k_acquire=Decimal(payload["k_acquire"]),
        cap_pct=Decimal(payload["cap_pct"]),
        fee_policy=payload["fee_policy"],
        max_houses=payload["max_houses"],
    )


class TestLayout:
    def test_exactly_forty_squares(self):
        assert len(seed_squares()) == 40

    def test_indices_are_contiguous(self):
        assert [s["index"] for s in seed_squares()] == list(range(40))

    def test_counts_add_up(self):
        kinds = [s["kind"] for s in seed_squares()]
        assert kinds.count("deal") == 22
        assert kinds.count("service") == 6
        assert kinds.count("tax") == 2
        assert kinds.count("chance") == 3
        assert kinds.count("community") == 3
        assert kinds.count("go") == 1
        assert kinds.count("jail") == 1
        assert kinds.count("free") == 1
        assert kinds.count("goto_jail") == 1

    def test_eight_funnel_stages(self):
        stages = {s["stage"] for s in seed_squares() if s["kind"] == "deal"}
        assert stages == {
            "lead_gen",
            "outreach",
            "qualification",
            "pitch",
            "negotiation",
            "contract",
            "delivery",
            "expansion",
        }

    def test_stage_sizes_are_2_3_3_3_3_3_3_2(self):
        from collections import Counter

        counts = Counter(s["stage"] for s in seed_squares() if s["kind"] == "deal")
        assert sorted(counts.values()) == [2, 2, 3, 3, 3, 3, 3, 3]

    def test_corners_are_at_the_corners(self):
        by_index = {s["index"]: s["kind"] for s in seed_squares()}
        assert by_index[0] == "go"
        assert by_index[10] == "jail"
        assert by_index[20] == "free"
        assert by_index[30] == "goto_jail"


class TestTheWorkedExampleLivesOnThisBoard:
    """The epic's example must land on real squares, or the docs lie."""

    def test_positions(self):
        by_index = {s["index"]: s for s in seed_squares()}
        assert by_index[34]["name"] == "Go-Live"
        assert by_index[36]["name"] == "Upsell Tier"
        assert by_index[37]["name"] == "Analytics Stack"
        assert by_index[39]["name"] == "Enterprise Renewal"

    def test_36_and_39_are_the_same_stage(self):
        by_index = {s["index"]: s for s in seed_squares()}
        assert by_index[36]["stage"] == by_index[39]["stage"] == "expansion"

    def test_enterprise_renewal_one_house_rent_is_900(self):
        by_index = {s["index"]: s for s in seed_squares()}
        assert by_index[39]["rent_table"][1] == 900


class TestValidity:
    def test_the_seeded_board_compiles_to_a_valid_spec(self, seeded_spec):
        assert seeded_spec.validate() == []

    def test_spec_hash_is_stable(self, seeded_spec):
        assert seeded_spec.spec_hash() == seeded_spec.spec_hash()

    def test_every_deal_has_a_rent_table_and_stage(self, seeded_spec):
        for square in seeded_spec.squares:
            if square.kind == SquareKind.DEAL:
                assert square.rent_table and square.stage

    def test_rents_rise_along_the_funnel(self):
        """Lead Gen cheap, Expansion brutal — the shape the theme promises."""
        by_index = {s["index"]: s for s in seed_squares()}
        assert by_index[1]["rent_table"][0] < by_index[36]["rent_table"][0]
        assert by_index[36]["price"] > by_index[1]["price"]


class TestCards:
    def test_every_card_effect_validates(self):
        for card in seed_cards():
            assert validate_effect(card["effect"]) == [], card["title"]

    def test_both_decks_are_populated(self):
        decks = {c["deck"] for c in seed_cards()}
        assert decks == {"chance", "community"}
        assert len([c for c in seed_cards() if c["deck"] == "chance"]) == 6
        assert len([c for c in seed_cards() if c["deck"] == "community"]) == 6

    def test_every_card_generates_a_description(self, seeded_spec):
        for card in seed_cards():
            described = describe_effect(card["effect"], seeded_spec)
            assert described, card["title"]
            for entry in described:
                assert entry.key.startswith("bdv.effect.")

    def test_every_card_produces_an_ev_hint(self, seeded_spec):
        for card in seed_cards():
            assert isinstance(effect_ev_hint(card["effect"], seeded_spec), int)

    def test_flavor_text_never_restates_the_mechanics(self):
        """Flavour is prose; mechanics are generated. If flavour started quoting
        amounts it would drift from the ops the moment anyone edits them."""
        for card in seed_cards():
            assert not any(ch.isdigit() for ch in card["flavor_text"]), card["title"]


class TestTrademarkClean:
    def test_no_hasbro_owned_names_in_squares(self):
        for square in seed_squares():
            assert square["name"].lower() not in DENYLIST, square["name"]

    def test_no_hasbro_owned_names_in_cards(self):
        for card in seed_cards():
            blob = f"{card['title']} {card['flavor_text']}".lower()
            for banned in DENYLIST:
                assert banned not in blob, f"{card['title']} contains {banned!r}"

    def test_board_display_name_is_ours(self):
        assert seed_board_payload()["game_display_name"] == "BizDevVibes"
        assert seed_board_payload()["slug"] == "funnel-40"


class TestPlayability:
    def test_a_full_match_completes_on_the_seeded_board(self, seeded_spec):
        """The strongest seed test: the board is not just valid, it is playable."""
        state, actions = play_match(
            seeded_spec, MatchConfig(seed="seed-check", seat_count=3), max_actions=6000
        )
        assert len(actions) > 50

    @pytest.mark.parametrize("seat_count", [2, 3, 4, 5, 6])
    def test_playable_at_every_supported_seat_count(self, seeded_spec, seat_count):
        state, actions = play_match(
            seeded_spec,
            MatchConfig(seed=f"seats-{seat_count}", seat_count=seat_count),
            max_actions=6000,
        )
        assert len(state.seats) == seat_count
        assert actions
