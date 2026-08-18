from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd
import requests

from tools import run_pipeline
from tools.akshare import finance_data
from tools.providers import baostock_provider
from tools.scoring import evidence as evidence_module


class _Response:
    def __init__(self, fields=(), rows=(), error_code="0", error_msg="success") -> None:
        self.fields = list(fields)
        self.rows = list(rows)
        self.error_code = error_code
        self.error_msg = error_msg
        self.index = -1

    def next(self) -> bool:
        self.index += 1
        return self.index < len(self.rows)

    def get_row_data(self):
        return self.rows[self.index]


class _Client:
    def __init__(self) -> None:
        self.kline = _Response(
            baostock_provider._KLINE_FIELDS.split(","),
            [[
                "2026-08-14", "sz.000001", "10", "11", "9", "10.5", "10", "100", "1050",
                "2", "1.2", "1", "5", "8", "1.1", "2", "3", "0",
            ]],
        )

    def login(self):
        return _Response()

    def logout(self):
        return _Response()

    def query_history_k_data_plus(self, *args, **kwargs):
        return self.kline

    def query_profit_data(self, *, code, year, quarter):
        if (year, quarter) != (2026, 2):
            return _Response(("code", "pubDate", "statDate"), ())
        return _Response(
            ("code", "pubDate", "statDate", "netProfit", "roeAvg", "totalShare", "liqaShare"),
            [[code, "2026-08-10", "2026-06-30", "102", "0.1", "1000", "800"]],
        )

    def query_balance_data(self, *, code, year, quarter):
        if (year, quarter) != (2026, 2):
            return _Response(("code", "pubDate", "statDate"), ())
        return _Response(
            ("code", "pubDate", "statDate", "liabilityToAsset"),
            [[code, "2026-08-11", "2026-06-30", "0.2"]],
        )

    def query_cash_flow_data(self, *, code, year, quarter):
        if (year, quarter) != (2026, 2):
            return _Response(("code", "pubDate", "statDate"), ())
        return _Response(
            ("code", "pubDate", "statDate", "CFOToNP"),
            [[code, "2026-08-10", "2026-06-30", "0.8"]],
        )


def _kline(rows: int = 80) -> pd.DataFrame:
    dates = pd.bdate_range(end="2026-08-14", periods=rows)
    return pd.DataFrame({
        "date": dates,
        "open": 10.0,
        "high": 11.0,
        "low": 9.0,
        "close": 10.0,
        "volume": 100.0,
        "amount": 1000.0,
    })


class BaoStockProviderTest(unittest.TestCase):
    def test_security_code_rejects_unsupported_beijing_exchange(self) -> None:
        self.assertEqual(baostock_provider.security_code("600519"), "sh.600519")
        self.assertEqual(baostock_provider.security_code("000001"), "sz.000001")
        with self.assertRaisesRegex(ValueError, "Beijing Stock Exchange"):
            baostock_provider.security_code("920445")

    def test_kline_is_normalized(self) -> None:
        client = _Client()
        with patch.object(baostock_provider, "_baostock_module", return_value=client):
            frame = baostock_provider.fetch_kline_daily("000001", count=10, end_date="2026-08-15")
        self.assertEqual(frame.iloc[0]["close"], 10.5)
        self.assertEqual(frame.iloc[0]["pct_chg"], 5.0)
        self.assertEqual(frame.attrs["source_tier"], "B")

    def test_financial_summary_merges_matching_periods(self) -> None:
        client = _Client()
        with patch.object(baostock_provider, "_baostock_module", return_value=client), \
             patch.object(baostock_provider, "_recent_quarters", return_value=iter([(2026, 2)])):
            frame = baostock_provider.fetch_financial_summary("000001")
        self.assertEqual(len(frame), 1)
        self.assertEqual(frame.iloc[0]["netProfit"], 102.0)
        self.assertEqual(frame.iloc[0]["liabilityToAsset"], 0.2)
        self.assertEqual(frame.iloc[0]["CFOToNP"], 0.8)
        self.assertEqual(str(frame.iloc[0]["pubDate"])[:10], "2026-08-11")

    def test_shared_kline_uses_baostock_after_easy_tdx_failure(self) -> None:
        frame = _kline()
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(run_pipeline, "CACHE_ROOT", Path(directory)), \
             patch("tools.providers.easy_tdx_provider.fetch_kline_daily", side_effect=requests.Timeout("tdx")), \
             patch("tools.providers.baostock_provider.fetch_kline_daily", return_value=frame):
            path = run_pipeline.prepare_kline("000001")
            meta = json.loads(path.with_suffix(".meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["fetch_state"], "fallback_ok")
        self.assertEqual([item["source"] for item in meta["source_chain"]], ["easy_tdx/TDX", "BaoStock"])

    def test_finance_kline_chain_places_baostock_before_web_sources(self) -> None:
        frame = _kline()
        with patch("tools.providers.easy_tdx_provider.fetch_kline_daily", side_effect=requests.Timeout("tdx")), \
             patch("tools.providers.baostock_provider.fetch_kline_daily", return_value=frame), \
             patch.object(finance_data.ak, "stock_zh_a_hist", side_effect=AssertionError("web fallback not expected")):
            result = finance_data.fetch_kline_daily("000001")
        self.assertEqual(result.attrs["fetch_state"], "fallback_ok")
        self.assertEqual(result.attrs["source_chain"][-1]["source"], "BaoStock")

    def test_financial_summary_crosschecks_and_only_fills_missing_fields(self) -> None:
        financials = {
            "lrb": pd.DataFrame([{"报告期": "2026-06-30", "归属于母公司的净利润": 100.0}]),
            "fzb": pd.DataFrame([{"报告期": "2026-06-30", "资产总计": 1000.0, "负债合计": 200.0}]),
            "llb": pd.DataFrame([{"报告期": "2026-06-30", "经营活动产生的现金流量净额": 80.0}]),
        }
        summary = pd.DataFrame([{
            "statDate": "2026-06-30", "pubDate": "2026-08-10", "netProfit": 102.0,
            "liabilityToAsset": 0.2, "CFOToNP": 0.8, "totalShare": 1000.0, "liqaShare": 800.0,
        }])
        summary.attrs.update({"fetch_state": "ok", "source_chain": [{"source": "BaoStock/B级财务摘要", "status": "ok"}]})
        metrics = finance_data._report_metrics(
            "000001", {}, {}, pd.DataFrame(), pd.DataFrame(), financials,
            baostock_summary=summary,
        )
        self.assertEqual(metrics["net_profit"], 100.0)
        self.assertEqual(metrics["baostock_financial_crosscheck"]["status"], "matched")
        self.assertEqual(metrics["total_shares"], 1000.0)
        self.assertIn("末级部分补缺", metrics["metric_source_overrides"]["total_shares"])

    def test_metric_source_override_reaches_evidence(self) -> None:
        report = (
            '<!-- moda_metrics: {"net_profit": 100, "metric_source_overrides": '
            '{"net_profit": "BaoStock/B级财务摘要（末级部分补缺）"}} -->'
        )
        evidence = evidence_module.build_evidence("000001", "测试", {"finance_data": report})
        self.assertEqual(evidence["metric_sources"]["net_profit"], ["BaoStock/B级财务摘要（末级部分补缺）"])


if __name__ == "__main__":
    unittest.main()
