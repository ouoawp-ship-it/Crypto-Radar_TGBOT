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

It calls only `GET /health`, `GET /chains`, `GET /ws/session-info`, and two
small high-threshold `/transfers` queries. It does not create a WebSocket
session. The output contains only authentication status, supported-chain
count, `type:cex` support, credit prices, and redacted rate-limit metadata.

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
and redacted errors.

Each stream starts from its durable cursor minus
`ARKHAM_REST_OVERLAP_SEC`. A page is normalized and persisted in one short
SQLite transaction. Its cursor advances only after every item in that page is
processed. A failed page is recorded as failed and leaves the previous cursor
position intact. Repeated overlap results are idempotently deduplicated.

## Valuation and attribution

- Positive finite `historicalUSD` is the event-time USD value.
- Missing or invalid `historicalUSD` is stored as unpriced and cannot create a
  USD directional alert.
- Arkham entity/label attribution is probabilistic. The runtime stores
  `arkham_entity`, `arkham_label`, or `unlabeled`; it does not manufacture a
  numeric Arkham confidence score.
- Normal tokens may use the existing CEX inflow/outflow directional prior.
- Configured stablecoins use `market_liquidity_context` and never claim that
  the stablecoin will pump or dump.
- Wrapped/receipt and unknown token policies are stored without a directional
  alert in P3.2A.

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
