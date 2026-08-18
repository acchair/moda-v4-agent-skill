from __future__ import annotations

import os
import unittest
from unittest.mock import Mock, patch

from tools import search_stack


class SearchStackTest(unittest.TestCase):
    def test_check_stack_prefers_searxng_when_available(self) -> None:
        with patch.object(search_stack, "_http_probe", return_value=(True, "http_200")), \
                patch.object(search_stack, "_mcp_search_probe", return_value=(False, "ReadTimeout")), \
                patch.object(search_stack, "_public_search_probe", return_value=(True, "search_ok")):
            result = search_stack.check_stack()
        self.assertEqual(result["recommended"], "searxng")
        self.assertTrue(result["endpoints"]["searxng"]["ok"])

    def test_check_stack_falls_back_for_minis_without_local_services(self) -> None:
        with patch.dict(os.environ, {"SEARXNG_URL": "http://127.0.0.1:8888", "DDG_MCP_URL": "http://127.0.0.1:7070/mcp"}, clear=False), \
                patch.object(search_stack, "_http_probe", return_value=(False, "ConnectionError")), \
                patch.object(search_stack, "_mcp_search_probe", return_value=(False, "ConnectionRefusedError")), \
                patch.object(search_stack, "_public_search_probe", return_value=(False, "anti_bot")):
            result = search_stack.check_stack()
        self.assertEqual(result["recommended"], "none")

    def test_check_stack_reports_model_bridge_when_other_backends_fail(self) -> None:
        def tcp_probe(url: str, timeout: float):
            return ("model.example" in url, "tcp_ok" if "model.example" in url else "ConnectionRefusedError")

        with patch.dict(os.environ, {
            "MODA_MODEL_SEARCH_URL": "https://model.example/search",
            "OPENAI_API_KEY": "",
        }, clear=False), patch.object(search_stack, "_http_probe", return_value=(False, "ConnectionError")), \
             patch.object(search_stack, "_mcp_search_probe", return_value=(False, "ConnectionRefusedError")), \
             patch.object(search_stack, "_tcp_probe", side_effect=tcp_probe), \
             patch.object(search_stack, "_public_search_probe", return_value=(False, "anti_bot")):
            result = search_stack.check_stack()
        self.assertTrue(result["ok"])
        self.assertEqual(result["recommended"], "model_search_bridge")

    def test_public_probe_rejects_duckduckgo_anomaly_page(self) -> None:
        response = Mock(status_code=202, text="anomaly")
        with patch.object(search_stack.requests, "get", return_value=response):
            self.assertEqual(
                search_stack._public_search_probe("https://example.test", 1, "result__a"),
                (False, "anti_bot"),
            )

    def test_mcp_probe_requires_a_completed_tool_call(self) -> None:
        initialize = Mock(status_code=200, headers={"Mcp-Session-Id": "session"}, text='{"result": {}}')
        initialized = Mock(status_code=202, headers={}, text="")
        tool_call = Mock(status_code=200, headers={}, text='data: {"result": {"content": []}}')
        session = Mock()
        session.post.side_effect = [initialize, initialized, tool_call]
        with patch.object(search_stack.requests, "Session", return_value=session):
            self.assertEqual(search_stack._mcp_search_probe("http://mcp.test/mcp", 1), (True, "tool_ok"))
        self.assertEqual(session.post.call_args_list[-1].kwargs["json"]["method"], "tools/call")


if __name__ == "__main__":
    unittest.main()
