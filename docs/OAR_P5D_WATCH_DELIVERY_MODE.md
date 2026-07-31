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

## Shared Telegram topic bootstrap

The production bot is outbound-only: it has no `getUpdates` loop, webhook,
or persisted inbound Update queue. OAR reuses the main BOT token and chat
from `.env.oi`; only its dedicated topic is stored separately. The operator
can validate or initialize that topic with:

```bash
python onchain_main.py telegram-topic bootstrap --allow-network
```

The command uses `getMe`, `getChat`, and `getChatMember` against the shared
group. It reuses a valid configured topic; otherwise, with topic-management
permission, it creates `链上活动雷达` once and atomically stores its ID. It
does not call `getUpdates`, send a message, enable Real mode, or print the
Bot token, Chat ID, or Topic ID. The older message-link CLI remains available
only as a compatibility recovery path.

Publishing the static topic introduction is a separate, persistent operation:

```bash
python onchain_main.py telegram-topic intro \
  --allow-network --send --confirm-real-send
```

It requires both real-send CLI gates, sends no report card, does not change
the long-running delivery mode, and reuses an already current pinned intro.

## systemd stop behavior

The CLI exits with status 130 after its SIGINT shutdown path. The versioned
unit declares only `SuccessExitStatus=130`, while retaining
`Restart=on-failure`, so operator stops are clean and unrelated crashes are
not hidden.
