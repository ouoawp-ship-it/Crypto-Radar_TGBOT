# OAR-P5D Watch Delivery Mode

The OAR systemd worker uses `scripts/run_oar_watch.sh` to translate a small,
validated delivery policy into fixed `watch-live` arguments. The launcher
does not call RPC, AI, or Telegram itself and never places credentials in
process arguments.

## Modes

- `observe` is the default. It starts `watch-live --allow-network` and never
  creates the report notifier or AI client.
- `dry_run` adds only `--notify-dry-run`. Telegram HTTP remains disabled by
  the existing gateway dry-run boundary.
- `real` adds `--send --confirm-real-send` only when
  `ONCHAIN_REAL_SEND=true`, the fixed acknowledgement is present, and the
  Telegram Bot, Chat, and on-chain Topic configuration is complete.

Unknown modes and incomplete real-send gates stop before the worker starts
with `real_send_gate_blocked` or `watch_delivery_mode_invalid`. No configured
value is printed.

## Automatic AI gate

`--with-ai` is added only in `dry_run` or `real` mode when all of the
following are true:

- `OAR_WATCH_WITH_AI=true`;
- `OAR_AI_ENABLE=true`;
- AI Base URL, API Key, and Model are configured.

Changing the watch AI preference never enables the global AI gate.

## Safe defaults

```dotenv
OAR_WATCH_DELIVERY_MODE=observe
OAR_WATCH_WITH_AI=false
OAR_WATCH_REAL_SEND_ACK=
ONCHAIN_REAL_SEND=false
OAR_AI_ENABLE=false
```

The Chinese `paopao` menu applies mode changes through the allowlisted,
locked, backed-up, atomic configuration manager. Real delivery and automatic
AI require their full Chinese confirmation phrases. Configuration changes
take effect only after an explicit OAR Watch restart.

## Existing Telegram topic binding

The production bot is outbound-only: it has no `getUpdates` loop, webhook,
or persisted inbound Update queue. An existing forum topic can be bound
offline from an official Telegram message link with:

```bash
python onchain_main.py telegram-topic-link bind --stdin
```

The link is read from stdin, validated in memory, and never stored. Private
`t.me/c/...` links are accepted only when their channel component matches the
configured supergroup Chat ID. Public links are rejected unless a separately
configured username can prove the same chat. The command does not call
Telegram, create a topic, send a message, or print the Chat ID, Topic ID,
Message ID, username, or original link.

## systemd stop behavior

The CLI exits with status 130 after its SIGINT shutdown path. The versioned
unit declares only `SuccessExitStatus=130`, while retaining
`Restart=on-failure`, so operator stops are clean and unrelated crashes are
not hidden.
