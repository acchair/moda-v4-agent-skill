from __future__ import annotations

import unittest
from unittest.mock import patch

import tools.sector_screening as screening
from tools.sector_screening import render_quick_screen, screen_sector, validate_quick_screen


def _candidate(
    code: str,
    name: str,
    *,
    industry: str,
    main_business: str,
    items: list[str],
    concepts: list[str] | None = None,
    price: float | None = 0.45,
    st_risk: bool | None = None,
    barrier_status: str = "",
    barrier_evidence: str = "",
    barrier_refs: list[str] | None = None,
    breakdown: list[dict] | None = None,
) -> dict:
    return {
        "code": code,
        "name": name,
        "industry": industry,
        "concepts": concepts or [],
        "main_business": main_business,
        "business_items": items,
        "business_breakdown": breakdown if breakdown is not None else [
            {"category": "按产品分类", "item": item, "revenue_ratio": 0.72}
            for item in items[:1]
        ],
        "net_profit": 10.0,
        "profit_yoy": 18.0,
        "pe_ttm": 24.0,
        "pb": 2.2,
        "price_percentile_3y": price,
        "market_congestion": 0.35,
        "st_risk": st_risk,
        "barrier_status": barrier_status,
        "barrier_evidence": barrier_evidence,
        "barrier_evidence_refs": barrier_refs or [],
    }


class SectorQuickScreenTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rows = [
            _candidate(
                "000001", "核心换热", industry="通用设备", main_business="液冷换热器、液冷泵、精密阀门",
                items=["精密换热器"], price=0.30,
            ),
            _candidate(
                "000002", "液冷系统", industry="通用设备", main_business="冷板式液冷设备、液冷分配单元",
                items=["液冷分配单元"], price=0.48,
            ),
            _candidate(
                "000003", "低位概念", industry="软件开发", main_business="办公软件",
                items=["办公软件"], concepts=["液冷"], price=0.05,
            ),
            _candidate(
                "000004", "*ST液冷", industry="通用设备", main_business="液冷泵",
                items=["液冷泵"], st_risk=True,
            ),
            _candidate(
                "000005", "液冷服务器", industry="计算机设备", main_business="液冷服务器",
                items=["液冷服务器"], price=0.55,
            ),
            _candidate(
                "000006", "换热材料", industry="有色金属", main_business="铜管、液冷换热器",
                items=["铜管"], price=0.28,
            ),
            _candidate(
                "000007", "冷却液", industry="化学制品", main_business="电子氟化液、液冷冷却液",
                items=["电子氟化液"], price=0.38,
            ),
            _candidate(
                "000008", "无关公司", industry="软件开发", main_business="企业办公软件",
                items=["企业办公软件"], price=0.10,
            ),
        ]

    def test_full_universe_is_screened_before_shortlist(self) -> None:
        result = screen_sector(
            "液冷",
            universe_rows=self.rows,
            use_live_universe=False,
            fetch_business=False,
            shortlist_limit=6,
        )

        self.assertEqual(result["universe"]["total"], 8)
        self.assertEqual(result["universe"]["coverage_status"], "user_supplied")
        self.assertLessEqual(len(result["shortlist"]), 6)
        self.assertEqual(result["full_pipeline_triggered"], False)
        self.assertEqual(result["research_score_used"], False)
        self.assertTrue(result["next_action"]["requires_user_confirmation"])
        selected = {item["code"] for item in result["shortlist"]}
        self.assertIn("000001", selected)
        self.assertNotIn("000003", selected, "概念低位不能取代主营受益")
        self.assertNotIn("000004", selected, "ST 候选不应进入优先深研")
        self.assertFalse(any("research_score" in item or "decision" in item for item in result["shortlist"]))
        validate_quick_screen(result)

    def test_upstream_technical_terms_are_clues_not_confirmed_moat(self) -> None:
        result = screen_sector(
            "液冷",
            universe_rows=self.rows,
            use_live_universe=False,
            fetch_business=False,
        )
        first = next(item for item in result["all_candidates"] if item["code"] == "000001")
        self.assertEqual(first["chain_stage"], "upstream")
        self.assertIn(first["barrier_status"], {"技术/工艺壁垒线索", "关键位置线索"})
        self.assertNotEqual(first["barrier_status"], "已验证")
        self.assertIn("尚", first["barrier_reason"])

    def test_primary_evidence_can_mark_barrier_verified(self) -> None:
        rows = [
            _candidate(
                "000009", "认证设备", industry="通用设备", main_business="液冷换热器",
                items=["换热器"], barrier_status="已验证",
                barrier_evidence="年报披露客户认证周期与专用工艺。",
                barrier_refs=["annual_report:2025"],
            )
        ]
        result = screen_sector("液冷", universe_rows=rows, use_live_universe=False, fetch_business=False)
        self.assertEqual(result["shortlist"][0]["barrier_status"], "已验证")
        self.assertIn("annual_report:2025", result["shortlist"][0]["evidence_refs"])

    def test_live_universe_falls_back_to_shenwan_components(self) -> None:
        candidate = dict(self.rows[0])
        candidate["universe_match"] = "申万二级成分股"
        with patch.object(screening, "_load_eastmoney_universe", return_value=([], {"error": "东方财富失败"})), \
             patch.object(screening, "_load_shenwan_universe", return_value=([candidate], {
                 "source": "申万宏源研究/申万二级成分股", "coverage_status": "live_full",
             })):
            rows, metadata = screening._load_live_universe("液冷")
        self.assertEqual(rows[0]["code"], "000001")
        self.assertEqual(metadata["coverage_status"], "live_full")
        self.assertEqual(metadata["primary_fallback"], "东方财富失败")

    def test_theme_aliases_use_concept_components_as_a_candidate_pool(self) -> None:
        boards = [
            {"板块名称": "AI手机"},
            {"板块名称": "人工智能"},
            {"板块名称": "端侧智能"},
        ]
        components = {
            "AI手机": [{"代码": "300001", "名称": "终端芯片"}],
            "端侧智能": [{"代码": "300002", "名称": "模型部署"}],
        }

        class FakeAk:
            @staticmethod
            def stock_board_concept_name_em():
                return __import__("pandas").DataFrame(boards)

            @staticmethod
            def stock_board_concept_cons_em(symbol: str):
                return __import__("pandas").DataFrame(components[symbol])

        with patch.dict("sys.modules", {"akshare": FakeAk}):
            rows, metadata = screening._load_eastmoney_concept_universe("端侧AI")
        self.assertEqual({row["code"] for row in rows}, {"300001", "300002"})
        self.assertEqual(metadata["coverage_status"], "live_theme")
        self.assertTrue(all(row["universe_match"] == "概念板块成分股" for row in rows))

    def test_theme_aliases_do_not_turn_generic_ai_into_end_side_ai(self) -> None:
        self.assertNotIn("ai手机", screening._variants("AI"))
        self.assertIn("ai手机", screening._variants("端侧AI"))

    def test_concept_input_is_detected_without_replacing_generic_industries(self) -> None:
        self.assertEqual(screening.infer_query_kind("端侧AI"), "concept")
        self.assertEqual(screening.infer_query_kind("液冷概念"), "concept")
        self.assertEqual(screening.infer_query_kind("半导体"), "sector")

    def test_concept_mode_requires_material_f10_revenue_exposure(self) -> None:
        rows = [
            _candidate(
                "000021", "端侧核心", industry="半导体", main_business="端侧AI芯片、传统芯片",
                items=["端侧AI芯片", "传统芯片"], concepts=["端侧AI"],
                breakdown=[
                    {"category": "按产品分类", "item": "端侧AI芯片", "revenue_ratio": 0.62},
                    {"category": "按产品分类", "item": "传统芯片", "revenue_ratio": 0.38},
                ],
            ),
            _candidate(
                "000022", "端侧重要", industry="电子", main_business="端侧AI模组、消费电子",
                items=["端侧AI模组", "消费电子"], concepts=["端侧AI"],
                breakdown=[
                    {"category": "按产品分类", "item": "端侧AI模组", "revenue_ratio": 0.27},
                    {"category": "按产品分类", "item": "消费电子", "revenue_ratio": 0.73},
                ],
            ),
            _candidate(
                "000023", "端侧边际", industry="电子", main_business="端侧AI工具、消费电子",
                items=["端侧AI工具", "消费电子"], concepts=["端侧AI"],
                breakdown=[
                    {"category": "按产品分类", "item": "端侧AI工具", "revenue_ratio": 0.08},
                    {"category": "按产品分类", "item": "消费电子", "revenue_ratio": 0.92},
                ],
            ),
            _candidate(
                "000024", "端侧题材", industry="软件开发", main_business="企业办公软件",
                items=["办公软件"], concepts=["端侧AI"],
            ),
        ]
        result = screen_sector(
            "端侧AI概念",
            query_kind="concept",
            universe_rows=rows,
            use_live_universe=False,
            fetch_business=False,
        )

        self.assertEqual(result["query_kind"], "concept")
        self.assertEqual([row["code"] for row in result["shortlist"]], ["000021", "000022"])
        core = next(row for row in result["all_candidates"] if row["code"] == "000021")
        marginal = next(row for row in result["all_candidates"] if row["code"] == "000023")
        thematic = next(row for row in result["all_candidates"] if row["code"] == "000024")
        self.assertEqual(core["concept_exposure_tier"], "核心主业")
        self.assertAlmostEqual(core["concept_revenue_ratio"], 0.62)
        self.assertEqual(marginal["concept_exposure_tier"], "边际受益")
        self.assertFalse(marginal["eligible_for_shortlist"])
        self.assertEqual(thematic["concept_exposure_tier"], "仅题材关联")
        self.assertEqual(result["universe"]["concept_core"], 1)
        self.assertEqual(result["universe"]["concept_material"], 1)
        rendered = render_quick_screen(result)
        self.assertIn("概念收入暴露", rendered)
        self.assertIn("边际受益", rendered)

    def test_concept_live_universe_does_not_substitute_industry_components(self) -> None:
        candidate = dict(self.rows[0])
        candidate["universe_match"] = "概念板块成分股"
        with patch.object(screening, "_load_eastmoney_concept_universe", return_value=([candidate], {
            "source": "AKShare/东方财富概念板块成分股", "coverage_status": "live_theme",
        })), patch.object(screening, "_load_eastmoney_universe", side_effect=AssertionError("不应调用行业源")):
            rows, metadata = screening._load_live_universe("端侧AI", query_kind="concept")
        self.assertEqual(rows[0]["code"], "000001")
        self.assertEqual(metadata["coverage_status"], "live_theme")

    def test_renderer_reports_coverage_and_confirmation_gate(self) -> None:
        result = screen_sector("液冷", universe_rows=self.rows, use_live_universe=False, fetch_business=False)
        rendered = render_quick_screen(result)
        self.assertIn("全量候选：8 家", rendered)
        self.assertIn("前六：优先深研候选", rendered)
        self.assertIn("观察池", rendered)
        self.assertIn("淘汰或暂不纳入", rendered)
        self.assertIn("是否继续对优先名单启动完整个股研究", rendered)
        self.assertNotIn("研究分", rendered)

    def test_light_screen_uses_scale_ranks_only_as_a_transparent_tie_breaker(self) -> None:
        rows = [
            _candidate(
                "000011", "规模甲", industry="通用设备", main_business="液冷换热器",
                items=["液冷换热器"], price=0.45,
            ),
            _candidate(
                "000012", "规模乙", industry="通用设备", main_business="液冷换热器",
                items=["液冷换热器"], price=0.45,
            ),
        ]

        def peer_fetcher(code: str, _timeout: int) -> dict:
            rank = 2 if code == "000012" else 9
            return {
                "fetch_state": "ok",
                "source": "AKShare/东方财富同行规模比较",
                "net_profit_rank": rank,
                "revenue_rank": rank,
                "market_cap_rank": rank,
            }

        result = screen_sector(
            "液冷",
            universe_rows=rows,
            use_live_universe=False,
            fetch_business=False,
            fetch_peer_snapshot=True,
            peer_snapshot_fetcher=peer_fetcher,
        )

        self.assertEqual(result["universe"]["peer_scale_requested"], 2)
        self.assertEqual(result["universe"]["peer_scale_available"], 2)
        self.assertEqual(result["shortlist"][0]["code"], "000012")
        self.assertEqual(result["shortlist"][0]["industry_scale"]["revenue_rank"], 2)
        self.assertIn("AKShare/东方财富同行规模比较", result["shortlist"][0]["evidence_refs"])
        rendered = render_quick_screen(result)
        self.assertIn("行业横截面", rendered)
        self.assertIn("营收#2", rendered)
        self.assertFalse(result["research_score_used"])
        self.assertFalse(result["full_pipeline_triggered"])

    def test_default_scale_fetcher_uses_scale_endpoint_only(self) -> None:
        snapshot = {
            "fetch_state": "ok",
            "status": "已获取",
            "target": {"market_cap_rank": 3, "revenue_rank": 2, "net_profit_rank": 1},
            "source_chain": {},
        }
        with patch("tools.akshare.finance_data.fetch_industry_peer_snapshot", return_value=snapshot) as fetch:
            result = screening._default_peer_scale_fetcher("000001", 3)
        fetch.assert_called_once_with("000001", timeout=3, comparisons=("scale",))
        self.assertEqual(result["net_profit_rank"], 1)


if __name__ == "__main__":
    unittest.main()
