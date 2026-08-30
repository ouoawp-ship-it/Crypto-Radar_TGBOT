from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from config import Settings
from runtime.cli import push_flow_radar


class FlowRadarCliTests(unittest.TestCase):
    def test_candidate_rotation_state_requires_both_real_send_gates(self) -> None:
        cases = (
            (False, False, False),
            (True, False, False),
            (False, True, False),
            (True, True, True),
        )

        for send, confirm_real_send, expected_persist in cases:
            with self.subTest(send=send, confirm_real_send=confirm_real_send):
                source_context = MagicMock()
                source = object()
                source_context.__enter__.return_value = source
                gateway = Mock()
                gateway.send.return_value = SimpleNamespace(
                    status="dry_run" if not expected_persist else "sent",
                    reason="test",
                )
                flow = {
                    "text": "flow",
                    "template_id": "TG_FLOW_RADAR",
                    "dedup_key": "flow:test",
                    "items": [],
                    "diagnostics": {},
                }

                with (
                    patch("runtime.cli.BinanceDataSource", return_value=source_context),
                    patch("runtime.cli.FlowRadarEngine") as engine_cls,
                    patch("runtime.cli.persist_flow_market_rows", return_value=0),
                ):
                    engine_cls.return_value.build.return_value = flow
                    push_flow_radar(
                        Settings(),
                        gateway,
                        SimpleNamespace(
                            send=send,
                            confirm_real_send=confirm_real_send,
                        ),
                    )

                engine_cls.return_value.build.assert_called_once_with(
                    source,
                    persist_candidate_state=expected_persist,
                )


if __name__ == "__main__":
    unittest.main()
