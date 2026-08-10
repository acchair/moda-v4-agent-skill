from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
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
memory = load_module("companion_memory", ROOT / "scripts" / "memory.py")
soul = load_module("companion_soul", ROOT / "adapters" / "openminis" / "install_soul.py")
installer = load_module("companion_installer", ROOT / "install.py")


class CompanionAnalysisTest(unittest.TestCase):
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
                    "action_rating": "矛",
                    "action_rating_reason": "测试原因",
                    "coverage": 0.8,
                    "unknown_maximum": 20,
                    "signal": "中性",
                    "hard_caps": [{"condition": "测试", "result": "未触发", "cap": "无"}],
                },
            }
            (root / "knowledge/research/scorecards/000001.json").write_text(json.dumps(card, ensure_ascii=False), encoding="utf-8")
            (root / "knowledge/research/scoring/000001.md").write_text("# 正式报告\n", encoding="utf-8")
            (root / "knowledge/research/pipeline/000001.json").write_text("[]", encoding="utf-8")

            result = analysis.load_analysis(root, "000001")
            self.assertEqual(result["research_score"], 72)
            self.assertEqual(result["action_rating"], "矛")
            self.assertEqual(result["formal_report"], "# 正式报告\n")

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
            import tomllib
            parsed = tomllib.loads(agent_path.read_text(encoding="utf-8"))
            self.assertEqual(parsed["name"], "moda_companion")
            self.assertEqual(len(parsed["skills"]["config"]), 2)

    def test_claude_install_writes_skill_list(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = installer.install_claude(Path(directory), force=False, moda_root=installer.MODA_ROOT)
            agent_text = Path(result["agent"]).read_text(encoding="utf-8")
            self.assertIn("skills:\n  - moda-companion\n  - moda-v4", agent_text)

    def test_openminis_install_copies_two_skills_and_installs_soul(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = installer.install_openminis(
                root / "skills",
                root / "memory",
                force=False,
                moda_root=installer.MODA_ROOT,
            )
            self.assertTrue((root / "skills/moda-companion/SKILL.md").exists())
            self.assertTrue((root / "skills/moda-v4/SKILL.md").exists())
            self.assertTrue((root / "memory/SOUL.md").exists())
            self.assertTrue(Path(result["soul"]["state_path"]).exists())

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
        for expected in ("research_score", "action_rating", "不能重算、覆盖或绕开评分"):
            self.assertIn(expected, method_text)

    def test_skill_routes_user_examples_without_creating_second_rating(self) -> None:
        skill_text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        for expected in ("莫大会怎么看这个股票", "这个行业符合莫大审美吗", "按照莫大逻辑选股"):
            self.assertIn(expected, skill_text)
        self.assertIn("不是第二套评分模型", skill_text)
        self.assertIn("最终行动名称仍以 moda-v4 的 `action_rating` 为准", skill_text)


if __name__ == "__main__":
    unittest.main()
