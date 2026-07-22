"""The bot-base consumer: play BizDevVibes by tapping cards in chat.

No new protocol. The bot bridge already has exactly the vocabulary this game
needs (S70): ``BotReply.meta = {"kind": "bot_choices"}`` plus ``BotChoice`` with
``hint`` — a compact secondary label designed for "€29/mo"-shaped annotations,
which is precisely what a PRICE is. Adding a ``bdv_board`` kind would mean
touching bot-base, both adapters and the fe renderer for no user-visible gain,
because the board lives in the 70 % pane, not the chat.

Trust rules, mirroring what bot-base already states about ``action_data``:

* the match id and seq in ``action_data`` are HINTS, never authority — the seat
  is derived from the authenticated identity;
* the price is never in ``action_data`` and is recomputed server-side;
* a stale ``state_seq`` gets a friendly re-post, not a 409 leaking into chat.

Chat and REST share ONE service path (``MatchService``), so the two surfaces
cannot drift.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

NAMESPACE = "bdv"

#: Commands this consumer contributes to /help.
COMMAND_PLAY = "bdv"
COMMAND_ROLL = "roll"
COMMAND_BOARD = "board"

_REASON_TEXT = {
    "unowned": "unowned — you could buy it",
    "completes_stage": "completes your funnel stage",
    "own_square": "yours — nothing happens",
    "pays_rent": "opponent owns it — you pay rent",
    "tax": "a cost square",
    "go": "collect your quarterly budget",
    "goto_jail": "straight to compliance hold",
    "draw_card": "draw a card",
    "jail_visiting": "just visiting",
    "neutral": "neutral ground",
}


def _types():
    """Import the neutral DTOs lazily (optional-bridge, D1).

    The plugin must import and function with bot-base absent — the REST/board
    path plays fine without any chat at all.
    """
    from plugins.bot_base.bot_base.types import BotChoice, BotCommand, BotReply

    return BotChoice, BotCommand, BotReply


def build_commands() -> List[Any]:
    _, BotCommand, _ = _types()
    return [
        BotCommand(
            name=COMMAND_PLAY,
            description="Your BizDevVibes tables",
            namespace=NAMESPACE,
        ),
        BotCommand(
            name=COMMAND_ROLL,
            description="Roll the dice on your turn",
            namespace=NAMESPACE,
        ),
        BotCommand(
            name=COMMAND_BOARD,
            description="Where everyone stands",
            namespace=NAMESPACE,
        ),
    ]


# --------------------------------------------------------------- action_data


def encode_option(match_id: str, state_seq: int, steps: int) -> str:
    """``bdv:opt:<match>:<seq>:<steps>`` — opaque to bot-base and the adapters."""
    return f"{NAMESPACE}:opt:{match_id}:{state_seq}:{steps}"


def encode_simple(action: str, match_id: str, state_seq: int) -> str:
    return f"{NAMESPACE}:{action}:{match_id}:{state_seq}"


def decode(action_data: str) -> Optional[Tuple[str, str, int, Optional[int]]]:
    """Parse ``action_data`` defensively. Returns None for anything malformed.

    Never raises: a bad tap must produce a polite reply, not a traceback on the
    transport.
    """
    if not action_data or len(action_data) > 200:
        return None
    parts = action_data.split(":")
    if len(parts) < 4 or parts[0] != NAMESPACE:
        return None
    action, match_id, raw_seq = parts[1], parts[2], parts[3]
    try:
        state_seq = int(raw_seq)
    except (TypeError, ValueError):
        return None
    steps: Optional[int] = None
    if len(parts) > 4:
        try:
            steps = int(parts[4])
        except (TypeError, ValueError):
            return None
    return action, match_id, state_seq, steps


# ------------------------------------------------------------------- replies


def option_reply(match, quotes, currency_label: str = "cr") -> Any:
    """The priced option set as tappable cards.

    ``text`` is ALWAYS populated as the plain fallback, so a transport that
    ignores ``meta`` still shows a playable message (Liskov).
    """
    BotChoice, _, BotReply = _types()

    lines = []
    choices = []
    for quote in quotes:
        price = "free — fate" if quote.is_sum else f"{quote.price} {currency_label}"
        if not quote.is_sum and not quote.affordable:
            price = f"{quote.price} {currency_label} — over your cap"
        lines.append(f"{quote.steps} → {quote.target_name} ({price})")
        choices.append(
            BotChoice(
                label=f"Move {quote.steps} → {quote.target_name}",
                action_data=encode_option(str(match.id), match.state_seq, quote.steps),
                hint=price,
            )
        )

    fallback = (
        "Your move — the sum is always free.\n"
        + "\n".join(lines)
        + "\nReply with the number."
    )
    return BotReply(
        text=fallback,
        choices=choices,
        meta={"kind": "bot_choices", "text": "Your move — the sum is always free:"},
    )


def roll_prompt(match) -> Any:
    BotChoice, _, BotReply = _types()
    return BotReply(
        text="It is your turn. Roll the dice.",
        choices=[
            BotChoice(
                label="🎲 Roll the dice",
                action_data=encode_simple("roll", str(match.id), match.state_seq),
            )
        ],
        meta={"kind": "bot_choices", "text": "It is your turn."},
    )


def turn_actions_reply(match) -> Any:
    BotChoice, _, BotReply = _types()
    return BotReply(
        text="Resolve your turn: buy this square, or end the turn.",
        choices=[
            BotChoice(
                label="Buy this square",
                action_data=encode_simple("buy", str(match.id), match.state_seq),
            ),
            BotChoice(
                label="End turn",
                action_data=encode_simple("end", str(match.id), match.state_seq),
            ),
        ],
        meta={"kind": "bot_choices", "text": "Resolve your turn:"},
    )


def match_list_reply(matches) -> Any:
    """Your tables, each one tappable."""
    BotChoice, _, BotReply = _types()
    if not matches:
        return BotReply(
            text="You have no BizDevVibes tables yet. Open one in the dashboard."
        )
    choices = [
        BotChoice(
            label=match.slug,
            action_data=encode_simple("open", str(match.id), match.state_seq),
            hint=match.status,
        )
        for match in matches
    ]
    return BotReply(
        text="Your tables:\n" + "\n".join(f"{m.slug} ({m.status})" for m in matches),
        choices=choices,
        meta={"kind": "bot_choices", "text": "Your tables:"},
    )


def board_reply(match, state, spec, seat_index: Optional[int]) -> Any:
    """A plain standings summary — no new meta kind needed."""
    _, _, BotReply = _types()
    lines = [f"{match.slug} — {match.status}"]
    for seat in state.seats:
        marker = " ←you" if seat.index == seat_index else ""
        name = (
            match.seats[seat.index].display_name
            if match.seats
            else f"Seat {seat.index}"
        )
        where = spec.square(seat.position).name
        bust = " (out)" if seat.bankrupt else ""
        lines.append(f"{name}: {seat.cash} at {where}{bust}{marker}")
    return BotReply(text="\n".join(lines))


def text_reply(message: str) -> Any:
    _, _, BotReply = _types()
    return BotReply(text=message)


def describe_reason(reason: str) -> str:
    return _REASON_TEXT.get(reason, reason)


# ------------------------------------------------------------------ dispatch


class BdvBotConsumer:
    """Turns an inbound update into a reply, using the SAME service as REST."""

    def __init__(self, match_repository, match_service, resolve_user_id):
        self._matches = match_repository
        self._service = match_service
        self._resolve_user_id = resolve_user_id

    def handle(self, context) -> Any:
        user_id = self._resolve_user_id(context)
        if user_id is None:
            return text_reply(
                "Link your account first, then your BizDevVibes tables appear here."
            )

        if context.action_data:
            return self._handle_tap(context, user_id)

        command = (context.command or "").lstrip("/")
        if command == COMMAND_PLAY:
            matches, _ = self._matches.list_for_user(user_id, per_page=10)
            return match_list_reply(matches)
        if command in (COMMAND_ROLL, COMMAND_BOARD):
            match = self._current_match(user_id)
            if match is None:
                return text_reply("You have no active table.")
            return (
                self._prompt_for(match, user_id)
                if command == COMMAND_ROLL
                else self._board(match, user_id)
            )
        return text_reply("Try /bdv to see your tables.")

    # ------------------------------------------------------------- internals

    def _current_match(self, user_id):
        matches, _ = self._matches.list_for_user(user_id, per_page=10)
        for match in matches:
            if match.status == "active":
                return match
        return None

    def _seat_of(self, match, user_id) -> Optional[int]:
        seat = self._matches.seat_for_user(match, user_id)
        return seat.seat_index if seat else None

    def _board(self, match, user_id):
        return board_reply(
            match,
            self._service.state_for(match),
            self._service.spec_for(match),
            self._seat_of(match, user_id),
        )

    def _prompt_for(self, match, user_id):
        """What this seat can do right now."""
        seat_index = self._seat_of(match, user_id)
        if seat_index is None:
            return text_reply("You do not hold a seat at that table.")
        state = self._service.state_for(match)
        if state.turn_seat != seat_index:
            name = match.seats[state.turn_seat].display_name
            return text_reply(f"Not your turn — waiting for {name}.")
        if state.phase.value == "await_roll":
            return roll_prompt(match)
        if state.phase.value in ("negotiate", "await_choice"):
            quotes = self._service.options_for(match, seat_index)
            return option_reply(match, quotes, self._currency(match))
        return turn_actions_reply(match)

    @staticmethod
    def _currency(match) -> str:
        try:
            return match.spec_snapshot["board"]["currency_label"]
        except Exception:
            return "cr"

    def _handle_tap(self, context, user_id):
        parsed = decode(context.action_data)
        if parsed is None:
            return text_reply("That button is no longer valid.")
        action, match_id, state_seq, steps = parsed

        match = self._matches.find_by_id(match_id)
        if match is None:
            return text_reply("That table no longer exists.")

        # The seat comes from the AUTHENTICATED identity, never the payload.
        seat_index = self._seat_of(match, user_id)
        if seat_index is None:
            return text_reply("You do not hold a seat at that table.")

        if action == "open":
            return self._prompt_for(match, user_id)

        # A stale tap is normal (someone moved first) — re-post, do not error.
        if state_seq != match.state_seq:
            return self._prompt_for(match, user_id)

        action_type = {
            "roll": "roll",
            "buy": "buy_property",
            "end": "end_turn",
            "opt": "choose_option",
        }.get(action)
        if action_type is None:
            return text_reply("That button is no longer valid.")

        payload: Dict[str, Any] = {}
        if action_type == "choose_option":
            if steps is None:
                return text_reply("That button is no longer valid.")
            # NOTE: no price is taken from the tap; the service recomputes it.
            payload = {"steps": steps}

        try:
            self._service.submit(
                match,
                seat_index=seat_index,
                action_type=action_type,
                payload=payload,
                expected_seq=state_seq,
            )
            # Agents answer immediately, exactly as over REST.
            self._service.advance_agents(match)
        except Exception as rejected:  # MatchError / StaleStateError
            return text_reply(f"That move was refused: {rejected}")

        return self._prompt_for(match, user_id)
