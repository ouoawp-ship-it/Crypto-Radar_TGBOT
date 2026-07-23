# AGENTS.md

## Repository purpose

This repository is a production Telegram-only crypto signal bot. The existing `paopao-radar` and `paopao-market-stream` services are live systems. Preserve their behavior unless a task explicitly requires a reviewed change.

## Required checks

Before declaring work complete, run:

```bash
python -m compileall -q paopao_radar tests scripts main.py onchain_main.py
python -m unittest discover -s tests -p "test_*.py"
git diff --check
```

When `onchain_main.py` does not yet exist, omit it from the first command until the task creates it.

Do not remove, skip, weaken, or rewrite existing tests merely to make a change pass.

## Production safety

- Real Telegram delivery is opt-in and must retain the existing dual gate: `--send --confirm-real-send`.
- Never commit tokens, chat IDs, API keys, RPC credentials, private endpoints, or production database content.
- Diagnostics must report whether a credential is configured, never its value.
- Network calls require finite timeouts, bounded retries, and a clear degraded mode.
- New features must default to disabled or dry-run unless a task explicitly says otherwise.

## On-chain flow module boundary

The authoritative design is `docs/ONCHAIN_FLOW_LISTENER_ARCHITECTURE.md`.

For P3.0/P3.1 work:

- Use a separate entry point: `onchain_main.py`.
- Put implementation under `paopao_radar/onchain_flow/`.
- Do not attach collectors or aggregation to `paopao_radar.cli.run_loop()` or `main.py live`.
- Do not change the existing `paopao-radar.service` or `paopao-market-stream.service` `ExecStart` commands.
- Write only under `data/onchain/`.
- Do not write `data/signals.db`, `data/market_snapshots.db`, `data/realtime_features.db`, `data/tg_push_history.json`, or `data/tg_outbox.json`.
- Reading existing market databases is allowed only through read-only SQLite connections with short lock timeouts and fail-open behavior.
- Use a dedicated Telegram template, topic, push history, outbox, cooldown, and hourly quota for on-chain alerts.
- `ONCHAIN_ENABLE=false` must cause zero collector connections, background threads, database writes, and Telegram calls.
- Treat `chain_id + tx_hash + log_index` as the idempotency key for EVM logs.
- Reorg, duplicate delivery, reconnect backfill, queue pressure, missing price, missing metadata, and low-confidence labels are first-class states, not exceptional afterthoughts.

## Scope discipline

Implement the smallest complete vertical slice required by the task. Do not introduce Node, Docker, PostgreSQL, ClickHouse, Arkham scraping, hidden APIs, or a new web service unless the task explicitly calls for it.

Do not copy code with incompatible licensing. Dune Spellbook is an algorithmic reference only unless licensing is separately approved.

## Pull request expectations

A PR description must include:

- changed behavior and explicit non-changes;
- process, storage, and Telegram isolation evidence;
- tests run and results;
- new configuration keys and their safe defaults;
- failure/degraded modes;
- manual rollout and rollback steps.
