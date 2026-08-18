from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


logic_entry = load_module("companion_logic_entry", ROOT / "scripts" / "analyze_logic.py")
installer = load_module("companion_logic_installer", ROOT / "install.py")


class LogicEntryTest(unittest.TestCase):
    def test_explicit_evidence_kind_limits_requests(self) -> None:
        case = {
            "evidence_requests": [
                {"kind": "system_change", "status": "pending"},
                {"kind": "profit", "status": "pending"},
            ]
        }
        self.assertEqual(logic_entry._request_kinds(case, ["profit"]), {"profit"})

    def test_pending_requests_are_default_evidence_plan(self) -> None:
        case = {
            "evidence_requests": [
                {"kind": "system_change", "status": "searched"},
                {"kind": "profit", "status": "completed"},
            ]
        }
        self.assertEqual(logic_entry._request_kinds(case, None), {"system_change"})

    def test_auto_theme_query_uses_concept_screening_contract(self) -> None:
        class FakeStock:
            @staticmethod
            def find_moda_root(_root=None):
                return ROOT.parent

            @staticmethod
            def _resolve_stock(_root, _query):
                raise ValueError("不是个股")

        class FakeSector:
            @staticmethod
            def screen_sector(_query, _candidates, **kwargs):
                self.assertEqual(kwargs["query_kind"], "concept")
                return {
                    "shortlist": [{
                        "code": "000021",
                        "name": "端侧核心",
                        "concept_exposure_tier": "核心主业",
                    }]
                }

        with patch.object(logic_entry, "_modules", return_value=(FakeStock, FakeSector)):
            result = logic_entry.analyze_logic("端侧AI", save=False)
        self.assertEqual(result["kind"], "concept")
        self.assertEqual(result["logic_case"]["company_branches"][0]["business_purity"], "核心主业")

    def test_installed_agents_route_through_logic_entry(self) -> None:
        codex = installer.render_codex_agent(ROOT)
        claude = installer.render_claude_agent()
        for rendered in (codex, claude):
            self.assertIn("analyze_logic.py", rendered)
            self.assertIn("Logic Case", rendered)
            self.assertIn("collector_only", rendered)
        self.assertNotIn("run analyze_a_share.py", codex)


if __name__ == "__main__":
    unittest.main()
