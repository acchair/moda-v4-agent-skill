from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from tools.scoring import sector_broad_research as broad
from tools.scoring import web_research as web


class SectorBroadResearchTest(unittest.TestCase):
    def test_collect_preserves_broad_url_and_body_audit_counts(self) -> None:
        def search(_provider: str, query: str, _timeout: float, cache_scope: str = ""):
            del cache_scope
            domain = query.split("site:", 1)[1].split()[0] if "site:" in query else "example.com"
            query_id = abs(hash(query))
            return "duckduckgo_lite", [
                {"title": f"端侧AI 资料 {index}", "url": f"https://{domain}/{query_id}/article-{index}", "snippet": "端侧AI 技术路线", "date": ""}
                for index in range(1, 7)
            ], []

        def fetch(url: str, _timeout: float):
            return "ok", f"端侧AI 正文核验 {url}"

        with patch.object(broad.web, "_search", side_effect=search), \
             patch.object(broad.web, "_fetch_page", side_effect=fetch), \
             patch.object(broad.web, "_reset_run_snapshot"), \
             patch.object(broad.web, "_search_cache_batch") as cache_batch:
            cache_batch.return_value.__enter__.return_value = None
            cache_batch.return_value.__exit__.return_value = None
            result = broad.collect_sector_broad_evidence("端侧AI", provider="model", raw_url_limit=100, body_page_limit=100)
        self.assertGreaterEqual(result["audit"]["query_executed"], broad.MIN_QUERY_COVERAGE)
        self.assertLessEqual(result["audit"]["query_executed"], result["audit"]["query_planned"])
        self.assertGreaterEqual(result["audit"]["raw_result_count"], 36)
        self.assertLessEqual(result["audit"]["raw_url_count"], 100)
        self.assertEqual(result["audit"]["body_attempted"], result["audit"]["raw_url_count"])
        self.assertEqual(result["audit"]["body_readable"], result["audit"]["body_attempted"])
        self.assertIn(result["audit"]["early_stop_reason"], {"candidate_source_coverage", "raw_url_target"})
        self.assertGreater(result["audit"]["usable_evidence_count"], 0)

    def test_resolved_board_and_f10_business_drive_queries_before_source_shortcuts(self) -> None:
        screening = {
            "query_kind": "concept",
            "universe": {
                "source": "AKShare/东方财富概念板块成分股",
                "coverage_status": "live_theme",
                "board_names": ["先进封装"],
                "total": 42,
            },
            "shortlist": [{
                "code": "688001",
                "name": "测试设备",
                "industry": "半导体设备",
                "main_business": "晶圆级封装设备与检测系统",
                "business_items": ["键合设备", "检测设备"],
                "chain_stage": "封装设备",
            }],
        }
        entity = broad.sector_search_planner.resolve_entity_context("先进封装题材", screening=screening)
        plan = broad.build_query_plan("先进封装题材", entity_context=entity)

        self.assertEqual(entity["query_kind"], "concept")
        self.assertEqual(entity["board_names"], ["先进封装"])
        self.assertEqual(entity["constituent_count"], 42)
        entity_query = next(item for item in plan if item["bucket"] == "实体校准")
        self.assertIn("测试设备", entity_query["query"])
        self.assertIn("晶圆级封装设备与检测系统", entity_query["query"])
        self.assertEqual(plan[0]["bucket"], "海外增量雷达")
        self.assertFalse(any("nvidia.com" in item["query"] for item in plan))

    def test_eda_and_material_profiles_add_sector_specific_sources_after_entity_split(self) -> None:
        eda = broad.build_query_plan("EDA板块")
        materials = broad.build_query_plan("半导体材料")

        self.assertTrue(any(item["domain_hint"] == "semi.org" for item in eda))
        self.assertTrue(any(item["domain_hint"] == "cspengyuan.com" for item in eda))
        self.assertTrue(any(item["stance"] == "counter" for item in eda))
        material_domains = {item["domain_hint"] for item in materials}
        self.assertTrue({"semi.org", "cpcic.org", "siscmag.com", "nepconasia.com", "fsemi.tech", "infoobs.com"}.issubset(material_domains))

    def test_url_target_cannot_stop_before_minimum_source_and_dimension_coverage(self) -> None:
        screening = {
            "query_kind": "concept",
            "universe": {"source": "AKShare/东方财富概念板块成分股", "coverage_status": "live_theme", "board_names": ["EDA概念"], "total": 3},
            "shortlist": [{"code": "301269", "name": "华大九天", "main_business": "EDA软件", "business_items": ["模拟电路设计EDA"]}],
        }

        def search(_provider: str, query: str, _timeout: float, cache_scope: str = ""):
            del cache_scope
            domain = query.split("site:", 1)[1].split()[0] if "site:" in query else "example.com"
            return "test", [{"title": "EDA市场资料", "url": f"https://{domain}/{abs(hash(query))}", "snippet": "EDA 市场规模 技术路线", "date": ""}], []

        with patch.object(broad.web, "_search", side_effect=search), \
             patch.object(broad.web, "_fetch_page", return_value=("ok", "EDA 市场规模 技术路线 正文")), \
             patch.object(broad.web, "_reset_run_snapshot"), \
             patch.object(broad.web, "_search_cache_batch") as cache_batch:
            cache_batch.return_value.__enter__.return_value = None
            cache_batch.return_value.__exit__.return_value = None
            result = broad.collect_sector_broad_evidence(
                "EDA板块", provider="model", raw_url_limit=2, body_page_limit=1, screening=screening,
            )

        self.assertEqual(result["query_plan"][0]["bucket"], "海外增量雷达")
        self.assertTrue(any(item["domain_hint"] == "semi.org" for item in result["query_plan"]))
        self.assertGreaterEqual(result["audit"]["query_executed"], broad.MIN_QUERY_COVERAGE)
        self.assertGreaterEqual(len(result["audit"]["query_dimension_coverage"]), broad.MIN_DIMENSION_COVERAGE)
        self.assertEqual(result["web_research_status"], "completed")

    def test_generic_summary_never_claims_a_sector_conclusion(self) -> None:
        result = broad.collect_sector_broad_evidence("端侧AI", provider="off")
        self.assertIn("未将空结果解释为行业事实", result["summary"])
        self.assertEqual(result["overseas_event_radar"]["status"], "disabled")

    def test_body_verified_overseas_event_stays_a_domestic_validation_template(self) -> None:
        plan = broad.build_query_plan("mRNA")
        sources = [{
            "bucket": "海外增量雷达",
            "title": "mRNA cancer vaccine Phase 3 trial reports data",
            "url": "https://clinicaltrials.gov/mrna-phase-3",
            "snippet": "Phase 3 study",
            "content_excerpt": "mRNA cancer vaccine Phase 3 pivotal trial clinical data.",
            "fetch_status": "ok",
            "body_scope_match": True,
            "source_role": "海外监管/法定披露",
            "source_tier": "A",
            "date": "2026-08-20",
        }]

        radar = broad._build_overseas_event_radar("mRNA", "", plan, sources)

        self.assertEqual(radar["status"], "body_verified_events")
        event = radar["events"][0]
        self.assertEqual(event["event_type"], "III期/关键临床读出")
        self.assertEqual(event["catalyst_type"], "产业催化")
        self.assertEqual(event["a_share_mapping_priority"], "中")
        self.assertEqual(event["mapping_status"], "待A股主营、收入暴露与订单/利润核验")
        self.assertTrue(any("F10" in item for item in event["a_share_validation"]))

    def test_brave_backend_collects_paginated_pages(self) -> None:
        def brave(_query: str, _timeout: float, count: int, offset: int):
            return [{"url": f"https://example.com/{offset}-{index}"} for index in range(count)] if offset < 2 else []

        with patch.object(broad.web, "_brave_search", side_effect=brave):
            used, rows, errors = broad._search_backend("brave", "端侧AI", 1, "test")
        self.assertEqual(used, "brave")
        self.assertEqual(len(rows), 40)
        self.assertEqual(errors, [])

    def test_brave_adapter_maps_paginated_results_without_exposing_key(self) -> None:
        response = MagicMock()
        response.json.return_value = {"web": {"results": [{
            "title": "端侧 AI 技术路线", "url": "https://example.com/edge-ai",
            "description": "正文摘要", "extra_snippets": ["补充摘要"], "page_age": "2026-08-17",
        }]}}
        session = MagicMock()
        session.get.return_value = response
        with patch.dict("os.environ", {"BRAVE_SEARCH_API_KEY": "test-token"}, clear=False), \
             patch.object(web, "_http_session", return_value=session):
            rows = web._brave_search("端侧AI", 3, count=20, offset=2)
        self.assertEqual(rows[0]["url"], "https://example.com/edge-ai")
        self.assertIn("补充摘要", rows[0]["snippet"])
        self.assertEqual(session.get.call_args.kwargs["params"]["offset"], 2)


if __name__ == "__main__":
    unittest.main()
