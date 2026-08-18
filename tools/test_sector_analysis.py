from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import tools.sector_analysis as sector_analysis
from tools.sector_analysis import (
    SECTOR_JUDGMENT_CONTRACT,
    SectorValidationError,
    build_sector_judgment,
    main,
    render_sector_judgment,
    validate_sector_judgment,
)


def _factor(key: str, status: str, reason: str) -> dict:
    return {"key": key, "status": status, "reason": reason, "sources": ["test-source"]}


def _artifact(
    code: str,
    name: str,
    *,
    direct_business: bool = True,
    realized: bool = True,
    noisy_supply_reason: bool = False,
) -> dict:
    source_keys = (
        "chain_name", "chain_stage", "main_business", "business_items",
        "supply_evidence_count", "supply_category_count", "supply_tightening",
        "supply_cr3", "capacity_expansion_cycle_years", "order_growth",
        "revenue_yoy", "profit_yoy", "operating_cashflow", "price_percentile_3y",
        "market_congestion", "pe_ttm", "pb", "concepts",
    )
    evidence = {
        "code": code,
        "name": name,
        "chain_name": "液冷产业链" if direct_business else "需人工确认",
        "chain_stage": "upstream" if direct_business else "需人工确认",
        "main_business": "服务器液冷冷板和CDU" if direct_business else "其他业务",
        "business_items": ["液冷冷板"] if direct_business else ["其他产品"],
        "supply_evidence_count": 2,
        "supply_category_count": 2,
        "supply_tightening": True,
        "supply_cr3": 72,
        "capacity_expansion_cycle_years": 3.5,
        "order_growth": 0.32 if realized else -0.05,
        "revenue_yoy": 0.21 if realized else -0.06,
        "profit_yoy": 0.28 if realized else -0.12,
        "operating_cashflow": 120.0 if realized else -5.0,
        "price_percentile_3y": 0.42,
        "market_congestion": 0.35,
        "pe_ttm": 31.0,
        "pb": 3.2,
        "concepts": ["液冷"],
        "metric_sources": {key: ["test-source"] for key in source_keys},
    }
    return {
        "evidence": evidence,
        "scorecard": {
            "coverage": 0.82,
            "factors": [
                {"subfactors": [
                    _factor("era_track", "已验证", "高功率机柜带动液冷基础设施需求。"),
                    _factor("capex_wave", "已验证", "数据中心资本开支存在兑现证据。"),
                    _factor("supply_gap", "已验证", "duckduckgo:ConnectionError" if noisy_supply_reason else "两类独立供需证据均显示趋紧。"),
                    _factor("chokepoint", "已验证", "冷板工艺与客户认证存在壁垒。"),
                    _factor("upstream", "已验证", "候选处于关键部件环节。"),
                ]},
                {"subfactors": [
                    _factor("leadership", "已验证", "已核验核心供应商线索。"),
                    _factor("business_match", "已验证" if direct_business else "需人工确认", "主营与液冷产业链匹配。" if direct_business else "没有主营嵌入证据。"),
                    _factor("profit_position", "已验证", "候选位于关键部件利润位置。"),
                    _factor("realization", "已验证", "订单、收入和利润均有可核验数据。"),
                ]},
                {"subfactors": [
                    _factor("expectation_gap", "已验证", "价格和基本面信息可交叉核验。"),
                    _factor("price_position", "已验证", "三年价格分位可核验。"),
                ]},
            ],
        },
    }


def _sector_evidence() -> dict:
    return {
        "industry_trend": {"status": "已验证", "summary": "高功率机柜使液冷由可选走向基础设施。", "evidence_refs": ["industry-report:trend"]},
        "supply_demand": {"status": "已验证", "summary": "订单、产能和认证周期构成两类独立供需证据。", "evidence_refs": ["industry-report:supply"]},
        "profit_pool": {"status": "已验证", "summary": "关键部件与系统集成的议价权有行业级核验。", "evidence_refs": ["industry-report:profit-pool"]},
        "scarcity": {"status": "已验证", "summary": "认证周期和工艺良率限制短期新增供给。", "evidence_refs": ["industry-report:scarcity"]},
        "profit_realization": {"status": "已验证", "summary": "行业订单、收入和利润有同向验证。", "evidence_refs": ["industry-report:realization"]},
        "market_pricing": {"status": "已验证", "summary": "行业估值、价格位置和拥挤度已经交叉核验。", "evidence_refs": ["industry-report:pricing"]},
    }


def _collected_sector_evidence() -> dict:
    return {
        "sector": "液冷",
        "web_research_status": "ready",
        "web_research_provider": "test-provider",
        "queries": ["液冷 供需"],
        "sources": [{"url": "https://example.test/liquid-cooling", "source_tier": "一级"}],
        "search_budget": {"used": 1, "limit": 3},
        "errors": [],
        "sections": _sector_evidence(),
    }


def _load_companion_sector_entry():
    path = Path(__file__).resolve().parents[1] / "moda-companion" / "scripts" / "analyze_sector.py"
    spec = importlib.util.spec_from_file_location("test_companion_sector_entry", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _research_packet() -> dict:
    return {
        "schema_version": 4,
        "packet_role": "collector_only",
        "company": {
            "code": "300001", "name": "研究包甲", "main_business": "液冷关键部件", "business_items": ["冷板"],
        },
        "industry": {"chain_name": "液冷产业链", "chain_stage": "upstream"},
        "realization": {
            "business_match_status": "已验证", "business_match_reason": "主营匹配液冷关键部件。",
            "realization_status": "已验证", "realization_reason": "订单、收入和利润已经出现正向事实。",
            "revenue_yoy": {"value": 0.2, "sources": ["packet-source"]},
            "profit_yoy": {"value": 0.3, "sources": ["packet-source"]},
            "order_growth": {"value": 0.4, "sources": ["packet-source"]},
            "operating_cashflow": {"value": 60.0, "sources": ["packet-source"]},
        },
        "valuation": {
            "pe_ttm": {"value": 25.0, "sources": ["packet-source"]},
            "pb": {"value": 2.0, "sources": ["packet-source"]},
            "price_percentile_3y": {"value": 0.4, "sources": ["packet-source"]},
        },
        "system_change": {
            "era_track": {"status": "已验证", "reason": "液冷渗透率提升。"},
            "capital_expenditure": {"status": "已验证", "reason": "数据中心扩产。"},
            "supply_demand": {"status": "已验证", "reason": "供需证据交叉验证。"},
        },
        "bottleneck": {
            "upstream_position": {"status": "已验证", "reason": "关键部件。"},
            "chokepoint": {"status": "已验证", "reason": "工艺壁垒。"},
            "leadership": {"status": "已验证", "reason": "客户认证。"},
        },
        "market_stage": {
            "expectation_gap_status": "已验证", "expectation_gap_reason": "预期差待持续跟踪。",
            "price_position_status": "已验证", "price_position_reason": "价格位置可核验。",
        },
        "research": {"coverage": 0.8},
    }


class SectorAnalysisTest(unittest.TestCase):
    def test_sample_contract_is_valid(self) -> None:
        validate_sector_judgment(SECTOR_JUDGMENT_CONTRACT)

    def test_candidate_evidence_does_not_overclaim_sector_state(self) -> None:
        output = build_sector_judgment("液冷", [_artifact("000001", "兑现候选"), _artifact("000002", "主题候选", direct_business=False, realized=False)])
        self.assertEqual(output["sector_state"], "等待验证")
        self.assertTrue(output["not_stock_decision"])
        self.assertEqual(output["candidate_comparison"]["comparison_type"], "evidence_ordering_not_score")
        self.assertEqual(output["candidate_comparison"]["candidates"][0]["name"], "兑现候选")
        self.assertEqual(output["candidate_comparison"]["candidates"][1]["candidate_class"], "主题关联")
        for section in output["sections"].values():
            self.assertTrue(section["evidence_refs"])
            self.assertIsInstance(section["unknowns"], list)

    def test_direct_industry_evidence_can_support_research_state(self) -> None:
        output = build_sector_judgment("液冷", [_artifact("000001", "兑现候选")], sector_evidence=_sector_evidence())
        self.assertEqual(output["sector_state"], "值得研究")
        self.assertEqual(output["sections"]["profit_pool"]["status"], "已验证")
        self.assertEqual(output["candidate_comparison"]["candidates"][0]["profit_realization"], "已兑现")

    def test_default_does_not_collect_sector_evidence(self) -> None:
        with patch.object(sector_analysis, "_collect_sector_evidence", side_effect=AssertionError("must stay offline")):
            output = build_sector_judgment("液冷", [_artifact("000001", "兑现候选")])
        self.assertFalse(output["sector_evidence_collection"]["requested"])
        self.assertEqual(output["sector_evidence_collection"]["web_research_status"], "not_requested")

    def test_collect_sector_injects_sections_and_user_evidence_wins(self) -> None:
        provided = {"sections": _sector_evidence()}
        provided["sections"]["supply_demand"] = {
            "status": "需人工确认",
            "summary": "用户提供的供需材料尚未完成核验。",
            "evidence_refs": ["user:supply"],
            "unknowns": [{"item": "供需变化", "reason": "等待原始报告核验。", "evidence_refs": ["user:supply"]}],
        }
        with patch.object(sector_analysis, "_collect_sector_evidence", return_value=_collected_sector_evidence()) as collect:
            output = build_sector_judgment(
                "液冷",
                [_artifact("000001", "兑现候选")],
                sector_evidence=provided,
                collect_sector=True,
                sector_context="高功率机柜",
                sector_provider="test-provider",
            )
        collect.assert_called_once_with("液冷", context="高功率机柜", provider="test-provider", timeout=12)
        self.assertEqual(output["sections"]["supply_demand"]["summary"], "用户提供的供需材料尚未完成核验。")
        self.assertEqual(output["sections"]["industry_trend"]["status"], "已验证")
        self.assertEqual(output["sector_state"], "等待验证")
        self.assertTrue(output["sector_evidence_collection"]["requested"])
        self.assertEqual(output["sector_evidence_collection"]["web_research_status"], "ready")
        self.assertEqual(output["sector_evidence_collection"]["web_research_provider"], "test-provider")
        self.assertEqual(output["sector_evidence_collection"]["queries"], ["液冷 供需"])

    def test_real_collector_off_mode_keeps_candidate_facts_and_records_gap(self) -> None:
        baseline = build_sector_judgment("液冷", [_artifact("000001", "兑现候选")])
        output = build_sector_judgment(
            "液冷",
            [_artifact("000001", "兑现候选")],
            collect_sector=True,
            sector_provider="off",
        )
        self.assertTrue(output["sector_evidence_collection"]["requested"])
        self.assertEqual(output["sector_evidence_collection"]["web_research_status"], "disabled")
        self.assertEqual(set(output["sector_evidence_collection"]["collected_sections"]), set(_sector_evidence()))
        self.assertEqual(output["sector_evidence_collection"]["effective_sections"], [])
        self.assertEqual(output["sector_state"], "等待验证")
        self.assertEqual(
            {key: section["status"] for key, section in output["sections"].items()},
            {key: section["status"] for key, section in baseline["sections"].items()},
        )

    def test_unresolved_collector_sections_do_not_replace_candidate_aggregation(self) -> None:
        collected = _collected_sector_evidence()
        collected["web_research_status"] = "completed"
        collected["sections"] = {
            key: {
                "status": "需人工确认",
                "summary": "本次行业搜索未形成可用事实，需人工确认。",
                "evidence_refs": [f"query:{key}"],
            }
            for key in _sector_evidence()
        }
        baseline = build_sector_judgment("液冷", [_artifact("000001", "兑现候选")])
        with patch.object(sector_analysis, "_collect_sector_evidence", return_value=collected):
            output = build_sector_judgment("液冷", [_artifact("000001", "兑现候选")], collect_sector=True)
        self.assertEqual(output["sector_evidence_collection"]["web_research_status"], "completed")
        self.assertEqual(output["sector_evidence_collection"]["effective_sections"], [])
        self.assertEqual(output["sector_state"], baseline["sector_state"])

    def test_candidate_comparison_exposes_valuation_safety_and_peer_limit(self) -> None:
        output = build_sector_judgment("液冷", [_artifact("000001", "兑现候选")])
        row = output["candidate_comparison"]["candidates"][0]
        self.assertEqual(row["valuation_snapshot"]["pe_ttm"]["value"], 31.0)
        self.assertTrue(row["safety_margin"]["evidence_refs"])
        self.assertIn("不能证明", row["why_not_peer"]["summary"])
        rendered = render_sector_judgment(output)
        self.assertIn("## 为什么现在看这个行业", rendered)
        self.assertIn("## 钱最终流向哪里", rendered)
        self.assertIn("## 谁先把产业逻辑变成业绩", rendered)
        self.assertIn("## 市场在交易什么", rendered)
        self.assertIn("## 候选公司：为什么是它，而不是同行", rendered)
        self.assertIn("为什么不是同行 / 为何不优先", rendered)

    def test_schema_v4_research_packet_is_accepted(self) -> None:
        output = build_sector_judgment("液冷", [_research_packet()])
        member = output["candidate_comparison"]["candidates"][0]
        self.assertEqual(member["code"], "300001")
        self.assertEqual(member["profit_realization"], "已兑现")

    def test_no_input_downgrades_deterministically(self) -> None:
        output = build_sector_judgment("未知主题", [])
        self.assertEqual(output["sector_state"], "暂不优先")
        self.assertFalse(output["candidate_comparison"]["candidates"])
        self.assertTrue(all(section["status"] == "需人工确认" for section in output["sections"].values()))

    def test_unsourced_positive_numbers_do_not_become_profit_realization(self) -> None:
        output = build_sector_judgment("液冷", [{
            "code": "000003", "name": "无来源候选", "chain_name": "液冷产业链", "chain_stage": "upstream",
            "profit_yoy": 0.5, "operating_cashflow": 100.0, "order_growth": 0.6,
        }])
        row = output["candidate_comparison"]["candidates"][0]
        self.assertEqual(row["profit_realization"], "需人工确认")
        self.assertEqual(output["sector_state"], "暂不优先")

    def test_renderer_hides_raw_search_errors(self) -> None:
        output = build_sector_judgment("液冷", [_artifact("000001", "兑现候选", noisy_supply_reason=True)])
        rendered = render_sector_judgment(output)
        self.assertIn("莫大 Agent 板块判断", rendered)
        self.assertNotIn("duckduckgo", rendered.lower())
        self.assertIn("候选公司：为什么是它，而不是同行", rendered)

    def test_validation_rejects_missing_evidence_refs(self) -> None:
        output = build_sector_judgment("液冷", [_artifact("000001", "兑现候选")])
        output["sections"]["supply_demand"]["evidence_refs"] = []
        with self.assertRaises(SectorValidationError):
            validate_sector_judgment(output)

    def test_cli_writes_markdown_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_path = root / "candidate.json"
            output_path = root / "sector.md"
            input_path.write_text(json.dumps(_artifact("000001", "兑现候选"), ensure_ascii=False), encoding="utf-8")
            result = main([
                "--sector", "液冷", "--input", str(input_path), "--format", "markdown", "--output", str(output_path),
            ])
            self.assertEqual(result, 0)
            self.assertIn("## 为什么现在看这个行业", output_path.read_text(encoding="utf-8"))

    def test_cli_collect_sector_is_explicit_and_serializes_collection_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_path = root / "candidate.json"
            output_path = root / "sector.json"
            input_path.write_text(json.dumps(_artifact("000001", "兑现候选"), ensure_ascii=False), encoding="utf-8")
            with patch.object(sector_analysis, "_collect_sector_evidence", return_value=_collected_sector_evidence()) as collect:
                result = main([
                    "--sector", "液冷", "--input", str(input_path), "--collect-sector",
                    "--sector-context", "高功率机柜", "--sector-provider", "test-provider",
                    "--output", str(output_path),
                ])
            self.assertEqual(result, 0)
            collect.assert_called_once_with("液冷", context="高功率机柜", provider="test-provider", timeout=12)
            payload = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["sector_evidence_collection"]["web_research_status"], "ready")

    def test_companion_entry_passes_collect_sector_and_evidence(self) -> None:
        entry = _load_companion_sector_entry()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            card_path = root / "candidate.json"
            card_path.write_text(json.dumps(_artifact("000001", "兑现候选"), ensure_ascii=False), encoding="utf-8")

            class FakeAShare:
                @staticmethod
                def find_moda_root(_value=None):
                    return root

                @staticmethod
                def _resolve_stock(_root, _query):
                    return "000001", "兑现候选"

                @staticmethod
                def load_analysis(_root, _code, _name):
                    return {"collector_status": "ready", "scorecard_path": str(card_path)}

            with patch.object(entry, "_load_a_share_module", return_value=FakeAShare), \
                 patch.object(entry, "_load_sector_module", return_value=sector_analysis), \
                 patch.object(sector_analysis, "_collect_sector_evidence", return_value=_collected_sector_evidence()) as collect:
                result = entry.analyze_sector(
                    "液冷",
                    ["000001"],
                    collect_sector=True,
                    sector_context="测试上下文",
                    sector_provider="test-provider",
                    sector_evidence={"sections": _sector_evidence()},
                )
        collect.assert_called_once_with("液冷", context="测试上下文", provider="test-provider", timeout=12)
        self.assertTrue(result["sector_evidence_collection"]["requested"])
        self.assertIn("安全边际事实", result["markdown"])


if __name__ == "__main__":
    unittest.main()
