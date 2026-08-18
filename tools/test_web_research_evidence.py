from __future__ import annotations

import os
import time
import unittest
from unittest.mock import Mock, patch

from tools.scoring import web_research
from tools.scoring.search_rules import RULES


def _target(key: str, maximum: float = 5) -> dict[str, object]:
    factor_key, subfactor_key = key.split(".", 1)
    return {
        "factor_key": factor_key,
        "subfactor_key": subfactor_key,
        "label": subfactor_key,
        "maximum": maximum,
        "original_status": "需人工确认",
    }


class WebResearchEvidenceTest(unittest.TestCase):
    def test_sector_specific_sources_keep_distinct_evidence_roles(self) -> None:
        self.assertEqual(web_research._source_role("www.semi.org"), ("权威来源", "A"))
        self.assertEqual(web_research._source_role("www.cpcic.org"), ("权威来源", "A"))
        self.assertEqual(web_research._source_role("www.news.cn"), ("财经媒体", "B"))
        self.assertEqual(web_research._source_role("www.cspengyuan.com"), ("行业研究", "B"))

    def test_first_pass_covers_every_gap_by_default(self) -> None:
        targets = [_target(key) for key in list(RULES)[:15]]
        with patch.dict(os.environ, {"MODA_GAP_FIRST_PASS_LIMIT": ""}, clear=False), \
             patch.object(web_research, "MAX_GAP_WORKERS", 1), \
             patch.object(web_research, "MAX_GAP_BUDGET_SECONDS", 30.0), \
             patch.object(web_research, "_search", return_value=("test", [], [])):
            result = web_research._collect_gap_targets("000001", "测试公司", "测试行业", targets, "auto", 0.1)

        self.assertEqual(len(result["web_gap_results"]), len(targets))
        self.assertFalse(any(item["selection_status"] == "skipped" for item in result["web_gap_results"]))
        self.assertEqual(result["search_budget"]["first_pass_mode"], "all_gaps")
        self.assertEqual(result["search_budget"]["targets_selected"], len(targets))

    def test_environment_cap_cannot_skip_an_unresolved_gap(self) -> None:
        targets = [_target("F2.controller_action"), _target("F4.realization")]
        with patch.dict(os.environ, {"MODA_GAP_FIRST_PASS_LIMIT": "1"}, clear=False), \
             patch.object(web_research, "MAX_GAP_WORKERS", 1), \
             patch.object(web_research, "_search", return_value=("test", [], [])):
            result = web_research._collect_gap_targets("000001", "测试公司", "测试行业", targets, "auto", 0.1)

        skipped = [item for item in result["web_gap_results"] if item["selection_status"] == "skipped"]
        self.assertEqual(skipped, [])
        self.assertEqual(result["search_budget"]["first_pass_mode"], "all_gaps")
        self.assertEqual(result["search_budget"]["targets_selected"], len(targets))

    def test_matching_primary_source_and_body_can_supply_an_unverified_web_signal(self) -> None:
        target = _target("F4.realization")
        rows = [
            {"title": "测试公司业绩快讯", "url": "https://news.example.com/a", "snippet": "测试公司营收增长"},
            {"title": "测试公司定期报告", "url": "https://www.cninfo.com.cn/a", "snippet": "测试公司营收增长"},
        ]

        def fetch(url: str, timeout: float) -> tuple[str, str]:
            if "cninfo.com.cn" in url:
                return "ok", "测试公司营收增长，净利润增长，在手订单持续增长。"
            return "ok", "测试公司营收增长。"

        with patch.object(web_research, "_search", return_value=("test", rows, [])), \
             patch.object(web_research, "_fetch_page", side_effect=fetch):
            result = web_research._collect_gap_target(
                "000001", "测试公司", "测试行业", target, "auto", 1,
                time.monotonic() + 3, 3,
            )["gap_result"]

        self.assertEqual(result["status"], "网络命中（未核验）")
        self.assertGreater(result["score"], 0)
        self.assertEqual(result["evidence_validation"]["evidence_type"], "公司事实与利润兑现")
        self.assertGreaterEqual(result["evidence_validation"]["body_verified_count"], 1)
        self.assertEqual(result["evidence_validation"]["source_matched_count"], 1)
        primary = next(item for item in result["evidence"] if "cninfo.com.cn" in item["url"])
        self.assertEqual(primary["source_status"], "来源类型匹配")
        self.assertEqual(primary["body_status"], "正文已核验")

    def test_title_or_summary_without_a_readable_body_cannot_supply_a_score(self) -> None:
        target = _target("F4.realization")
        rows = [{"title": "测试公司业绩预告", "url": "https://www.cninfo.com.cn/a", "snippet": "测试公司营收增长"}]
        with patch.object(web_research, "_search", return_value=("test", rows, [])), \
             patch.object(web_research, "_fetch_page", return_value=("timeout", "")):
            result = web_research._collect_gap_target(
                "000001", "测试公司", "测试行业", target, "auto", 1,
                time.monotonic() + 3, 3,
            )["gap_result"]

        self.assertEqual(result["status"], "搜索结果待正文核验，需人工确认")
        self.assertEqual(result["score"], 0)
        self.assertEqual(result["evidence"][0]["body_status"], "正文未读取")

    def test_exhausted_budget_is_explicitly_manual_confirmation(self) -> None:
        target = _target("F4.realization")
        with patch.object(web_research, "_search") as search:
            result = web_research._collect_gap_target(
                "000001", "测试公司", "测试行业", target, "auto", 1,
                time.monotonic() - 0.01, 1,
            )["gap_result"]

        search.assert_not_called()
        self.assertEqual(result["status"], "搜索预算耗尽，需人工确认")
        self.assertEqual(result["evidence_validation"]["budget_status"], "全局预算耗尽")

    def test_source_policy_query_runs_before_generic_rule_queries(self) -> None:
        target = _target("F4.realization")
        seen: list[str] = []

        def search(provider: str, query: str, timeout: float, cache_scope: str = "") -> tuple[str, list[dict], list[str]]:
            seen.append(query)
            return "test", [], []

        with patch.object(web_research, "MAX_GAP_QUERIES_PER_TARGET", 2), \
             patch.object(web_research, "queries_for", return_value=["通用查询一", "通用查询二"]), \
             patch.object(web_research, "_search", side_effect=search):
            web_research._collect_gap_target(
                "000001", "测试公司", "测试行业", target, "auto", 1,
                time.monotonic() + 3, 3,
            )

        self.assertEqual(len(seen), 2)
        self.assertIn("site:cninfo.com.cn", seen[0])
        self.assertEqual(seen[1], "通用查询一")

    def test_long_chinese_context_is_compacted_and_generic_query_is_retained(self) -> None:
        context = "、".join(["铋相关材料业务", "专用设备制造业务", "半导体材料", "铋相关材料业务"] * 8)
        seen: list[str] = []

        def search(provider: str, query: str, timeout: float, cache_scope: str = "") -> tuple[str, list[dict], list[str]]:
            seen.append(query)
            return "test", [], []

        with patch.object(web_research, "MAX_GAP_QUERIES_PER_TARGET", 3), \
                patch.object(web_research, "queries_for", return_value=["先导基电 订单 产能利用率"]), \
                patch.object(web_research, "_search", side_effect=search):
            web_research._collect_gap_target(
                "600641", "先导基电", context, _target("F4.realization"), "auto", 1,
                time.monotonic() + 3, 3,
            )

        self.assertIn("先导基电 订单 产能利用率", seen)
        self.assertLess(len(seen[0]), 180)
        self.assertEqual(seen[0].count("铋相关材料业务"), 1)

    def test_ddg_anomaly_is_explicitly_blocked(self) -> None:
        response = Mock(status_code=202, text="anomaly")
        with self.assertRaisesRegex(web_research.SearchBackendBlockedError, "anti_bot"):
            web_research._raise_if_search_blocked(response)

    def test_so360_parser_keeps_the_direct_result_url(self) -> None:
        html = (
            '<li class="res-list"><h3 class="res-title"><a href="https://www.so.com/link" '
            'data-mdurl="https://www.cninfo.com.cn/a">先导基电年报</a></h3>'
            '<p class="res-desc">公告摘要</p></li>'
        )
        response = Mock(status_code=200, text=html)
        with patch.object(web_research, "_http_session") as session:
            session.return_value.get.return_value = response
            rows = web_research._so360_search("先导基电", 1)
        self.assertEqual(rows, [{
            "title": "先导基电年报",
            "url": "https://www.cninfo.com.cn/a",
            "snippet": "公告摘要",
            "date": "",
            "engine": "360 Search",
        }])

    def test_explicit_so360_provider_reaches_gap_collection(self) -> None:
        seen: list[str] = []

        def search(provider: str, query: str, timeout: float, cache_scope: str = "") -> tuple[str, list[dict], list[str]]:
            seen.append(provider)
            return "so360", [], []

        with patch.object(web_research, "_search", side_effect=search):
            result = web_research.collect(
                "600641", "先导基电", "铋相关材料", provider="so360", timeout=0.1,
                targets=[_target("F4.realization")],
            )

        self.assertTrue(seen)
        self.assertEqual(set(seen), {"so360"})
        self.assertEqual(len(result["web_gap_results"]), 1)

    def test_empty_so360_result_names_the_actual_backend(self) -> None:
        target = _target("F1.supply_gap")
        with patch.object(web_research, "_search", return_value=("none", [], [])):
            result = web_research._collect_gap_target(
                "600641", "先导基电", "铋相关材料", target, "so360", 1,
                time.monotonic() + 3, 3,
            )["gap_result"]

        self.assertEqual(result["status"], "已搜索未命中")
        self.assertEqual(result["reason"], "360搜索未返回相关结果")

    def test_industry_web_signal_uses_the_configured_provider_when_empty(self) -> None:
        from tools.akshare import industry_prosperity

        providers: list[str] = []

        def search(provider: str, query: str, timeout: float, cache_scope: str = "") -> tuple[str, list[dict], list[str]]:
            providers.append(provider)
            return "none", [], []

        with patch.dict(os.environ, {"MODA_SEARCH_PROVIDER": "so360"}, clear=False), \
             patch.object(web_research, "_search", side_effect=search):
            result = industry_prosperity.collect_web_signal("先导基电", "半导体材料", timeout=0.1)

        self.assertEqual(set(providers), {"so360"})
        self.assertEqual(result["provider"], "so360")

    def test_public_pdf_fetch_for_annual_report_forwards_caller_limits(self) -> None:
        response = Mock()
        response.is_redirect = False
        response.headers = {"content-type": "application/pdf"}
        response.iter_content.return_value = [b"%PDF-test"]
        response.raise_for_status.return_value = None
        session = Mock()
        session.get.return_value = response
        with patch.object(web_research, "_safe_public_url", return_value=True), \
             patch.object(web_research, "_http_session", return_value=session), \
             patch.object(web_research, "_extract_pdf_text", return_value=("ok", "第1页\f第2页")) as extract:
            status, text = web_research.fetch_pdf_document(
                "http://static.cninfo.com.cn/finalpage/2026-08-01/1225452105.PDF",
                3,
                max_pages=80,
                max_text_chars=12_345,
                max_fetch_bytes=67_890,
            )

        self.assertEqual((status, text), ("ok", "第1页\f第2页"))
        self.assertEqual(extract.call_args.args[0], b"%PDF-test")
        self.assertEqual(extract.call_args.kwargs["max_pages"], 80)
        self.assertEqual(extract.call_args.kwargs["max_text_chars"], 12_345)

    def test_industry_gap_runs_domestic_and_overseas_source_layers_before_generic_queries(self) -> None:
        target = _target("F1.era_track")
        seen: list[str] = []

        def search(provider: str, query: str, timeout: float, cache_scope: str = "") -> tuple[str, list[dict], list[str]]:
            seen.append(query)
            return "test", [], []

        with patch.object(web_research, "MAX_GAP_QUERIES_PER_TARGET", 2), \
             patch.object(web_research, "queries_for", return_value=["通用查询一"]), \
             patch.object(web_research, "_search", side_effect=search):
            result = web_research._collect_gap_target(
                "000001", "测试公司", "液冷", target, "auto", 1,
                time.monotonic() + 3, 3,
            )["gap_result"]

        self.assertEqual(len(seen), 2)
        self.assertIn("site:gov.cn", seen[0])
        self.assertIn("site:nvidia.com", seen[1])
        layers = result["evidence_validation"]["source_layers"]
        self.assertEqual([item["id"] for item in layers], ["industry_authority", "overseas_first_party"])

    def test_chokepoint_and_leadership_query_overseas_technology_layer(self) -> None:
        for key in ("F1.chokepoint", "F3.leadership"):
            with self.subTest(key=key):
                target = _target(key)
                seen: list[str] = []

                def search(provider: str, query: str, timeout: float, cache_scope: str = "") -> tuple[str, list[dict], list[str]]:
                    seen.append(query)
                    return "test", [], []

                with patch.object(web_research, "MAX_GAP_QUERIES_PER_TARGET", 3), \
                     patch.object(web_research, "_search", side_effect=search):
                    result = web_research._collect_gap_target(
                        "000001", "测试公司", "液冷", target, "auto", 1,
                        time.monotonic() + 3, 3,
                    )["gap_result"]

                self.assertEqual(len(seen), 3)
                self.assertIn("site:cninfo.com.cn", seen[0])
                self.assertIn("site:nvidia.com", seen[1])
                # The third slot stays available for the generic company query.
                # Site groups are narrowed to one working endpoint per query;
                # they are no longer passed as an unreliable large OR expression.
                self.assertNotIn("site:", seen[2])
                layer_ids = {item["id"] for item in result["evidence_validation"]["source_layers"]}
                self.assertTrue({"overseas_first_party", "technology_authority"}.issubset(layer_ids))

    def test_company_ir_is_traceable_but_cannot_supply_gap_score(self) -> None:
        target = _target("F1.era_track")
        row = {
            "title": "测试公司液冷需求增长",
            "url": "https://investor.nvidia.com/liquid-cooling",
            "snippet": "测试公司液冷需求增长。",
        }
        with patch.object(web_research, "_search", return_value=("test", [row], [])), \
             patch.object(web_research, "_fetch_page", return_value=(
                 "ok", "测试公司所在液冷产业需求增长，技术路线持续演进。"
             )):
            result = web_research._collect_gap_target(
                "000001", "测试公司", "液冷", target, "auto", 1,
                time.monotonic() + 3, 3,
            )["gap_result"]

        self.assertEqual(result["score"], 0)
        self.assertEqual(result["evidence_validation"]["company_ir_clue_count"], 1)
        self.assertTrue(result["evidence"][0]["company_ir_clue"])
        self.assertEqual(result["evidence"][0]["source_layer"], "overseas_first_party")
        self.assertIn("主体待核验", result["evidence"][0]["source_status"])

    def test_chokepoint_external_sources_cannot_replace_domestic_dual_side_crosscheck(self) -> None:
        target = _target("F1.chokepoint", 4)
        rows = [
            {"title": "测试公司国产替代", "url": "https://www.cninfo.com.cn/a", "snippet": "测试公司国产替代"},
            {"title": "液冷技术路线", "url": "https://www.nvidia.com/a", "snippet": "液冷进口依赖"},
        ]

        def fetch(url: str, timeout: float) -> tuple[str, str]:
            if "cninfo.com.cn" in url:
                return "ok", "测试公司在液冷环节推进国产替代，降低进口依赖。"
            return "ok", "液冷技术路线仍有进口依赖，需要国产替代。"

        with patch.object(web_research, "_search", return_value=("test", rows, [])), \
             patch.object(web_research, "_fetch_page", side_effect=fetch):
            result = web_research._collect_gap_target(
                "000001", "测试公司", "液冷", target, "auto", 1,
                time.monotonic() + 3, 3,
            )["gap_result"]

        self.assertEqual(result["status"], "产业链双侧未闭环，需人工确认")
        self.assertEqual(result["score"], 0)
        self.assertIn("overseas_first_party", [
            item["id"] for item in result["evidence_validation"]["source_layers"]
        ])

    def test_chokepoint_needs_company_and_industry_bodies(self) -> None:
        target = _target("F1.chokepoint", 4)
        rows = [{"title": "测试公司国产替代", "url": "https://www.cninfo.com.cn/a", "snippet": "测试公司国产替代"}]
        with patch.object(web_research, "_search", return_value=("test", rows, [])), \
             patch.object(web_research, "_fetch_page", return_value=(
                 "ok", "测试公司在半导体气体环节推进国产替代，降低进口依赖。"
             )):
            result = web_research._collect_gap_target(
                "000001", "测试公司", "半导体气体", target, "auto", 1,
                time.monotonic() + 3, 3,
            )["gap_result"]

        self.assertEqual(result["status"], "产业链双侧未闭环，需人工确认")
        self.assertEqual(result["score"], 0)
        self.assertEqual(result["evidence_validation"]["crosscheck_status"], "需公司与产业双侧确认")

    def test_sector_collector_keeps_all_sections_manual_when_disabled(self) -> None:
        result = web_research.collect_sector_evidence("液冷", provider="off")

        self.assertEqual(result["web_research_status"], "disabled")
        self.assertEqual(result["web_research_provider"], "off")
        self.assertEqual(result["sources"], [])
        self.assertTrue(all(
            section["status"] == "需人工确认"
            for section in result["sections"].values()
        ))

    def test_sector_collector_requires_a_readable_body_before_partial_evidence(self) -> None:
        row = {
            "title": "液冷产业趋势与供需",
            "url": "https://www.gov.cn/liquid-cooling",
            "snippet": "液冷需求增长、订单增加、产能利用率提升。",
        }
        with patch.object(web_research, "MAX_SECTOR_WORKERS", 1), \
             patch.object(web_research, "MAX_SECTOR_QUERIES_PER_SECTION", 1), \
             patch.object(web_research, "_search", return_value=("test", [row], [])), \
             patch.object(web_research, "_fetch_page", return_value=("timeout", "")):
            result = web_research.collect_sector_evidence("液冷", context="数据中心", timeout=1)

        trend = result["sections"]["industry_trend"]
        self.assertEqual(trend["status"], "需人工确认")
        source = next(item for item in result["sources"] if item["section"] == "industry_trend")
        self.assertEqual(source["body_status"], "正文未读取")
        self.assertNotIn("content_excerpt", source)

    def test_sector_collector_needs_two_independent_bodies_for_confirmed_section(self) -> None:
        government_row = {
            "title": "液冷产业趋势需求增长",
            "url": "https://www.gov.cn/liquid-cooling",
            "snippet": "液冷产业需求增长和技术路线演进。",
        }
        overseas_row = {
            "title": "Liquid cooling demand and technology roadmap",
            "url": "https://www.nvidia.com/liquid-cooling",
            "snippet": "液冷需求增长与技术路线。",
        }

        def search(provider: str, query: str, timeout: float, cache_scope: str = "") -> tuple[str, list[dict], list[str]]:
            if not cache_scope.endswith("industry_trend"):
                return "test", [], []
            return ("test", [government_row], []) if "site:gov.cn" in query else ("test", [overseas_row], [])

        def fetch(url: str, timeout: float) -> tuple[str, str]:
            if "nvidia.com" in url:
                return "ok", "液冷需求增长，技术路线正在演进。"
            return "ok", "液冷产业需求增长，技术路线持续演进。"

        with patch.object(web_research, "MAX_SECTOR_WORKERS", 1), \
             patch.object(web_research, "_search", side_effect=search), \
             patch.object(web_research, "_fetch_page", side_effect=fetch):
            result = web_research.collect_sector_evidence("液冷", timeout=1)

        trend = result["sections"]["industry_trend"]
        self.assertEqual(trend["status"], "已验证")
        self.assertEqual(len(trend["evidence_refs"]), 2)
        source_refs = {item["evidence_ref"] for item in result["sources"] if item["section"] == "industry_trend"}
        self.assertTrue(set(trend["evidence_refs"]).issubset(source_refs))


if __name__ == "__main__":
    unittest.main()
