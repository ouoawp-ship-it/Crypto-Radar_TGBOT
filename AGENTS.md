# AGENTS.md

## Repository purpose

This repository is a production Telegram-only crypto signal bot. The existing `paopao-radar` and `paopao-market-stream` services are live systems. Preserve their behavior unless a task explicitly requires a reviewed change.

## Required checks

Before declaring work complete, run:

```bash
python -m compileall -q paopao_radar tests scripts main.py
python -m unittest discover -s tests -p "test_*.py"
git diff --check
```

Do not remove, skip, weaken, or rewrite existing tests merely to make a change pass.

## Production safety

- Real Telegram delivery is opt-in and must retain the existing dual gate: `--send --confirm-real-send`.
- Never commit tokens, chat IDs, API keys, RPC credentials, private endpoints, or production database content.
- Diagnostics must report whether a credential is configured, never its value.
- Network calls require finite timeouts, bounded retries, and a clear degraded mode.
- New features must default to disabled or dry-run unless a task explicitly says otherwise.

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
