from __future__ import annotations

import contextlib
import io
import importlib.util
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


analysis = load_module("companion_analysis", ROOT / "scripts" / "analyze_a_share.py")
sector = load_module("companion_sector", ROOT / "scripts" / "analyze_sector.py")
memory = load_module("companion_memory", ROOT / "scripts" / "memory.py")
soul = load_module("companion_soul", ROOT / "adapters" / "openminis" / "install_soul.py")
installer = load_module("companion_installer", ROOT / "install.py")


class CompanionAnalysisTest(unittest.TestCase):
    def test_sector_discovery_builds_bounded_research_universe(self) -> None:
        result = sector.discover_sector_candidates(ROOT.parent, "液冷", limit=3)
        self.assertEqual(result["source"], "专精特新_行业龙头_核心供应商_A股名单_完整版.csv")
        self.assertLessEqual(len(result["candidates"]), 3)
        self.assertIn("不是投资排序", result["selection_note"])
        for candidate in result["candidates"]:
            self.assertRegex(candidate["code"], r"^\d{6}$")
            self.assertIn(candidate["match_field"], {"行业名称", "概念名称", "证券简称"})

    def test_sector_screen_uses_lightweight_universe_and_requires_confirmation(self) -> None:
        class FakeAShare:
            @staticmethod
            def find_moda_root(_value=None):
                return ROOT.parent

        universe = [{
            "code": "000001", "name": "测试液冷", "industry": "通用设备",
            "main_business": "液冷换热器", "business_items": ["换热器"],
            "business_breakdown": [{"category": "按产品分类", "item": "换热器", "revenue_ratio": 0.8}],
            "net_profit": 1.0, "price_percentile_3y": 0.3,
        }]
        with patch.object(sector, "_load_a_share_module", return_value=FakeAShare):
            result = sector.screen_sector(
                "液冷",
                universe_rows=universe,
                use_live_universe=False,
                fetch_business=False,
            )
        self.assertEqual(result["universe"]["total"], 1)
        self.assertFalse(result["full_pipeline_triggered"])
        self.assertTrue(result["next_action"]["requires_user_confirmation"])

    def test_sector_cli_defaults_to_lightweight_screen(self) -> None:
        universe = [{
            "code": "000001", "name": "测试液冷", "industry": "通用设备",
            "main_business": "液冷换热器", "business_items": ["换热器"],
            "business_breakdown": [{"category": "按产品分类", "item": "换热器", "revenue_ratio": 0.8}],
            "net_profit": 1.0, "price_percentile_3y": 0.3,
        }]
        with tempfile.TemporaryDirectory() as directory:
            universe_path = Path(directory) / "universe.json"
            universe_path.write_text(json.dumps(universe, ensure_ascii=False), encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exit_code = sector.main([
                    "液冷", "--universe-file", str(universe_path), "--no-live-universe",
                    "--no-business-fetch", "--no-peer-snapshot", "--format", "json",
                ])
        self.assertEqual(exit_code, 0)
        self.assertIn('"screening_type": "moda_full_universe_quick_screen"', output.getvalue())
        self.assertIn('"full_pipeline_triggered": false', output.getvalue())

    def test_load_analysis_preserves_authoritative_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "knowledge/research/scorecards").mkdir(parents=True)
            (root / "knowledge/research/scoring").mkdir(parents=True)
            (root / "knowledge/research/pipeline").mkdir(parents=True)
            card = {
                "evidence": {"name": "测试股份"},
                "scorecard": {
                    "research_score": 72,
                    "coverage": 0.8,
                    "unknown_maximum": 20,
                    "signal": "中性",
                    "hard_caps": [{"condition": "测试", "result": "未触发", "decision_effect": "无"}],
                },
                "thesis": {
                    "expression_status": "collector_only",
                    "research_packet": {"schema_version": 4, "research": {"research_score": 72}, "evidence_gaps": []},
                },
            }
            (root / "knowledge/research/scorecards/000001.json").write_text(json.dumps(card, ensure_ascii=False), encoding="utf-8")
            (root / "knowledge/research/scoring/000001.md").write_text("# 正式报告\n", encoding="utf-8")
            (root / "knowledge/research/pipeline/000001.json").write_text("[]", encoding="utf-8")

            result = analysis.load_analysis(root, "000001")
            self.assertEqual(result["research_score"], 72)
            self.assertNotIn("action_rating", result)
            self.assertIn("需先生成 Agent Judgment V4", result["formal_report"])
            self.assertEqual(result["formal_report_status"], "judgment_rebuild_required")
            self.assertEqual(result["next_action"], "generate_agent_judgment_v4")
            self.assertEqual(result["expression_status"], "collector_only")
            self.assertEqual(result["collector_status"], "ready")
            self.assertEqual(result["research_packet"]["research"]["research_score"], 72)
            self.assertEqual(result["thesis_context"]["schema_version"], 4)

    def test_load_analysis_marks_old_packet_stale_without_rebuilding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "knowledge/research/scorecards").mkdir(parents=True)
            (root / "knowledge/research/scoring").mkdir(parents=True)
            (root / "knowledge/research/pipeline").mkdir(parents=True)
            payload = {
                "evidence": {"name": "旧报告"},
                "scorecard": {"research_score": 70},
                "thesis": {"research_packet": {"schema_version": 3}},
            }
            (root / "knowledge/research/scorecards/000001.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            (root / "knowledge/research/scoring/000001.md").write_text("# 旧报告\n", encoding="utf-8")
            (root / "knowledge/research/pipeline/000001.json").write_text("[]", encoding="utf-8")

            result = analysis.load_analysis(root, "000001")
            self.assertEqual(result["collector_status"], "stale")
            self.assertEqual(result["expression_status"], "stale_schema")
            self.assertEqual(result["research_packet"]["schema_version"], 3)

    def test_load_analysis_marks_legacy_expression_without_rerunning_collector(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "knowledge/research/scorecards").mkdir(parents=True)
            (root / "knowledge/research/scoring").mkdir(parents=True)
            (root / "knowledge/research/pipeline").mkdir(parents=True)
            payload = {
                "evidence": {"name": "旧判断"},
                "scorecard": {"research_score": 70},
                "thesis": {
                    "expression_status": "agent_generated",
                    "research_packet": {"schema_version": 4},
                    "thesis_output": {"schema_version": 2, "decision": {"state": "等待"}},
                },
            }
            (root / "knowledge/research/scorecards/000001.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            (root / "knowledge/research/scoring/000001.md").write_text("# 旧判断\n", encoding="utf-8")
            (root / "knowledge/research/pipeline/000001.json").write_text("[]", encoding="utf-8")

            result = analysis.load_analysis(root, "000001")
            self.assertEqual(result["collector_status"], "ready")
            self.assertEqual(result["expression_status"], "stale_expression")
            self.assertIsNone(result["decision_state"])
            self.assertIn("旧版判断卡不能作为当前结论", result["formal_report"])
            self.assertNotEqual(result["formal_report"], "# 旧判断\n")
            self.assertEqual(result["next_action"], "generate_agent_judgment_v4")

    def test_finalize_analysis_embeds_agent_expression_without_rescoring(self) -> None:
        from tools.scoring import grader
        from tools.scoring.model import score_evidence
        from tools.scoring.thesis import build_thesis_context
        from tools.test_pipeline_efficiency import full_evidence

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "knowledge/research/scorecards").mkdir(parents=True)
            (root / "knowledge/research/scoring").mkdir(parents=True)
            (root / "knowledge/research/pipeline").mkdir(parents=True)
            evidence = full_evidence()
            evidence["code"] = "300820"
            evidence["name"] = "英杰电气"
            card = score_evidence(evidence)
            context = build_thesis_context(card, evidence).to_dict()
            before = card.to_dict()
            before_json = json.loads(json.dumps(before, ensure_ascii=False))
            scorecard_path = root / "knowledge/research/scorecards/300820.json"
            scorecard_path.write_text(
                json.dumps(
                    {
                        "evidence": evidence,
                        "scorecard": before,
                        "thesis": {
                            "expression_status": "collector_only",
                            "research_packet": context,
                            "thesis_context": context,
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            report_path = root / "knowledge/research/scoring/300820.md"
            report_path.write_text(grader.render_report("300820", "英杰电气", evidence, card, ()), encoding="utf-8")
            (root / "knowledge/research/pipeline/300820.json").write_text("[]", encoding="utf-8")
            from tools.test_thesis import agent_payload

            thesis_payload = agent_payload()

            result = analysis.finalize_analysis(root, "300820", thesis_payload, "英杰电气")
            after = json.loads(scorecard_path.read_text(encoding="utf-8"))
            combined = report_path.read_text(encoding="utf-8")
            self.assertEqual(after["scorecard"], before_json)
            for field in ("research_score", "coverage", "hard_caps"):
                self.assertEqual(after["scorecard"][field], before_json[field])
            self.assertEqual(after["thesis"]["expression_status"], "agent_generated")
            self.assertEqual(result["expression_status"], "agent_generated")
            self.assertEqual(result["decision_state"], "等待")
            self.assertTrue(result["final_report_ready"])
            self.assertIn("## 莫大判断", combined)
            self.assertIn("### 核心因果链", combined)
            self.assertIn("**等待**", combined)
            self.assertNotIn("行动评级", combined)
            self.assertNotIn("采集器仅提供事实包", combined)
            judgment = json.loads(Path(result["judgment_path"]).read_text(encoding="utf-8"))
            self.assertEqual(judgment["latest"]["state"], "等待")

            evidence["prior_judgment"] = judgment["latest"]
            next_context = build_thesis_context(card, evidence).to_dict()
            scorecard_path.write_text(
                json.dumps(
                    {
                        "evidence": evidence,
                        "scorecard": before,
                        "thesis": {
                            "expression_status": "collector_only",
                            "research_packet": next_context,
                            "thesis_context": next_context,
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            next_payload = agent_payload()
            next_payload["state_transition"] = {
                "previous_state": "等待",
                "current_state": "等待",
                "reason": "验证条件尚未发生，维持等待。",
            }
            second = analysis.finalize_analysis(root, "300820", next_payload, "英杰电气")
            history = json.loads(Path(second["judgment_path"]).read_text(encoding="utf-8"))
            self.assertEqual(len(history["history"]), 2)
            self.assertEqual(history["latest"]["state"], "等待")
            self.assertFalse(Path(second["judgment_path"] + ".tmp").exists())

    def test_runtime_config_resolves_existing_moda_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            companion = root / "skills/moda-companion"
            script = companion / "scripts/analyze_a_share.py"
            moda = root / "external/moda-v4"
            script.parent.mkdir(parents=True)
            moda.joinpath("tools").mkdir(parents=True)
            moda.joinpath("SKILL.md").write_text("---\nname: moda-v4\ndescription: test\n---\n", encoding="utf-8")
            moda.joinpath("tools/run_pipeline.py").write_text("", encoding="utf-8")
            companion.joinpath(".moda-companion-runtime.json").write_text(
                json.dumps({"moda_root": str(moda)}), encoding="utf-8"
            )
            self.assertEqual(analysis.candidate_roots(script)[0], moda.resolve())


class CompanionMemoryTest(unittest.TestCase):
    def test_memory_round_trip_and_forget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memory.json"
            memory.remember("关注方向", "机器人", ["行业"], path)
            self.assertEqual(memory.load_memory(path)["entries"]["关注方向"]["value"], "机器人")
            self.assertTrue(memory.forget("关注方向", path))
            self.assertNotIn("关注方向", memory.load_memory(path)["entries"])

    def test_rejects_sensitive_memory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                memory.remember("api_key", "YOUR_API_KEY_HERE", path=Path(directory) / "memory.json")

    def test_method_memory_is_seeded_once_without_overwriting_user_edit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memory.json"
            first = memory.seed_method_memory(path)
            self.assertTrue(first["created"])
            seeded = memory.load_memory(path)["entries"][memory.METHOD_MEMORY_KEY]
            self.assertEqual(seeded["value"]["decision_states"], ["观察", "等待", "试错", "买入", "退出"])
            self.assertIn("卡脖子瓶颈型", seeded["value"]["opportunity_models"])

            memory.remember(memory.METHOD_MEMORY_KEY, {"customized": True}, ["用户修改"], path)
            second = memory.seed_method_memory(path)
            self.assertFalse(second["created"])
            self.assertEqual(second["entry"]["value"], {"customized": True})
            self.assertFalse(path.with_suffix(".json.tmp").exists())


class OpenMinisSoulTest(unittest.TestCase):
    def test_install_backs_up_and_restore_recovers_original(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            memory_dir = Path(directory)
            target = memory_dir / "SOUL.md"
            template = memory_dir / "template.md"
            target.write_text("original", encoding="utf-8")
            template.write_text("moda", encoding="utf-8")

            installed = soul.install_soul(memory_dir, template)
            self.assertEqual(target.read_text(encoding="utf-8"), "moda")
            self.assertTrue(Path(installed["backup_path"]).exists())
            soul.restore_soul(memory_dir)
            self.assertEqual(target.read_text(encoding="utf-8"), "original")
            self.assertFalse((memory_dir / soul.STATE_NAME).exists())

    def test_install_refuses_to_overwrite_user_edit_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            memory_dir = Path(directory)
            template = memory_dir / "template.md"
            template.write_text("moda", encoding="utf-8")
            soul.install_soul(memory_dir, template)
            (memory_dir / "SOUL.md").write_text("user edit", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                soul.install_soul(memory_dir, template)


class InstallerTest(unittest.TestCase):
    def test_codex_install_is_isolated_and_parseable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            result = installer.install_codex(home, force=False, moda_root=installer.MODA_ROOT)
            companion_skill = Path(result["companion_skill"])
            moda_skill = Path(result["moda_skill"])
            agent_path = Path(result["agent"])
            self.assertTrue((companion_skill / "SKILL.md").exists())
            self.assertTrue((moda_skill / "tools" / "run_pipeline.py").exists())
            self.assertEqual(moda_skill.resolve(), installer.MODA_ROOT.resolve())
            self.assertFalse((home / ".agents/skills/moda-v4").exists())
            runtime = json.loads(Path(result["runtime_config"]).read_text(encoding="utf-8"))
            self.assertEqual(Path(runtime["moda_root"]).resolve(), installer.MODA_ROOT.resolve())
            self.assertEqual(Path(runtime["memory_path"]).resolve(), Path(result["method_memory"]["path"]).resolve())
            self.assertTrue(Path(result["method_memory"]["path"]).is_file())
            self.assertTrue(result["method_memory"]["created"])
            import tomllib
            parsed = tomllib.loads(agent_path.read_text(encoding="utf-8"))
            self.assertEqual(parsed["name"], "moda_companion")
            self.assertEqual(len(parsed["skills"]["config"]), 1)
            self.assertTrue(Path(result["updater"]).is_file())
            hook = json.loads(Path(result["hook"]).read_text(encoding="utf-8"))
            command = hook["hooks"]["SessionStart"][-1]["hooks"][0]["command"]
            self.assertIn("moda-companion", command)
            self.assertIn("release_updater.py session-start", command)

    def test_claude_install_writes_skill_list(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = installer.install_claude(Path(directory), force=False, moda_root=installer.MODA_ROOT)
            agent_text = Path(result["agent"]).read_text(encoding="utf-8")
            self.assertIn("skills:\n  - moda-companion", agent_text)
            self.assertNotIn("  - moda-v4", agent_text)
            self.assertTrue((Path(result["companion_skill"]) / "_runtime/moda-v4/tools/run_pipeline.py").is_file())
            self.assertTrue(Path(result["method_memory"]["path"]).is_file())

    def test_openminis_install_exposes_one_skill_and_installs_soul(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = installer.install_openminis(
                root / "skills",
                root / "memory",
                force=False,
                moda_root=installer.MODA_ROOT,
            )
            self.assertTrue((root / "skills/moda-companion/SKILL.md").exists())
            self.assertFalse((root / "skills/moda-v4/SKILL.md").exists())
            self.assertTrue((root / "skills/moda-companion/_runtime/moda-v4/SKILL.md").exists())
            self.assertTrue((root / "skills/moda-companion/scripts/release_updater.py").exists())
            self.assertTrue((root / "memory/SOUL.md").exists())
            self.assertTrue(Path(result["soul"]["state_path"]).exists())
            self.assertTrue(result["soul_activated"])
            method_path = Path(result["method_memory"]["path"])
            self.assertEqual(method_path, root / "memory/moda-companion-memory.json")
            method_payload = json.loads(method_path.read_text(encoding="utf-8"))
            self.assertIn(memory.METHOD_MEMORY_KEY, method_payload["entries"])

    def test_copy_helpers_allow_source_already_at_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "SKILL.md").write_text("---\nname: moda-v4\ndescription: test\n---\n", encoding="utf-8")
            (root / "tools").mkdir()
            (root / "tools/run_pipeline.py").write_text("", encoding="utf-8")
            self.assertEqual(installer._copy_tree(root, root, force=False), root)
            self.assertEqual(installer._copy_moda_skill(root, root, force=False), root)

    def test_existing_complete_moda_skill_is_reused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            target = Path(directory) / "target"
            for root in (source, target):
                (root / "tools").mkdir(parents=True)
                (root / "SKILL.md").write_text("---\nname: moda-v4\ndescription: test\n---\n", encoding="utf-8")
                (root / "tools/run_pipeline.py").write_text("", encoding="utf-8")
            marker = target / "keep.txt"
            marker.write_text("original", encoding="utf-8")
            reused = installer._copy_moda_skill(source, target, force=False)
            self.assertEqual(reused, target)
            self.assertEqual(marker.read_text(encoding="utf-8"), "original")


class SkillContractTest(unittest.TestCase):
    def test_method_framework_keeps_authoritative_rating_boundary(self) -> None:
        method_text = (ROOT / "references" / "method.md").read_text(encoding="utf-8")
        for expected in ("五类机会模型", "五问过滤器", "A 已坐实", "三级信息不能成为投资理由"):
            self.assertIn(expected, method_text)
        for expected in ("research_score", "五态", "不能重算、覆盖或绕开评分"):
            self.assertIn(expected, method_text)

    def test_skill_routes_user_examples_into_guarded_agent_judgment(self) -> None:
        skill_text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        for expected in ("莫大会怎么看这个股票", "这个行业符合莫大审美吗", "按照莫大逻辑选股"):
            self.assertIn(expected, skill_text)
        self.assertIn("不是第二套评分模型", skill_text)
        self.assertIn("观察 / 等待 / 试错 / 买入 / 退出", skill_text)
        self.assertIn("research_score", skill_text)
        self.assertNotIn("action_rating", skill_text)
        self.assertIn("scripts/analyze_logic.py", skill_text)
        self.assertIn("--logic-json", skill_text)
        self.assertIn("--judgment-json", skill_text)
        self.assertIn("collector_only", skill_text)
        self.assertIn("scripts/memory.py seed-method", skill_text)
        self.assertIn("scripts/memory.py list", skill_text)
        self.assertIn("原样输出整份合并报告", skill_text)
        self.assertIn("不得用链接、摘要或“报告已生成”代替完整报告内容", skill_text)


if __name__ == "__main__":
    unittest.main()
