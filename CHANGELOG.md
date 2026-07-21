# Changelog

## [26.7.0] — 2026-07-21

Initial release — the BizDevVibes game engine and board domain.

### Added
- **Pure game-core engine** (`bdv/core/`): board spec, frozen match state,
  deterministic dice, the `{a, b, a+b}` option set, **EV pricing**, swappable fee
  distribution policies, and a replay format. Imports nothing from `vbwd`, Flask
  or SQLAlchemy — enforced by an AST purity oracle.
- **Board configuration domain**: `bdv_board` / `bdv_square` / `bdv_card` with a
  root Alembic revision, `BoardSpecFactory` (rows → pure spec) and publish-time
  validation.
- **Card rule engine**: 11 declarative effect ops with generated human-readable
  descriptions and EV hints; no stored code, no `eval`.
- **Match lifecycle**: append-only action log as the source of truth, rebuildable
  state snapshot, optimistic `state_seq` concurrency, bribe-to-fate offers.
- **Seeded `funnel-40` board**: 40 squares themed as a bizdev deal pipeline.
- **`BaselineSeat`** deterministic agent and a **balance harness** (`bin/bdv_balance.py`).
