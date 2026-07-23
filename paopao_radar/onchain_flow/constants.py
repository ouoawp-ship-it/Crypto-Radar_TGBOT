from __future__ import annotations


FIXTURE_VERSION = "p3.0-v1"
ALGORITHM_VERSION = "p3.0-v1"
THRESHOLD_VERSION = "p3.0-conservative-v1"
SEVERITY_VERSION = "p3.0-v1"
TEMPLATE_ID = "TG_ONCHAIN_FLOW_ALERT"

WINDOW_15M_SEC = 15 * 60
WINDOW_60M_SEC = 60 * 60

DIRECTIONAL_FLOW_TYPES = frozenset({"inflow", "outflow"})
NON_DIRECTIONAL_FLOW_TYPES = frozenset(
    {"internal", "cross_cex", "consolidation", "non_cex"}
)
FLOW_TYPES = DIRECTIONAL_FLOW_TYPES | NON_DIRECTIONAL_FLOW_TYPES

PRODUCTION_WRITE_PATHS = (
    "data/signals.db",
    "data/market_snapshots.db",
    "data/realtime_features.db",
    "data/tg_push_history.json",
    "data/tg_outbox.json",
)
