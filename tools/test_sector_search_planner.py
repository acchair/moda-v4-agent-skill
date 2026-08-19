from __future__ import annotations

import unittest
from unittest.mock import patch

from tools.scoring import sector_search_planner as planner


class SectorSearchPlannerTest(unittest.TestCase):
    def test_live_resolution_uses_existing_akshare_board_adapter(self) -> None:
        rows = [{
            "code": "301269",
            "name": "华大九天",
            "industry": "软件开发",
            "main_business": "EDA软件",
            "business_items": ["模拟电路设计EDA"],
        }]
        metadata = {
            "source": "AKShare/东方财富概念板块成分股",
            "coverage_status": "live_theme",
            "board_names": ["EDA概念"],
        }
        with patch("tools.sector_screening.infer_query_kind", return_value="sector"), \
             patch("tools.sector_screening.resolve_sector_universe", return_value=(rows, metadata)) as resolve:
            entity = planner.resolve_entity_context("EDA板块")

        self.assertEqual(entity["status"], "resolved")
        self.assertEqual(entity["query_kind"], "concept")
        self.assertEqual(entity["board_names"], ["EDA概念"])
        self.assertEqual(entity["representative_companies"][0]["name"], "华大九天")
        self.assertIn("EDA软件", entity["business_phrases"])
        resolve.assert_called_once()

    def test_unknown_board_still_splits_universal_dimensions_from_resolved_members(self) -> None:
        screening = {
            "query_kind": "sector",
            "universe": {"source": "AKShare/东方财富行业板块成分股", "board_name": "未知新行业", "total": 3},
            "all_candidates": [{
                "code": "000001",
                "name": "样本公司",
                "industry": "未知新行业",
                "main_business": "专用精密设备与控制系统",
                "business_items": ["精密设备"],
            }],
        }
        entity = planner.resolve_entity_context("未知新行业", screening=screening)
        plan = planner.build_broad_query_plan("未知新行业", entity_context=entity)
        dimensions = {item["dimension"] for item in plan}

        self.assertTrue({
            "entity_definition", "market_size", "value_chain", "technology_route",
            "supply_demand", "competition", "company_exposure", "counterevidence",
        }.issubset(dimensions))
        self.assertTrue(any("下游资本开支" in item["query"] for item in plan))
        self.assertFalse(any("nvidia.com" in item["query"] for item in plan))

    def test_local_fallback_is_partial_and_preserves_live_failure(self) -> None:
        screening = {
            "query_kind": "sector",
            "universe": {
                "source": "本地部分名单",
                "coverage_status": "local_partial",
                "live_fallback": "ConnectionError: 概念成分股获取失败",
                "total": 1,
            },
            "all_candidates": [{"code": "301269", "name": "华大九天", "industry": "软件开发"}],
        }
        entity = planner.resolve_entity_context("EDA板块", screening=screening)

        self.assertEqual(entity["status"], "partial")
        self.assertEqual(entity["coverage_status"], "local_partial")
        self.assertIn("ConnectionError", entity["live_resolution_error"])

    def test_mrna_uses_a_compact_life_science_overseas_radar(self) -> None:
        plan = planner.build_broad_query_plan("mRNA")
        radar = [item for item in plan if item["bucket"] == "海外增量雷达"]

        self.assertEqual(len(radar), 4)
        self.assertEqual([item["domain_hint"] for item in radar[:3]], [
            "fda.gov", "clinicaltrials.gov", "sec.gov",
        ])
        self.assertTrue(all(item["dimension"] == "overseas_event" for item in radar))


if __name__ == "__main__":
    unittest.main()
