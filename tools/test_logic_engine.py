from __future__ import annotations

from copy import deepcopy
import tempfile
import unittest
from pathlib import Path

from tools import logic_engine


def packet() -> dict:
    return {
        "schema_version": 4,
        "company": {
            "code": "000001",
            "name": "测试股份",
            "main_business": "关键设备与工艺服务",
        },
    }


def valid_case() -> dict:
    case = logic_engine.new_logic_case("000001", "stock", research_packet=packet())
    case["system_change"] = {
        "claim": "客户资本开支从试点进入规模建设。",
        "why_structural": "建设周期跨越多个年度，且需求来自持续扩容。",
        "time_horizon": "未来三年",
        "fact": "公司主营为关键设备与工艺服务。",
        "inference": "若扩容持续，公司订单可能增长。",
        "evidence_refs": ["research_packet.company.main_business"],
    }
    case["chain_map"] = {
        "nodes": ["客户扩容", "设备订单", "公司利润"],
        "edges": [
            {
                "from": "客户扩容", "to": "设备订单", "claim": "扩容形成采购需求",
                "claim_id": "H1", "status": "partial", "support_refs": [], "counter_refs": [],
                "invalidation_conditions": ["客户资本开支转弱"],
            },
            {
                "from": "设备订单", "to": "公司利润", "claim": "交付形成利润",
                "claim_id": "H3", "status": "partial", "support_refs": [], "counter_refs": [],
                "invalidation_conditions": ["订单增长但利润和现金流不改善"],
            },
        ],
    }
    case["bottleneck"] = {
        "link": "客户认证后的关键设备",
        "scarcity_type": "工艺与认证",
        "why_scarce": "验证周期长且切换成本高",
        "replacement_risk": "需人工确认",
        "evidence_refs": [],
    }
    case["profit_pool"] = {
        "payer": "扩容客户",
        "receiver": "关键设备供应商",
        "mechanism": "设备交付后确认收入并形成利润",
        "realization_window": "未来两个报告期",
        "evidence_refs": [],
    }
    for hypothesis in case["hypotheses"]:
        hypothesis["status"] = "partial"
        hypothesis["support_refs"] = ["research_packet.company.main_business"]
    return case


class LogicEngineTest(unittest.TestCase):
    def test_draft_starts_from_logic_not_score(self) -> None:
        case = logic_engine.new_logic_case("000001", "stock", research_packet=packet())
        self.assertEqual(case["status"], "needs_logic")
        self.assertEqual(case["audit"]["score_role"], "evidence_dashboard_only")
        self.assertNotIn("research_score", case)

    def test_concept_case_keeps_disclosed_exposure_as_business_purity(self) -> None:
        screening = {
            "shortlist": [{
                "code": "000021",
                "name": "端侧核心",
                "concept_exposure_tier": "核心主业",
                "business_status": "主营文本线索",
            }]
        }
        case = logic_engine.new_logic_case("端侧AI", "concept", screening=screening)
        self.assertEqual(case["kind"], "concept")
        self.assertEqual(case["company_branches"][0]["business_purity"], "核心主业")

    def test_valid_logic_advances_to_judgment(self) -> None:
        case = logic_engine.validate_logic_case(valid_case())
        self.assertEqual(case["status"], "needs_judgment")

    def test_nonexistent_reference_is_rejected(self) -> None:
        case = valid_case()
        case["system_change"]["evidence_refs"] = ["research_packet.company.not_real"]
        with self.assertRaisesRegex(logic_engine.LogicValidationError, "不存在的证据"):
            logic_engine.validate_logic_case(case)

    def test_verified_hypothesis_requires_support(self) -> None:
        case = valid_case()
        case["hypotheses"][0]["status"] = "verified"
        case["hypotheses"][0]["support_refs"] = []
        with self.assertRaisesRegex(logic_engine.LogicValidationError, "没有支持证据"):
            logic_engine.validate_logic_case(case)

    def test_every_chain_arrow_requires_counterevidence_plan(self) -> None:
        case = valid_case()
        case["chain_map"]["edges"][0].pop("invalidation_conditions")
        with self.assertRaisesRegex(logic_engine.LogicValidationError, "缺少失效条件"):
            logic_engine.validate_logic_case(case)
        case = valid_case()
        case["evidence_requests"] = [
            item for item in case["evidence_requests"] if item["claim_id"] != "H1"
        ]
        with self.assertRaisesRegex(logic_engine.LogicValidationError, "没有对应的支持与反证请求"):
            logic_engine.validate_logic_case(case)

    def test_target_selection_preserves_support_and_counter_queries(self) -> None:
        case = logic_engine.new_logic_case("000001", "stock", research_packet=packet())
        targets = logic_engine.evidence_requests_to_targets(case, ["bottleneck"])
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0]["request_kind"], "bottleneck")
        self.assertEqual(targets[0]["claim_id"], "H2")
        self.assertGreaterEqual(len(targets[0]["queries"]), 2)

    def test_web_result_stays_candidate_evidence(self) -> None:
        case = logic_engine.new_logic_case("000001", "stock", research_packet=packet())
        merged = logic_engine.merge_web_evidence(case, {
            "web_gap_results": [{
                "logic_request_id": "R2",
                "claim_id": "H2",
                "evidence": [{
                    "title": "候选产业线索",
                    "url": "https://example.com/evidence",
                    "provider": "test",
                    "body_status": "仅标题",
                }],
            }],
        })
        evidence = merged["evidence_graph"][0]
        self.assertEqual(evidence["status"], "candidate")
        self.assertEqual(evidence["claim_links"][0]["relation"], "candidate")
        self.assertEqual(next(item for item in merged["evidence_requests"] if item["request_id"] == "R2")["status"], "searched")

    def test_formal_decision_needs_exactly_three_variables(self) -> None:
        case = valid_case()
        case["decision"]["state"] = "等待"
        case["verification"]["top_variables"] = [{"variable": "订单"}, {"variable": "毛利率"}]
        with self.assertRaisesRegex(logic_engine.LogicValidationError, "恰好三项"):
            logic_engine.validate_logic_case(case, require_decision=True)
        case["verification"]["top_variables"].append({"variable": "现金流"})
        ready = logic_engine.validate_logic_case(case, require_decision=True)
        self.assertEqual(ready["status"], "ready")

    def test_contradicted_core_hypothesis_forces_exit(self) -> None:
        case = valid_case()
        case["evidence_graph"] = [{
            "evidence_id": "E-counter",
            "fact": "需求连续下降",
            "claim_links": [{"claim_id": "H1", "relation": "contradicts", "strength": "high"}],
        }]
        case["hypotheses"][0]["status"] = "contradicted"
        case["hypotheses"][0]["counter_refs"] = ["E-counter"]
        case["decision"]["state"] = "等待"
        case["verification"]["top_variables"] = [
            {"variable": "订单"}, {"variable": "毛利率"}, {"variable": "现金流"},
        ]
        with self.assertRaisesRegex(logic_engine.LogicValidationError, "只能为退出"):
            logic_engine.validate_logic_case(case, require_decision=True)

    def test_persistence_keeps_current_and_history(self) -> None:
        case = valid_case()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = logic_engine.save_logic_case(root, case)
            changed = deepcopy(case)
            changed["system_change"]["claim"] = "客户资本开支已进入规模建设。"
            logic_engine.save_logic_case(root, changed)
            self.assertTrue(Path(paths["case_path"]).exists())
            self.assertTrue(Path(paths["report_path"]).exists())
            history = list((Path(paths["case_path"]).parent / "history").glob("*.json"))
            self.assertEqual(len(history), 1)

    def test_report_leads_with_logic_and_keeps_scores_in_audit(self) -> None:
        report = logic_engine.render_logic_case(valid_case())
        self.assertLess(report.index("## 为什么这个方向"), report.index("## 证据审计"))
        self.assertIn("## 下一步补什么证据", report)
        self.assertEqual(report.count("F1-F6"), 1)


if __name__ == "__main__":
    unittest.main()
