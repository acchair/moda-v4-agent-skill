from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tools.providers import eastmoney_transport as transport
from tools.providers import a_stock_data_provider


class EastmoneyTransportTest(unittest.TestCase):
    def test_wait_turn_persists_shared_clock(self) -> None:
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(transport, "_state_path", return_value=Path(directory) / "rate.state"):
            transport.wait_turn(min_interval=0, jitter=0)
            payload = (Path(directory) / "rate.state").read_text(encoding="ascii")
        self.assertGreater(float(payload), 0)

    def test_request_serializes_an_eastmoney_call(self) -> None:
        session = MagicMock()
        session.request.return_value = MagicMock()
        with patch.object(transport, "wait_turn") as wait:
            transport.get("https://push2.eastmoney.com/api/qt/stock/get", params={"secid": "0.000001"}, session=session)
        wait.assert_called_once_with()
        self.assertEqual(session.request.call_args.args[:2], ("GET", "https://push2.eastmoney.com/api/qt/stock/get"))

    def test_rejects_unrelated_hosts(self) -> None:
        with self.assertRaises(ValueError):
            transport.get("https://example.com/data")

    def test_eastmoney_code_normalization_rejects_legacy_bse_codes(self) -> None:
        self.assertEqual(a_stock_data_provider.clean_code("sh600000"), "600000")
        with self.assertRaisesRegex(ValueError, "历史代码"):
            a_stock_data_provider.clean_code("430047")


if __name__ == "__main__":
    unittest.main()
