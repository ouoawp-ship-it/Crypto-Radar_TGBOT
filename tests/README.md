# Test suite

The test suite is organized by product domain instead of one file per source
module. Keep related cases together and add a new test file only when a domain
cannot remain readable in its current file.

## Layout

| File | Responsibility |
| --- | --- |
| `test_ai_delivery.py` | AI/Telegram delivery, formatting, retries, and provider failures |
| `test_ai_interactions.py` | Assistant commands, menus, and price-alert interactions |
| `test_ai_routing.py` | Intent routing, group behavior, and access control |
| `test_bot_support.py` | AI prompts, price-alert storage, and market links |
| `test_telegram.py` | Telegram delivery, retries, topics, and message history |
| `test_radar_logic.py` | Announcement parsing, radar scoring, and launch alerts |
| `test_flow_radar.py` | Flow-radar calculations and windows |
| `test_funding_alert.py` | Funding alert decisions and state |
| `test_funding_sources.py` | Funding-provider clients, caching, and interval transitions |
| `test_market_data.py` | Exchange market data, market-cap metadata, data quality, and HTTP behavior |
| `test_market_cockpit.py` | Versioned market snapshots, aggregation, legacy SQLite migration, and quality gates |
| `test_market_funds.py` | Sector rotation, asset flow tables, ranking coverage, and graceful degradation |
| `test_coin_evidence.py` | Coin evidence timeline, chart/series contracts, and partial-source failures |
| `test_news_intelligence.py` | Official-event normalization, clustering, rights metadata, and retention |
| `test_agent_intelligence.py` | Rule-first agents, evidence/counter-evidence gates, expiry, and persistence |
| `test_cockpit_operations.py` | V2 rollout modes, rollback isolation, SSE projections, and deploy contracts |
| `test_signal_store.py` | Signal persistence, filtering, statistics, and compatibility |
| `test_storage_core.py` | Atomic JSON, JSON storage, runtime cache, and configuration |
| `test_jobs.py` | Job persistence, execution, cleanup, and API payloads |
| `test_web_platform.py` | Web surface, authentication, and API contracts |
| `test_deployment.py` | CLI, environment sync, Git ignore, reports, and update scripts |
| `test_maintenance.py` | Runtime cleanup and retention rules |

## Commands

Run the complete suite from the repository root:

```bash
python -m unittest discover -s tests -p "test_*.py"
```

Run one domain while developing:

```bash
python -m unittest discover -s tests -p "test_telegram.py"
```

## Rules

- A bug fix should include a regression test in the owning domain file.
- A new feature should extend an existing domain file when practical.
- Tests must not send real Telegram messages or modify production data.
- External HTTP calls must use a fake client or a mock.
- Temporary databases and files must use `TemporaryDirectory` or an equivalent
  isolated fixture.
- Do not delete a test only because implementation details changed; rewrite it
  around the intended behavior.
- Keep the full suite runnable with the standard-library `unittest` command so
  install and server-update checks remain dependency-free.
