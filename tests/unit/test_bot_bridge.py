"""The chat bridge: play by tapping cards, with action_data untrusted.

These tests use fakes for the repository/service — the point is the message
shapes and the trust rules, not the persistence, which the integration suite
already covers.
"""
import types

import pytest

from plugins.bdv.bdv.bot import consumer as bot


class FakeSeat:
    def __init__(self, index, name="Agent"):
        self.seat_index = index
        self.display_name = name


class FakeMatch:
    def __init__(self, match_id="m1", seq=7, status="active", slug="amber-orbit-42"):
        self.id = match_id
        self.state_seq = seq
        self.status = status
        self.slug = slug
        self.seats = [FakeSeat(0, "You"), FakeSeat(1, "Agent 1")]
        self.spec_snapshot = {"board": {"currency_label": "cr"}}


class FakeQuote:
    def __init__(self, steps, name, price, is_sum=False, affordable=True):
        self.steps = steps
        self.target_name = name
        self.price = price
        self.is_sum = is_sum
        self.affordable = affordable
        self.reason = "unowned"


QUOTES = [
    FakeQuote(2, "Upsell Tier", 600),
    FakeQuote(3, "Analytics Stack", 450),
    FakeQuote(5, "Enterprise Renewal", 0, is_sum=True),
]


class TestActionDataEncoding:
    def test_round_trips(self):
        encoded = bot.encode_option("m1", 7, 2)
        assert encoded == "bdv:opt:m1:7:2"
        assert bot.decode(encoded) == ("opt", "m1", 7, 2)

    def test_simple_actions_round_trip(self):
        assert bot.decode(bot.encode_simple("roll", "m1", 3)) == ("roll", "m1", 3, None)

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            None,
            "tarot:opt:m1:7:2",  # foreign namespace
            "bdv:opt:m1",  # too few parts
            "bdv:opt:m1:notanint:2",  # unparsable seq
            "bdv:opt:m1:7:xyz",  # unparsable steps
            "bdv:" + "x" * 400,  # oversized
        ],
    )
    def test_malformed_payloads_return_none_rather_than_raising(self, bad):
        assert bot.decode(bad) is None

    def test_the_price_is_never_encoded(self):
        """A tap must not be able to assert what a move costs."""
        assert "600" not in bot.encode_option("m1", 7, 2)


class TestReplyShapes:
    def test_option_reply_uses_the_existing_bot_choices_kind(self):
        reply = bot.option_reply(FakeMatch(), QUOTES)
        assert reply.meta["kind"] == "bot_choices", "no new protocol kind"
        assert len(reply.choices) == 3

    def test_the_price_rides_in_the_hint(self):
        reply = bot.option_reply(FakeMatch(), QUOTES)
        hints = [c.hint for c in reply.choices]
        assert "600 cr" in hints
        assert any("free" in h for h in hints)

    def test_text_is_always_a_playable_fallback(self):
        """A transport that ignores meta must still show something usable."""
        reply = bot.option_reply(FakeMatch(), QUOTES)
        assert "Upsell Tier" in reply.text
        assert "Enterprise Renewal" in reply.text
        assert "free" in reply.text

    def test_unaffordable_options_are_shown_with_their_price(self):
        quotes = [FakeQuote(2, "Upsell Tier", 600, affordable=False), QUOTES[2]]
        reply = bot.option_reply(FakeMatch(), quotes)
        assert any("over your cap" in (c.hint or "") for c in reply.choices)
        assert "600" in reply.text, "seeing what you cannot afford IS the mechanic"

    def test_every_choice_carries_namespaced_action_data(self):
        reply = bot.option_reply(FakeMatch(), QUOTES)
        for choice in reply.choices:
            assert choice.action_data.startswith("bdv:")

    def test_match_list_is_tappable(self):
        reply = bot.match_list_reply(
            [FakeMatch(), FakeMatch("m2", 1, "lobby", "quiet-ledger-11")]
        )
        assert len(reply.choices) == 2
        assert reply.meta["kind"] == "bot_choices"

    def test_empty_match_list_is_plain_text(self):
        reply = bot.match_list_reply([])
        assert not reply.choices


class FakeService:
    def __init__(self, phase="await_choice", turn_seat=0):
        self.phase = phase
        self.turn_seat = turn_seat
        self.submitted = []
        self.advanced = 0

    def state_for(self, match):
        return types.SimpleNamespace(
            phase=types.SimpleNamespace(value=self.phase),
            turn_seat=self.turn_seat,
            seats=[],
        )

    def spec_for(self, match):
        return types.SimpleNamespace(square=lambda i: types.SimpleNamespace(name="X"))

    def options_for(self, match, seat_index):
        return QUOTES

    def submit(self, match, **kwargs):
        self.submitted.append(kwargs)

    def advance_agents(self, match):
        self.advanced += 1


class FakeRepo:
    def __init__(self, match=None, seat_index=0):
        self.match = match or FakeMatch()
        self.seat_index = seat_index

    def find_by_id(self, match_id):
        return self.match if str(self.match.id) == str(match_id) else None

    def seat_for_user(self, match, user_id):
        return FakeSeat(self.seat_index) if self.seat_index is not None else None

    def list_for_user(self, user_id, per_page=10):
        return [self.match], 1


def inbound(action_data=None, command=None, user_id="u1"):
    identity = types.SimpleNamespace(vbwd_user_id=user_id) if user_id else None
    return types.SimpleNamespace(
        action_data=action_data, command=command, text=None, identity=identity
    )


def consumer_with(repo=None, service=None):
    return bot.BdvBotConsumer(
        repo or FakeRepo(),
        service or FakeService(),
        lambda ctx: getattr(getattr(ctx, "identity", None), "vbwd_user_id", None),
    )


class TestTrustRules:
    def test_an_unlinked_sender_cannot_act(self):
        reply = consumer_with().handle(inbound(command="/bdv", user_id=None))
        assert "Link your account" in reply.text

    def test_a_tap_from_a_non_seat_is_refused(self):
        repo = FakeRepo()
        repo.seat_index = None
        reply = consumer_with(repo).handle(inbound(bot.encode_option("m1", 7, 2)))
        assert "do not hold a seat" in reply.text

    def test_a_tap_for_an_unknown_match_is_refused(self):
        reply = consumer_with().handle(inbound(bot.encode_option("nope", 7, 2)))
        assert "no longer exists" in reply.text

    def test_a_forged_payload_never_reaches_the_service(self):
        service = FakeService()
        consumer_with(FakeRepo(), service).handle(inbound("bdv:opt:m1:zz:2"))
        assert service.submitted == []

    def test_the_seat_comes_from_identity_not_the_payload(self):
        """Seat 1's tap must act on seat 1, whatever the payload claims."""
        service = FakeService(turn_seat=1)
        consumer_with(FakeRepo(seat_index=1), service).handle(
            inbound(bot.encode_option("m1", 7, 5))
        )
        assert service.submitted[0]["seat_index"] == 1

    def test_no_price_is_forwarded_to_the_service(self):
        service = FakeService()
        consumer_with(FakeRepo(), service).handle(
            inbound(bot.encode_option("m1", 7, 2))
        )
        assert service.submitted[0]["payload"] == {"steps": 2}


class TestTapDispatch:
    def test_a_valid_tap_submits_and_lets_the_agents_reply(self):
        service = FakeService()
        consumer_with(FakeRepo(), service).handle(
            inbound(bot.encode_option("m1", 7, 2))
        )
        assert service.submitted[0]["action_type"] == "choose_option"
        assert service.advanced == 1, "agents answer in the same turn, as over REST"

    def test_a_stale_tap_reposts_instead_of_erroring(self):
        service = FakeService()
        # match.state_seq is 7; the tap claims 3
        reply = consumer_with(FakeRepo(), service).handle(
            inbound(bot.encode_option("m1", 3, 2))
        )
        assert service.submitted == [], "nothing submitted for a stale seq"
        assert reply.meta["kind"] == "bot_choices", "the current options are re-posted"

    def test_roll_and_end_turn_map_to_engine_actions(self):
        for encoded, expected in (
            (bot.encode_simple("roll", "m1", 7), "roll"),
            (bot.encode_simple("end", "m1", 7), "end_turn"),
            (bot.encode_simple("buy", "m1", 7), "buy_property"),
        ):
            service = FakeService()
            consumer_with(FakeRepo(), service).handle(inbound(encoded))
            assert service.submitted[0]["action_type"] == expected

    def test_a_refused_move_answers_politely(self):
        class Refusing(FakeService):
            def submit(self, match, **kwargs):
                raise RuntimeError("price exceeds the affordability cap")

        reply = consumer_with(FakeRepo(), Refusing()).handle(
            inbound(bot.encode_option("m1", 7, 2))
        )
        assert "refused" in reply.text


class TestCommands:
    def test_bdv_lists_your_tables(self):
        reply = consumer_with().handle(inbound(command="/bdv"))
        assert "amber-orbit-42" in reply.text

    def test_roll_when_it_is_not_your_turn(self):
        reply = consumer_with(FakeRepo(), FakeService(turn_seat=1)).handle(
            inbound(command="/roll")
        )
        assert "Not your turn" in reply.text

    def test_roll_offers_the_dice_in_the_roll_phase(self):
        reply = consumer_with(FakeRepo(), FakeService(phase="await_roll")).handle(
            inbound(command="/roll")
        )
        assert reply.choices[0].action_data.startswith("bdv:roll:")

    def test_an_unknown_command_points_at_bdv(self):
        reply = consumer_with().handle(inbound(command="/nonsense"))
        assert "/bdv" in reply.text


class TestCommandCatalogue:
    def test_commands_are_namespaced(self):
        for command in bot.build_commands():
            assert command.namespace == "bdv"

    def test_the_three_commands_are_present(self):
        assert {c.name for c in bot.build_commands()} == {"bdv", "roll", "board"}
