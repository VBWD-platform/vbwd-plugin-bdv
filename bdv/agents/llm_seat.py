"""An LLM-driven seat.

Substitutable for :class:`BaselineSeat` — the turn loop calls ``next_action``
and never learns which kind of seat answered (Liskov). Everything hard is
already done by core ``LlmClient``: provider selection, the JSON repair loop,
key scrubbing. This module supplies a prompt, a schema, and a validator.

Three rules shape it:

* **Fairness.** The prompt is built ONLY from the seat's own view — the same
  projection a human gets. Deck order and other seats' reasoning are excluded at
  construction, so an agent physically cannot see what a human cannot. That is
  also the GDPR line: the view carries display names and public holdings, never
  another user's personal data.
* **The engine's own numbers are handed over.** Prices and EV deltas are
  deterministic and auditable, so making the model re-derive them would only add
  error.
* **A model failure can never stall a table.** Every path ends in a legal move:
  repair, then the baseline agent, then the free sum — which is just the "fate
  default" the rules already have.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from ..core.board import BoardSpec
from ..core.engine import Action, ActionType
from ..core.pricing import OptionQuote, evaluate_options
from ..core.state import ConstraintKind, MatchState, Phase
from .baseline import BaselineSeat

#: The move contract, in the LIGHTWEIGHT ``{field: type}`` form core expects.
#:
#: This was originally written as a full JSON Schema, which core's adapter does
#: not take: it iterates the TOP-LEVEL KEYS of whatever it is handed and turns
#: each into a tool field. A real schema therefore asked the model to fill in
#: fields literally named ``type``, ``properties`` and ``required`` — and it
#: obligingly did, returning a schema instead of a move. Every answer was then
#: rejected as illegal, the repair retries burned, and the seat degraded to the
#: baseline on its first real decision. The legal-move constraints live in
#: ``_validate``, which is the only place that can enforce them anyway, since
#: they depend on the board state rather than on the shape.
MOVE_SCHEMA: Dict[str, Any] = {
    "action": "string",
    "steps": "integer",
    "reasoning": "string",
}

#: Named so the failure shows up in the action log rather than nowhere.
LEGAL_ACTIONS = ("choose_option", "buy_property", "decline_purchase", "end_turn")

SYSTEM_PROMPT = (
    "You are playing BizDevVibes, a dice-market board game.\n"
    "A roll of two dice gives exactly three legal moves: the first die, the "
    "second die, or their sum. THE SUM IS ALWAYS FREE. Choosing a single die is "
    "a purchase, priced by the engine, and the fee is paid to your OPPONENTS "
    "rather than the bank — so buying a better move funds your rivals.\n"
    "You may never spend more than the affordability cap shown.\n"
    "Prices and value estimates are computed by the engine and given to you; "
    "trust them rather than recomputing.\n"
    "Answer with these fields: action (one of choose_option, buy_property, "
    "decline_purchase, end_turn), steps (only for choose_option — it MUST be "
    "one of the steps values listed in options), and reasoning (one short "
    "sentence).\n"
    "Reply with JSON only."
)


class LlmSeatError(RuntimeError):
    """Raised only internally; callers always get a legal move instead."""


@dataclass
class LlmDecision:
    """What the seat decided, and how it got there — persisted for audit."""

    action: Action
    reasoning: str = ""
    degraded_from: Optional[str] = None
    attempts: int = 0
    #: Why the model was abandoned, in words. Without this a degraded seat is
    #: indistinguishable from a seat that simply never spoke — which is exactly
    #: how a broken schema hid for a whole match.
    failure: str = ""


class LlmSeat:
    """A seat played by a language model, with a hard fallback ladder."""

    seat_kind = "llm"

    def __init__(
        self,
        *,
        client: Any,
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
        max_repair_retries: int = 2,
        token_budget: Optional[int] = None,
        fallback: Optional[BaselineSeat] = None,
        tokens_spent: int = 0,
    ) -> None:
        self._client = client
        self._system_prompt = system_prompt or SYSTEM_PROMPT
        self._temperature = temperature
        self._max_repair_retries = max(0, int(max_repair_retries))
        self._token_budget = token_budget
        self._fallback = fallback or BaselineSeat()
        self.tokens_spent = tokens_spent
        #: Set once the seat has degraded — it stays degraded for the match.
        self.degraded = False

    # ---------------------------------------------------------------- prompt

    def seat_view(
        self, state: MatchState, spec: BoardSpec, seat_index: int
    ) -> Dict[str, Any]:
        """The seat-visible projection. Nothing hidden leaks in here."""
        me = state.seat(seat_index)
        quotes = self._quotes(state, spec, seat_index)
        return {
            "you": {
                "seat": seat_index,
                "cash": me.cash,
                "position": me.position,
                "position_name": spec.square(me.position).name,
                "in_jail": me.in_jail,
                "owns": [spec.square(i).name for i in state.owned_by(seat_index)],
            },
            "opponents": [
                {
                    "seat": s.index,
                    "cash": s.cash,
                    "position_name": spec.square(s.position).name,
                    "bankrupt": s.bankrupt,
                    "owns": len(state.owned_by(s.index)),
                }
                for s in state.seats
                if s.index != seat_index
            ],
            "roll": list(state.pending_roll) if state.pending_roll else None,
            "must_take_the_free_sum": state.has_constraint(
                ConstraintKind.FORCED_SUM, seat_index
            ),
            "options": [
                {
                    "steps": q.steps,
                    "lands_on": q.target_name,
                    "price": q.price,
                    "free": q.is_sum,
                    "value_delta_vs_sum": q.ev_delta,
                    "affordable": q.affordable,
                    "why": q.reason,
                }
                for q in quotes
            ],
            "fee_goes_to": "your opponents, never the bank",
            "phase": state.phase.value,
        }

    @staticmethod
    def _quotes(
        state: MatchState, spec: BoardSpec, seat_index: int
    ) -> List[OptionQuote]:
        if state.pending_roll is None:
            return []
        return list(evaluate_options(state, spec, state.pending_roll, seat_index))

    # -------------------------------------------------------------- decision

    def next_action(
        self, state: MatchState, spec: BoardSpec, seat_index: int
    ) -> Action:
        """Same signature as ``BaselineSeat`` — the turn loop cannot tell them apart."""
        return self.decide(state, spec, seat_index).action

    def decide(
        self, state: MatchState, spec: BoardSpec, seat_index: int
    ) -> LlmDecision:
        """The full ladder: model → repair → baseline → the free sum."""
        # Phases with exactly one sensible action never burn a token.
        if state.phase in (Phase.AWAIT_ROLL, Phase.NEGOTIATE):
            return LlmDecision(
                action=self._fallback.next_action(state, spec, seat_index),
                reasoning="mechanical phase",
            )

        if self.degraded or self._budget_exhausted():
            return LlmDecision(
                action=self._fallback.next_action(state, spec, seat_index),
                degraded_from="budget"
                if self._budget_exhausted()
                else "earlier_failure",
            )

        repair: Optional[str] = None
        last_failure = ""
        for attempt in range(self._max_repair_retries + 1):
            try:
                raw = self._ask(state, spec, seat_index, repair)
            except Exception as provider_error:  # transport, timeout, bad JSON
                last_failure = f"{type(provider_error).__name__}: {provider_error}"
                break
            try:
                action = self._validate(raw, state, spec, seat_index)
            except LlmSeatError as invalid:
                last_failure = str(invalid)
                repair = (
                    f"Your previous answer was rejected: {invalid}. "
                    "Answer again with a legal move."
                )
                continue
            return LlmDecision(
                action=action,
                reasoning=str(raw.get("reasoning", ""))[:2000],
                attempts=attempt + 1,
            )

        # Every attempt failed — degrade for the rest of the match, and SAY WHY.
        self.degraded = True
        return LlmDecision(
            action=self._fallback.next_action(state, spec, seat_index),
            reasoning=f"degraded to baseline — {last_failure}"[:2000],
            degraded_from="illegal_or_error",
            attempts=self._max_repair_retries + 1,
            failure=last_failure,
        )

    # --------------------------------------------------------------- helpers

    def _budget_exhausted(self) -> bool:
        return (
            self._token_budget is not None and self.tokens_spent >= self._token_budget
        )

    def _ask(
        self,
        state: MatchState,
        spec: BoardSpec,
        seat_index: int,
        repair: Optional[str],
    ) -> Dict[str, Any]:
        view = self.seat_view(state, spec, seat_index)
        user = json.dumps(view, separators=(",", ":"))
        if repair:
            user = f"{user}\n\n{repair}"
        result = self._client.generate(
            self._system_prompt,
            user,
            json_schema=MOVE_SCHEMA,
            **(
                {"temperature": self._temperature}
                if self._temperature is not None
                else {}
            ),
        )
        # Rough accounting — enough to enforce a ceiling without a tokeniser.
        self.tokens_spent += max(1, (len(self._system_prompt) + len(user)) // 4)
        if isinstance(result, str):
            result = json.loads(result)
        if not isinstance(result, dict):
            raise LlmSeatError("model did not return an object")
        return result

    def _validate(
        self,
        raw: Dict[str, Any],
        state: MatchState,
        spec: BoardSpec,
        seat_index: int,
    ) -> Action:
        """Reject anything the engine would reject — before it reaches the engine."""
        action_name = raw.get("action")

        if action_name == "choose_option":
            if state.pending_roll is None:
                raise LlmSeatError("no roll is pending")
            quotes = {q.steps: q for q in self._quotes(state, spec, seat_index)}
            try:
                steps = int(raw.get("steps"))
            except (TypeError, ValueError):
                raise LlmSeatError("steps must be an integer") from None
            quote = quotes.get(steps)
            if quote is None:
                raise LlmSeatError(
                    f"{steps} is not one of the legal options {sorted(quotes)}"
                )
            if not quote.affordable:
                raise LlmSeatError(
                    f"option {steps} costs {quote.price}, over your affordability cap"
                )
            if (
                state.has_constraint(ConstraintKind.FORCED_SUM, seat_index)
                and not quote.is_sum
            ):
                raise LlmSeatError("an accepted bribe binds this turn to the free sum")
            return Action(ActionType.CHOOSE_OPTION, seat_index, {"steps": steps})

        if action_name == "buy_property":
            if not self._fallback.should_buy_property(state, spec, seat_index):
                raise LlmSeatError("this square is not purchasable right now")
            return Action(ActionType.BUY_PROPERTY, seat_index)

        if action_name == "decline_purchase":
            return Action(ActionType.DECLINE_PURCHASE, seat_index)

        if action_name == "end_turn":
            return Action(ActionType.END_TURN, seat_index)

        raise LlmSeatError(
            f"unknown action {action_name!r} — legal actions are "
            f"{', '.join(LEGAL_ACTIONS)}"
        )


def build_llm_seat(
    profile,
    *,
    client_factory: Callable[[Optional[str]], Any],
    max_repair_retries: int = 2,
    fallback: Optional[BaselineSeat] = None,
) -> LlmSeat:
    """Build a seat from a ``bdv_agent_profile`` row.

    ``client_factory`` takes the connection slug/id and returns an
    ``LlmClient`` — injected so tests never touch a provider and so the plugin
    depends on the core seam rather than importing the service directly.
    """
    return LlmSeat(
        client=client_factory(
            str(profile.llm_connection_id) if profile.llm_connection_id else None
        ),
        system_prompt=profile.system_prompt or None,
        temperature=float(profile.temperature) if profile.temperature else None,
        max_repair_retries=max_repair_retries,
        token_budget=profile.max_tokens_per_match or None,
        fallback=fallback,
    )
