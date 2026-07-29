from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path


FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "onchain"
    / "oar_p2_cases.json"
)
ZERO = "0x0000000000000000000000000000000000000000"
CEX = "0x1111111111111111111111111111111111111111"


def identity(
    address: str,
    *,
    cex: bool = False,
    known: bool = False,
    entity_name: str = "",
) -> dict[str, object]:
    return {
        "address": address,
        "known": cex or known,
        "classification_eligible": cex,
        "entity_name": entity_name or ("Binance" if cex else "未知钱包"),
        "entity_type": "cex" if cex else ("wallet" if known else ""),
        "address_type": "hot" if cex else ("wallet" if known else ""),
        "source": "reviewed" if cex or known else "",
        "confidence": 0.99 if cex else (0.9 if known else 0),
    }


def record(
    index: int,
    *,
    block_time: int,
    from_address: str,
    to_address: str,
    amount: str,
    flow_type: str,
    amount_usd: str | None = None,
    from_cex: bool | None = None,
    to_cex: bool | None = None,
    from_known: bool = False,
    to_known: bool = False,
) -> dict[str, object]:
    from_cex = (
        flow_type in {"outflow", "internal", "cross_cex"}
        if from_cex is None
        else from_cex
    )
    to_cex = (
        flow_type in {
            "inflow",
            "internal",
            "cross_cex",
            "consolidation",
        }
        if to_cex is None
        else to_cex
    )
    return {
        "event_id": f"8453:0x{index + 1:064x}:{index}",
        "block_number": 1000 + index,
        "block_hash": f"0x{2000 + index:064x}",
        "block_time": block_time,
        "block_time_iso": "2023-11-14T00:00:00Z",
        "tx_hash": f"0x{3000 + index:064x}",
        "log_index": index,
        "explorer_url": f"https://basescan.org/tx/0x{3000 + index:064x}",
        "token_contract": "0x9999999999999999999999999999999999999999",
        "from": identity(
            from_address,
            cex=bool(from_cex),
            known=from_known,
            entity_name="Binance" if from_cex else "",
        ),
        "to": identity(
            to_address,
            cex=bool(to_cex),
            known=to_known,
            entity_name="Binance" if to_cex else "",
        ),
        "amount_raw": amount.replace(".", ""),
        "amount": amount,
        "amount_usd": amount_usd,
        "price_status": "available" if amount_usd is not None else "disabled",
        "flow_type": flow_type,
    }


def activity(
    *,
    window: str = "1h",
    complete: bool = True,
    transfers: list[dict[str, object]] | None = None,
    labels_status: str = "ok",
    price_status: str = "disabled",
    to_time: int = 1_700_000_000,
) -> dict[str, object]:
    transfers = list(transfers or [])
    return {
        "schema_version": 1,
        "status": "ok" if complete else "partial",
        "complete": complete,
        "truncated": not complete,
        "truncation_reason": None if complete else "max_events",
        "query": {
            "chain": "base",
            "chain_id": 8453,
            "contract": "0x9999999999999999999999999999999999999999",
            "window": window,
            "window_seconds": {
                "15m": 900,
                "1h": 3600,
                "4h": 14400,
                "24h": 86400,
            }[window],
            "from_block": 1,
            "to_block": 2,
            "from_time": to_time
            - {
                "15m": 900,
                "1h": 3600,
                "4h": 14400,
                "24h": 86400,
            }[window],
            "to_time": to_time,
            "confirmation_depth": 20,
            "min_usd": None,
            "usd_filter_applied": False,
        },
        "token": {
            "contract": "0x9999999999999999999999999999999999999999",
            "symbol": "TST",
            "name": "Test Token",
            "decimals": 6,
            "metadata_status": "verified_erc20",
        },
        "price": {
            "enabled": price_status != "disabled",
            "status": price_status,
            "price_usd": "2" if price_status == "available" else None,
            "source": "fixture" if price_status == "available" else "",
            "observed_at": to_time if price_status == "available" else 0,
            "historical_price": False,
        },
        "labels": {
            "status": labels_status,
            "count": 1 if labels_status == "ok" else 0,
            "identity_label_count": 1 if labels_status == "ok" else 0,
            "classification_eligible_cex_count": (
                1 if labels_status == "ok" else 0
            ),
        },
        "summary": {"transfer_count": len(transfers)},
        "largest_transfers": deepcopy(transfers[:10]),
        "transfers": deepcopy(transfers),
        "limits": {
            "max_events": 5000,
            "max_rpc_requests": 256,
            "max_unique_block_headers": 2000,
            "top_n": 50,
        },
        "diagnostics": {
            "rpc_request_count": 0,
            "adaptive_split_count": 0,
            "duplicate_log_count": 0,
            "skipped_indexed_value_count": 0,
            "unique_block_header_count": 0,
            "elapsed_ms": 0,
        },
        "warnings": [],
    }


def fixture_case(name: str) -> dict[str, object]:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    case = next(item for item in payload["cases"] if item["name"] == name)
    to_time = int(payload["to_time"])
    transfers = [
        record(
            index,
            block_time=to_time - int(item["minutes_ago"]) * 60,
            from_address=item["from"],
            to_address=item["to"],
            amount=item["amount"],
            flow_type=item["flow_type"],
        )
        for index, item in enumerate(case["events"])
    ]
    return activity(
        window=case["window"],
        complete=bool(case["complete"]),
        transfers=transfers,
        to_time=to_time,
    )
