# OAR Telegram group query

`paopao-oar-query.service` is an optional single Worker for explicit,
read-only on-chain queries in the configured Telegram group and the
`链上活动雷达` topic. It is disabled by default and is isolated from the main
BOT loop and `paopao-oar-watch.service`.

## Accepted input

```text
@BotUsername 查询 CBDOGE 15m
@BotUsername 查询 0x20-byte-base-contract 1h
/oar@BotUsername CBDOGE 4h
```

The explicit `/oar@BotUsername` form works with Telegram group privacy mode.
The Worker requires the shared bot to be a group administrator so free-text
mentions are reliably delivered as well. Startup also verifies that the
configured destination is a forum supergroup. The Worker ignores messages from
other chats, other topics, bots, stale updates, and text that does not
explicitly invoke its own username.

Symbol input is resolved only through one verified Primary Registry entry.
It never guesses a contract from a token name. A full EVM address is queried
directly on Base and still has to pass the existing ERC-20 metadata checks.

## Safety gates

The Worker starts only when all of the following are true:

- `OAR_TELEGRAM_QUERY_ENABLE=true`;
- `OAR_TELEGRAM_QUERY_ACK=启用群内链上查询`;
- shared Bot, Chat and on-chain Topic configuration is complete;
- the CLI contains `--allow-network --send --confirm-real-send`;
- Telegram has no configured webhook and no competing polling Worker.

Queries use fixed Token Activity budgets, do not enable price or AI, do not
create Watch entries, and cannot execute wallet, signing, or transaction
operations. Per-user cooldown and a bounded hourly query budget protect the
RPC and Telegram APIs.

The state file contains only the next Telegram update offset, bounded
timestamps, and a SHA-256 user key. It does not persist inbound message text,
Chat ID, Topic ID, user ID, credentials, headers, or Provider responses.

## Operations

Use the Chinese menu:

```text
paopao
→ Telegram 设置与测试
→ 群内 @Bot 链上异动查询
```

The enable action installs and starts the dedicated Unit only after the
fixed Chinese confirmation. Disable stops the service and atomically clears
its acknowledgement. Neither action changes main BOT Dry-run, OAR Observe,
automatic AI, or automatic real delivery.
