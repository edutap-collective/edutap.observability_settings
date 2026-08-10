# Design records (Claude/Superpowers documents)

* **Spec** (`specs/YYYY-MM-DD-<topic>-design.md`) — the design worked out in dialogue.
* **Plan** (`plans/YYYY-MM-DD-<topic>.md`) — the implementation derived from it.

| Date | Topic | Spec |
|---|---|---|
| 2026-08-10 | Why this package exists, the three backends, and what may be said about a person | [`specs/2026-08-10-observability-settings-design.md`](specs/2026-08-10-observability-settings-design.md) |

Records are snapshots of a decision at a point in time. They are not rewritten to
match a later state; a changed decision gets a new record.

The Sentry half of the design is inherited rather than invented here: the options and
the measurements behind them come from `edutap.data_provider`,
`docs/superpowers/specs/2026-08-04-observability-design.md`.
