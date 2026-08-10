from __future__ import annotations

import unittest
from unittest.mock import patch

import pandas as pd
import requests

from tools.data_call import run_fallback_chain
from tools.akshare import business_data, finance_data, announcements, popularity, market_events
from tools.scoring import supply_demand


class DataFallbackContractTest(unittest.TestCase):
    def test_generic_chain_distinguishes_fallback_and_failure(self) -> None:
        def fail() -> pd.DataFrame:
            raise requests.Timeout("primary")

        success = pd.DataFrame([{"value": 1}])
        result = run_fallback_chain(
            "合同测试",
            [("primary", fail), ("fallback", lambda: success)],
            empty=lambda value: value is None or value.empty,
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.fetch_state, "fallback_ok")
        self.assertEqual([item["source"] for item in result.source_chain], ["primary", "fallback"])

        failed = run_fallback_chain(
            "合同测试",
            [("primary", fail), ("fallback", fail)],
            empty=lambda value: value is None or value.empty,
        )
        self.assertFalse(failed.ok)
        self.assertEqual(failed.fetch_state, "failed")
        self.assertEqual(len(failed.source_chain), 2)

    def test_business_field_drift_uses_akshare_fallback(self) -> None:
        drifted = pd.DataFrame([{
            "报告期": "2026-03-31", "分类类型": "按产品分类", "主营构成": "测试设备",
            "主营收入": 100, "收入比例": 0.5, "毛利率": 0.3,
        }])
        with patch("tools.akshare.business_data.requests.get", side_effect=requests.Timeout("eastmoney")), \
             patch("akshare.stock_zygc_em", return_value=drifted):
            frame = business_data.fetch_business_data("000001")
        self.assertEqual(frame.attrs["fetch_state"], "fallback_ok")
        structured = business_data.build_structured(frame)
        self.assertEqual(structured["business_items"], ["测试设备"])
        self.assertEqual(structured["business_breakdown"][0]["revenue_ratio"], 0.5)

    def test_financial_report_fallback_normalizes_report_names(self) -> None:
        fallback = pd.DataFrame([{
            "报告日期": "2026-03-31", "营业收入": 100, "营业收入同比": 0.2,
            "归属于母公司所有者的净利润": 10,
        }])
        with patch("tools.providers.easy_tdx_provider.fetch_financial_report", side_effect=requests.Timeout("sina")), \
             patch.object(finance_data.ak, "stock_financial_report_sina", return_value=fallback):
            frame = finance_data.fetch_financial_report("000001", "lrb")
        self.assertEqual(frame.attrs["fetch_state"], "fallback_ok")
        self.assertIn("报告期", frame.columns)
        self.assertIn("营业收入_同比", frame.columns)

    def test_financial_report_uses_ths_after_sina_tls_failures(self) -> None:
        ths = pd.DataFrame([
            {"report_date": "2026-03-31", "metric_name": "operating_income", "value": 100, "yoy": 0.2},
            {"report_date": "2026-03-31", "metric_name": "parent_holder_net_profit", "value": 10, "yoy": 0.1},
        ])
        with patch("tools.providers.easy_tdx_provider.fetch_financial_report", side_effect=requests.exceptions.SSLError("eof")), \
             patch.object(finance_data.ak, "stock_financial_report_sina", side_effect=requests.ConnectionError("refused")), \
             patch.object(finance_data.ak, "stock_financial_benefit_new_ths", return_value=ths):
            frame = finance_data.fetch_financial_report("000001", "lrb")
        self.assertEqual(frame.attrs["fetch_state"], "fallback_ok")
        self.assertEqual(frame.attrs["source_chain"][-1]["source"], "AKShare/同花顺")
        self.assertEqual(frame.iloc[0]["营业收入"], 100)
        self.assertEqual(frame.iloc[0]["归属于母公司的净利润"], 10)

    def test_irm_all_sources_failed_is_not_zero_fact(self) -> None:
        with patch.object(announcements.ak, "stock_irm_cninfo", side_effect=requests.Timeout("akshare")), \
             patch.object(announcements, "_fetch_cninfo_irm_http", side_effect=requests.Timeout("http")):
            result = announcements.fetch_irm_qa("000001")
        self.assertEqual(result["fetch_state"], "failed")
        self.assertEqual(result["total"], 0)
        self.assertIn("不能据此", announcements.generate_report("000001", "测试", result, {"ann_list": [], "fetch_state": "empty"}))

    def test_irm_uses_plain_http_only_after_https_failures(self) -> None:
        frame = pd.DataFrame([{"问题": "测试问题", "回答内容": "测试回答"}])

        def fallback(code: str, timeout: int = 12, scheme: str = "https") -> pd.DataFrame:
            if scheme == "https":
                raise requests.exceptions.SSLError("eof")
            return frame

        with patch.object(announcements.ak, "stock_irm_cninfo", side_effect=requests.exceptions.SSLError("eof")), \
             patch.object(announcements, "_fetch_cninfo_irm_http", side_effect=fallback):
            result = announcements.fetch_irm_qa("000001")
        self.assertEqual(result["fetch_state"], "fallback_ok")
        self.assertEqual(result["total"], 1)
        self.assertEqual([item["source"] for item in result["source_chain"]], [
            "AKShare/stock_irm_cninfo", "CNINFO/public HTTPS", "CNINFO/public HTTP",
        ])

    def test_holder_number_fallback_receives_stock_code(self) -> None:
        empty = pd.DataFrame()
        holder = pd.DataFrame([{"HOLDER_NUM": 12345, "HOLDER_NUM_RATIO": -1.2}])
        with patch.object(market_events.provider, "holder_num_change", side_effect=requests.Timeout("eastmoney")), \
             patch.object(market_events, "_ak_holder_num", return_value=holder) as fallback, \
             patch.object(market_events.provider, "lockup_expiry", return_value=empty), \
             patch.object(market_events.provider, "concept_blocks", return_value=empty), \
             patch.object(market_events.provider, "research_reports", return_value=empty), \
             patch.object(market_events, "fetch_pledge", return_value=empty), \
             patch.object(market_events, "fetch_fund_holding", return_value=empty), \
             patch.object(market_events, "fetch_top_holders", return_value=[]):
            structured, _ = market_events.collect("002422")
        fallback.assert_called_once_with("002422")
        self.assertEqual(structured["holder_count"], 12345)
        self.assertEqual(structured["holder_count_change_pct"], -1.2)

    def test_holder_number_cninfo_columns_are_normalized(self) -> None:
        raw = pd.DataFrame([{
            "证券代码": "002422", "证券简称": "科伦药业", "变动日期": "2026-03-31",
            "本期股东人数": 41048, "上期股东人数": 44067, "股东人数增幅": -6.85,
        }])
        with patch.object(market_events, "_quarter_dates", return_value=["20260331"]), \
             patch.object(market_events.ak, "stock_hold_num_cninfo", return_value=raw):
            frame = market_events._ak_holder_num("002422")
        self.assertEqual(frame.iloc[0]["SECURITY_CODE"], "002422")
        self.assertEqual(frame.iloc[0]["HOLDER_NUM"], 41048)
        self.assertEqual(frame.iloc[0]["HOLDER_NUM_RATIO"], -6.85)

    def test_popularity_failure_has_no_semantic_substitute(self) -> None:
        with patch.object(popularity.requests, "post", side_effect=requests.Timeout("rank")):
            result = popularity.collect("000001")
        self.assertEqual(result["fetch_state"], "failed")
        self.assertNotIn("attention_heat", result)
        self.assertIn("不以社交热度替代", result["attention_reason"])

    def test_market_event_fallback_keeps_status_chain(self) -> None:
        def fail() -> pd.DataFrame:
            raise requests.Timeout("primary")

        frame, status = market_events._fetch_frame_status(
            "解禁",
            fail,
            lambda: pd.DataFrame([{"FREE_DATE": "2026-12-31"}]),
        )
        self.assertFalse(frame.empty)
        self.assertEqual(status["fetch_state"], "fallback_ok")
        self.assertEqual(len(status["source_chain"]), 2)

    def test_supply_snapshot_fallback_does_not_create_history_signal(self) -> None:
        inventory = pd.DataFrame([{"日期": "2026-07-01", "库存": 100}, {"日期": "2026-07-30", "库存": 90}])
        snapshot = pd.DataFrame([{"最新价": 100}])
        with patch.object(supply_demand.ak, "futures_spot_price_daily", side_effect=requests.Timeout("spot")), \
             patch.object(supply_demand.ak, "futures_zh_spot", return_value=snapshot), \
             patch.object(supply_demand.ak, "futures_inventory_em", return_value=inventory):
            result = supply_demand.collect("铜矿")
        self.assertEqual(result["supply_fetch_status"]["spot"]["fetch_state"], "fallback_ok")
        self.assertIsNone(result["supply_tightening"])


if __name__ == "__main__":
    unittest.main()
