from __future__ import annotations

from paopao_radar.onchain_flow.cli import main


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
