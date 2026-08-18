from __future__ import annotations

import unittest
from unittest.mock import patch

import pandas as pd

from tools.akshare import business_data, finance_data
from tools.scoring.model import score_evidence
from tools.scoring.thesis import build_thesis_context
from tools.test_pipeline_efficiency import full_evidence


class AksharePeerSnapshotTest(unittest.TestCase):
    def test_snapshot_uses_target_rank_and_never_promotes_samples_to_direct_peers(self) -> None:
        scale = pd.DataFrame([{
            "代码": "000001", "简称": "测试公司",
            "总市值排名": 3, "流通市值排名": 4, "营业收入排名": 1, "净利润排名": 2,
        }])
        growth = pd.DataFrame([
            {"代码": "行业平均", "简称": "行业平均", "营业收入增长率-TTM": 6.0},
            {"代码": "行业中值", "简称": "行业中值", "营业收入增长率-TTM": 4.0},
            {"代码": "000001", "简称": "测试公司", "营业收入增长率-TTM": 9.0,
             "净利润增长率-TTM": 8.0, "基本每股收益增长率-3年复合排名": 2},
            {"代码": "000002", "简称": "样本同行", "营业收入增长率-TTM": 7.0},
        ])
        dupont = pd.DataFrame([
            {"代码": "行业中值", "简称": "行业中值", "ROE-3年平均": 10.0},
            {"代码": "000001", "简称": "测试公司", "ROE-3年平均": 12.0, "ROE-3年平均排名": 2},
            {"代码": "000002", "简称": "样本同行", "ROE-3年平均": 11.0},
        ])
        valuation = pd.DataFrame([
            {"代码": "000001", "简称": "测试公司", "市盈率-TTM": 15.0},
            {"代码": "000002", "简称": "样本同行", "市盈率-TTM": 12.0},
        ])
        with patch.object(finance_data.ak, "stock_zh_scale_comparison_em", return_value=scale), \
             patch.object(finance_data.ak, "stock_zh_growth_comparison_em", return_value=growth), \
             patch.object(finance_data.ak, "stock_zh_dupont_comparison_em", return_value=dupont), \
             patch.object(finance_data.ak, "stock_zh_valuation_comparison_em", return_value=valuation):
            snapshot = finance_data.fetch_industry_peer_snapshot("000001", timeout=2)

        self.assertEqual(snapshot["fetch_state"], "ok")
        self.assertEqual(snapshot["target"]["market_cap_rank"], 3.0)
        self.assertEqual(snapshot["industry_benchmarks"]["growth"]["行业中值"]["revenue_growth_ttm"], 4.0)
        self.assertEqual([item["code"] for item in snapshot["peer_samples"]], ["000002"])
        self.assertIn("需主营与产业链位置确认", snapshot["peer_samples"][0]["status"])

        rows = finance_data._enrich_direct_peers_with_snapshot(
            [{"code": "000002", "name": "样本同行", "status": "已验证"}],
            snapshot,
        )
        self.assertEqual(rows[0]["status"], "已验证")
        self.assertEqual(rows[0]["industry_snapshot_metrics"]["roe_3y_avg"], 11.0)

    def test_broken_valuation_endpoint_is_recorded_without_losing_other_peer_facts(self) -> None:
        scale = pd.DataFrame([{"代码": "000001", "简称": "测试公司", "总市值排名": 3}])
        target = pd.DataFrame([{"代码": "000001", "简称": "测试公司", "ROE-3年平均": 12.0}])
        with patch.object(finance_data.ak, "stock_zh_scale_comparison_em", return_value=scale), \
             patch.object(finance_data.ak, "stock_zh_growth_comparison_em", return_value=target), \
             patch.object(finance_data.ak, "stock_zh_dupont_comparison_em", return_value=target), \
             patch.object(finance_data.ak, "stock_zh_valuation_comparison_em", side_effect=KeyError("EV/EBITDA-24A")):
            snapshot = finance_data.fetch_industry_peer_snapshot("000001", timeout=2)

        self.assertEqual(snapshot["fetch_state"], "ok")
        self.assertEqual(snapshot["target"]["market_cap_rank"], 3.0)
        self.assertEqual(snapshot["source_chain"]["valuation"]["fetch_state"], "failed")


class AkshareBusinessContextTest(unittest.TestCase):
    def test_ths_business_description_cross_checks_but_does_not_replace_eastmoney_revenue_data(self) -> None:
        frame = pd.DataFrame([{
            "REPORT_DATE": pd.Timestamp("2026-03-31"),
            "MAINOP_TYPE": "2",
            "ITEM_NAME": "半导体设备",
            "MAIN_BUSINESS_INCOME": 100.0,
            "MBI_RATIO": 1.0,
            "GROSS_RPOFIT_RATIO": 0.4,
        }])
        frame.attrs.update({"fetch_state": "ok", "source_chain": [], "fetch_error": None})
        profile = {"status": "已验证", "company_name": "测试公司", "industry": "半导体"}
        intro = {
            "status": "已验证",
            "main_business": "半导体设备",
            "product_types": "离子注入设备",
            "product_names": "离子注入机",
        }
        with patch.object(business_data, "fetch_business_data", return_value=frame), \
             patch.object(business_data, "fetch_company_profile_cninfo", return_value=profile), \
             patch.object(business_data, "fetch_business_intro_ths", return_value=intro):
            raw, structured = business_data.collect_business_context("600641", timeout=1)

        self.assertEqual(structured["main_business"], "半导体设备")
        self.assertEqual(structured["business_breakdown"][0]["revenue_ratio"], 1.0)
        self.assertEqual(structured["business_intro_ths"]["product_names"], "离子注入机")
        self.assertIn("双源可比对", structured["business_crosscheck"]["status"])
        self.assertEqual(structured["metric_source_overrides"]["business_intro_ths"], "AKShare/同花顺主营介绍")
        self.assertIs(raw, frame)

    def test_ths_only_fallback_is_marked_and_has_no_composition_claim(self) -> None:
        frame = pd.DataFrame()
        frame.attrs.update({"fetch_state": "failed", "source_chain": [], "fetch_error": "timeout"})
        intro = {
            "status": "已验证",
            "main_business": "设备及服务",
            "product_types": "设备、服务",
            "product_names": "核心设备",
        }
        with patch.object(business_data, "fetch_business_data", return_value=frame), \
             patch.object(business_data, "fetch_company_profile_cninfo", return_value={"status": "需人工确认"}), \
             patch.object(business_data, "fetch_business_intro_ths", return_value=intro):
            _, structured = business_data.collect_business_context("000001", timeout=1)

        self.assertEqual(structured["fetch_state"], "fallback_ok")
        self.assertIn("无收入/毛利分部数据", structured["business_fallback_reason"])
        self.assertEqual(structured["metric_source_overrides"]["main_business"], "AKShare/同花顺主营介绍")
        self.assertNotIn("business_breakdown", structured)


class AgentJudgmentV4ContextTest(unittest.TestCase):
    def test_v4_packet_exposes_profile_business_crosscheck_and_peer_snapshot(self) -> None:
        evidence = full_evidence()
        evidence.update({
            "company_profile": {"status": "已验证", "industry": "半导体", "source_tier": "A"},
            "business_intro_ths": {"status": "已验证", "product_names": "核心设备", "source_tier": "B"},
            "business_crosscheck": {"status": "双源可比对，需结合原文语义核验"},
            "industry_peer_snapshot": {
                "status": "已获取",
                "fetch_state": "ok",
                "target": {"market_cap_rank": 2},
            },
        })
        context = build_thesis_context(score_evidence(evidence), evidence).to_dict()

        self.assertEqual(context["company"]["company_profile"]["industry"], "半导体")
        self.assertEqual(context["company"]["business_intro_ths"]["product_names"], "核心设备")
        self.assertEqual(context["company"]["peer_snapshot"]["target"]["market_cap_rank"], 2)
        self.assertEqual(context["data_quality"]["industry_peer_snapshot_fetch_state"], "ok")


if __name__ == "__main__":
    unittest.main()
