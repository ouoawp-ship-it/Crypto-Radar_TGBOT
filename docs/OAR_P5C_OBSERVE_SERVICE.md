# OAR-P5C Observe Service

`paopao-oar-watch.service` runs the existing bounded `watch-live` command as
an independent observe-only worker. It does not change the `paopao-radar` or
`paopao-market-stream` services.

## Safety defaults

- The unit starts only `watch-live --allow-network`.
- It never includes `--with-ai`, `--notify-dry-run`, `--send`, or
  `--confirm-real-send`.
- Runtime configuration comes from the private `.env.onchain` file.
- Initial rollout must keep `OAR_AI_ENABLE=false` and
  `ONCHAIN_REAL_SEND=false`.
- The installer validates Ubuntu, systemd, the project virtual environment,
  `.env.onchain` mode `600`, and the absence of an extra manual
  `watch-live` worker.
- Installation copies the versioned unit and reloads systemd. It does not
  enable or start the service.

## Installation

Before installation, create a recovery point and verify a dedicated Base RPC
can complete the approved token query. Then run:

```bash
sudo bash scripts/install_oar_watch.sh
sudo systemctl enable --now paopao-oar-watch.service
```

The menu refuses to start or restart the unit while an extra manual
`watch-live` process exists. A running systemd MainPID is not treated as an
extra writer.

## Verification

```bash
systemctl is-active paopao-oar-watch
systemctl show paopao-oar-watch \
  -p MainPID,NRestarts,MemoryCurrent,CPUUsageNSec
pgrep -af 'onchain_main.py.*watch-live'
python onchain_main.py status
python onchain_main.py doctor
```

Expected rollout state:

- one systemd-managed worker;
- `OAR_AUTOMATION_ENABLE=true`;
- `OAR_AI_ENABLE=false`;
- `ONCHAIN_REAL_SEND=false`;
- no Telegram HTTP calls;
- no automatic AI calls;
- no writes to the main `data/signals.db`.

## Rollback

Stop only the OAR unit:

```bash
sudo systemctl stop paopao-oar-watch
```

Set `OAR_AUTOMATION_ENABLE=false`. Keep the automation database, Registry,
Watchlist, and audit history. Confirm the two main services remain active.
After validation, set the automation gate back to true and start the OAR unit.
Do not delete databases as a rollback method.
