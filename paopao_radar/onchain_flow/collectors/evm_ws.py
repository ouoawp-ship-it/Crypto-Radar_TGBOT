from __future__ import annotations

import json
from collections import deque
from typing import Any, Callable

from ..config import OnchainSettings
from .evm_http import parse_hex_quantity


class WssError(RuntimeError):
    pass


class WssHeadTrigger:
    """Synchronous newHeads trigger; HTTP remains the finalized fact source."""

    def __init__(
        self,
        settings: OnchainSettings,
        *,
        connection_factory: Callable[..., Any] | None = None,
    ):
        self.settings = settings
        self.connection_factory = connection_factory
        self.connection: Any | None = None
        self.subscription_id = ""
        self.queue: deque[int] = deque(maxlen=settings.wss_queue_max)
        self.connected = False
        self.reconnect_count = 0

    def _factory(self) -> Callable[..., Any]:
        if self.connection_factory is not None:
            return self.connection_factory
        from websocket import create_connection

        return create_connection

    def connect(self) -> None:
        if not self.settings.base_wss_rpc_url:
            raise WssError("Base WSS provider is not configured")
        self.close()
        try:
            self.connection = self._factory()(
                self.settings.base_wss_rpc_url,
                timeout=float(self.settings.wss_idle_timeout_sec),
            )
            self.connection.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "eth_subscribe",
                        "params": ["newHeads"],
                    }
                )
            )
            response = json.loads(self.connection.recv())
            if (
                not isinstance(response, dict)
                or response.get("id") != 1
                or not isinstance(response.get("result"), str)
            ):
                raise WssError("newHeads subscription was rejected")
            self.subscription_id = str(response["result"])
            self.connected = True
        except WssError:
            self.close()
            raise
        except Exception as exc:
            self.close()
            raise WssError(
                f"WSS connect failed: {type(exc).__name__}"
            ) from exc

    def receive_head(self) -> int:
        if self.connection is None or not self.connected:
            raise WssError("WSS is not connected")
        try:
            while True:
                payload = json.loads(self.connection.recv())
                if not isinstance(payload, dict):
                    continue
                params = payload.get("params")
                if not isinstance(params, dict):
                    continue
                if params.get("subscription") != self.subscription_id:
                    continue
                result = params.get("result")
                if not isinstance(result, dict):
                    continue
                head = parse_hex_quantity(result.get("number"), "WSS head")
                self.queue.append(head)
                return self.queue.popleft()
        except Exception as exc:
            self.connected = False
            if isinstance(exc, WssError):
                raise
            raise WssError(
                f"WSS receive failed: {type(exc).__name__}"
            ) from exc

    def close(self) -> None:
        connection = self.connection
        self.connection = None
        self.connected = False
        self.subscription_id = ""
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
