from __future__ import annotations

import os
import unittest
from unittest.mock import Mock, patch

from tools import search_stack


class SearchStackTest(unittest.TestCase):
    def test_lite_is_preferred_when_a_real_result_page_is_available(self) -> None:
        with patch.object(search_stack, "_public_search_probe", return_value=(True, "search_ok")):
            result = search_stack.check_stack()
        self.assertEqual(result["recommended"], "duckduckgo_lite")
        self.assertTrue(result["endpoints"]["duckduckgo_lite"]["ok"])

    def test_configured_deepseek_is_the_no_probe_fallback(self) -> None:
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key", "OPENAI_API_KEY": ""}, clear=False), \
             patch.object(search_stack, "_public_search_probe", return_value=(False, "anti_bot")):
            result = search_stack.check_stack()
        self.assertEqual(result["recommended"], "deepseek_web_search")
        self.assertEqual(result["endpoints"]["deepseek_web_search"]["detail"], "configured_not_probed")

    def test_public_probe_rejects_anomaly_page(self) -> None:
        response = Mock(status_code=202, text="unusual traffic")
        with patch.object(search_stack.requests, "get", return_value=response):
            self.assertEqual(search_stack._public_search_probe("https://example.test", 1), (False, "anti_bot"))


if __name__ == "__main__":
    unittest.main()
