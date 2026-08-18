from __future__ import annotations

import json
import unittest
from unittest.mock import Mock, patch

from tools.akshare import social_sentiment
from tools.providers import tencent_provider
from tools.scoring import grader, news_sentiment, stock_discussion
from tools.scoring.evidence import build_evidence
from tools.scoring.model import score_evidence


def _report(marker: str, payload: dict) -> str:
    return f"<!-- moda_{marker}: {json.dumps(payload, ensure_ascii=False)} -->"


class SocialSourceQualityTest(unittest.TestCase):
    def test_tencent_quote_is_normalized(self) -> None:
        fields = ["51", "平安银行", "000001", "10.50", "10.00", "10.10", "1000"] + [""] * 28
        response = Mock()
        response.text = f'v_sz000001="{"~".join(fields)}";'
        response.raise_for_status.return_value = None
        with patch.object(tencent_provider.requests, "get", return_value=response):
            quote = tencent_provider.fetch_realtime_quote("000001")
        self.assertEqual(quote["最新价"], 10.5)
        self.assertEqual(quote["涨跌幅"], 5.0)
        self.assertEqual(quote["source"], "Tencent/qt.gtimg.cn")

    def test_tencent_kline_is_normalized(self) -> None:
        response = Mock()
        response.json.return_value = {"data": {"sz000001": {"qfqday": [
            ["2026-08-06", "10", "10.2", "10.3", "9.9", "100", "1000"],
            ["2026-08-07", "10.2", "10.4", "10.5", "10.1", "120", "1200"],
        ]}}}
        response.raise_for_status.return_value = None
        with patch.object(tencent_provider.requests, "get", return_value=response):
            frame = tencent_provider.fetch_kline_daily("000001", count=2)
        self.assertEqual(len(frame), 2)
        self.assertEqual(frame.iloc[-1]["close"], 10.4)
        self.assertGreater(frame.iloc[-1]["pct_chg"], 0)

    def test_tencent_rejects_legacy_bse_codes_and_marks_stale_quote(self) -> None:
        with self.assertRaisesRegex(ValueError, "历史代码"):
            tencent_provider.fetch_realtime_quote("430047")
        fields = ["51", "测试公司", "000001", "10.00", "10.00", "10.00", "0"] + [""] * 28
        response = Mock()
        response.text = f'v_sz000001="{"~".join(fields)}";'
        response.raise_for_status.return_value = None
        with patch.object(tencent_provider.requests, "get", return_value=response):
            quote = tencent_provider.fetch_realtime_quote("000001")
        self.assertTrue(quote["quote_stale_suspect"])

    def test_discussion_marks_single_source_and_small_sample_as_partial(self) -> None:
        rows = [{"source": "xueqiu", "title": "订单改善", "text": "订单改善", "author": "用户A", "status": "结构化接口"}]
        with patch.object(stock_discussion, "_xueqiu", return_value=rows), \
             patch.object(stock_discussion, "_eastmoney", return_value=[]), \
             patch.object(stock_discussion, "_search_fallback", side_effect=AssertionError("structured data remains primary")):
            data = stock_discussion.collect("300684", "中石科技")
        self.assertTrue(data["discussion_partial"])
        self.assertEqual(data["discussion_sample_status"], "样本不足")
        self.assertIsNone(data["discussion_sentiment"])

    def test_news_deduplicates_cross_source_titles(self) -> None:
        row = {"source": "测试", "title": "平安银行发布业绩快报", "text": "利润增长", "url": "", "published_at": ""}
        with patch.object(news_sentiment, "_cached", return_value=([row], "live", "ok", "")):
            data = news_sentiment.collect(["平安银行", "000001"])
        self.assertEqual(data["news_posts_total"], 1)
        self.assertEqual(data["news_sources_ok"], 3)

    def test_news_search_candidates_do_not_change_structured_news_metrics(self) -> None:
        structured = {
            "news_posts_total": 0,
            "news_sources_checked": 3,
            "news_sources_ok": 3,
            "news_partial": False,
            "news_sentiment": None,
            "news_sentiment_score": None,
            "news_records": [],
            "fetch_state": "ok",
            "source_chain": [],
        }
        candidate = {
            "title": "先导基电（600641）发布半年度业绩预告",
            "snippet": "公告显示公司预计上半年业绩增长。",
            "url": "https://example.com/600641",
            "date": "2026-07-14",
        }
        with patch.object(news_sentiment, "collect", return_value=structured), \
             patch("tools.scoring.web_research._search", return_value=("so360", [candidate], [])) as search:
            data = social_sentiment._collect_news("600641", "先导基电", ["先导基电", "600641"])

        self.assertEqual(data["news_posts_total"], 0)
        self.assertIsNone(data["news_sentiment"])
        self.assertEqual(data["news_search_count"], 1)
        self.assertEqual(data["news_search_records"][0]["status"], "网络候选新闻（未核验）")
        self.assertEqual(data["news_search_records"][0]["provider"], "so360")
        self.assertEqual(search.call_args.args[1], "先导基电 600641 新闻 公告")

    def test_news_search_is_not_run_when_realtime_news_matches(self) -> None:
        structured = {
            "news_posts_total": 1,
            "news_sources_checked": 3,
            "news_sources_ok": 3,
            "news_partial": False,
            "news_sentiment": "中性",
            "news_sentiment_score": 0.0,
            "news_records": [{"title": "先导基电公告"}],
            "fetch_state": "ok",
            "source_chain": [],
        }
        with patch.object(news_sentiment, "collect", return_value=structured), \
             patch("tools.scoring.web_research._search") as search:
            data = social_sentiment._collect_news("600641", "先导基电", ["先导基电", "600641"])

        search.assert_not_called()
        self.assertEqual(data["news_search_fetch_state"], "not_run")

    def test_news_search_failure_is_not_reported_as_no_match(self) -> None:
        with patch("tools.scoring.web_research._search", return_value=("none", [], ["so360:SSLError"])):
            data = social_sentiment._collect_news_candidates("600641", "先导基电")

        self.assertEqual(data["news_search_count"], 0)
        self.assertEqual(data["news_search_fetch_state"], "failed")
        self.assertEqual(data["news_search_status"], "搜索失败，需人工确认")

    def test_news_search_explicit_no_result_is_reported_as_empty(self) -> None:
        with patch("tools.scoring.web_research._search", return_value=("none", [], ["so360:no_results"])):
            data = social_sentiment._collect_news_candidates("600641", "先导基电")

        self.assertEqual(data["news_search_fetch_state"], "empty")
        self.assertEqual(data["news_search_status"], "已搜索未命中")

    def test_social_report_separates_realtime_news_from_unverified_candidates(self) -> None:
        data = {
            "social_platforms_checked": 2,
            "social_platforms_total": 6,
            "social_hot_hits": 0,
            "social_unique_topics": 0,
            "social_platform_hits": 0,
            "social_cross_platform_topics": 0,
            "social_heat": None,
            "social_propagation_status": "首次快照，等待时间序列",
            "social_new_topics_24h": 0,
            "social_fast_spread_topics": 0,
            "social_rank_jump_max": None,
            "promotional_keyword_hits": [],
            "discussion_promotion_hits": [],
            "rumor_keyword_hits": [],
            "discussion_posts_total": 0,
            "discussion_sample_status": "样本不足",
            "discussion_sentiment": None,
            "discussion_source_status": "需人工确认",
            "news_posts_total": 0,
            "news_sources_ok": 3,
            "news_sources_checked": 3,
            "news_sentiment": None,
            "news_search_count": 1,
            "news_search_status": "网络候选新闻（未核验）",
            "news_search_provider": "so360",
            "news_search_query": "先导基电 600641 新闻 公告",
            "news_search_records": [{
                "provider": "so360",
                "title": "先导基电（600641）公告",
                "url": "https://example.com/600641",
            }],
            "social_mentions": {"baidu": []},
        }
        report = social_sentiment.build_report("600641", "先导基电", data)

        self.assertIn("实时快讯匹配 0 条；网络候选 1 条（未核验", report)
        self.assertIn("不计入情绪、评分、热度或异常推广判断", report)
        self.assertIn("## 网络候选新闻（未核验）", report)

    def test_scoring_report_renders_candidates_without_changing_score(self) -> None:
        social = {
            "social_platforms_checked": 2,
            "social_platforms_total": 6,
            "social_hot_hits": 0,
            "social_platform_hits": 0,
            "social_partial": True,
            "discussion_partial": True,
            "news_posts_total": 0,
            "news_sources_checked": 3,
            "news_sources_ok": 3,
            "news_partial": True,
            "news_sentiment": None,
            "news_search_count": 3,
            "news_search_status": "网络候选新闻（未核验）",
        }
        without_candidates = {key: value for key, value in social.items() if not key.startswith("news_search_")}
        baseline_evidence = build_evidence("600641", "先导基电", {"social_sentiment": _report("social_sentiment", without_candidates)})
        candidate_evidence = build_evidence("600641", "先导基电", {"social_sentiment": _report("social_sentiment", social)})
        baseline_card = score_evidence(baseline_evidence)
        candidate_card = score_evidence(candidate_evidence)
        report = grader.render_report("600641", "先导基电", candidate_evidence, candidate_card, ())

        self.assertEqual(candidate_card.research_score, baseline_card.research_score)
        self.assertEqual(candidate_card.coverage, baseline_card.coverage)
        self.assertIn("实时快讯匹配 0 条；网络候选 3 条（未核验", report)
        self.assertIn("不计入情绪、评分、热度或异常推广判断", report)

    def test_history_tracks_rank_jump_and_fast_spread(self) -> None:
        previous = [
            {"ts": 1_786_000_000.0, "topics": [{"key": "业绩增长", "platforms": ["weibo"], "best_rank": 20}]},
            {"ts": 1_786_000_300.0, "topics": [{"key": "业绩增长", "platforms": ["weibo", "zhihu"], "best_rank": 12}]},
        ]
        current = {"ts": 1_786_000_500.0, "topics": [{"key": "业绩增长", "platforms": ["weibo", "zhihu", "baidu"], "best_rank": 3}]}
        metrics = social_sentiment._history_metrics(previous, current)
        self.assertEqual(metrics["social_rank_jump_max"], 9)
        self.assertEqual(metrics["social_fast_spread_topics"], 1)
        self.assertEqual(metrics["social_persistent_topics"], 1)

    def test_plain_hotlist_mentions_do_not_become_promotion_signals(self) -> None:
        social = {
            "social_platforms_checked": 6,
            "social_platforms_total": 6,
            "social_platform_hits": 3,
            "social_heat": 0.9,
            "social_partial": False,
            "discussion_partial": False,
            "discussion_source_status": "结构化接口",
            "promotional_keyword_hits": [],
            "discussion_promotion_hits": [],
            "social_promotional_platforms": [],
        }
        evidence = build_evidence("000001", "平安银行", {"social_sentiment": _report("social_sentiment", social)})
        checks = {item["signal"]: item for item in evidence["trap_checks"]}
        self.assertFalse(checks["大量账号/平台同步推荐"]["hit"])
        self.assertFalse(checks["跨平台联动推广"]["hit"])
        self.assertEqual(evidence["trap_independent_categories"], 0)

    def test_financial_presence_alone_is_not_independent_abnormality(self) -> None:
        social = {
            "social_platforms_checked": 6,
            "social_platforms_total": 6,
            "social_platform_hits": 3,
            "social_heat": 0.9,
            "social_partial": False,
            "discussion_partial": False,
            "discussion_source_status": "结构化接口",
            "promotional_keyword_hits": ["必涨", "翻倍", "股神", "VIP"],
            "discussion_promotion_hits": ["必涨", "翻倍", "股神", "VIP"],
            "discussion_promotion_record_count": 4,
            "discussion_promotion_source_count": 3,
            "discussion_author_count": 4,
            "discussion_template_cluster_count": 1,
            "social_promotional_platforms": ["weibo", "xueqiu", "eastmoney"],
        }
        finance = {"net_profit": 1, "profit_yoy": 0.1, "revenue_yoy": 0.1, "price_percentile_3y": 0.5}
        evidence = build_evidence("000001", "平安银行", {
            "social_sentiment": _report("social_sentiment", social),
            "finance_data": _report("finance_data", finance),
        })
        self.assertEqual(evidence["trap_independent_categories"], 1)
        self.assertEqual(evidence["trap_risk_level"], "注意")

        finance.update({"net_profit": -1, "profit_yoy": -0.2})
        abnormal = build_evidence("000001", "平安银行", {
            "social_sentiment": _report("social_sentiment", social),
            "finance_data": _report("finance_data", finance),
        })
        self.assertEqual(abnormal["trap_independent_categories"], 2)
        self.assertEqual(abnormal["trap_risk_level"], "高")


if __name__ == "__main__":
    unittest.main()
