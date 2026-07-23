# P3.1 Base dry-run runbook

P3.1 is an isolated Base mainnet collector. It is disabled by default and
does not authorize real Telegram delivery. The required seven-day observation
starts only after this change is merged and deliberately deployed; it is not
claimed by the implementation PR.

## Local configuration

1. Copy `.env.onchain.example` to the untracked `.env.onchain`.
2. Keep `ONCHAIN_REAL_SEND=false`.
3. Set `ONCHAIN_CEX_LABELS_FILE` to an untracked, reviewed CSV containing at
   least one active Base CEX label. Synthetic fixture labels are rejected in
   live mode.
4. Set Base HTTP and WSS endpoints. Never paste endpoints or keys into issues,
   logs, screenshots, or committed files.
5. Set `ONCHAIN_ENABLE=true` and `ONCHAIN_BASE_ENABLE=true` only when ready for
   an isolated dry-run.

## Preflight

```bash
python onchain_main.py doctor
python onchain_main.py labels-check
python onchain_main.py provider-check --chain base
python onchain_main.py cursor-status --chain base
python onchain_main.py once
```

`provider-check` performs bounded read-only RPC calls. `once` reconciles every
missing finalized block and emits only Telegram dry-run output while
`ONCHAIN_REAL_SEND=false`.

## Bounded observation

Run a five-minute foreground check first:

```bash
python onchain_main.py live --duration-minutes 5
```

Inspect only redacted health fields:

```bash
python onchain_main.py status
python onchain_main.py cursor-status --chain base
python onchain_main.py db-check
```

The durable runtime file is `data/onchain/runtime_status.json`. During the
post-merge seven-day observation, record cursor lag, WSS reconnects, HTTP
reconciliation, RPC errors, duplicates, orphans, priced/unpriced counts and
dry-run alerts. Do not report the observation complete until seven real days
have elapsed with reviewed evidence.

## Dedicated service

The installer writes only `paopao-onchain-flow.service`. Its default invocation
does not enable or start anything:

```bash
sudo scripts/install_onchain_flow.sh
```

After preflight and explicit approval:

```bash
sudo scripts/install_onchain_flow.sh --enable
```

The enable path re-runs doctor, live-label validation and provider-check before
calling systemd.

## Rollback

Stop and disable only `paopao-onchain-flow.service`, remove its dedicated unit,
and remove `data/onchain/` only after preserving any required audit backup.
Revert the P3.1 merge commit if code rollback is required. Existing production
BOT services, commands, databases, Telegram history and thresholds do not
require any change or restart.
