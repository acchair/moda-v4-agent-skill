from __future__ import annotations

import copy
import unittest

import pandas as pd

from tools.akshare.finance_data import _history_quantiles
from tools.scoring import grader
from tools.scoring.model import score_evidence
from tools.scoring.thesis import (
    ThesisValidationError,
    build_thesis_context,
    build_valuation_scenarios,
    validate_thesis_output,
)
from tools.test_pipeline_efficiency import full_evidence


def v2_evidence(*, peers: bool = False) -> dict:
    evidence = full_evidence()
    evidence.update({
        "latest_price": 30.0,
        "pe_history_quantiles_5y": {"q20": 5.0, "q50": 15.0, "q80": 30.0, "samples": 1000},
        "pb_history_quantiles_5y": {"q20": 0.6, "q50": 1.2, "q80": 2.4, "samples": 1000},
        "price_history_quantiles_3y": {"q20": 20.0, "q50": 30.0, "q80": 45.0, "samples": 720},
    })
    if peers:
        evidence["peer_comparison"] = [
            {"code": "000001", "name": "同行甲", "status": "已验证", "revenue_yoy": 0.1},
            {"code": "000002", "name": "同行乙", "status": "已验证", "revenue_yoy": 0.15},
        ]
    evidence["metric_sources"].update({key: ["test"] for key in (
        "latest_price", "pe_history_quantiles_5y", "pb_history_quantiles_5y",
        "price_history_quantiles_3y", "peer_comparison",
    )})
    return evidence


def agent_payload(state: str = "等待") -> dict:
    return {
        "schema_version": 4,
        "one_sentence": "产业和利润改善值得研究，但同行优势与预期差仍需验证。",
        "core_contradiction": {
            "statement": "市场可能先交易产业改善，但公司利润兑现仍只完成第一步。",
            "market_stage": "市场正在交易产业改善预期。",
            "fundamental_stage": "收入和部分经营指标已有改善，利润兑现仍需连续验证。",
            "fact": "产业相关经营指标已出现改善，但订单到利润的首个箭头仍是部分验证。",
            "meaning": "市场预期与公司兑现速度可能不同步。",
            "decision_impact": "不能把产业预期直接当成公司已完成反转。",
            "evidence_refs": ["realization.revenue_yoy", "realization.profit_yoy", "realization.order_growth"],
        },
        "industry_positioning": {
            "industry_chain": "产业扩张到关键设备与工艺服务的链条。",
            "demand_driver": "客户资本开支和项目启动形成设备与服务需求。",
            "company_link": "公司向产业链关键环节提供设备与工艺支持。",
            "profit_path": "客户扩产先形成订单，再转为收入、利润和现金流。",
            "fact": "主营与产业链位置均有本次事实包支持。",
            "meaning": "公司是否受益取决于资本开支能否传导到自身订单。",
            "decision_impact": "先验证订单传导，再判断利润弹性。",
            "evidence_refs": ["company.main_business", "industry.chain_name", "industry.chain_stage"],
        },
        "thesis": {
            "statement": "产业扩张若继续传到订单、利润和现金流，公司可能获得重新定价。",
            "time_horizon": "未来两个报告期",
            "key_drivers": ["行业资本开支", "订单兑现", "利润和现金流"],
            "required_conditions": ["订单持续增长", "利润和现金流同步改善"],
            "invalidation_conditions": ["订单和利润连续转弱"],
            "fact": "资本开支、订单、收入和利润的关键事实均已纳入本次研究包。",
            "meaning": "产业改善只有传到订单并沉淀为利润时才构成重新定价基础。",
            "decision_impact": "假设尚待下一期经营数据持续验证。",
            "evidence_refs": ["company.main_business", "system_change.capital_expenditure"],
        },
        "why_watch": {
            "why_now": "产业经营指标已有改善，且公司订单到利润的传导正进入可验证窗口。",
            "industry_change": "资本开支和供需证据显示产业处于改善阶段。",
            "company_position": "公司向产业链关键环节提供设备与工艺支持。",
            "scarcity": "主营与产业链位置匹配，但相对同行的稀缺性仍需验证。",
            "profit_pool": "客户扩产带来的设备、工艺服务与后续项目交付利润。",
            "fact": "产业和公司位置的可验证事实同时存在。",
            "meaning": "它进入视野不是因为标签，而是因为产业变化可能向公司订单传导。",
            "decision_impact": "应优先跟踪订单和利润传导，而不是先下结论。",
            "evidence_refs": ["company.main_business", "industry.chain_name", "system_change.capital_expenditure"],
        },
        "reversal_judgment": {
            "past_pressure": "此前产业扩张和公司订单的持续性不足，市场难以确认利润兑现。",
            "marginal_change": "近期资本开支、收入和订单相关证据开始出现改善。",
            "reversal_stage": "产业与收入端已有线索，订单到利润和现金流仍处于部分验证。",
            "remaining_gap": "需要连续报告期证明订单质量、利润率与现金流同步改善。",
            "fact": "本次事实包显示订单、收入、利润和现金流的验证强度并不一致。",
            "meaning": "这不是静态好坏判断，而是反转链条只走到中段。",
            "decision_impact": "在利润和现金流确认前，不能把边际变化直接等同于反转完成。",
            "evidence_refs": ["realization.order_growth", "realization.revenue_yoy", "realization.profit_yoy", "realization.operating_cashflow"],
        },
        "business_judgment": {
            "quality": "合格",
            "summary": "公司向产业链关键环节提供设备，客户扩产可能转成收入。",
            "fact": "主营和财务安全相关事实已得到研究包支持。",
            "meaning": "业务能够承接产业投资，但护城河仍需与同行对比确认。",
            "decision_impact": "可继续研究，但不能仅凭产业标签升格。",
            "evidence_refs": ["company.main_business", "survival.financial_safety"],
        },
        "industry_judgment": {
            "timing": "正在改善",
            "summary": "资本开支和供需证据显示行业处于改善阶段。",
            "fact": "资本开支与供需相关指标给出改善线索。",
            "meaning": "行业可能提供订单恢复的外部条件。",
            "decision_impact": "行业改善只是前提，仍需公司订单确认。",
            "evidence_refs": ["system_change.era_track", "system_change.capital_expenditure"],
        },
        "profit_judgment": {
            "state": "开始兑现",
            "summary": "订单、收入、利润和现金流已经出现正向证据。",
            "fact": "收入、利润和现金流相关事实已进入研究包，订单传导仍是部分验证。",
            "meaning": "利润兑现已有起点，但持续性尚未坐实。",
            "decision_impact": "需要把下一期利润和现金流作为升级门槛。",
            "evidence_refs": ["realization.realization_reason", "realization.operating_cashflow"],
        },
        "causal_chain": [
            {"from": "行业资本开支", "to": "公司订单", "claim": "扩产需求带来设备订单", "status": "部分验证", "evidence_refs": ["system_change.capital_expenditure", "realization.order_growth"]},
            {"from": "公司订单", "to": "收入", "claim": "订单开始转成收入", "status": "已验证", "evidence_refs": ["realization.order_growth", "realization.revenue_yoy"]},
            {"from": "收入", "to": "利润和现金流", "claim": "增长已经开始形成利润和现金", "status": "已验证", "evidence_refs": ["realization.profit_yoy", "realization.operating_cashflow"]},
        ],
        "causal_breakpoint": {
            "key_link": "行业资本开支 -> 公司订单",
            "reason": "客户扩产是否持续传导到公司订单仍是部分验证。",
            "closure_conditions": ["后续报告确认订单持续增长", "订单质量继续转为利润和现金流"],
            "fact": "资本开支已改善，但订单增长尚未完成全部验证。",
            "meaning": "这是产业景气能否转成公司业绩的第一道门。",
            "decision_impact": "断点未闭环前，不将产业改善直接上调为高状态。",
            "evidence_refs": ["system_change.capital_expenditure", "realization.order_growth"],
        },
        "why_this_company": {
            "profit_pool": "核心设备与工艺环节",
            "advantages": ["主营与产业链位置匹配"],
            "weaknesses": ["直接同行经营质量对照仍不足"],
            "peer_verdict": "无法证明优于同行",
            "selection_conclusion": "产业方向可研究，但现有证据不足以证明公司优于直接同行。",
            "peer_comparison": [],
            "fact": "公司主营可确认，但已验证同行比较不足。",
            "meaning": "行业变好不自动等于当前公司是最优受益者。",
            "decision_impact": "没有同业优势前，不进入优选结论。",
            "evidence_refs": ["company.main_business", "bottleneck.leadership", "peers"],
        },
        "market_expectation": {
            "gap_state": "预期合理",
            "market_narrative": "市场正在交易产业改善能否带来公司业绩修复。",
            "market_stage": "产业预期先行，经营兑现仍在验证。",
            "fundamental_stage": "订单到利润和现金流已有部分证据，尚未形成连续确认。",
            "market_vs_fundamentals": "市场可能先于公司利润兑现反应，核心是后续能否补齐经营确认。",
            "known": "市场可见产业相关经营指标与公司收入改善线索。",
            "priced_in": "市场已经交易部分业绩改善。",
            "unpriced": "利润和现金流改善是否可持续仍有分歧。",
            "mispriced": "若市场把产业预期直接当作公司反转完成，可能高估兑现速度。",
            "fact": "市场阶段与经营兑现证据同时存在，但强度不同。",
            "meaning": "估值位置不能代替对市场叙事和兑现节奏的判断。",
            "decision_impact": "先验证公司阿尔法，避免把板块贝塔当作公司优势。",
            "evidence_refs": ["market_stage.expectation_gap_reason", "a_share_signals.profit_yoy"],
        },
        "driver_judgment": {
            "stage": "业绩",
            "summary": "当前已有业绩支撑，不只是题材。",
            "fact": "利润和订单相关指标已纳入本次事实包。",
            "meaning": "驱动需要同时接受订单与利润兑现检验。",
            "decision_impact": "不把单纯情绪或技术强势当成核心依据。",
            "evidence_refs": ["a_share_signals.profit_yoy", "a_share_signals.order_growth"],
        },
        "bull_case": {
            "summary": "订单持续转成利润和现金流时，公司可能获得估值回归。",
            "conditions": ["订单增长", "现金流同步改善"],
            "fact": "订单、利润和现金流是已定义的验证变量。",
            "meaning": "产业改善若被公司经营数据连续确认，才可能形成公司阿尔法。",
            "decision_impact": "这是升级状态的基本面条件。",
            "evidence_refs": ["realization.order_growth", "realization.operating_cashflow"],
        },
        "base_case": {
            "summary": "产业改善延续，但利润和现金流只缓慢修复，公司仍以板块贝塔为主。",
            "conditions": ["收入改善延续", "利润修复慢于收入"],
            "fact": "收入、利润与现金流的验证强度并不一致。",
            "meaning": "公司可能受益，但尚不足以证明独立超额收益。",
            "decision_impact": "维持等待并继续比较同行。",
            "evidence_refs": ["realization.revenue_yoy", "realization.profit_yoy", "realization.operating_cashflow"],
        },
        "bear_case": {
            "summary": "行业扩产若放缓，收入改善可能无法持续转成利润。",
            "conditions": ["资本开支转弱", "利润与现金流背离"],
            "fact": "资本开支、利润和现金流均是因果链的必要验证项。",
            "meaning": "产业预期一旦不能转成订单和现金，重新定价逻辑会失效。",
            "decision_impact": "触发时应下调研究优先级或退出。",
            "evidence_refs": ["system_change.capital_expenditure", "realization.operating_cashflow"],
        },
        "valuation_interpretation": {
            "conclusion": "历史估值情景提供赔率参考，但不是目标价。",
            "fact": "估值情景由采集器依据历史分位生成。",
            "meaning": "它只能检验当前价格是否值得等待基本面验证。",
            "decision_impact": "不以历史分位替代产业和公司判断。",
            "evidence_refs": ["valuation_scenarios"],
        },
        "decision": {
            "state": state,
            "rationale": "核心逻辑可研究，但同行优势和预期差还不够。",
            "why_not_higher_state": "订单到利润和现金流的闭环、直接同行优势和预期差仍未全部验证。",
            "fact": "当前因果链仍保留部分验证的箭头。",
            "meaning": "市场与基本面错位尚不能确认是机会而非提前透支。",
            "decision_impact": "维持当前状态，等待经营验证。",
            "evidence_refs": ["decision_gates", "valuation_scenarios"],
        },
        "verification": {
            "next_event": "下一份定期报告",
            "window": "未来两个报告期",
            "upgrade_if": "订单、利润和现金流继续同步改善。",
            "downgrade_if": "订单或利润连续转弱。",
            "top_variables": [
                {"variable": "订单持续性", "why": "订单决定产业变化能否传导到公司。", "window": "未来两个报告期", "upgrade_signal": "订单连续改善", "downgrade_signal": "订单转弱", "evidence_refs": ["realization.order_growth"]},
                {"variable": "利润与现金流", "why": "验证收入质量和反转兑现。", "window": "未来两个报告期", "upgrade_signal": "利润和现金流同步改善", "downgrade_signal": "利润或现金流转弱", "evidence_refs": ["realization.profit_yoy", "realization.operating_cashflow"]},
                {"variable": "同行优势与预期差", "why": "区分板块贝塔和公司阿尔法。", "window": "未来两个报告期", "upgrade_signal": "两家直接同行比较和预期差获得验证", "downgrade_signal": "同行持续领先或市场预期继续抬升", "evidence_refs": ["peers", "market_stage.expectation_gap_reason"]},
            ],
            "fact": "本次事实包明确了订单、利润现金流和同行预期差三个关键缺口。",
            "meaning": "验证变量决定本轮判断能否升级或证伪。",
            "decision_impact": "下一步只跟踪能改变判断的变量。",
            "evidence_refs": ["realization.order_growth", "realization.operating_cashflow"],
        },
        "state_transition": {
            "previous_state": "首次判断",
            "current_state": state,
            "reason": "首次按V2建立投资假设和验证计划。",
        },
        "confidence": "中",
        "expression_status": "agent_generated",
    }


def verified_peer_rows() -> list[dict]:
    return [
        {
            "company": "同行甲",
            "trend_exposure": "直接受益于同一轮产业资本开支。",
            "business_purity": "核心业务聚焦该产业环节。",
            "industry_position": "已验证直接同行。",
            "core_barrier": "客户认证与项目经验。",
            "profit_realization": "收入和利润均有可比披露。",
            "market_cap_elasticity": "市值体量较大，弹性需结合估值复核。",
            "overseas_risk": "海外收入和政策风险需持续复核。",
            "crowding_and_expectation": "市场预期已部分反映。",
            "largest_flaw": "与目标公司相比的订单质量仍需同口径核验。",
            "valuation_and_odds": "估值与赔率需按同口径比较。",
            "current_choice": "作为直接可比对象，不先形成优选。",
            "evidence_refs": ["peers.0"],
        },
        {
            "company": "同行乙",
            "trend_exposure": "直接受益于同一轮产业资本开支。",
            "business_purity": "核心业务聚焦该产业环节。",
            "industry_position": "已验证直接同行。",
            "core_barrier": "客户认证与项目经验。",
            "profit_realization": "收入和利润均有可比披露。",
            "market_cap_elasticity": "市值体量较大，弹性需结合估值复核。",
            "overseas_risk": "海外收入和政策风险需持续复核。",
            "crowding_and_expectation": "市场预期已部分反映。",
            "largest_flaw": "与目标公司相比的订单质量仍需同口径核验。",
            "valuation_and_odds": "估值与赔率需按同口径比较。",
            "current_choice": "作为直接可比对象，不先形成优选。",
            "evidence_refs": ["peers.1"],
        },
    ]


class ValuationScenarioTest(unittest.TestCase):
    def test_history_quantiles_use_twenty_fifty_eighty_percentiles(self) -> None:
        frame = pd.DataFrame({"value": range(1, 1001)})
        quantiles = _history_quantiles(frame)
        self.assertEqual(quantiles["samples"], 1000)
        self.assertAlmostEqual(quantiles["q20"], 200.8)
        self.assertAlmostEqual(quantiles["q50"], 500.5)
        self.assertAlmostEqual(quantiles["q80"], 800.2)
        self.assertIsNone(_history_quantiles(frame.head(249)))

    def test_profitable_company_uses_pe_history(self) -> None:
        scenario = build_valuation_scenarios(v2_evidence())
        self.assertEqual(scenario["method"], "pe_history_5y")
        self.assertEqual(scenario["scenarios"]["bear"]["price"], 15.0)
        self.assertEqual(scenario["risk_reward"], 4.0)

    def test_loss_company_uses_pb_history(self) -> None:
        evidence = v2_evidence()
        evidence["net_profit"] = -1
        scenario = build_valuation_scenarios(evidence)
        self.assertEqual(scenario["method"], "pb_history_5y")

    def test_profitable_company_tries_pb_when_pe_history_is_short(self) -> None:
        evidence = v2_evidence()
        evidence["pe_history_quantiles_5y"]["samples"] = 249
        scenario = build_valuation_scenarios(evidence)
        self.assertEqual(scenario["method"], "pb_history_5y")

    def test_insufficient_valuation_uses_price_fallback(self) -> None:
        evidence = v2_evidence()
        evidence.pop("pe_history_quantiles_5y")
        evidence.pop("pb_history_quantiles_5y")
        scenario = build_valuation_scenarios(evidence)
        self.assertEqual(scenario["method"], "price_history_3y_fallback")

    def test_downside_not_covered_does_not_invent_ratio(self) -> None:
        evidence = v2_evidence()
        evidence["pe_history_quantiles_5y"] = {"q20": 12, "q50": 15, "q80": 20, "samples": 1000}
        scenario = build_valuation_scenarios(evidence)
        self.assertIsNone(scenario["risk_reward"])
        self.assertIn("未覆盖", scenario["risk_reward_status"])


class ThesisContractTest(unittest.TestCase):
    def context(self, *, peers: bool = False) -> tuple[dict, object, dict]:
        evidence = v2_evidence(peers=peers)
        card = score_evidence(evidence)
        return build_thesis_context(card, evidence).to_dict(), card, evidence

    def test_context_is_v2_without_trade_rating(self) -> None:
        context, card, _ = self.context()
        self.assertEqual(context["schema_version"], 4)
        self.assertEqual(context["research"]["research_score"], card.research_score)
        self.assertNotIn("action_rating", context["research"])
        self.assertEqual(context["decision_rules"]["states"], ["观察", "等待", "试错", "买入", "退出"])
        self.assertEqual(context["valuation_scenarios"]["status"], "ready")

    def test_context_preserves_annual_report_for_traceable_judgment_refs(self) -> None:
        evidence = v2_evidence()
        annual_report = {
            "status": "ok",
            "title": "2025年年度报告（更正后）",
            "report_period": "2025-12-31",
            "publication_date": "2026-08-01",
            "corrected": True,
            "url": "http://static.cninfo.com.cn/finalpage/2026-08-01/1225452105.PDF",
            "fetch_status": "ok",
            "text_pages": 80,
            "control_chain": {"actual_controller": "朱世会", "controlling_shareholder": "先导科技集团"},
            "bismuth_business": {"revenue_reported_100m": 13.2, "revenue_cny": 1_320_000_000, "revenue_ratio": 0.7127},
            "production_bases": ["广东清远", "安徽五河", "湖北荆州", "浙江衢州"],
            "ion_implant_orders": {"new_customer_order_count": 4},
            "specialized": {"entity": "凯世通", "qualification": "国家级专精特新小巨人企业", "scope": "子公司"},
            "overseas_revenue": {"value_cny": 550415075.58, "domestic_value_cny": 1286405606.78, "ratio_pct": 29.9656, "period": "FY2025"},
            "operating_cashflow": {"value_cny": -4_130_756_268.10, "period": "FY2025"},
        }
        evidence["annual_report"] = annual_report
        packet = build_thesis_context(score_evidence(evidence), evidence).to_dict()
        self.assertEqual(packet["company"]["annual_report"], annual_report)

    def test_valid_wait_judgment(self) -> None:
        context, _, _ = self.context()
        output = validate_thesis_output(agent_payload(), context)
        self.assertEqual(output.decision["state"], "等待")
        self.assertEqual(len(output.causal_chain), 3)

    def test_v3_payload_must_be_regenerated_for_v4(self) -> None:
        context, _, _ = self.context()
        legacy = agent_payload()
        legacy["schema_version"] = 3
        with self.assertRaisesRegex(ThesisValidationError, "V3"):
            validate_thesis_output(legacy, context)

    def test_v4_requires_fact_meaning_and_decision_impact_for_visible_blocks(self) -> None:
        context, _, _ = self.context()
        payload = validate_thesis_output(agent_payload(), context).to_dict()
        payload["why_watch"].pop("fact")
        with self.assertRaisesRegex(ThesisValidationError, "why_watch.fact"):
            validate_thesis_output(payload, context)

    def test_v4_requires_market_narrative_and_all_four_market_layers(self) -> None:
        context, _, _ = self.context()
        payload = validate_thesis_output(agent_payload(), context).to_dict()
        expectation = payload["market_expectation"]
        self.assertEqual(
            set(("known", "priced_in", "unpriced", "mispriced")).difference(expectation),
            set(),
        )
        expectation.pop("mispriced")
        with self.assertRaisesRegex(ThesisValidationError, "market_expectation.mispriced"):
            validate_thesis_output(payload, context)
        payload = validate_thesis_output(agent_payload(), context).to_dict()
        payload["market_expectation"].pop("market_vs_fundamentals")
        with self.assertRaisesRegex(ThesisValidationError, "market_expectation.market_vs_fundamentals"):
            validate_thesis_output(payload, context)

    def test_v4_preferred_company_needs_two_verified_direct_peer_comparisons(self) -> None:
        context, _, _ = self.context(peers=True)
        context["decision_gates"]["expectation_gap_status"] = "已验证"
        payload = validate_thesis_output(agent_payload(), context).to_dict()
        payload["decision"].update({
            "state": "买入",
            "rationale": "同行优势、预期差和因果链均已验证。",
            "why_not_higher_state": "买入已是最高积极状态，仍需持续复核。",
        })
        payload["state_transition"] = {
            "previous_state": "首次判断",
            "current_state": "买入",
            "reason": "两家直接同行比较和预期差均获得验证。",
        }
        payload["market_expectation"]["gap_state"] = "预期偏低"
        payload["why_this_company"]["peer_verdict"] = "最优候选"
        payload["why_this_company"]["selection_conclusion"] = "若只选一家，当前公司在已验证同行中更优。"
        payload["why_this_company"]["peer_comparison"] = verified_peer_rows()
        for item in payload["causal_chain"]:
            item["status"] = "已验证"
        payload["causal_breakpoint"].update({
            "key_link": "当前无关键断点",
            "reason": "已列出的核心箭头均有验证，仍需持续复核。",
            "fact": "核心箭头均有已验证证据。",
            "meaning": "产业逻辑已经传导至利润兑现。",
            "decision_impact": "可进入买入状态，但仍需跟踪。",
        })
        self.assertEqual(validate_thesis_output(payload, context).decision["state"], "买入")
        payload["why_this_company"]["peer_comparison"] = payload["why_this_company"]["peer_comparison"][:1]
        with self.assertRaisesRegex(ThesisValidationError, "至少两家"):
            validate_thesis_output(payload, context)

    def test_v4_breakpoint_must_name_the_weakest_arrow(self) -> None:
        context, _, _ = self.context()
        payload = validate_thesis_output(agent_payload(), context).to_dict()
        payload["causal_breakpoint"]["key_link"] = "收入 -> 利润和现金流"
        with self.assertRaisesRegex(ThesisValidationError, "最关键"):
            validate_thesis_output(payload, context)

    def test_v4_rejects_raw_search_errors_from_card(self) -> None:
        context, _, _ = self.context()
        payload = validate_thesis_output(agent_payload(), context).to_dict()
        payload["core_contradiction"]["fact"] = "duckduckgo:ConnectionError"
        with self.assertRaisesRegex(ThesisValidationError, "原始网络搜索错误"):
            validate_thesis_output(payload, context)

    def test_v4_rejects_scorecard_jargon_from_card(self) -> None:
        context, _, _ = self.context()
        payload = validate_thesis_output(agent_payload(), context).to_dict()
        payload["why_watch"]["decision_impact"] = "F1 得分足以支持更高状态。"
        with self.assertRaisesRegex(ThesisValidationError, "六层评分"):
            validate_thesis_output(payload, context)

        payload = validate_thesis_output(agent_payload(), context).to_dict()
        payload["decision"]["rationale"] = "研究评分和覆盖率足以支持更高状态。"
        with self.assertRaisesRegex(ThesisValidationError, "六层评分"):
            validate_thesis_output(payload, context)

        payload = validate_thesis_output(agent_payload(), context).to_dict()
        payload["one_sentence"] = "评分很好，但仍需等待。"
        with self.assertRaisesRegex(ThesisValidationError, "六层评分"):
            validate_thesis_output(payload, context)

        payload = validate_thesis_output(agent_payload(), context).to_dict()
        payload["one_sentence"] = "量化得分很好，但仍需等待。"
        with self.assertRaisesRegex(ThesisValidationError, "六层评分"):
            validate_thesis_output(payload, context)

    def test_one_sentence_keeps_five_state_for_final_decision(self) -> None:
        context, _, _ = self.context()
        payload = validate_thesis_output(agent_payload(), context).to_dict()
        payload["one_sentence"] = "产业逻辑尚未闭环，当前等待。"
        with self.assertRaisesRegex(ThesisValidationError, "五态只能在最后"):
            validate_thesis_output(payload, context)

    def test_unknown_reference_is_rejected(self) -> None:
        context, _, _ = self.context()
        payload = agent_payload()
        payload["thesis"]["evidence_refs"] = ["company.no_such_field"]
        with self.assertRaises(ThesisValidationError):
            validate_thesis_output(payload, context)

    def test_low_coverage_caps_state_at_observe(self) -> None:
        context, _, _ = self.context()
        context["decision_gates"]["coverage"] = 0.5
        with self.assertRaisesRegex(ThesisValidationError, "覆盖率"):
            validate_thesis_output(agent_payload("等待"), context)

    def test_trial_requires_two_to_one_and_no_broken_link(self) -> None:
        context, _, _ = self.context()
        payload = agent_payload("试错")
        self.assertEqual(validate_thesis_output(payload, context).decision["state"], "试错")
        payload["causal_chain"][0]["status"] = "缺失"
        with self.assertRaisesRegex(ThesisValidationError, "断点"):
            validate_thesis_output(payload, context)

    def test_trial_requires_a_partially_verified_condition(self) -> None:
        context, _, _ = self.context()
        payload = agent_payload("试错")
        for item in payload["causal_chain"]:
            item["status"] = "已验证"
        payload["causal_breakpoint"]["key_link"] = "当前无关键断点"
        with self.assertRaisesRegex(ThesisValidationError, "部分验证"):
            validate_thesis_output(payload, context)

    def test_missing_chain_requires_observe(self) -> None:
        context, _, _ = self.context()
        payload = agent_payload("等待")
        payload["causal_chain"][0]["status"] = "缺失"
        with self.assertRaisesRegex(ThesisValidationError, "只能为观察"):
            validate_thesis_output(payload, context)

    def test_buy_requires_two_verified_peers_and_low_expectation(self) -> None:
        context, _, _ = self.context(peers=True)
        context["decision_gates"]["expectation_gap_status"] = "已验证"
        payload = agent_payload("买入")
        for item in payload["causal_chain"]:
            item["status"] = "已验证"
        payload["causal_breakpoint"]["key_link"] = "当前无关键断点"
        payload["market_expectation"]["gap_state"] = "预期偏低"
        payload["why_this_company"]["peer_verdict"] = "最优候选"
        payload["why_this_company"]["peer_comparison"] = verified_peer_rows()
        self.assertEqual(validate_thesis_output(payload, context).decision["state"], "买入")

    def test_force_exit_hard_cap(self) -> None:
        context, _, _ = self.context()
        context["decision_gates"]["hard_caps"] = [{"condition": "ST", "result": "已触发", "decision_effect": "强制退出"}]
        with self.assertRaisesRegex(ThesisValidationError, "强制退出"):
            validate_thesis_output(agent_payload(), context)

    def test_wait_ceiling_blocks_trial_and_buy(self) -> None:
        context, _, _ = self.context()
        context["decision_gates"]["hard_caps"] = [
            {"condition": "实控人减持", "result": "已触发", "decision_effect": "最高等待"}
        ]
        with self.assertRaisesRegex(ThesisValidationError, "最高为等待"):
            validate_thesis_output(agent_payload("试错"), context)

    def test_contradicted_chain_requires_exit(self) -> None:
        context, _, _ = self.context()
        payload = agent_payload()
        payload["causal_chain"][0]["status"] = "矛盾"
        with self.assertRaisesRegex(ThesisValidationError, "核心因果链"):
            validate_thesis_output(payload, context)

    def test_weak_business_or_no_benefit_requires_exit(self) -> None:
        context, _, _ = self.context()
        weak = agent_payload()
        weak["business_judgment"]["quality"] = "较弱"
        with self.assertRaisesRegex(ThesisValidationError, "生意质量"):
            validate_thesis_output(weak, context)

        no_benefit = agent_payload()
        no_benefit["profit_judgment"]["state"] = "未受益"
        with self.assertRaisesRegex(ThesisValidationError, "未受益"):
            validate_thesis_output(no_benefit, context)

    def test_previous_state_must_match_snapshot(self) -> None:
        context, _, _ = self.context()
        context["prior_judgment"] = {"state": "观察"}
        with self.assertRaisesRegex(ThesisValidationError, "观察"):
            validate_thesis_output(agent_payload(), context)

    def test_report_renders_judgment_without_old_rating(self) -> None:
        context, card, evidence = self.context()
        before = copy.deepcopy(card.to_dict())
        report = grader.render_report("300820", "英杰电气", evidence, card, (), agent_payload())
        self.assertEqual(before, card.to_dict())
        for expected in ("## 莫大判断", "### 投资假设", "### 核心因果链", "### Bull / Base / Bear 情景", "### 估值与赔率"):
            self.assertIn(expected, report)
        self.assertLess(report.index("## 莫大判断"), report.index("## 研究概览"))
        self.assertLess(report.index("## 莫大判断"), report.index("### 研究评分"))
        self.assertNotIn("行动评级", report)
        self.assertNotIn("legacy_heading", report)
        self.assertNotIn("证据索引", report)
        self.assertIn("<!-- evidence_refs:", report)
        self.assertIn('"expression_status": "agent_generated"', report)

    def test_v4_report_puts_judgment_before_score_overview_and_hides_refs(self) -> None:
        context, card, evidence = self.context()
        payload = validate_thesis_output(agent_payload(), context).to_dict()
        visible_blocks = {
            "core_contradiction": "核心矛盾",
            "industry_positioning": "产业链定位",
            "thesis": "投资假设",
            "why_watch": "为什么现在进入视野",
            "reversal_judgment": "边际变化",
            "business_judgment": "生意判断",
            "industry_judgment": "行业判断",
            "profit_judgment": "利润判断",
            "driver_judgment": "市场驱动",
            "bull_case": "Bull情景",
            "base_case": "Base情景",
            "bear_case": "Bear情景",
        }
        for key, marker in visible_blocks.items():
            payload[key]["fact"] = f"{marker}事实"
            payload[key]["meaning"] = f"{marker}含义"
            payload[key]["decision_impact"] = f"{marker}决策影响"
        report = grader.render_report("300820", "英杰电气", evidence, card, (), payload)
        overview_index = report.index("## 研究概览")
        judgment_index = report.index("## 莫大判断")
        evidence_index = report.index("## 证据与六层诊断")
        audit_index = report.index("## 六层评分与量化审计")
        self.assertLess(judgment_index, overview_index)
        self.assertLess(judgment_index, evidence_index)
        self.assertLess(evidence_index, audit_index)
        self.assertLess(report.index("### 核心上下游对应表"), overview_index)
        self.assertLess(overview_index, audit_index)
        self.assertLess(judgment_index, report.index("### 六层图形概览"))
        front_layer = report[judgment_index:evidence_index]
        for heading in ("### 一句话结论", "### 核心矛盾", "### 产业链定位", "### 为什么现在进入视野", "### 过去为什么不行，现在改变了什么", "### 市场现在在交易什么", "### 为什么是它"):
            self.assertIn(heading, front_layer)
        for marker in visible_blocks.values():
            for suffix in ("事实", "含义", "决策影响"):
                self.assertIn(f"{marker}{suffix}", front_layer)
        self.assertNotIn("证据索引", front_layer)
        self.assertIn("| 公司 | 吃哪段趋势 / 业务纯度 | 地位 / 壁垒 | 利润 / 市值弹性 | 海外风险 / 拥挤与预期 | 最大缺陷 | 当前选择 |", front_layer)
        self.assertIn("| 从 | 到 | 判断 | 状态 |", front_layer)
        self.assertIn("<!-- evidence_refs:", front_layer)
        self.assertNotIn("F1", front_layer)
        self.assertNotIn("legacy_heading", front_layer)
        self.assertNotIn("ConnectionError", front_layer)

    def test_visible_report_cleans_search_backend_failures(self) -> None:
        self.assertEqual(
            grader._public_text("缺少订单证据；360搜索未返回相关结果"),
            "缺少订单证据",
        )
        self.assertEqual(grader._public_text("search_budget_exhausted"), "无")
        self.assertEqual(grader._public_status("搜索失败，需人工确认"), "无")
        report = grader._sanitize_visible_report(
            "<!-- audit: 360搜索未返回相关结果；需人工确认 -->\n"
            "正文：360搜索未返回相关结果；需人工确认\n"
        )
        self.assertIn("<!-- audit: 360搜索未返回相关结果；需人工确认 -->", report)
        self.assertNotIn("360搜索未返回相关结果", report.split("-->", 1)[1])
        self.assertNotIn("需人工确认", report.split("-->", 1)[1])

    def test_invalid_expression_is_not_replaced(self) -> None:
        _, card, evidence = self.context()
        report = grader.render_report("300820", "英杰电气", evidence, card, (), {"decision": {}})
        self.assertIn('"expression_status": "expression_failed"', report)
        self.assertIn("判断层生成失败", report)


if __name__ == "__main__":
    unittest.main()
