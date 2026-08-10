from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from tools import search_stack


class SearchStackTest(unittest.TestCase):
    def test_check_stack_prefers_searxng_when_available(self) -> None:
        with patch.object(search_stack, "_http_probe", return_value=(True, "http_200")), \
                patch.object(search_stack, "_tcp_probe", return_value=(True, "tcp_ok")):
            result = search_stack.check_stack()
        self.assertEqual(result["recommended"], "searxng")
        self.assertTrue(result["endpoints"]["searxng"]["ok"])

    def test_check_stack_falls_back_for_minis_without_local_services(self) -> None:
        with patch.dict(os.environ, {"SEARXNG_URL": "http://127.0.0.1:8888", "DDG_MCP_URL": "http://127.0.0.1:7070/mcp"}, clear=False), \
                patch.object(search_stack, "_http_probe", return_value=(False, "ConnectionError")), \
                patch.object(search_stack, "_tcp_probe", return_value=(False, "ConnectionRefusedError")):
            result = search_stack.check_stack()
        self.assertEqual(result["recommended"], "public_fallback")


if __name__ == "__main__":
    unittest.main()
