from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

from tools.akshare import congestion, industry_prosperity
from tools.daily_cache import load_daily_json


HTML = """
<html><body>
<p>当前报告期：20260630(2026中报) 已更新财报家数分别为：71</p>
<table><tr><td>说明</td></tr></table>
<table>
  <thead><tr><th>序号</th><th>行业</th><th>数值(2026-03-31)</th><th>数值(2025-12-31)</th><th>差值</th></tr></thead>
  <tbody>
    <tr><td>1</td><td>机械设备 [查看成分股]</td><td>6.50%</td><td>4.00%</td><td>2.50%</td></tr>
    <tr><td>2</td><td>电子 [查看成分股]</td><td>-1.00%</td><td>2.00%</td><td>-3.00%</td></tr>
  </tbody>
</table>
</body></html>
"""


def raw_industries() -> dict:
    return {
        "source_date": "2026-03-31",
        "sw_first": [{"行业代码": "801890.SI", "行业名称": "机械设备"}],
        "sw_second": [{"行业代码": "801074.SI", "行业名称": "专用设备", "上级行业": "机械设备"}],
        "metrics": {},
        "errors": [],
    }


class DailyCacheTest(unittest.TestCase):
    def test_same_day_uses_one_fetch_and_next_day_refreshes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "daily.json"
            calls = 0

            def fetch() -> dict:
                nonlocal calls
                calls += 1
                return {"source_date": "2026-07-30", "value": calls}

            day1 = datetime(2026, 7, 31, 9)
            first = load_daily_json(path, fetch, now=day1)
            second = load_daily_json(path, fetch, now=day1)
            third = load_daily_json(path, fetch, now=datetime(2026, 8, 1, 9))
            self.assertEqual(calls, 2)
            self.assertFalse(first["cache_hit"])
            self.assertTrue(second["cache_hit"])
            self.assertEqual(third["payload"]["value"], 2)

    def test_concurrent_requests_share_one_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "daily.json"
            calls = 0
            guard = threading.Lock()

            def fetch() -> dict:
                nonlocal calls
                with guard:
                    calls += 1
                time.sleep(0.1)
                return {"source_date": "2026-07-31", "value": 1}

            now = datetime(2026, 7, 31, 9)
            with ThreadPoolExecutor(max_workers=4) as executor:
                results = list(executor.map(lambda _: load_daily_json(path, fetch, now=now), range(4)))
            self.assertEqual(calls, 1)
            self.assertEqual(sum(result["cache_hit"] is False for result in results), 1)

    def test_failed_refresh_falls_back_but_is_not_usable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "daily.json"
            load_daily_json(
                path,
                lambda: {"source": "test-source", "source_date": "2026-07-30", "value": 1},
                now=datetime(2026, 7, 31),
            )

            def fail() -> dict:
                raise requests_error

            requests_error = PermissionError("403")
            result = load_daily_json(path, fail, now=datetime(2026, 8, 1))
            self.assertEqual(result["status"], "fallback")
            self.assertFalse(result["usable"])
            self.assertEqual(result["payload"]["value"], 1)
            self.assertEqual(result["source"], "test-source")

    def test_today_cache_does_not_make_old_congestion_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "congestion.json"
            payload = {
                "source": "乐咕乐股/申万二级行业拥挤度",
                "source_date": "2026-05-21",
                "rows": [{
                    "sw_second_name": "电子化学品Ⅱ", "sw_second_code": "801086.SI",
                    "sw_first_code": "801080.SI", "turnover_percentile": 45,
                    "amount_ratio_percentile": 45, "congestion": 0.45,
                    "strength_score": 45, "strength": "中性",
                }],
            }
            with patch.object(congestion, "_fetch_latest", return_value=payload), \
                 patch.object(congestion, "SW_SNAPSHOT_CACHE_PATH", Path(directory) / "snapshot.json"), \
                 patch.object(congestion, "_fetch_sw_second_snapshot", side_effect=PermissionError("unavailable")):
                result = congestion.collect("电子化学品", cache_path=path, now=datetime(2026, 7, 31, 9))
            self.assertEqual(result["market_congestion_checked_date"], "2026-07-31")
            self.assertEqual(result["market_congestion_date"], "2026-05-21")
            self.assertFalse(result["market_congestion_fresh"])
            self.assertEqual(result["market_congestion_industry"], "电子化学品Ⅱ")

    def test_stale_legulegu_uses_sw_activity_proxy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stale = {
                "source": "乐咕乐股/申万二级行业拥挤度",
                "source_date": "2026-05-21",
                "rows": [{
                    "sw_second_name": "专用设备", "sw_second_code": "801074",
                    "sw_first_code": "801070", "turnover_percentile": 45,
                    "amount_ratio_percentile": 45, "congestion": 0.45,
                    "strength_score": 45, "strength": "中性",
                }],
            }
            snapshot = {"source_date": "2026-07-31", "rows": [{
                "sw_second_name": "专用设备", "sw_second_code": "801074", "sw_first_code": "",
            }]}
            proxy = {
                "source": "申万宏源研究/二级行业指数成交活跃度代理",
                "source_date": "2026-07-31", "sw_second_name": "专用设备", "sw_second_code": "801074",
                "amount_percentile": 0.8, "volume_percentile": 0.7, "congestion": 0.75,
                "strength_score": 75, "strength": "偏热", "proxy": True,
            }
            with patch.object(congestion, "_fetch_latest", return_value=stale), \
                 patch.object(congestion, "SW_SNAPSHOT_CACHE_PATH", root / "snapshot.json"), \
                 patch.object(congestion, "SW_PROXY_CACHE_DIR", root / "proxy"), \
                 patch.object(congestion, "_fetch_sw_second_snapshot", return_value=snapshot), \
                 patch.object(congestion, "_fetch_sw_activity_proxy", return_value=proxy):
                result = congestion.collect("专用设备", cache_path=root / "legu.json", now=datetime(2026, 7, 31, 9))
            self.assertTrue(result["market_congestion_proxy"])
            self.assertTrue(result["market_congestion_fresh"])
            self.assertEqual(result["fetch_state"], "fallback_ok")
            self.assertEqual(result["market_congestion"], 0.75)

    def test_industry_congestion_cache_is_shared_by_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "congestion.json"
            payload = {
                "source": "乐咕乐股/申万二级行业拥挤度", "source_date": "2026-07-31",
                "rows": [{
                    "sw_second_name": "专用设备", "sw_second_code": "801074.SI",
                    "sw_first_code": "801070.SI", "turnover_percentile": 70,
                    "amount_ratio_percentile": 60, "congestion": 0.65,
                    "strength_score": 65, "strength": "偏热",
                }],
            }
            with patch.object(congestion, "_fetch_latest", return_value=payload) as fetch:
                first = congestion.collect("专用设备", cache_path=path, now=datetime(2026, 7, 31, 9))
                second = congestion.collect("专用设备 / 其他专用设备", cache_path=path, now=datetime(2026, 7, 31, 10))
            self.assertEqual(fetch.call_count, 1)
            self.assertFalse(first["market_congestion_cache_hit"])
            self.assertTrue(second["market_congestion_cache_hit"])
            self.assertEqual(second["market_congestion_strength"], "偏热")


class IndustryProsperityTest(unittest.TestCase):
    def test_fetches_all_six_legulegu_metric_pages(self) -> None:
        response = MagicMock()
        response.text = HTML
        response.raise_for_status.return_value = None
        session = MagicMock()
        session.get.return_value = response
        session.headers = MagicMock()
        with patch.object(industry_prosperity.requests, "Session", return_value=session), \
             patch.object(industry_prosperity.ak, "sw_index_first_info", return_value=pd.DataFrame()), \
             patch.object(industry_prosperity.ak, "sw_index_second_info", return_value=pd.DataFrame()):
            result = industry_prosperity.fetch_legulegu_tables()
        self.assertEqual(set(result["metrics"]), set(industry_prosperity.METRICS))
        self.assertEqual(session.get.call_count, 6)
        self.assertEqual(result["source"], "乐咕乐股/申万行业中位数")

    def test_legulegu_parser_uses_completed_table_period(self) -> None:
        result = industry_prosperity.parse_legulegu_metric(HTML, "netProfitYoy")
        self.assertEqual(result["current_period"], "2026-03-31")
        self.assertEqual(result["pending_period"], "20260630")
        self.assertEqual(result["pending_announced_count"], 71)
        self.assertEqual(result["rows"]["机械设备"]["delta"], 2.5)

    def test_301128_industry_maps_to_machinery(self) -> None:
        mapping = industry_prosperity.map_industry("专用设备 / 其他专用设备", raw_industries())
        self.assertEqual(mapping["sw_second_name"], "专用设备")
        self.assertEqual(mapping["sw_first_name"], "机械设备")
        self.assertEqual(mapping["status"], "已验证")

    def test_profit_revenue_divergence_is_flagged(self) -> None:
        financial = {
            "status": "改善",
            "values": {
                "orYoy": {"delta": -1},
                "trYoy": {"delta": -2},
                "netProfitYoy": {"delta": 3},
            },
        }
        conflicts = industry_prosperity._conflicts(financial, {"status": "中性"})
        self.assertIn("利润改善但营收边际下降", conflicts)

    def test_supply_weakness_conflicts_with_financial_improvement(self) -> None:
        conflicts = industry_prosperity._conflicts(
            {"status": "改善", "values": {}},
            {"status": "走弱"},
        )
        self.assertIn("财务改善但价格/库存供需信号走弱", conflicts)

    def test_industry_raw_tables_are_shared_for_the_day(self) -> None:
        raw = raw_industries()
        raw["source"] = "乐咕乐股/申万行业中位数"
        raw["metrics"] = {
            code: {
                "label": label,
                "current_period": "2026-03-31",
                "rows": {"机械设备": {"current": 10.0, "previous": 5.0, "delta": 5.0}},
            }
            for code, label in industry_prosperity.METRICS.items()
        }
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(industry_prosperity, "fetch_legulegu_tables", return_value=raw) as fetch, \
             patch.object(industry_prosperity, "collect_supply_signal", return_value={"status": "不可用"}), \
             patch.object(industry_prosperity, "collect_market_signal", return_value={"status": "中性", "available_checks": 3}):
            cache_path = Path(directory) / "industry.json"
            first = industry_prosperity.collect("301128", "专用设备", cache_path=cache_path, now=datetime(2026, 7, 31))
            second = industry_prosperity.collect("000001", "专用设备", cache_path=cache_path, now=datetime(2026, 7, 31))
        self.assertEqual(fetch.call_count, 1)
        self.assertFalse(first["industry_prosperity_cache_hit"])
        self.assertTrue(second["industry_prosperity_cache_hit"])


    def test_manufacturing_without_supply_signal_is_partial(self) -> None:
        raw = raw_industries()
        raw["metrics"] = {
            code: {
                "label": label,
                "current_period": "2026-03-31",
                "rows": {"机械设备": {"current": 10.0, "previous": 5.0, "delta": 5.0}},
            }
            for code, label in industry_prosperity.METRICS.items()
        }
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(industry_prosperity, "fetch_legulegu_tables", return_value=raw), \
             patch.object(industry_prosperity, "collect_supply_signal", return_value={"status": "不可用"}), \
             patch.object(industry_prosperity, "collect_market_signal", return_value={"status": "上行", "available_checks": 3}):
            result = industry_prosperity.collect(
                "301128", "专用设备 / 其他专用设备",
                cache_path=Path(directory) / "industry.json",
                now=datetime(2026, 7, 31),
            )
        self.assertEqual(result["industry_prosperity_coverage"], "部分")


if __name__ == "__main__":
    unittest.main()
