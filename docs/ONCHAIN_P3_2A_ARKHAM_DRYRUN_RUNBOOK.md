# P3.2A Arkham REST dry-run runbook

P3.2A adds Arkham `/transfers` as an isolated primary event source. It does
not remove the P3.1 Base RPC collector, create an Arkham WebSocket session, or
authorize real Telegram delivery.

## Safety boundary

- Keep `ONCHAIN_REAL_SEND=false`.
- Keep `ARKHAM_WS_ENABLE=false`.
- Put `ARKHAM_API_KEY` only in the untracked `.env.onchain`.
- Do not paste the key into commands, logs, screenshots, issues, or PRs.
- Arkham state uses the existing isolated `data/onchain/` root.
- Do not change or restart `paopao-radar.service` or
  `paopao-market-stream.service` for this manual check.

## Source modes

```dotenv
ONCHAIN_SOURCE_MODE=arkham
ONCHAIN_ENABLE=true
ARKHAM_ENABLE=true
ARKHAM_REST_ENABLE=true
ARKHAM_WS_ENABLE=false
ONCHAIN_REAL_SEND=false
```

`arkham` requires an Arkham key but does not require Base HTTP/WSS endpoints
or a private Base CEX CSV. `base_rpc` preserves the P3.1 collector. `hybrid`
uses Arkham ingestion and reserves the Base RPC interface for optional later
verification; P3.2A does not reconcile the two providers.

## CEX filter capability

Start with:

```dotenv
ARKHAM_CEX_FILTER_MODE=type_cex
ARKHAM_CEX_ENTITY_IDS=
```

Run the read-only capability check:

```bash
python onchain_main.py arkham-check
```

It requires only authenticated `GET /chains` plus two small high-threshold
`/transfers` queries. `/health` remains an optional client diagnostic and is
not a capability dependency. P3.2A does not call deprecated WebSocket session
endpoints or create a WebSocket session. Output contains only authentication
status, supported-chain count, `type:cex` support, REST capability status,
redacted rate-limit metadata, and `websocket_check=not_run_p3_2a`.

If `type_cex_rest_supported=false`, obtain and review explicit Arkham entity
IDs, then configure both values deliberately:

```dotenv
ARKHAM_CEX_FILTER_MODE=entity_ids
ARKHAM_CEX_ENTITY_IDS=reviewed-id-1,reviewed-id-2
```

The runtime never guesses entity IDs or silently switches filter modes.

## Bounded REST reconciliation

Run one bounded dry-run:

```bash
python onchain_main.py arkham-once
python onchain_main.py arkham-status
python onchain_main.py db-check
```

`arkham-once` performs sequential inbound (`to=...`) and outbound
(`from=...`) queries. `/transfers` is budgeted at no more than one request per
second. Every request has a finite timeout, bounded retry, bounded pagination,
disabled redirects, and redacted errors. Numeric `Retry-After` values are
capped by `ARKHAM_RETRY_AFTER_MAX_SEC=60`; malformed values use bounded
exponential backoff. The API key is therefore never forwarded to a redirect
host.

Each stream starts from its durable cursor minus
`ARKHAM_REST_OVERLAP_SEC`. A page is normalized and persisted in one short
SQLite transaction. The first run uses
`ARKHAM_REST_BOOTSTRAP_LOOKBACK_SEC=3600`; every cycle freezes
`timeLte=now-ARKHAM_REST_INDEXING_DELAY_SEC` with a default 60-second indexing
delay. All pages in that cycle reuse the same lower and upper time bounds.
If the page budget is exhausted, Migration 4 preserves the frozen lower/upper
bounds and the next offset. The next invocation resumes that exact window
from the durable offset rather than replaying page zero. Only a fully drained
window advances the completed cursor to its frozen safe upper bound.

Each payload is normalized independently. A malformed payload is durably
quarantined as `rejected_schema`, using its payload hash when it has no
transfer ID, without downgrading valid siblings or an already processed
event. The cursor advances only after every item is either processed or
quarantined. When the page budget is exhausted, status is
`partial_backlog`, backlog metrics remain visible, and `arkham-once` exits
with an attention code instead of reporting success. Repeated overlap results
are idempotently deduplicated.

`arkham-status` derives its top-level result from every initialized stream:
`ok` exits 0, `failed` exits 1, `partial_backlog` exits 2, and an
uninitialized database exits 0 as `not_initialized`. It also reports each
stream's completed cursor, last success, backlog, frozen window, and next
offset.

Arkham raw payloads retain an immutable transfer fingerprint separately from
mutable entity/label enrichment. Updated attribution with unchanged transfer
facts creates an audit version and refreshes the latest entity snapshot. A
real immutable-fact conflict fails closed without advancing the cursor.

## Valuation and attribution

- Positive finite `historicalUSD` is the event-time USD value.
- Missing or invalid `historicalUSD` is stored as unpriced and cannot create a
  USD directional alert.
- A mixed priced/unpriced window aggregates only the priced subset and records
  `excluded_unpriced_count`; unpriced events never erase valid priced flow.
- Arkham entity/label attribution is probabilistic. The runtime stores
  `arkham_entity`, `arkham_label`, or `unlabeled`; it does not manufacture a
  numeric Arkham confidence score.
- Normal tokens may use the existing CEX inflow/outflow directional prior.
- Built-in stablecoin token IDs are `usd-coin`, `tether`, and `dai`.
  `ONCHAIN_STABLECOIN_TOKEN_IDS` extends this set; it does not erase these
  safety defaults. Stablecoins use `market_liquidity_context` and never claim
  that the stablecoin itself will rise or fall.
- Wrapped/receipt and unknown token policies are stored without a directional
  alert in P3.2A.
- A transfer with neither contract nor Arkham token ID receives a deterministic
  `arkham-token-unknown:<immutable-fingerprint>` identity. It remains auditable
  under the unknown policy and cannot create a directional alert.

Arkham notifications use a stable source/chain/token/direction/duration/
detection/severity key. Completed dry-run and sent attempts start the isolated
on-chain cooldown. Re-reading the identical Arkham transfer reuses its one
single-event alert fact and delivery row. A distinct transfer still creates
its own fact; when its notification is inside cooldown, that fact remains
audited with `cooldown_suppressed`. Severity escalation and direction reversal
use a new notification key. If mutable Arkham attribution changes the
directional interpretation of one transfer, the prior interpretation is
marked superseded and one revision fact is created.

## Manual acceptance with a private key

The implementation and GitHub Actions use mocked transports only. When a
private key is later available on the reviewed server:

1. Back up `.env.onchain` without printing it.
2. Configure Arkham with the safety values above.
3. Run `arkham-check`.
4. Stop if authentication, schema, or `type:cex` capability fails.
5. Run one `arkham-once` without `--send`.
6. Review `arkham-status`, `db-check`, dry-run counts, cursor movement,
   duplicates, unpriced events, and error types.
7. Keep the dedicated on-chain systemd service disabled until a separate
   rollout is approved.

## Known P3.2A limitations

- No Arkham WebSocket session or P3.2B streaming.
- No full Arkham/Base RPC reconciliation.
- Unknown Arkham chain names use a deterministic internal numeric key for the
  existing SQLite aggregation schema.
- Entity attribution can change as Arkham intelligence changes.
- REST cursor progress is timestamp based; overlap and event IDs provide
  replay-safe deduplication.

## Rollback

Disable Arkham with `ARKHAM_ENABLE=false` or select
`ONCHAIN_SOURCE_MODE=base_rpc`. Revert the P3.2A commit if code rollback is
required. Migration 4 only adds columns and isolated Arkham tables; do not
delete `data/onchain/` unless its audit data has been reviewed and backed up.
The production BOT services and their databases require no change or restart.
