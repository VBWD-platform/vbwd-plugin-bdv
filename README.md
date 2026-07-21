# bdv — BizDevVibes

A **dice-market board game** as a vbwd backend plugin.

> You roll two dice publicly. The roll `{a, b}` yields exactly **three** legal
> moves: `a`, `b`, or `a+b`. **The sum is always free** — that is fate, classic
> play. Choosing a single die instead is a **purchase**, priced deterministically
> from the game state. **The fee is not paid to the bank — it goes to your
> opponents.** A player with no money plays classic rules; a rich player buys
> partial control and directly finances the underdogs.

## The pricing rule

```
options(roll{a,b}) = dedup([a, b, a+b])          # {3,3} -> (3, 6)
ev(option)         = immediate value delta of landing on the resulting square
price(option)      = max(0, round(k_price x (ev(option) - ev(sum))))
price(sum)         = 0                            # fate is always free
affordable         = price <= floor(cap_pct x cash)
recipients         = fee_policy.distribute(price, state)   # opponents, never the bank
```

Worked example — 5 squares from a hotel with rent 900, 2 squares from the square
that completes your funnel stage, roll `{2, 3}`:

| option | ev | price |
|---|---|---|
| `+2` → completes your stage | `+300` | **600** |
| `+3` → neutral | `0` | **450** |
| `+5` → opponent's, rent 900 | `−900` | **free (fate)** |

Escaping disaster is expensive, and you pay the table for it.

## Architecture

```
bdv/
├── core/          ★ PURE — no flask, no SQLAlchemy, no clock, no global RNG
│   ├── board.py     BoardSpec + validation (the ONE definition of a valid board)
│   ├── state.py     frozen MatchState; every transition returns a new state
│   ├── dice.py      roll = f(seed, cursor) — deterministic, replayable
│   ├── options.py   the three-option set
│   ├── pricing.py   EV -> price -> affordability cap
│   ├── fees.py      all_to_poorest | split_among_opponents | to_bank (control)
│   ├── effects.py   declarative card ops + generated descriptions
│   ├── engine.py    the turn-loop state machine
│   └── replay.py    same spec + seed + actions => byte-identical state
├── models/  repositories/  services/  routes.py
└── agents/        BaselineSeat (deterministic)
```

**Why the engine is pure:** the product sells *auditable* move prices. A price
computed inside a request handler reading `datetime.now()` and a global `random`
could never be re-derived, so a disputed charge could never be answered — and the
balance harness could not run hundreds of matches in seconds. Purity is the
feature, not a style preference.

**The action log is the source of truth.** Current state is the fold of
`engine.apply()` over `bdv_action`; the snapshot is a rebuildable cache. Replay,
audit and the harness come for free instead of as three retrofitted features.

## Card rules

Cards are **declarative descriptors**, never stored code:

```json
{ "ops": [ { "op": "collect_from_each_player", "params": { "amount": 500 } } ] }
```

Each op supplies `apply` (pure), `describe` (an i18n key + params, so the
human-readable text is *generated* and cannot drift from behaviour), `ev_hint`
(so a draw square can be priced without simulating the deck) and a
`params_schema` (which drives the admin form generically). Adding an op needs
**zero frontend code**.

## Config

| Key | Default | Purpose |
| --- | --- | --- |
| `game_display_name` | `BizDevVibes` | Public display name (config-driven so a rename is not a refactor). |
| `turn_timeout_seconds` | `120` | Time before the turn auto-takes the FREE sum. |
| `negotiation_window_seconds` | `30` | Bribe-to-fate window; `0` disables negotiation. |
| `agent_max_tokens_per_match` | `60000` | LLM ceiling per agent seat; crossing it degrades to the baseline agent. |
| `default_seats` / `min_seats` / `max_seats` | `3` / `2` / `4` | Lobby bounds (engine supports 2–6). |

## Quality gate

```bash
cd vbwd-backend && bin/pre-commit-check.sh --plugin bdv --full
```

## Balance harness

The product rests on one empirical claim: because the fee is a **transfer** to
opponents rather than a **sink** to the bank, the leader finances the underdogs
and the snowball self-damps. That is measured, not asserted:

```bash
python plugins/bdv/bin/bdv_balance.py --matches 500 --seats 3
```

It includes a `to_bank` **control arm** — the classic-game baseline — because
without one there is nothing to measure damping against.

## Naming

The seeded board uses **original** square names themed as a business-development
deal funnel (colour groups are funnel stages, so completing a set means owning a
whole pipeline stage). No Hasbro-owned names appear anywhere; a denylist test
enforces it.

## Licence

BSL 1.1 with a Bitcoin-denominated Additional Use Grant — see [`LICENSE`](LICENSE).
