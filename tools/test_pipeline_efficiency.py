from __future__ import annotations

import os
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import numpy as np
import requests

from tools.akshare import announcements, business_data, finance_data, market_events
from tools import run_pipeline
from tools import stock_resolver
from tools.scoring import grader
from tools.scoring import evidence as evidence_module
from tools.scoring.model import score_evidence
from tools.scoring import web_research
from tools.scoring import search_rules
from tools.scoring import stock_discussion
from tools.scoring import classification_db
from tools.scoring import announcement_rules
from tools.providers import axdata_provider
from tools.tdx.analyzer import AlphaSorosAnalyzer


def full_evidence() -> dict:
    values = {
        "track_strength": 1.0, "industry_cagr_3y": 0.35, "penetration_rate": 0.10,
        "chain_stage": "upstream", "chain_name": "半导体设备产业链", "chain_partial": False,
        "main_business": "刻蚀设备、薄膜沉积设备", "business_chain_revenue_ratio": 0.6,
        "supply_evidence_count": 3, "supply_tightening": True,
        "supply_cr3": 80, "capacity_expansion_cycle_years": 4,
        "chokepoint_score": 100, "capex_strength": 1.0, "capex_yoy": 0.35,
        "controller_action": "increase", "top1_holder_pct": 30, "holder_count_change_pct": -20,
        "top10_quality": 1.0, "fund_holding_change_pct": 1.0, "pledge_ratio": 0, "unlock_ratio": 0,
        "background_quality": 1.0, "leadership_strength": 1.0, "net_profit": 1,
        "operating_cashflow": 2, "debt_ratio": 0.3, "cash_to_debt": 0.5,
        "net_cash_ratio": 0.25, "cash_to_short_debt": 4,
        "operating_cashflow_to_net_profit": 2, "receivables_to_assets": 0.1,
        "st_risk": False, "audit_risk": False, "goodwill_risk": False, "specialized_strength": 1.0,
        "business_chain_match": 1.0, "overseas_revenue_ratio": 40,
        "revenue_yoy": 0.2, "profit_yoy": 0.2, "order_growth": 20,
        "price_percentile_3y": 0.1, "product_price_to_history_high": 0.2,
        "pe_ttm": 10, "peer_pe_ttm_median": 20, "pb": 1, "pb_to_5y_median": 0.4,
        "attention_heat": 0.1, "industry_cycle_cold": True,
        "revenue_yoy_delta": 0.1, "profit_yoy_delta": 0.1,
        "alpha_score": 0.5, "market_congestion": 0.3, "market_congestion_fresh": True,
        "alpha_trend": "上升", "ma_structure": "bullish", "momentum_20d": 0.08,
        "ma20_slope_5d": 0.03, "volume_ratio_20d": 1.3, "technical_position": 0.3,
        "technical_overheat": False,
        "verified_catalyst_count": 2, "technical_signal": "建仓",
        "technical_structure_score": 4, "technical_structure_reason": "技术结构明确偏多",
        "technical_indicators": {},
        "chan_structure": {"status": "可分析", "latest_direction": "向上", "relation": "中枢上方",
                           "current_price": 30.0, "support": 28.0, "resistance": 33.0},
    }
    values["metric_sources"] = {key: ["test"] for key in values if key != "metric_sources"}
    return values


class PipelineEfficiencyTest(unittest.TestCase):
    def test_stock_name_resolver_prefers_local_index(self) -> None:
        original = stock_resolver._INDEX
        try:
            stock_resolver._INDEX = {
                "300820": {"code": "300820", "name": "英杰电气", "source": "test"},
            }
            with patch("tools.efinance.provider.search_stock", side_effect=AssertionError("local hit must not call remote")):
                self.assertEqual(stock_resolver.resolve_stock_input(" 英杰电气 "), ("300820", "英杰电气"))
        finally:
            stock_resolver._INDEX = original

    def test_stock_name_resolver_realtime_fallback_is_cached(self) -> None:
        original = stock_resolver._INDEX
        try:
            stock_resolver._INDEX = {}
            rows = [{"code": "000001", "name": "平安银行", "source": "test"}]
            with patch("tools.efinance.provider.search_stock", return_value=rows) as search, \
                 patch.object(stock_resolver, "_write_index") as write:
                self.assertEqual(stock_resolver.resolve_stock_input("平安银行"), ("000001", "平安银行"))
            search.assert_called_once_with("平安银行", limit=20)
            write.assert_called()
            self.assertEqual(stock_resolver.resolve_stock_input("000001"), ("000001", "平安银行"))
        finally:
            stock_resolver._INDEX = original

    def test_stock_name_resolver_rejects_ambiguous_fallback(self) -> None:
        original = stock_resolver._INDEX
        try:
            stock_resolver._INDEX = {}
            rows = [
                {"code": "000001", "name": "平安银行", "source": "test"},
                {"code": "000002", "name": "平安证券", "source": "test"},
            ]
            with patch("tools.efinance.provider.search_stock", return_value=rows):
                with self.assertRaisesRegex(ValueError, "不唯一"):
                    stock_resolver.resolve_stock_input("平安")
        finally:
            stock_resolver._INDEX = original

    def test_latest_classification_database_confirms_categories_by_code(self) -> None:
        result = classification_db.lookup("000002", "万科A")
        self.assertTrue(result["found"])
        self.assertEqual(result["source"], "专精特新_行业龙头_核心供应商_A股名单_完整版.csv")
        self.assertTrue(classification_db.has_category(result, "leadership"))
        self.assertFalse(classification_db.has_category(result, "specialized"))

    def test_classification_database_hit_skips_matching_web_gap(self) -> None:
        with patch("tools.scoring.evidence.read_reports", return_value={}):
            targets = run_pipeline.unresolved_targets("000002", "万科A", (), 0)
        self.assertNotIn(("F3", "leadership"), {
            (item["factor_key"], item["subfactor_key"]) for item in targets
        })

    def test_discussion_structured_data_precedes_search(self) -> None:
        structured = [{"source": "xueqiu", "title": "中石科技订单改善", "text": "订单改善，业绩增长", "status": "结构化接口"}]
        with patch.object(stock_discussion, "_xueqiu", return_value=structured), \
             patch.object(stock_discussion, "_eastmoney", return_value=[]), \
             patch.object(stock_discussion, "_search_fallback", side_effect=AssertionError("must not search")):
            data = stock_discussion.collect("300684", "中石科技")
        self.assertEqual(data["discussion_structured_count"], 1)
        self.assertEqual(data["discussion_source_status"], "结构化接口（部分覆盖）")
        self.assertEqual(data["discussion_sample_status"], "样本不足")
        self.assertIsNone(data["discussion_sentiment"])

    def test_discussion_search_fallback_is_unverified(self) -> None:
        row = {"title": "中石科技讨论", "snippet": "可能订单恢复", "url": "https://example.test"}
        with patch.object(stock_discussion, "_xueqiu", return_value=[]), \
             patch.object(stock_discussion, "_eastmoney", return_value=[]), \
             patch.object(stock_discussion, "_search", return_value=("searxng", [row], [])):
            data = stock_discussion.collect("300684", "中石科技")
        self.assertEqual(data["discussion_search_count"], 1)
        self.assertEqual(data["discussion_records"][0]["status"], "网络命中（未核验）")

    def test_social_module_preserves_platform_partial_failure_with_discussion_data(self) -> None:
        from tools.akshare import social_sentiment

        def fake_cached(platform, _fetcher):
            if platform in {"weibo", "douyin"}:
                raise RuntimeError(f"{platform} unavailable")
            return ([{"rank": 1, "title": "平安银行", "url": ""}], "live")

        discussion = {
            "discussion_posts_total": 2,
            "discussion_structured_count": 2,
            "discussion_search_count": 0,
            "discussion_source_count": 1,
            "discussion_source_status": "结构化接口",
            "discussion_sources": ["eastmoney"],
            "discussion_partial": False,
            "discussion_records": [],
            "discussion_search_errors": [],
            "discussion_sentiment": "中性",
            "discussion_sentiment_score": 0.0,
            "discussion_positive_count": 0,
            "discussion_negative_count": 0,
            "discussion_neutral_count": 2,
            "discussion_promotion_hits": [],
            "discussion_rumor_hits": [],
            "fetch_state": "ok",
            "source_chain": [{"source": "东方财富股吧", "status": "ok", "error": ""}],
        }
        news = {"news_posts_total": 0, "news_sources_checked": 3, "news_sources_ok": 3, "news_partial": False, "news_sentiment": None, "fetch_state": "ok", "source_chain": []}
        history = {"social_history_snapshots": 1, "social_new_topics_24h": 1, "social_persistent_topics": 0, "social_fast_spread_topics": 0, "social_rank_jump_max": None, "social_first_seen_at": "2026-08-08T00:00:00+08:00", "social_propagation_status": "首次快照，等待时间序列"}
        with patch.object(social_sentiment, "_cached", side_effect=fake_cached), \
             patch.object(social_sentiment, "_collect_discussion", return_value=discussion), \
             patch.object(social_sentiment, "_collect_news", return_value=news), \
             patch.object(social_sentiment, "_update_history", return_value=history):
            data = social_sentiment.collect("000001", "平安银行")
        self.assertEqual(data["fetch_state"], "fallback_ok")
        self.assertEqual(data["discussion_fetch_state"], "ok")
        self.assertEqual(data["social_platforms_checked"], 4)
        self.assertIn("platforms", data["source_chain"])
        self.assertIn("discussion", data["source_chain"])

    def test_moda_f1_uses_cagr_penetration_cr3_expansion_and_capex(self) -> None:
        data = full_evidence()
        f1 = next(factor for factor in score_evidence(data).factors if factor.key == "F1")
        self.assertEqual(f1.score, 30)
        reasons = "；".join(item.reason for item in f1.subfactors)
        self.assertIn("CAGR", reasons)
        self.assertIn("CR3", reasons)
        self.assertIn("扩产周期", reasons)
        self.assertIn("资本开支同比", reasons)

    def test_holder_count_uses_stricter_moda_bands(self) -> None:
        expected = {"deep": 3, "medium": 2, "slight": 1, "increase": 0}
        for label, change in (("deep", -20), ("medium", -8), ("slight", -2), ("increase", 3)):
            data = full_evidence()
            data["holder_count_change_pct"] = change
            item = next(item for factor in score_evidence(data).factors for item in factor.subfactors if item.key == "holder_trend")
            self.assertEqual(item.score, expected[label])

    def test_financial_safety_uses_survival_metrics(self) -> None:
        data = full_evidence()
        item = next(item for factor in score_evidence(data).factors for item in factor.subfactors if item.key == "financial_safety")
        self.assertEqual(item.score, 5)
        self.assertIn("净现金率", item.reason)
        weak = full_evidence()
        weak.update({"net_cash_ratio": -0.1, "cash_to_short_debt": 0.5,
                     "operating_cashflow_to_net_profit": 0.2, "debt_ratio": 0.8,
                     "receivables_to_assets": 0.4})
        weak_item = next(item for factor in score_evidence(weak).factors for item in factor.subfactors if item.key == "financial_safety")
        self.assertEqual(weak_item.score, 0)

    def test_f5_combines_stock_product_cycle_and_early_reversal(self) -> None:
        data = full_evidence()
        data.update({"revenue_yoy": 0.1, "profit_yoy": -0.2, "profit_yoy_delta": 0.1,
                     "operating_cashflow": 1})
        factors = score_evidence(data).factors
        price = next(item for factor in factors for item in factor.subfactors if item.key == "price_position")
        inflection = next(item for factor in factors for item in factor.subfactors if item.key == "inflection")
        self.assertEqual(price.score, 2.5)
        self.assertEqual(inflection.score, 2)
        self.assertIn("周期底部前兆", inflection.reason)

    def test_f5_low_position_is_inverse_but_does_not_create_reversal(self) -> None:
        data = full_evidence()
        data.update({
            "price_percentile_3y": 0.08,
            "product_price_to_history_high": 0.20,
            "attention_heat": 0.80,
            "industry_cycle_cold": False,
            "order_growth": -10,
            "revenue_yoy_delta": -0.10,
            "profit_yoy_delta": -0.10,
        })
        f5 = next(factor for factor in score_evidence(data).factors if factor.key == "F5")
        price = next(item for item in f5.subfactors if item.key == "price_position")
        inflection = next(item for item in f5.subfactors if item.key == "inflection")
        expectation = next(item for item in f5.subfactors if item.key == "expectation_gap")
        self.assertEqual(price.score, 2.5)
        self.assertEqual(inflection.score, 0)
        self.assertEqual(expectation.score, 0)
        self.assertIn("逆向评分", price.reason)
        self.assertIn("低位本身不等于反转", inflection.reason)

    def test_industry_web_signal_uses_three_layers_and_independent_domains(self) -> None:
        from tools.akshare import industry_prosperity

        rows = [
            {"title": "行业订单增长、业绩改善、行业指数上涨", "snippet": "资金流入，成交放量", "url": "https://static.cninfo.com.cn/a"},
            {"title": "行业复苏与价格上涨", "snippet": "排产饱满，跑赢市场", "url": "https://www.cls.cn/a"},
        ]
        with patch.object(web_research, "_search", return_value=("duckduckgo_html", rows, [])), \
             patch.object(web_research, "_fetch_page", return_value=("ok", "")):
            result = industry_prosperity.collect_web_signal("华特气体", "电子化学品", timeout=0.1)
        self.assertEqual(result["status"], "上行")
        self.assertEqual(result["coverage"], "完整")
        self.assertEqual(result["layers"]["financial"]["status"], "上行")
        self.assertEqual(result["layers"]["supply"]["status"], "上行")
        self.assertEqual(result["layers"]["market"]["status"], "上行")

    def test_web_gap_overlay_is_unverified_and_does_not_override_complete_data(self) -> None:
        missing = {"metric_sources": {}, "web_subfactor_results": {
            "F1.era_track": {"status": "网络命中（未核验）", "score": 8,
                               "reason": "CAGR=25%", "provider": "duckduckgo"}
        }}
        item = next(item for factor in score_evidence(missing).factors for item in factor.subfactors if item.key == "era_track")
        self.assertEqual((item.score, item.status), (8, "网络命中（未核验）"))
        complete = full_evidence()
        complete["web_subfactor_results"] = missing["web_subfactor_results"]
        complete_item = next(item for factor in score_evidence(complete).factors for item in factor.subfactors if item.key == "era_track")
        self.assertEqual((complete_item.score, complete_item.status), (10, "已验证"))

    def test_web_positive_risk_can_trigger_hard_cap_without_overriding_structured_safe_value(self) -> None:
        missing = full_evidence()
        missing.pop("controller_action")
        missing["metric_sources"].pop("controller_action")
        missing["web_subfactor_results"] = {
            "F2.controller_action": {"status": "网络命中（未核验）", "score": 0,
                                       "reason": "控股股东减持", "provider": "duckduckgo",
                                       "hard_cap_signals": {"controller_reduction": True}}
        }
        self.assertEqual(score_evidence(missing).rating, "根")
        self.assertEqual(score_evidence(missing).action_rating, "买入")
        safe = full_evidence()
        safe["web_subfactor_results"] = missing["web_subfactor_results"]
        self.assertEqual(score_evidence(safe).rating, "根")

    def test_search_rule_registry_covers_all_f1_to_f5_subfactors(self) -> None:
        card = score_evidence({"metric_sources": {}})
        expected = {f"{factor.key}.{item.key}" for factor in card.factors if factor.key != "F6" for item in factor.subfactors}
        self.assertEqual(set(search_rules.RULES), expected)

    def test_leadership_headline_is_only_a_clue_without_structured_evidence(self) -> None:
        reports = {
            "market_events": '<!-- moda_market_events: {"research_titles": ["测试公司是行业龙头、核心供应商"]} -->',
        }
        evidence = evidence_module.build_evidence("000001", "测试公司", reports)
        self.assertNotIn("leadership_strength", evidence)
        self.assertEqual(evidence.get("leadership_clues"), ["行业龙头", "核心供应商"])
        self.assertIn("只有研报/行情标题线索", evidence.get("leadership_missing_reason", ""))

    def test_leadership_uses_sector_appropriate_dimensions(self) -> None:
        reports = {
            "finance_data": '<!-- moda_metrics: {"industry": "乘用车"} --> 全国乘用车市场份额第一，销量领先',
            "business_data": '<!-- moda_business: {"main_business": "汽车制造"} --> 核心技术领先，发明专利数量较多',
            "announcements": "公司是核心供应商，已量产供货并进入头部客户供应链。",
        }
        evidence = evidence_module.build_evidence("000001", "测试公司", reports)
        self.assertEqual(evidence["leadership_profile"], "制造/消费")
        self.assertEqual(evidence["leadership_strength"], 1.0)
        self.assertEqual(evidence["leadership_dimension_count"], 4)
        self.assertIn("市场份额/排名", evidence["leadership_reason"])
        self.assertIn("客户/核心供应关系", evidence["leadership_reason"])

    def test_leadership_search_requires_dimensions_not_a_single_label(self) -> None:
        row = {
            "title": "某公司行业龙头",
            "snippet": "市场地位稳固",
            "url": "https://example.test",
            "fetch_status": "ok",
        }
        result = search_rules.evaluate("F3.leadership", 5, [row])
        self.assertEqual(result["status"], "已搜索未命中")
        row["snippet"] = "市场份额第一，核心供应商，客户覆盖全国"
        result = search_rules.evaluate("F3.leadership", 5, [row])
        self.assertEqual(result["status"], "网络命中（未核验）")
        self.assertEqual(result["score"], 3.75)

    def test_gap_budget_selection_covers_each_factor_and_prioritizes_leadership(self) -> None:
        targets = [
            {"factor_key": factor, "subfactor_key": f"x{i}", "maximum": 5 if i == 0 else 2, "original_status": "需人工确认"}
            for factor in ("F1", "F2", "F3", "F4", "F5")
            for i in range(4)
        ]
        selected, skipped = web_research._select_gap_targets(targets)
        self.assertEqual(len(selected), web_research.MAX_GAP_TARGETS)
        self.assertEqual(len(skipped), len(targets) - web_research.MAX_GAP_TARGETS)
        self.assertEqual({item["factor_key"] for item in selected}, {"F1", "F2", "F3", "F4", "F5"})
        self.assertIn("F3.x0", {f"{item['factor_key']}.{item['subfactor_key']}" for item in selected})
        budgets = web_research._allocate_gap_budgets(selected)
        self.assertAlmostEqual(sum(budgets.values()), web_research.MAX_GAP_BUDGET_SECONDS, places=2)
        self.assertGreaterEqual(min(budgets.values()), web_research.MIN_TARGET_BUDGET_SECONDS)

    def test_gap_budget_report_distinguishes_target_limit_from_time_exhaustion(self) -> None:
        report = web_research.build_report("000001", "测试", {
            "web_gap_results": [{
                "factor_key": "F3", "subfactor_key": "leadership", "label": "龙头/核心供应商",
                "original_status": "需人工确认", "status": "搜索失败，需人工确认", "score": 0, "maximum": 5,
                "provider": "none", "reason": "超过缺口目标数量上限 12，本目标未分配搜索预算",
                "selection_status": "skipped", "target_budget_seconds": 0,
                "budget_used_seconds": 0, "evidence": [],
            }],
            "web_research_provider": "none",
            "search_budget": {
                "budget_total_seconds": 75,
                "budget_used_seconds": 75,
                "targets_total": 20,
                "targets_selected": 12,
                "targets_skipped": 8,
                "global_time_exhausted": True,
                "selection_policy": "测试选择规则",
                "allocation_policy": "测试分配规则",
                "skip_reasons": {"target_limit_exceeded": 8, "global_time_exhausted": 1, "target_time_exhausted": 2},
            },
        })
        self.assertIn("目标数量上限 8 个", report)
        self.assertIn("全局时间耗尽：是", report)
        self.assertIn("单目标时间耗尽 2 个", report)

    def test_pipeline_targets_manual_and_partial_f1_to_f5_but_not_f6(self) -> None:
        with patch("tools.scoring.evidence.read_reports", return_value={}), \
             patch("tools.scoring.evidence.build_evidence", return_value={"metric_sources": {}}):
            targets = run_pipeline.unresolved_targets("000001", "测试", (), 0)
        self.assertEqual(len(targets), 24)
        self.assertNotIn("F6", {item["factor_key"] for item in targets})
        partial = full_evidence()
        partial.pop("penetration_rate")
        partial["metric_sources"].pop("penetration_rate")
        with patch("tools.scoring.evidence.read_reports", return_value={}), \
             patch("tools.scoring.evidence.build_evidence", return_value=partial):
            targets = run_pipeline.unresolved_targets("000001", "测试", (), 0)
        self.assertIn("F1.era_track", {f"{item['factor_key']}.{item['subfactor_key']}" for item in targets})

    def test_finance_metrics_include_survival_balance_sheet_fields(self) -> None:
        balance = pd.DataFrame([{
            "资产总计": 100.0, "负债合计": 40.0, "货币资金": 30.0, "应收账款": 10.0,
            "短期借款": 5.0, "一年内到期的非流动负债": 5.0, "长期借款": 5.0,
            "应付债券": 0.0, "租赁负债": 0.0, "商誉": 0.0,
        }])
        income = pd.DataFrame([{"营业收入": 50.0, "归属于母公司的净利润": 10.0}])
        cashflow = pd.DataFrame([{"经营活动产生的现金流量净额": 12.0}])
        metrics = finance_data._report_metrics(
            "000001", {}, {}, pd.DataFrame(), pd.DataFrame(),
            {"fzb": balance, "lrb": income, "llb": cashflow},
        )
        self.assertAlmostEqual(metrics["net_cash_ratio"], 0.15)
        self.assertEqual(metrics["cash_to_short_debt"], 3.0)
        self.assertEqual(metrics["operating_cashflow_to_net_profit"], 1.2)
        self.assertEqual(metrics["receivables_to_assets"], 0.1)

    def test_search_auto_prefers_searxng_and_falls_back_to_ddg(self) -> None:
        row = [{"title": "测试", "url": "https://example.com", "snippet": "测试"}]
        with patch.dict(os.environ, {"SEARXNG_URL": "https://search.example", "DDG_MCP_URL": "https://ddg.example"}, clear=False), \
             patch.object(web_research, "_searxng_search", return_value=row), \
             patch.object(web_research, "_ddg_mcp_search") as ddg:
            used, rows, _ = web_research._search("auto", "test", 0.1)
        self.assertEqual((used, rows), ("searxng", row))
        ddg.assert_not_called()
        with patch.dict(os.environ, {"SEARXNG_URL": "https://search.example", "DDG_MCP_URL": "https://ddg.example"}, clear=False), \
             patch.object(web_research, "_searxng_search", side_effect=TimeoutError), \
             patch.object(web_research, "_ddg_mcp_search", return_value=row):
            used, rows, errors = web_research._search("auto", "test", 0.1)
        self.assertEqual((used, rows), ("duckduckgo", row))
        self.assertIn("searxng:TimeoutError", errors)

    def test_search_public_falls_back_to_duckduckgo_lite(self) -> None:
        row = [{"title": "测试", "url": "https://example.com", "snippet": "测试"}]
        with patch.dict(
            os.environ,
            {
                "SEARXNG_URL": "",
                "DDG_MCP_URL": "",
                "MODA_PUBLIC_SEARCH": "auto",
            },
            clear=False,
        ), patch.object(web_research, "_duckduckgo_html_search", side_effect=TimeoutError), \
                patch.object(web_research, "_duckduckgo_lite_search", return_value=row):
            used, rows, errors = web_research._search("auto", "test", 0.1)
        self.assertEqual((used, rows), ("duckduckgo_lite", row))
        self.assertIn("duckduckgo_html:TimeoutError", errors)

    def test_gap_search_reuses_only_same_day_success(self) -> None:
        row = [{"title": "测试", "url": "https://example.com", "snippet": "测试"}]
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(web_research, "CACHE_PATH", Path(directory) / "search.json"), \
             patch.dict(os.environ, {"SEARXNG_URL": "https://search.example", "DDG_MCP_URL": "", "MODA_PUBLIC_SEARCH": "off"}, clear=False), \
             patch.object(web_research, "_searxng_search", return_value=row) as search:
            first = web_research._search("auto", "测试查询", 0.1, cache_scope="000001|F1.era_track")
            second = web_research._search("auto", "测试查询", 0.1, cache_scope="000001|F1.era_track")
        self.assertEqual(first, second)
        search.assert_called_once()

    def test_gap_search_does_not_cache_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(web_research, "CACHE_PATH", Path(directory) / "search.json"), \
             patch.dict(os.environ, {"SEARXNG_URL": "https://search.example", "DDG_MCP_URL": "", "MODA_PUBLIC_SEARCH": "off"}, clear=False), \
             patch.object(web_research, "_searxng_search", return_value=[]) as search:
            web_research._search("auto", "失败查询", 0.1, cache_scope="000001|F1.era_track")
            web_research._search("auto", "失败查询", 0.1, cache_scope="000001|F1.era_track")
        self.assertEqual(search.call_count, 2)

    def test_gap_search_cache_scope_separates_stocks_and_factors(self) -> None:
        row = [{"title": "测试", "url": "https://example.com", "snippet": "测试"}]
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(web_research, "CACHE_PATH", Path(directory) / "search.json"), \
             patch.dict(os.environ, {"SEARXNG_URL": "https://search.example", "DDG_MCP_URL": "", "MODA_PUBLIC_SEARCH": "off"}, clear=False), \
             patch.object(web_research, "_searxng_search", return_value=row) as search:
            web_research._search("auto", "同一查询", 0.1, cache_scope="000001|F1.era_track")
            web_research._search("auto", "同一查询", 0.1, cache_scope="000002|F1.era_track")
            web_research._search("auto", "同一查询", 0.1, cache_scope="000001|F1.supply_gap")
        self.assertEqual(search.call_count, 3)

    def test_gap_search_cache_can_be_disabled_for_refresh(self) -> None:
        row = [{"title": "测试", "url": "https://example.com", "snippet": "测试"}]
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(web_research, "CACHE_PATH", Path(directory) / "search.json"), \
             patch.dict(os.environ, {"SEARXNG_URL": "https://search.example", "DDG_MCP_URL": "", "MODA_PUBLIC_SEARCH": "off", "MODA_SEARCH_CACHE": "off"}, clear=False), \
             patch.object(web_research, "_searxng_search", return_value=row) as search:
            web_research._search("auto", "刷新查询", 0.1, cache_scope="000001|F1.era_track")
            web_research._search("auto", "刷新查询", 0.1, cache_scope="000001|F1.era_track")
        self.assertEqual(search.call_count, 2)

    def test_search_cache_batch_flushes_once_for_multiple_successes(self) -> None:
        row = [{"title": "测试", "url": "https://example.com", "snippet": "测试"}]
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(web_research, "CACHE_PATH", Path(directory) / "search.json"), \
             patch.dict(os.environ, {"SEARXNG_URL": "https://search.example", "DDG_MCP_URL": "", "MODA_PUBLIC_SEARCH": "off"}, clear=False), \
             patch.object(web_research, "_searxng_search", return_value=row), \
             patch.object(web_research, "_flush_search_cache", wraps=web_research._flush_search_cache) as flush:
            with web_research._search_cache_batch():
                web_research._search("auto", "查询一", 0.1, cache_scope="000001|F1.era_track")
                web_research._search("auto", "查询二", 0.1, cache_scope="000001|F1.supply_gap")
        flush.assert_called_once()

    def test_ddg_mcp_session_initializes_once_per_worker(self) -> None:
        calls: list[str] = []

        class FakeResponse:
            headers = {"Mcp-Session-Id": "session-1"}
            content = b""

            def __init__(self, payload: dict | None = None) -> None:
                self.payload = payload or {}

            def raise_for_status(self) -> None:
                pass

            def json(self) -> dict:
                return self.payload

        class FakeSession:
            def post(self, _url: str, *, json: dict, headers: dict, timeout: float) -> FakeResponse:
                calls.append(str(json.get("method") or ""))
                if json.get("method") == "tools/call":
                    query = json["params"]["arguments"]["query"]
                    payload = {"result": {"content": [{"type": "text", "text": f"1. {query}\nURL: https://example.com/{query}"}]}}
                    return FakeResponse(payload)
                return FakeResponse()

            def close(self) -> None:
                pass

        web_research._reset_ddg_runtime()
        with patch.object(web_research.requests, "Session", return_value=FakeSession()):
            first = web_research._ddg_mcp_search("https://ddg.example", "first", 0.1)
            second = web_research._ddg_mcp_search("https://ddg.example", "second", 0.1)
        web_research._reset_ddg_runtime()
        self.assertEqual(calls.count("initialize"), 1)
        self.assertEqual(calls.count("notifications/initialized"), 1)
        self.assertEqual(calls.count("tools/call"), 2)
        self.assertEqual([first[0]["title"], second[0]["title"]], ["first", "second"])

    def test_page_snapshot_reuses_only_successful_content(self) -> None:
        successful = [("ok", "正文")]
        with patch.object(web_research, "_fetch_page_uncached", side_effect=successful) as fetch:
            web_research._reset_run_snapshot()
            first = web_research._fetch_page("https://example.com/a", 0.1)
            second = web_research._fetch_page("https://example.com/a", 0.1)
        self.assertEqual((first, second), (("ok", "正文"), ("ok", "正文")))
        fetch.assert_called_once()

        with patch.object(web_research, "_fetch_page_uncached", side_effect=[("timeout", ""), ("ok", "正文")]) as fetch:
            web_research._reset_run_snapshot()
            first = web_research._fetch_page("https://example.com/b", 0.1)
            second = web_research._fetch_page("https://example.com/b", 0.1)
        self.assertEqual((first, second), (("timeout", ""), ("ok", "正文")))
        self.assertEqual(fetch.call_count, 2)

    def test_parallel_gap_collection_preserves_targets_queries_and_order(self) -> None:
        targets = [
            {"factor_key": "F1", "subfactor_key": "era_track", "label": "时代赛道", "maximum": 5, "original_status": "需人工确认"},
            {"factor_key": "F2", "subfactor_key": "controller_action", "label": "实控人行为", "maximum": 5, "original_status": "需人工确认"},
            {"factor_key": "F3", "subfactor_key": "leadership", "label": "行业地位", "maximum": 5, "original_status": "需人工确认"},
        ]
        calls: list[tuple[str, str]] = []

        def fake_search(_provider: str, query: str, _timeout: float, cache_scope: str = ""):
            calls.append((cache_scope, query))
            return "duckduckgo_html", [], []

        with patch.object(web_research, "_search", side_effect=fake_search), \
             patch.object(web_research, "MAX_GAP_BUDGET_SECONDS", 30.0):
            result = web_research._collect_gap_targets("000001", "测试", "测试行业", targets, "auto", 1)

        selected, _ = web_research._select_gap_targets(targets)
        expected_keys = [f"{item['factor_key']}.{item['subfactor_key']}" for item in selected]
        actual_keys = [f"{item['factor_key']}.{item['subfactor_key']}" for item in result["web_gap_results"]]
        self.assertEqual(actual_keys, expected_keys)
        self.assertEqual(len(calls), len(selected) * web_research.MAX_GAP_QUERIES_PER_TARGET)
        self.assertEqual(
            {scope for scope, _ in calls},
            {f"000001|{key}" for key in expected_keys},
        )
        for item in result["web_gap_results"]:
            key = f"{item['factor_key']}.{item['subfactor_key']}"
            self.assertEqual(item["queries"], [
                *search_rules.queries_for(key, "测试", "000001", "测试行业"),
                web_research._china_finance_query(key, "测试", "000001", "测试行业"),
            ][:web_research.MAX_GAP_QUERIES_PER_TARGET])

    def test_specialized_status_gets_a_search_slot_before_target_limit(self) -> None:
        targets = [
            {"factor_key": "F1", "subfactor_key": f"gap_{i}", "label": f"F1-{i}", "maximum": 5, "original_status": "需人工确认"}
            for i in range(8)
        ] + [
            {"factor_key": "F3", "subfactor_key": "specialized", "label": "专精特新/单项冠军", "maximum": 2, "original_status": "需人工确认"},
            {"factor_key": "F3", "subfactor_key": "leadership", "label": "龙头/核心供应商", "maximum": 5, "original_status": "需人工确认"},
            {"factor_key": "F5", "subfactor_key": "expectation_gap", "label": "预期差", "maximum": 1.5, "original_status": "需人工确认"},
        ]
        selected, skipped = web_research._select_gap_targets(targets, limit=10)
        selected_keys = {f"{item['factor_key']}.{item['subfactor_key']}" for item in selected}
        skipped_keys = {f"{item['factor_key']}.{item['subfactor_key']}" for item in skipped}
        self.assertIn("F3.specialized", selected_keys)
        self.assertNotIn("F3.specialized", skipped_keys)

    def test_china_finance_sources_are_tiered_and_prioritized(self) -> None:
        self.assertEqual(web_research._source_role("static.cninfo.com.cn"), ("法定信息披露", "A"))
        self.assertEqual(web_research._source_role("news.cls.cn"), ("财经媒体", "B"))
        self.assertEqual(web_research._source_role("guba.eastmoney.com"), ("线索来源", "C"))
        rows = [
            {"url": "https://xueqiu.com/a", "rank": 1},
            {"url": "https://www.cls.cn/a", "rank": 2},
            {"url": "https://static.cninfo.com.cn/a", "rank": 3},
        ]
        ordered = web_research._prioritize_search_rows(rows)
        self.assertEqual([web_research._domain(row["url"]) for row in ordered], [
            "static.cninfo.com.cn", "cls.cn", "xueqiu.com",
        ])

    def test_full_framework_reaches_100_and_root(self) -> None:
        card = score_evidence(full_evidence())
        self.assertEqual(card.base_score, 90)
        self.assertEqual(card.adjustment_score, 10)
        self.assertEqual(card.final_score, 100)
        self.assertEqual(card.rating, "根")
        self.assertEqual(next(factor for factor in card.factors if factor.key == "F5").score, 10)
        self.assertEqual(next(factor for factor in card.factors if factor.key == "F6").score, 10)
        self.assertEqual([factor.key for factor in card.factors], ["F1", "F2", "F3", "F4", "F5", "F6"])
        self.assertEqual(sum(len(factor.subfactors) for factor in card.factors), 28)

    def test_missing_evidence_is_unknown_not_negative(self) -> None:
        card = score_evidence({"metric_sources": {}})
        self.assertEqual(card.base_score, 0)
        self.assertTrue(all(item.score == 0 for factor in card.factors for item in factor.subfactors))
        self.assertTrue(all(item.status == "需人工确认" for factor in card.factors for item in factor.subfactors))
        self.assertEqual(card.unknown_maximum, 100)
        self.assertEqual(card.coverage, 0)
        self.assertEqual(card.research_score, 0)
        self.assertEqual(card.rating, "不碰")
        self.assertEqual(card.action_rating, "卖出")
        self.assertEqual(card.legacy_rating, "不碰")

    def test_f5_modifiers_are_bounded_to_ten_points(self) -> None:
        positive = score_evidence(full_evidence())
        self.assertEqual(positive.adjustment_score, 10)
        negative_data = full_evidence()
        negative_data.update({
            "alpha_score": -1, "price_percentile_3y": 0.95, "attention_heat": 0.95,
            "verified_catalyst_count": 0, "trap_risk_level": "高", "ma_structure": "bearish",
            "momentum_20d": -0.10, "ma20_slope_5d": -0.03, "volume_ratio_20d": 1.4,
            "alpha_trend": "下降", "technical_signal": "清仓", "technical_position": 0.9,
            "technical_overheat": True, "technical_structure_score": 0,
        })
        negative = score_evidence(negative_data)
        self.assertGreaterEqual(negative.adjustment_score, 0)
        self.assertLessEqual(negative.adjustment_score, 10)
        self.assertEqual(negative.adjustment_score, 0)
        self.assertEqual(negative.final_score, negative.base_score)

    def test_institutional_direction_uses_two_methods(self) -> None:
        card = score_evidence(full_evidence())
        institutional = next(item for item in card.adjustments if item.key == "institutional_direction")
        self.assertEqual(institutional.score, 2)
        self.assertIn("量化选股筛选=看多", institutional.reason)
        self.assertIn("投资逻辑追踪=看多", institutional.reason)

    def test_institutional_direction_is_separate_from_technical_structure(self) -> None:
        data = full_evidence()
        data.update({
            "ma_structure": "bearish", "momentum_20d": -0.10, "ma20_slope_5d": -0.03,
            "volume_ratio_20d": 1.4, "alpha_trend": "下降", "technical_signal": "清仓",
            "technical_position": 0.9, "technical_overheat": True, "technical_structure_score": 4,
        })
        card = score_evidence(data)
        adjustments = {item.key: item for item in card.adjustments}
        self.assertEqual(adjustments["institutional_direction"].score, 0)
        self.assertEqual(adjustments["technical_structure"].score, 4)

    def test_missing_institutional_methods_do_not_create_score(self) -> None:
        data = full_evidence()
        for key in ("ma_structure", "momentum_20d", "ma20_slope_5d", "volume_ratio_20d",
                    "alpha_trend", "technical_signal", "technical_position", "technical_overheat"):
            data.pop(key, None)
        card = score_evidence(data)
        institutional = next(item for item in card.adjustments if item.key == "institutional_direction")
        self.assertEqual(institutional.score, 0)
        self.assertEqual(institutional.status, "需人工确认")

    def test_high_trap_risk_zeros_sentiment_score(self) -> None:
        data = full_evidence()
        data["trap_risk_level"] = "高"
        card = score_evidence(data)
        sentiment = next(item for item in card.adjustments if item.key == "sentiment")
        self.assertEqual(sentiment.score, 0)

    def test_social_heat_alone_is_not_treated_as_positive_or_negative(self) -> None:
        data = full_evidence()
        data.update({"price_percentile_3y": 0.5, "attention_heat": 0.9, "social_heat": 0.9})
        card = score_evidence(data)
        sentiment = next(item for item in card.adjustments if item.key == "sentiment")
        self.assertEqual(sentiment.score, 1)

    def test_low_price_cold_attention_and_sound_f1_is_plus_two(self) -> None:
        data = full_evidence()
        data.update({"price_percentile_3y": 0.2, "attention_heat": 0.2, "social_heat": 0.2})
        card = score_evidence(data)
        sentiment = next(item for item in card.adjustments if item.key == "sentiment")
        self.assertEqual(sentiment.score, 2)

    def test_zero_announcement_catalysts_ignore_web_research_status(self) -> None:
        reasons = set()
        for web_status in ("completed", "unavailable"):
            data = full_evidence()
            data.update({"verified_catalyst_count": 0, "web_research_status": web_status})
            catalyst = next(item for item in score_evidence(data).adjustments if item.key == "catalyst")
            self.assertEqual(catalyst.score, 0)
            self.assertEqual(catalyst.status, "部分覆盖")
            self.assertNotIn("网页", catalyst.reason)
            reasons.add(catalyst.reason)
        self.assertEqual(reasons, {"公告标题未发现可验证催化"})

    def test_st_hard_cap(self) -> None:
        data = full_evidence()
        data["st_risk"] = True
        self.assertEqual(score_evidence(data).rating, "不碰")

    def test_controller_reduction_hard_cap(self) -> None:
        data = full_evidence()
        data["controller_action"] = "reduction"
        self.assertEqual(score_evidence(data).rating, "学习仓")

    def test_factor_floor_does_not_trigger_hard_cap(self) -> None:
        data = full_evidence()
        data.update({"track_strength": 0, "industry_cagr_3y": 0.05, "penetration_rate": 0.8,
                     "chain_stage": "downstream", "supply_tightening": False, "supply_cr3": 20,
                     "capacity_expansion_cycle_years": 0.5, "chokepoint_score": 0,
                     "capex_strength": 0, "capex_yoy": -0.1})
        card = score_evidence(data)
        self.assertNotIn("F1 < 15 或 F3 < 8", {item["condition"] for item in card.hard_caps})
        self.assertNotEqual(card.rating_reason, "综合分 0 对应不碰；F1 < 15 或 F3 < 8，评级最高为学习仓")

    def test_stale_congestion_does_not_cap(self) -> None:
        data = full_evidence()
        data.update({"price_percentile_3y": 0.9, "attention_heat": 0.2, "market_congestion": 0.95,
                     "market_congestion_fresh": False})
        cap = score_evidence(data).hard_caps[-1]
        self.assertEqual(cap["result"], "需人工确认")

    def test_fresh_congestion_caps_at_spear(self) -> None:
        data = full_evidence()
        data.update({"price_percentile_3y": 0.9, "attention_heat": 0.2, "market_congestion": 0.95,
                     "market_congestion_fresh": True})
        card = score_evidence(data)
        self.assertEqual(card.hard_caps[-1]["result"], "已触发")
        self.assertEqual(card.rating, "矛")

    def test_axdata_is_opt_in(self) -> None:
        with patch.dict(os.environ, {"MODA_AXDATA": "0"}):
            self.assertFalse(axdata_provider.available())
            self.assertIsNone(axdata_provider.fetch("valuation", "300820"))

    def test_collectors_run_in_parallel(self) -> None:
        def slow_module(*_args) -> dict:
            time.sleep(0.1)
            return {"ok": True}

        started = time.perf_counter()
        with patch.object(run_pipeline, "run_module", side_effect=slow_module):
            results = run_pipeline.run_collectors([("a", "a.py", []), ("b", "b.py", []), ("c", "c.py", [])])
        self.assertEqual(len(results), 3)
        self.assertLess(time.perf_counter() - started, 0.25)

    def test_report_snapshot_reads_only_new_modules(self) -> None:
        snapshot = {"finance_data": "财务"}
        with patch("tools.scoring.evidence.read_reports", return_value={"business_data": "主营"}) as read:
            result = run_pipeline.update_report_snapshot(
                "000001",
                ("finance_data", "business_data"),
                1.0,
                snapshot,
            )
        self.assertIs(result, snapshot)
        self.assertEqual(result, {"finance_data": "财务", "business_data": "主营"})
        read.assert_called_once_with("000001", ("business_data",), 1.0)

    def test_contextual_collectors_start_followups_before_sidecars_finish(self) -> None:
        events: list[str] = []

        def fake_run(label: str, *_args) -> dict:
            if label == "slow_sidecar":
                time.sleep(0.08)
            elif label == "context":
                time.sleep(0.01)
            events.append(label)
            return {"label": label, "ok": True}

        def followups(_results: list[dict]) -> list[tuple]:
            events.append("followup_created")
            return [("followup", "followup.py", [])]

        collectors = [
            ("context", "context.py", []),
            ("slow_sidecar", "slow.py", []),
        ]
        with patch.object(run_pipeline, "run_module", side_effect=fake_run):
            first, second = run_pipeline.run_contextual_collectors(collectors, {"context"}, followups)
        self.assertEqual([item["label"] for item in first], ["context", "slow_sidecar"])
        self.assertEqual([item["label"] for item in second], ["followup"])
        self.assertLess(events.index("followup_created"), events.index("slow_sidecar"))

    def test_scoring_reads_only_current_reports(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "finance_data" / "300820.md"
            fresh = root / "tdx_analysis" / "300820.md"
            old.parent.mkdir()
            fresh.parent.mkdir()
            old.write_text("旧报告", encoding="utf-8")
            fresh.write_text("新报告", encoding="utf-8")
            started = time.time()
            os.utime(old, (started - 60, started - 60))
            with patch.object(evidence_module, "REPORT_ROOT", root):
                reports = evidence_module.read_reports("300820", ("finance_data", "tdx_analysis"), since=started)
            self.assertEqual(reports, {"tdx_analysis": "新报告"})

    def test_report_contains_required_sections(self) -> None:
        evidence = full_evidence()
        evidence["completed_modules"] = []
        card = score_evidence(evidence)
        report = grader.render_report("300820", "英杰电气", evidence, card, ())
        for heading in ("## 研究评分", "## 一句话结论与最终判断", "## 技术分析（easy-tdx 日 K）", "## 行业景气度交叉验证", "## 六层图形概览",
                        "## 六层评分卡", "## F5 低位与困境反转", "## F6 修正项", "## 舆情、社交热榜与异常推广风险",
                        "## Hard Cap 检查", "## 睡得着检查"):
            self.assertIn(heading, report)
        self.assertNotIn("原始综合分", report)
        self.assertNotIn("原始评级", report)
        conclusion = report.split("## 一句话结论与最终判断", 1)[1].split("## 六层图形概览", 1)[0]
        for number in range(1, 7):
            self.assertIn(f"{number}. ", conclusion)
        self.assertNotIn("## 最终结论", report)
        for forbidden in ("说白了", "他娘的", "我认为", "我觉得"):
            self.assertNotIn(forbidden, conclusion)
        self.assertLess(report.index("## 一句话结论与最终判断"), report.index("## 技术分析（easy-tdx 日 K）"))
        self.assertLess(report.index("## 技术分析（easy-tdx 日 K）"), report.index("## 六层图形概览"))
        self.assertIn("| 指标 | 当前读数 | 当前评价 |", report)
        self.assertNotIn("| 排名 | 指标", report)
        self.assertNotIn("A股适用性", report)
        self.assertIn("当前价格：30.00；支撑位：28.00；压力位：33.00", report)
        self.assertIn("F6 是独立的第六层，已计入研究分", report)
        self.assertIn("**1. 投资主张**\n\n", report)
        self.assertIn("**6. 行动评级与证伪条件**\n\n", report)
        self.assertIn("投资主张态度为", report)
        self.assertIn("同行竞争与为什么是它", report)
        self.assertIn("## 研究评分 📊", report)
        self.assertIn("## Hard Cap 检查 🛡️", report)
        self.assertIn("✅", report)
        self.assertNotIn("F6修正：机构方向", report)
        self.assertNotIn("## 证据覆盖与行动状态", report)
        self.assertNotIn("> 🧭 阅读顺序", report)
        self.assertNotIn("## 数据覆盖与待确认", report)
        self.assertIn("### 技术信号：", report)
        self.assertNotIn("## 机构方法交叉验证", report)
        self.assertIn("### 投资判断总览", report)
        self.assertIn("| 核心问题 | 图示 | 大白话结论 | 数据与理由 |", report)
        self.assertIn("投资主张", report)
        self.assertIn("为什么可能值得买", report)
        self.assertIn("同行竞争", report)
        self.assertIn("### 核心上下游对应表", report)
        self.assertIn("产业链：半导体设备产业链；公司位置：上游；匹配类型：待确认", report)
        self.assertIn("| 环节 | 核心内容 | 与公司的关系 | 判断 |", report)
        self.assertIn("公司主营映射到本环节：刻蚀设备、薄膜沉积设备；相关收入占比 60.0%", report)

    def test_report_uses_current_company_evidence_and_readable_numbers(self) -> None:
        evidence = full_evidence()
        evidence.update({
            "name": "航天彩虹",
            "main_business": "航空航天产品制造、无人机及相关产品、塑料薄膜制造、光学膜",
            "latest_price": 18.469999313354492,
            "pe_ttm": 675.396484375,
            "pb": 2.2423210234058315,
            "price_percentile_3y": 0.386674214959284,
            "attention_heat": 0.8461,
            "market_congestion": 0.32,
            "technical_signal": "中性/无触发",
            "technical_indicators": {
                "macd": {"state": "多头"},
                "obv": {"state": "资金中性"},
                "wr": {"state": "中性"},
            },
        })
        card = score_evidence(evidence)
        report = grader.render_report("002389", "航天彩虹", evidence, card, ())
        conclusion = report.split("## 一句话结论与最终判断", 1)[1].split("## 技术分析（easy-tdx 日 K）", 1)[0]

        self.assertIn("航天彩虹当前正式行动评级为", conclusion)
        self.assertIn("主营集中在航空航天产品制造", conclusion)
        self.assertIn("当前价格为18.47元", conclusion)
        self.assertIn("TTM PE为675.40、PB为2.24", conclusion)
        self.assertIn("个股关注度为84.61%，所属行业拥挤度为32.00%", conclusion)
        self.assertIn("缠论结构向上、MACD多头、OBV资金中性、WR中性", conclusion)
        self.assertNotIn("聚光科技", report)
        self.assertNotIn("环保监测", report)
        self.assertNotIn("缠论结构仍向下、WR处于超买区", report)

    def test_coldness_does_not_require_f3_survival_gate(self) -> None:
        data = full_evidence()
        for key in ("background_quality", "leadership_strength", "net_profit", "operating_cashflow",
                    "debt_ratio", "cash_to_debt", "st_risk", "audit_risk", "goodwill_risk", "specialized_strength"):
            data.pop(key, None)
            data["metric_sources"].pop(key, None)
        f5 = next(factor for factor in score_evidence(data).factors if factor.key == "F5")
        coldness = next(item for item in f5.subfactors if item.key == "coldness")
        self.assertEqual(coldness.score, 2)
        self.assertNotIn("生存门槛未通过", coldness.reason)

    def test_expectation_gap_does_not_require_f3_gate(self) -> None:
        data = full_evidence()
        data["attention_heat"] = 0.8
        f5 = next(factor for factor in score_evidence(data).factors if factor.key == "F5")
        gap = next(item for item in f5.subfactors if item.key == "expectation_gap")
        self.assertEqual(gap.score, 0)

    def test_expectation_gap_can_score_before_f3_is_complete(self) -> None:
        data = full_evidence()
        for key in ("background_quality", "leadership_strength", "net_profit", "operating_cashflow",
                    "debt_ratio", "cash_to_debt", "st_risk", "audit_risk", "goodwill_risk", "specialized_strength"):
            data.pop(key, None)
            data["metric_sources"].pop(key, None)
        data.update({"attention_heat": 0.2, "track_strength": 1.0, "order_growth": 10})
        f5 = next(factor for factor in score_evidence(data).factors if factor.key == "F5")
        gap = next(item for item in f5.subfactors if item.key == "expectation_gap")
        self.assertEqual(gap.score, 1.5)

    def test_missing_evidence_does_not_trigger_action_no_touch(self) -> None:
        card = score_evidence({"metric_sources": {}})
        self.assertEqual(card.action_rating, "卖出")
        self.assertFalse(any(item["result"] == "已触发" for item in card.hard_caps))

    def test_search_failure_preserves_structured_score_and_status(self) -> None:
        data = full_evidence()
        data["web_subfactor_results"] = {
            "F1.era_track": {
                "status": "搜索失败，需人工确认",
                "score": 0,
                "reason": "search_budget_exhausted",
            },
        }
        card = score_evidence(data)
        era = next(item for factor in card.factors for item in factor.subfactors if item.key == "era_track")
        self.assertEqual(era.score, 10)
        self.assertEqual(era.status, "已验证")

    def test_unverified_web_hard_cap_does_not_trigger(self) -> None:
        data = full_evidence()
        data.pop("controller_action")
        data["metric_sources"].pop("controller_action")
        data["web_subfactor_results"] = {
            "F2.controller_action": {
                "status": "网络命中（未核验）",
                "score": 0,
                "reason": "控股股东减持",
                "hard_cap_signals": {"controller_reduction": True},
            },
        }
        card = score_evidence(data)
        self.assertEqual(card.action_rating, "买入")
        self.assertEqual(next(item for item in card.hard_caps if item["condition"] == "控股股东或实控人减持")["result"], "需人工确认")

    def test_nominee_holder_is_not_negative_background(self) -> None:
        holders = [
            {"rank": 1, "name": "HKSCC NOMINEES LIMITED", "ratio": 40.38},
            {"rank": 2, "name": "王传福", "ratio": 16.90},
        ]
        metrics = market_events._holder_metrics(holders)
        self.assertTrue(metrics["background_nominee_holder"])
        self.assertNotIn("background_quality", metrics)
        self.assertIn("名义持有人", metrics["background_reason"])

    def test_precise_validation_chains_outrank_generic_industry_labels(self) -> None:
        cases = [
            ("002594", "比亚迪", "新能源汽车综合产业链"),
            ("688114", "华大智造", "生命科学仪器产业链"),
            ("688268", "华特气体", "半导体电子特气产业链"),
            ("000762", "西藏矿业", "锂资源与盐湖产业链"),
        ]
        for code, name, expected in cases:
            evidence = evidence_module.build_evidence(code, name, {
                "finance_data": f'<!-- moda_metrics: {{"industry": "综合"}} -->',
                "business_data": f'<!-- moda_business: {{"main_business": "{name}"}} -->',
            })
            self.assertEqual(evidence.get("chain_name"), expected)

    def test_confirmed_high_risk_caps_survive_unknown_coverage(self) -> None:
        data = {"metric_sources": {}, "st_risk": True}
        card = score_evidence(data)
        self.assertEqual(card.action_rating, "卖出")
        self.assertEqual(card.rating, "不碰")

    def test_industry_prosperity_only_changes_confidence_not_score(self) -> None:
        baseline = score_evidence(full_evidence())
        data = full_evidence()
        data.update({
            "industry_prosperity_status": "走弱",
            "industry_prosperity_coverage": "完整",
            "industry_prosperity_conflicts": ["利润改善但营收边际下降"],
            "industry_financial_signal": {"status": "走弱"},
            "industry_supply_signal": {"status": "走弱"},
        })
        checked = score_evidence(data)
        self.assertEqual(checked.final_score, baseline.final_score)
        realization = next(item for factor in checked.factors for item in factor.subfactors if item.key == "realization")
        self.assertEqual(realization.status, "部分覆盖")

    def test_concept_only_track_is_a_clue_not_a_score(self) -> None:
        reports = {"market_events": '<!-- moda_market_events: {"concepts": ["AI算力", "商业航天"]} -->'}
        evidence = evidence_module.build_evidence("301128", "强瑞技术", reports)
        self.assertNotIn("track_strength", evidence)
        self.assertIn("AI 算力与数据中心", evidence["track_clues"])

    def test_one_dominant_track_and_revenue_backed_chain(self) -> None:
        reports = {
            "finance_data": '<!-- moda_metrics: {"industry": "半导体"} -->',
            "business_data": '<!-- moda_business: {"main_business": "半导体设备", "business_items": ["半导体设备"], "business_breakdown": [{"category": "按产品分类", "item": "半导体设备", "revenue_ratio": 0.4}]} -->',
            "market_events": '<!-- moda_market_events: {"concepts": ["AI算力", "商业航天", "储能"]} -->',
        }
        evidence = evidence_module.build_evidence("301128", "强瑞技术", reports)
        self.assertEqual(evidence["dominant_track"], "半导体国产替代")
        self.assertNotIn("AI 算力与数据中心", evidence["track_reason"])
        self.assertGreaterEqual(evidence["business_chain_revenue_ratio"], 0.3)
        self.assertFalse(evidence["chain_partial"])

    def test_moda_track_match_supports_era_track_without_cagr(self) -> None:
        reports = {
            "finance_data": '<!-- moda_metrics: {"industry": "生物制药"} -->',
            "business_data": '<!-- moda_business: {"main_business": "创新药、生物制药", "business_items": ["创新药", "生物制药"]} -->',
        }
        evidence = evidence_module.build_evidence("300765", "测试创新药", reports)
        card = score_evidence(evidence)
        era = next(item for factor in card.factors if factor.key == "F1" for item in factor.subfactors if item.key == "era_track")
        self.assertEqual(evidence["dominant_track"], "创新药与生命科学")
        self.assertGreater(era.score, 0)
        self.assertIn("莫大选股判断", era.reason)

    def test_sw_mapping_prioritizes_auto_export_track(self) -> None:
        reports = {
            "finance_data": '<!-- moda_metrics: {"industry": "汽车零部件"} -->',
            "business_data": '<!-- moda_business: {"main_business": "新能源汽车零部件、海外市场", "business_items": ["新能源汽车零部件", "海外市场"], "overseas_revenue_ratio": 35} -->',
            "industry_prosperity": '<!-- moda_industry_prosperity: {"industry_mapping": {"status": "已验证", "sw_first_name": "汽车", "sw_second_name": "汽车零部件"}} -->',
        }
        evidence = evidence_module.build_evidence("000001", "汽车测试", reports)
        self.assertEqual(evidence["dominant_track"], "汽车电动化与出海")
        self.assertIn("申万一级：汽车", evidence["track_reason"])
        self.assertIn("申万二级：汽车零部件", evidence["track_reason"])

    def test_sw_resource_mapping_can_support_track_directly(self) -> None:
        reports = {
            "finance_data": '<!-- moda_metrics: {"industry": "能源金属"} -->',
            "business_data": '<!-- moda_business: {"main_business": "矿产资源开发"} -->',
            "industry_prosperity": '<!-- moda_industry_prosperity: {"industry_mapping": {"status": "已验证", "sw_first_name": "有色金属", "sw_second_name": "能源金属"}} -->',
        }
        evidence = evidence_module.build_evidence("000002", "资源测试", reports)
        self.assertEqual(evidence["dominant_track"], "资源与周期")
        self.assertGreater(evidence["track_strength"], 0)

    def test_sw_broad_industry_without_business_terms_is_only_clue(self) -> None:
        reports = {
            "finance_data": '<!-- moda_metrics: {"industry": "软件服务"} -->',
            "business_data": '<!-- moda_business: {"main_business": "ERP管理软件"} -->',
            "industry_prosperity": '<!-- moda_industry_prosperity: {"industry_mapping": {"status": "已验证", "sw_first_name": "计算机", "sw_second_name": "软件开发"}} -->',
        }
        evidence = evidence_module.build_evidence("000003", "软件测试", reports)
        self.assertNotIn("track_strength", evidence)
        self.assertIn("AI 算力与数据中心", evidence["track_clues"])

    def test_electronic_specialty_gas_prefers_semiconductor_supply_chain(self) -> None:
        reports = {
            "finance_data": '<!-- moda_metrics: {"industry": "电子化学品"} -->',
            "business_data": '<!-- moda_business: {"main_business": "特种气体、光刻及其他混合气体", "business_items": ["特种气体", "光刻及其他混合气体", "氢化物"], "business_breakdown": [{"category": "按产品分类", "item": "光刻及其他混合气体", "revenue_ratio": 0.22}, {"category": "按产品分类", "item": "氢化物", "revenue_ratio": 0.07}]} -->',
        }
        evidence = evidence_module.build_evidence("688268", "华特气体", reports)
        self.assertEqual(evidence["chain_name"], "半导体电子特气产业链")
        self.assertEqual(evidence["chain_stage"], "upstream")
        self.assertGreaterEqual(evidence["business_chain_revenue_ratio"], 0.29)
        self.assertIn("光刻及其他混合气体", evidence["chain_matches"][0]["specific_hits"])

    def test_web_chain_fallback_marks_unverified_semiconductor_position(self) -> None:
        reports = {
            "finance_data": '<!-- moda_metrics: {"industry": "电子化学品"} -->',
            "business_data": '<!-- moda_business: {"main_business": "特种气体", "business_items": ["特种气体"]} -->',
            "web_research": '<!-- moda_web_research: {"web_subfactor_results": {"F1.upstream": {"status": "网络命中（未核验）", "reason": "主营电子特气属于半导体关键气体供应链", "signals": ["电子特气", "关键供应链"]}}} -->',
        }
        evidence = evidence_module.build_evidence("688268", "华特气体", reports)
        self.assertEqual(evidence["chain_name"], "半导体电子特气产业链")
        self.assertEqual(evidence["chain_stage"], "upstream")
        self.assertTrue(evidence["chain_partial"])
        self.assertIn("SearXNG + DuckDuckGo MCP", evidence["metric_sources"]["chain_name"])

    def test_unconfirmed_business_revenue_caps_chain_match(self) -> None:
        data = full_evidence()
        data.update({"business_chain_match": 1.0, "business_match_partial": True})
        f4 = next(factor for factor in score_evidence(data).factors if factor.key == "F4")
        business_match = next(item for item in f4.subfactors if item.key == "business_match")
        self.assertEqual(business_match.score, 2)
        self.assertEqual(business_match.status, "部分覆盖")

    def test_report_progress_bars_are_bounded_and_complete(self) -> None:
        self.assertEqual(grader._progress_bar(-1, 100, 10), "░" * 10)
        self.assertEqual(grader._progress_bar(50, 100, 10), "█" * 5 + "░" * 5)
        self.assertEqual(grader._progress_bar(101, 100, 10), "█" * 10)

        evidence = full_evidence()
        evidence["completed_modules"] = []
        card = score_evidence(evidence)
        report = grader.render_report("301128", "强瑞技术", evidence, card, ())
        overview = report.split("## 六层图形概览", 1)[1].split("## 六层评分卡", 1)[0]
        for factor in card.factors:
            self.assertIn(factor.key, overview)
            self.assertIn(grader._progress_bar(factor.score, factor.maximum), overview)

    def test_web_supply_requires_two_domains_categories_and_authority(self) -> None:
        records = [
            {"fetch_status": "ok", "domain": "cninfo.com.cn", "source_tier": "A", "supply_categories": ["orders"], "supply_direction": "tightening"},
            {"fetch_status": "ok", "domain": "example.com", "source_tier": "B", "supply_categories": ["capacity"], "supply_direction": "tightening"},
        ]
        result = web_research._validate_supply(records)
        self.assertEqual(result["status"], "已验证")
        self.assertTrue(result["tightening"])

    def test_web_supply_rejects_duplicate_domain_or_single_category(self) -> None:
        duplicate_domain = [
            {"fetch_status": "ok", "domain": "cninfo.com.cn", "source_tier": "A", "supply_categories": ["orders"], "supply_direction": "tightening"},
            {"fetch_status": "ok", "domain": "cninfo.com.cn", "source_tier": "A", "supply_categories": ["capacity"], "supply_direction": "tightening"},
        ]
        single_category = [
            {"fetch_status": "ok", "domain": "cninfo.com.cn", "source_tier": "A", "supply_categories": ["orders"], "supply_direction": "tightening"},
            {"fetch_status": "ok", "domain": "example.com", "source_tier": "B", "supply_categories": ["orders"], "supply_direction": "tightening"},
        ]
        self.assertEqual(web_research._validate_supply(duplicate_domain)["status"], "需人工确认")
        self.assertEqual(web_research._validate_supply(single_category)["status"], "需人工确认")

    def test_web_supply_conflicting_directions_do_not_score(self) -> None:
        records = [
            {"fetch_status": "ok", "domain": "cninfo.com.cn", "source_tier": "A", "supply_categories": ["orders"], "supply_direction": "tightening"},
            {"fetch_status": "ok", "domain": "example.com", "source_tier": "B", "supply_categories": ["capacity"], "supply_direction": "loosening"},
        ]
        self.assertEqual(web_research._validate_supply(records)["status"], "证据冲突")

    def test_statutory_disclosure_is_high_confidence(self) -> None:
        self.assertEqual(web_research._source_role("cninfo.com.cn"), ("法定信息披露", "A"))
        self.assertEqual(web_research._source_role("www.szse.cn"), ("法定信息披露", "A"))
        row = web_research._classify({
            "url": "https://static.cninfo.com.cn/finalpage/example.pdf",
            "content": "强瑞技术的半导体设备产品用于国产替代，具体产品已经量产",
        }, "强瑞技术")
        self.assertEqual(row["source_role"], "法定信息披露")
        self.assertTrue(row["company_product_relation"])

    def test_financial_forums_are_clue_only_and_cannot_confirm(self) -> None:
        for domain in ("xueqiu.com", "guba.eastmoney.com", "news.gw.com.cn"):
            self.assertEqual(web_research._source_role(domain), ("线索来源", "C"))
        records = [
            {"fetch_status": "ok", "domain": "cninfo.com.cn", "source_tier": "A",
             "source_role": "法定信息披露", "supply_categories": ["orders"], "supply_direction": "tightening"},
            {"fetch_status": "ok", "domain": "xueqiu.com", "source_tier": "C",
             "source_role": "线索来源", "supply_categories": ["capacity"], "supply_direction": "tightening"},
        ]
        result = web_research._validate_supply(records)
        self.assertEqual(result["status"], "需人工确认")
        self.assertEqual(result["evidence_count"], 1)

    def test_web_chokepoint_requires_company_and_industry_crosscheck(self) -> None:
        records = [
            {"fetch_status": "ok", "domain": "cninfo.com.cn", "source_tier": "A", "company_product_relation": True, "industry_dependency": False},
            {"fetch_status": "ok", "domain": "miit.gov.cn", "source_tier": "A", "company_product_relation": False, "industry_dependency": True},
        ]
        result = web_research._validate_chokepoint(records)
        self.assertEqual(result["status"], "已验证")
        self.assertEqual(result["score"], 80)

    def test_web_risk_requires_company_and_authority_body(self) -> None:
        records = [
            {"fetch_status": "ok", "source_tier": "A", "company_named": True,
             "risk_signals": {"delisting": [], "audit": ["保留意见"], "goodwill": []}},
            {"fetch_status": "ok", "source_tier": "B", "company_named": True,
             "risk_signals": {"delisting": ["退市风险警示"], "audit": [], "goodwill": []}},
        ]
        result = web_research._validate_risk(records)
        self.assertEqual(result["status"], "已验证")
        self.assertTrue(result["audit_risk"])
        self.assertIsNone(result["st_risk"])

    def test_web_goodwill_keyword_does_not_override_low_current_goodwill(self) -> None:
        reports = {
            "finance_data": '<!-- moda_metrics: {"goodwill_to_assets": 0.0, "goodwill_risk": false} -->',
            "web_research": '<!-- moda_web_research: {"web_risk_validation": {"status": "已验证", "goodwill_risk": true}} -->',
        }
        evidence = evidence_module.build_evidence("300179", "四方达", reports)
        self.assertFalse(evidence["goodwill_risk"])
        self.assertTrue(evidence["web_goodwill_risk_conflict"])

    def test_web_risk_ignores_report_template_and_unqualified_goodwill_text(self) -> None:
        harmless = web_research._classify({
            "url": "https://static.cninfo.com.cn/example.pdf",
            "content": "强瑞技术 非标准审计意见提示 适用 不适用。审计意见为：标准的无保留意见。公司执行商誉减值测试。",
        }, "强瑞技术")
        self.assertEqual(harmless["risk_signals"]["audit"], [])
        self.assertEqual(harmless["risk_signals"]["goodwill"], [])

        risky = web_research._classify({
            "url": "https://static.cninfo.com.cn/example.pdf",
            "content": "强瑞技术被出具保留意见，并计提了相关商誉减值准备。",
        }, "强瑞技术")
        self.assertTrue(risky["risk_signals"]["audit"])
        self.assertTrue(risky["risk_signals"]["goodwill"])

    def test_web_specialized_requires_authority_and_company_name(self) -> None:
        valid = [{"fetch_status": "ok", "source_tier": "A", "company_named": True, "specialized_labels": ["专精特新小巨人"]}]
        invalid = [{"fetch_status": "ok", "source_tier": "B", "company_named": True, "specialized_labels": ["专精特新小巨人"]}]
        self.assertEqual(web_research._validate_specialized(valid)["strength"], 1.0)
        self.assertEqual(web_research._validate_specialized(invalid)["status"], "需人工确认")

    def test_announcement_title_without_date_or_hard_detail_is_only_a_clue(self) -> None:
        events = announcement_rules.extract_announcement_events([
            {"date": "", "title": "公司扩产项目公告"},
            {"date": "2026-08-03", "title": "公司新增产能10000吨，项目投产"},
        ])
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["hard_detail"])
        self.assertEqual(events[0]["category"], "capacity")

    def test_web_catalyst_requires_authority_company_event_and_fresh_date(self) -> None:
        valid = [{"fetch_status": "ok", "source_tier": "A", "company_named": True,
                  "catalyst_categories": ["orders"], "evidence_fresh": True}]
        stale = [{**valid[0], "evidence_fresh": False}]
        self.assertEqual(web_research._validate_catalysts(valid)["verified_count"], 1)
        self.assertEqual(web_research._validate_catalysts(stale)["status"], "需人工确认")

    def test_legacy_web_catalyst_validation_does_not_enter_f6(self) -> None:
        reports = {
            "announcements": '<!-- moda_metrics: {"verified_catalyst_count": 0, "announcement_coverage_complete": true} -->',
            "web_research": '<!-- moda_web_research: {"web_catalyst_validation": {"status": "已验证", "verified_count": 2}} -->',
        }
        evidence = evidence_module.build_evidence("300179", "四方达", reports)
        self.assertEqual(evidence["verified_catalyst_count"], 0)
        catalyst = next(item for item in score_evidence(evidence).adjustments if item.key == "catalyst")
        self.assertEqual(catalyst.score, 0)
        self.assertNotIn("网页", catalyst.reason)

    def test_search_timeout_and_http_error_degrade_cleanly(self) -> None:
        with patch.dict(os.environ, {"SEARXNG_URL": "https://search.example", "DDG_MCP_URL": "", "MODA_PUBLIC_SEARCH": "off"}, clear=False), \
             patch.object(web_research, "_searxng_search", side_effect=TimeoutError):
            used, rows, errors = web_research._search("auto", "test", 0.1)
        self.assertEqual(used, "none")
        self.assertEqual(rows, [])
        self.assertIn("searxng:TimeoutError", errors)

        with patch.dict(os.environ, {"SEARXNG_URL": "https://search.example", "DDG_MCP_URL": "", "MODA_PUBLIC_SEARCH": "off"}, clear=False), \
             patch.object(web_research, "_searxng_search", side_effect=requests.HTTPError("403 Forbidden")):
            used, rows, errors = web_research._search("auto", "test", 0.1)
        self.assertEqual((used, rows), ("none", []))
        self.assertIn("searxng:HTTPError", errors)

    def test_pdf_text_reader_stops_at_page_limit(self) -> None:
        pages = [SimpleNamespace(extract_text=lambda value=str(index): value) for index in range(web_research.MAX_PDF_PAGES + 3)]
        with patch.object(web_research, "PdfReader", return_value=SimpleNamespace(pages=pages)):
            text = web_research._read_pdf_text(b"pdf")
        self.assertEqual(text.split(), [str(index) for index in range(web_research.MAX_PDF_PAGES)])

    def test_pdf_extraction_timeout_terminates_worker(self) -> None:
        class FakeConnection:
            def __init__(self, ready: bool = False) -> None:
                self.ready = ready
                self.closed = False

            def poll(self, timeout: float) -> bool:
                return self.ready

            def close(self) -> None:
                self.closed = True

        class FakeProcess:
            def __init__(self) -> None:
                self.daemon = False
                self.started = False
                self.terminated = False

            def start(self) -> None:
                self.started = True

            def terminate(self) -> None:
                self.terminated = True

            def join(self, timeout: float) -> None:
                pass

            def is_alive(self) -> bool:
                return self.started and not self.terminated

            def kill(self) -> None:
                self.terminated = True

        receive_connection = FakeConnection()
        send_connection = FakeConnection()
        process = FakeProcess()
        context = SimpleNamespace(
            Pipe=lambda duplex: (receive_connection, send_connection),
            Process=lambda target, args: process,
        )
        with patch.object(web_research.multiprocessing, "get_context", return_value=context):
            result = web_research._extract_pdf_text(b"pdf", 0.01)
        self.assertEqual(result, ("pdf_timeout", ""))
        self.assertTrue(process.terminated)
        self.assertTrue(receive_connection.closed)
        self.assertTrue(send_connection.closed)

    def test_public_search_fallback_is_used_without_local_backend(self) -> None:
        row = [{"title": "公开搜索结果", "url": "https://example.com", "snippet": "测试"}]
        with patch.dict(os.environ, {"SEARXNG_URL": "", "DDG_MCP_URL": "", "MODA_PUBLIC_SEARCH": "auto"}, clear=False), \
             patch.object(web_research, "_duckduckgo_html_search", return_value=row):
            used, rows, errors = web_research._search("auto", "test", 0.1)
        self.assertEqual((used, rows), ("duckduckgo_html", row))
        self.assertEqual(errors, [])

    def test_industry_alias_maps_non_shenwan_label(self) -> None:
        from tools.akshare import congestion, industry_prosperity

        raw = {"sw_second": [{"行业名称": "软件开发", "行业代码": "801080", "上级行业": "计算机"}],
               "sw_first": [{"行业名称": "计算机", "行业代码": "801080"}]}
        mapped = industry_prosperity.map_industry("软件服务 / 行业应用软件", raw)
        self.assertEqual(mapped["status"], "已验证")
        self.assertEqual(mapped["sw_second_name"], "软件开发")
        rows = [{"sw_second_name": "软件开发", "sw_second_code": "801080", "sw_first_code": "801080"}]
        self.assertEqual(congestion._map_industry("软件服务", rows)["status"], "已验证")

    def test_duckduckgo_mcp_numbered_results_are_parsed(self) -> None:
        text = "Found 2 search results:\n\n1. 标题一\n   URL: https://example.com/a\n   Summary: 摘要一\n\n2. 标题二\n   URL: https://example.org/b\n   Summary: 摘要二\n"
        rows = web_research._parse_ddg_text(text)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["title"], "标题一")
        self.assertEqual(rows[1]["url"], "https://example.org/b")

    def test_web_fallback_does_not_override_complete_structured_supply(self) -> None:
        supply = '<!-- moda_supply_demand: {"supply_evidence_count": 3, "supply_tightening": false} -->'
        web = '<!-- moda_web_research: {"web_supply_validation": {"status": "已验证", "evidence_count": 2, "tightening": true}} -->'
        evidence = evidence_module.build_evidence("000001", "测试", {"supply_demand": supply, "web_research": web})
        self.assertEqual(evidence["supply_evidence_count"], 3)
        self.assertFalse(evidence["supply_tightening"])
        self.assertNotIn("supply_web_fallback", evidence)

    def test_web_fallback_can_fill_missing_supply_and_chokepoint(self) -> None:
        web = (
            '<!-- moda_web_research: {"web_supply_validation": {"status": "已验证", "evidence_count": 2, '
            '"tightening": true}, "web_chokepoint_validation": {"status": "已验证", "score": 80}} -->'
        )
        evidence = evidence_module.build_evidence("000001", "测试", {"web_research": web})
        self.assertEqual(evidence["supply_evidence_count"], 2)
        self.assertTrue(evidence["supply_tightening"])
        self.assertEqual(evidence["chokepoint_score"], 80)

    def test_quarterly_kline_reuses_daily_frame(self) -> None:
        dates = pd.date_range("2024-01-01", periods=400, freq="D")
        daily = pd.DataFrame(
            {"date": dates, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 10.0, "amount": 15.0}
        )
        with patch.object(finance_data, "fetch_kline_daily", side_effect=AssertionError("network fetch not expected")):
            quarterly = finance_data.fetch_kline_quarterly("300820", daily)
        self.assertFalse(quarterly.empty)

    def test_tdx_report_accepts_numpy_score_array(self) -> None:
        dates = pd.date_range("2025-01-01", periods=180, freq="D")
        close = np.linspace(20, 30, len(dates)) + np.sin(np.arange(len(dates)) / 5)
        frame = pd.DataFrame({
            "date": dates, "open": close - 0.2, "high": close + 0.5,
            "low": close - 0.5, "close": close, "volume": np.linspace(1000, 2000, len(dates)),
        })
        analyzer = AlphaSorosAnalyzer(frame, "测试", "000001")
        self.assertIsInstance(analyzer.A_PINGFEN, np.ndarray)
        report = analyzer.generate_report()
        self.assertIn("moda_technical", report)
        self.assertIn("缠论（日线简化结构）", report)
        for indicator in ("OBV", "30日BIAS", "MACD", "BOLL", "ATR", "DMI", "RSI", "WR"):
            self.assertIn(indicator, report)
        self.assertIn("当前价", report)
        self.assertIn("支撑位", report)
        self.assertIn("压力位", report)
        self.assertIn("技术结构得分", report)

    def test_finance_metrics_preserve_goodwill_risk_boolean(self) -> None:
        assets = pd.DataFrame([{"资产总计": 100.0, "负债合计": 20.0, "货币资金": 10.0, "商誉": 5.0}])
        metrics = finance_data._report_metrics("000001", {}, {}, pd.DataFrame(), pd.DataFrame(), {"fzb": assets})
        self.assertIs(metrics["goodwill_risk"], False)

    def test_finance_metrics_accept_current_sina_profit_aliases(self) -> None:
        income = pd.DataFrame([
            {"营业收入_同比": 0.20, "归属于母公司的净利润": 12.0, "归属于母公司的净利润_同比": 0.30},
            {"营业收入_同比": 0.10, "归属于母公司的净利润": 10.0, "归属于母公司的净利润_同比": 0.05},
        ])
        metrics = finance_data._report_metrics("000001", {}, {}, pd.DataFrame(), pd.DataFrame(), {"lrb": income})
        self.assertEqual(metrics["net_profit"], 12.0)
        self.assertEqual(metrics["profit_yoy"], 0.30)
        self.assertAlmostEqual(metrics["profit_yoy_delta"], 0.25)

    def test_three_year_price_percentile_requires_720_trading_rows(self) -> None:
        short = pd.DataFrame({"close": np.linspace(10, 20, 719)})
        complete = pd.DataFrame({"close": np.linspace(10, 20, 720)})
        short_metrics = finance_data._report_metrics("000001", {}, {}, short, pd.DataFrame(), {})
        complete_metrics = finance_data._report_metrics("000001", {}, {}, complete, pd.DataFrame(), {})
        self.assertNotIn("price_percentile_3y", short_metrics)
        self.assertIn("price_percentile_3y", complete_metrics)

    def test_single_pb_component_cannot_earn_full_valuation_score(self) -> None:
        data = full_evidence()
        for key in ("pe_ttm", "peer_pe_ttm_median", "pe_percentile_5y", "pb_percentile_5y"):
            data.pop(key, None)
            data["metric_sources"].pop(key, None)
        valuation = next(item for factor in score_evidence(data).factors for item in factor.subfactors if item.key == "valuation")
        self.assertEqual(valuation.score, 1)
        self.assertEqual(valuation.status, "部分覆盖")
        self.assertGreater(valuation.unknown_maximum, 0)

    def test_valuation_without_historical_percentiles_keeps_unknown_headroom(self) -> None:
        data = full_evidence()
        for key in ("pe_percentile_5y", "pb_percentile_5y", "pb_to_5y_median"):
            data.pop(key, None)
            data["metric_sources"].pop(key, None)
        valuation = next(item for factor in score_evidence(data).factors for item in factor.subfactors if item.key == "valuation")
        self.assertEqual(valuation.status, "部分覆盖")
        self.assertGreater(valuation.unknown_maximum, 0)

    def test_high_valuation_and_weak_technical_structure_still_deduct(self) -> None:
        data = full_evidence()
        data.update({
            "pe_ttm": 120,
            "peer_pe_ttm_median": 90,
            "pb": 8,
            "pe_percentile_5y": 0.95,
            "pb_to_5y_median": 1.6,
            "technical_structure_score": 0,
            "technical_structure_reason": "趋势走弱，缠论向下",
        })
        card = score_evidence(data)
        valuation = next(item for factor in card.factors for item in factor.subfactors if item.key == "valuation")
        technical = next(item for item in card.adjustments if item.key == "technical_structure")
        self.assertEqual(valuation.score, 0)
        self.assertEqual(technical.score, 0)
        self.assertLess(card.final_score, score_evidence(full_evidence()).final_score)

    def test_supply_details_do_not_crash_f3_scoring(self) -> None:
        data = full_evidence()
        data.update({"supply_cr3": 65, "capacity_expansion_cycle_years": 2, "capacity_utilization_trend": "上升"})
        card = score_evidence(data)
        self.assertEqual(next(factor for factor in card.factors if factor.key == "F3").maximum, 20)

    def test_missing_region_disclosure_does_not_become_zero_overseas_revenue(self) -> None:
        frame = pd.DataFrame([{
            "REPORT_DATE": pd.Timestamp("2025-12-31"), "MAINOP_TYPE": "2", "ITEM_NAME": "设备",
            "MBI_RATIO": 1.0, "GROSS_RPOFIT_RATIO": 0.3,
        }])
        self.assertNotIn("overseas_revenue_ratio", business_data.build_structured(frame))
        domestic = pd.concat([frame, pd.DataFrame([{
            "REPORT_DATE": pd.Timestamp("2025-12-31"), "MAINOP_TYPE": "3", "ITEM_NAME": "中国大陆",
            "MBI_RATIO": 1.0, "GROSS_RPOFIT_RATIO": 0.3,
        }])], ignore_index=True)
        self.assertEqual(business_data.build_structured(domestic)["overseas_revenue_ratio"], 0.0)

    def test_numeric_name_does_not_prove_non_st_status(self) -> None:
        evidence = evidence_module.build_evidence("000001", "000001", {})
        self.assertNotIn("st_risk", evidence)
        report = '<!-- moda_market_events: {"security_name": "*ST测试"} -->'
        evidence = evidence_module.build_evidence("000001", "000001", {"market_events": report})
        self.assertTrue(evidence["st_risk"])

    def test_market_event_failures_do_not_become_verified_zero(self) -> None:
        empty = pd.DataFrame()
        with patch.object(market_events.provider, "holder_num_change", return_value=empty), \
             patch.object(market_events.provider, "lockup_expiry", side_effect=requests.Timeout("timeout")), \
             patch.object(market_events.provider, "concept_blocks", return_value=empty), \
             patch.object(market_events.provider, "research_reports", return_value=empty), \
             patch.object(market_events, "fetch_pledge", side_effect=requests.Timeout("timeout")), \
             patch.object(market_events, "_ak_lockup", side_effect=requests.Timeout("timeout")), \
             patch.object(market_events, "_ak_pledge", side_effect=requests.Timeout("timeout")), \
             patch.object(market_events, "fetch_fund_holding", return_value=empty), \
             patch.object(market_events, "fetch_top_holders", return_value=[]):
            structured, _ = market_events.collect("000001")
        self.assertFalse(structured["pledge_fetch_ok"])
        self.assertFalse(structured["unlock_fetch_ok"])
        self.assertNotIn("pledge_ratio", structured)
        self.assertNotIn("unlock_ratio", structured)

    def test_successful_empty_pledge_and_unlock_are_verified_zero(self) -> None:
        empty = pd.DataFrame()
        with patch.object(market_events.provider, "holder_num_change", return_value=empty), \
             patch.object(market_events.provider, "lockup_expiry", return_value=empty), \
             patch.object(market_events.provider, "concept_blocks", return_value=empty), \
             patch.object(market_events.provider, "research_reports", return_value=empty), \
             patch.object(market_events, "fetch_pledge", return_value=empty), \
             patch.object(market_events, "fetch_fund_holding", return_value=empty), \
             patch.object(market_events, "fetch_top_holders", return_value=[]):
            structured, _ = market_events.collect("000001")
        self.assertEqual(structured["pledge_ratio"], 0.0)
        self.assertEqual(structured["unlock_ratio"], 0.0)

    def test_missing_pledge_and_unlock_ratio_columns_remain_unconfirmed(self) -> None:
        empty = pd.DataFrame()
        lockup = pd.DataFrame([{"FREE_DATE": "2026-12-31"}])
        pledge = pd.DataFrame([{"UNFREEZE_STATE": "未解押"}])
        with patch.object(market_events.provider, "holder_num_change", return_value=empty), \
             patch.object(market_events.provider, "lockup_expiry", return_value=lockup), \
             patch.object(market_events.provider, "concept_blocks", return_value=empty), \
             patch.object(market_events.provider, "research_reports", return_value=empty), \
             patch.object(market_events, "fetch_pledge", return_value=pledge), \
             patch.object(market_events, "fetch_fund_holding", return_value=empty), \
             patch.object(market_events, "fetch_top_holders", return_value=[]):
            structured, _ = market_events.collect("000001")
        self.assertNotIn("pledge_ratio", structured)
        self.assertNotIn("unlock_ratio", structured)

    def test_f4_and_f5_sources_include_all_metrics_used(self) -> None:
        data = full_evidence()
        data["business_chain_revenue_ratio"] = 0.4
        data["social_heat"] = 0.1
        data["metric_sources"].update({
            "business_chain_match": ["chains.yaml"],
            "business_chain_revenue_ratio": ["EastMoney/F10"],
            "attention_heat": ["EastMoney/stockrank"],
            "social_heat": ["公开社交热榜"],
        })
        factors = score_evidence(data).factors
        business_match = next(item for factor in factors for item in factor.subfactors if item.key == "business_match")
        coldness = next(item for factor in factors for item in factor.subfactors if item.key == "coldness")
        self.assertEqual(business_match.sources, ("chains.yaml", "EastMoney/F10"))
        self.assertEqual(coldness.sources, ("EastMoney/stockrank", "公开社交热榜", "test"))

    def test_company_peers_use_easy_tdx_industry(self) -> None:
        boards = pd.DataFrame([
            {"board_type": 4, "board_code": "880952", "board_name": "芯片"},
            {"board_type": 12, "board_code": "881285", "board_name": "其他发电设备"},
        ])
        members = pd.DataFrame([{
            "code": "300820", "name": "英杰电气", "close": 46.75,
            "pe_dynamic": 71.47, "pe_ttm": 46.61, "net_assets": 11.613,
            "total_market_cap_ab": 10_388_041_728, "eps": 0.16,
        }])
        with patch("tools.providers.easy_tdx_provider.fetch_belong_boards", return_value=boards), \
             patch("tools.providers.easy_tdx_provider.fetch_board_members", return_value=members) as fetch_members:
            info, peers = finance_data.fetch_company_and_peers("300820")
        fetch_members.assert_called_once_with("881285")
        self.assertEqual(info["行业"], "其他发电设备")
        self.assertAlmostEqual(peers.iloc[0]["市净率"], 46.75 / 11.613)

    def test_announcements_use_one_stock_request(self) -> None:
        frame = pd.DataFrame(
            [{"date": datetime.now().strftime("%Y-%m-%d"), "title": "测试公告", "type": "PDF", "url": "https://example.test"}]
        )
        with patch("tools.providers.easy_tdx_provider.fetch_announcements", return_value=frame) as fetch:
            result = announcements.fetch_announcements("300820", days=30)
        fetch.assert_called_once()
        self.assertEqual(result["total"], 1)
        self.assertTrue(result["announcement_fetch_ok"])
        self.assertTrue(result["announcement_coverage_complete"])

    def test_incomplete_announcement_coverage_cannot_prove_stable_or_audit_safe(self) -> None:
        frame = pd.DataFrame([{
            "date": datetime.now().strftime("%Y-%m-%d"), "title": f"普通公告{i}", "type": "PDF", "url": "https://example.test"
        } for i in range(announcements.ANNOUNCEMENT_PAGE_SIZE)])
        with patch("tools.providers.easy_tdx_provider.fetch_announcements", return_value=frame), \
             patch.object(announcements, "ANNOUNCEMENT_MAX_PAGES", 2):
            data = announcements.fetch_announcements("300820", days=180)
        self.assertFalse(data["announcement_coverage_complete"])
        report = announcements.generate_report("300820", "测试", {"qa_list": []}, data)
        evidence = evidence_module.build_evidence("300820", "测试", {"announcements": report})
        self.assertNotIn("controller_action", evidence)
        self.assertNotIn("audit_risk", evidence)

    def test_announcement_page_failure_marks_overall_fetch_failed(self) -> None:
        frame = pd.DataFrame([{
            "date": datetime.now().strftime("%Y-%m-%d"), "title": f"普通公告{i}", "type": "PDF", "url": "https://example.test"
        } for i in range(announcements.ANNOUNCEMENT_PAGE_SIZE)])
        with patch("tools.providers.easy_tdx_provider.fetch_announcements", side_effect=[frame, requests.Timeout("timeout")]), \
             patch.object(announcements.ak, "stock_zh_a_disclosure_report_cninfo", side_effect=requests.Timeout("timeout")):
            data = announcements.fetch_announcements("300820", days=180)
        self.assertFalse(data["announcement_fetch_ok"])
        self.assertFalse(data["announcement_coverage_complete"])
        self.assertEqual(data["fetch_state"], "failed")
        report = announcements.generate_report("300820", "测试", {"qa_list": [], "fetch_state": "failed"}, data)
        evidence = evidence_module.build_evidence("300820", "测试", {"announcements": report})
        self.assertNotIn("controller_action", evidence)
        self.assertNotIn("audit_risk", evidence)

    def test_announcement_fallback_preserves_source_chain(self) -> None:
        fallback = pd.DataFrame([{
            "公告日期": datetime.now().strftime("%Y-%m-%d"), "公告标题": "备用公告", "公告类型": "PDF", "公告链接": "https://example.test/fallback",
        }])
        with patch("tools.providers.easy_tdx_provider.fetch_announcements", side_effect=requests.Timeout("timeout")), \
             patch.object(announcements.ak, "stock_zh_a_disclosure_report_cninfo", return_value=fallback):
            data = announcements.fetch_announcements("300820", days=30)
        self.assertTrue(data["announcement_fetch_ok"])
        self.assertTrue(data["announcement_coverage_complete"])
        self.assertEqual(data["fetch_state"], "fallback_ok")
        self.assertEqual([item["status"] for item in data["source_chain"]], ["failed", "ok"])

    def test_authority_capex_requires_two_fresh_independent_domains(self) -> None:
        records = [
            {"fetch_status": "ok", "source_role": "权威来源", "source_tier": "A", "domain": "stats.gov.cn", "industry_context_match": True, "evidence_fresh": True, "industry_capex_direction": "up"},
            {"fetch_status": "ok", "source_role": "权威来源", "source_tier": "A", "domain": "miit.gov.cn", "industry_context_match": True, "evidence_fresh": True, "industry_capex_direction": "up"},
        ]
        result = web_research._validate_industry_capex(records)
        self.assertEqual(result["status"], "已验证")
        self.assertEqual(result["signal"], "上行")

    def test_verified_web_capex_overrides_unavailable_structured_placeholder(self) -> None:
        reports = {
            "announcements": '<!-- moda_announcements: {"announcement_titles": ["扩产项目公告"]} -->',
            "web_research": '<!-- moda_web_research: {"web_industry_capex_validation": {"status": "已验证", "signal": "上行"}} -->',
            "industry_prosperity": '<!-- moda_industry_prosperity: {"industry_capex_signal": "不可用"} -->',
        }
        evidence = evidence_module.build_evidence("000001", "测试", reports)
        self.assertEqual(evidence["industry_capex_signal"], "上行")
        self.assertEqual(evidence["capex_strength"], 0.5)
        self.assertTrue(evidence["capex_partial"])
        self.assertNotIn("easy_tdx/CNINFO + AKShare/CNINFO", evidence["metric_sources"]["capex_strength"])
        self.assertIn("SearXNG + DuckDuckGo MCP", evidence["metric_sources"]["capex_strength"])

if __name__ == "__main__":
    unittest.main()
