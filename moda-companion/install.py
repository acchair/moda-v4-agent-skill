#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

COMPANION_ROOT = Path(__file__).resolve().parent
MODA_ROOT = COMPANION_ROOT.parent
IGNORED_NAMES = {
    ".git", ".env", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "knowledge", "output", "skills", "tests", "moda-companion",
}


def _ignore_runtime_files(directory: str, names: list[str]) -> set[str]:
    ignored: set[str] = set()
    for name in names:
        if (
            name in IGNORED_NAMES
            or name.startswith("test_")
            or name.endswith((".pyc", ".pyo", ".log", ".bak"))
        ):
            ignored.add(name)
    return ignored


def _copy_tree(source: Path, target: Path, *, force: bool) -> Path:
    if source.resolve() == target.resolve():
        return target
    if target.exists():
        if not force:
            raise FileExistsError(f"目标已存在：{target}。确认更新时使用 --force")
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, ignore=_ignore_runtime_files)
    return target


def find_moda_source(explicit: Path | None = None) -> Path:
    candidates = [
        explicit,
        Path(os.environ["MODA_V4_ROOT"]) if os.environ.get("MODA_V4_ROOT") else None,
        MODA_ROOT,
        COMPANION_ROOT.parent / "moda-v4",
        Path("/var/minis/skills/moda-v4"),
        Path.home() / ".agents" / "skills" / "moda-v4",
        Path.home() / ".codex" / "skills" / "moda-v4",
        Path.home() / ".claude" / "skills" / "moda-v4",
    ]
    for candidate in candidates:
        if candidate is None:
            continue
        root = candidate.expanduser().resolve()
        if (root / "SKILL.md").is_file() and (root / "tools" / "run_pipeline.py").is_file():
            return root
    raise FileNotFoundError("未找到 moda-v4 源目录；请使用 --moda-root 或设置 MODA_V4_ROOT")


def _copy_moda_skill(source: Path, target: Path, *, force: bool) -> Path:
    source = source.resolve()
    if source == target.resolve():
        return target
    if target.exists():
        if (target / "SKILL.md").is_file() and (target / "tools" / "run_pipeline.py").is_file() and not force:
            return target
        if not force:
            raise FileExistsError(f"目标已存在但不是完整 moda-v4：{target}。确认替换时使用 --force")
        shutil.rmtree(target)
    target.mkdir(parents=True)
    for filename in ("SKILL.md", "work.md", "requirements.txt", ".env.example"):
        source_file = source / filename
        if source_file.exists():
            shutil.copy2(source_file, target / filename)
    shutil.copytree(source / "tools", target / "tools", ignore=_ignore_runtime_files)
    if (source / "agents").exists():
        shutil.copytree(source / "agents", target / "agents", ignore=_ignore_runtime_files)
    (target / "knowledge" / "research").mkdir(parents=True)
    (target / "knowledge" / "research" / ".gitkeep").write_text("", encoding="utf-8")
    return target


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def write_runtime_config(companion_skill: Path, moda_skill: Path, memory_path: Path) -> Path:
    path = companion_skill / ".moda-companion-runtime.json"
    path.write_text(
        json.dumps({
            "version": 2,
            "moda_root": str(moda_skill.resolve()),
            "memory_path": str(memory_path.resolve()),
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def install_method_memory(companion_skill: Path, memory_path: Path) -> dict[str, Any]:
    import importlib.util

    module_path = companion_skill / "scripts" / "memory.py"
    spec = importlib.util.spec_from_file_location("moda_companion_memory_installer", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载莫大方法记忆安装器")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.seed_method_memory(memory_path)


def install_internal_updater(companion_skill: Path, moda_source: Path) -> Path:
    source = moda_source / "release-updater" / "scripts" / "release_updater.py"
    if not source.is_file():
        raise FileNotFoundError(f"缺少内部更新器：{source}")
    destination = companion_skill / "scripts" / "release_updater.py"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return destination


def _shell_command(parts: list[str]) -> str:
    return subprocess.list2cmdline(parts) if os.name == "nt" else shlex.join(parts)


def install_codex_hook(home: Path, updater: Path, moda_root: Path) -> Path:
    path = home / ".codex" / "hooks.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            payload = {}
    except (FileNotFoundError, json.JSONDecodeError):
        payload = {}
    groups = payload.setdefault("hooks", {}).setdefault("SessionStart", [])

    def managed(group: Any) -> bool:
        if not isinstance(group, dict):
            return False
        return any(
            isinstance(hook, dict) and "release_updater.py session-start" in str(hook.get("command") or "")
            for hook in group.get("hooks", [])
        )

    groups[:] = [group for group in groups if not managed(group)]
    command = _shell_command([
        sys.executable, str(updater.resolve()), "session-start", "--target", str(moda_root.resolve()),
    ])
    groups.append({
        "matcher": "startup|resume|clear|compact",
        "hooks": [{"type": "command", "command": command, "timeout": 5}],
    })
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return path


def render_codex_agent(companion_skill: Path) -> str:
    return "\n".join(
        [
            'name = "moda_companion"',
            'description = "莫大方法研究、A股分析和版本管理的唯一入口。"',
            'developer_instructions = """',
            "Read the enabled moda-companion skill before responding. You are inspired by public Moda materials, not the real person.",
            "Use analyze_logic.py as the single entry for stock and sector research. Start with --phase baseline, build and validate a falsifiable Logic Case, then run --phase evidence only for missing arrows. Facts and inferences must remain separate; search hits are candidates, not confirmed facts. Never present a collector_only report as the conclusion.",
            "For a specific A-share, do deep collection only after the Logic Case is coherent. Finalize through analyze_logic.py with both --logic-json and --judgment-json. Agent Judgment V4 must cover the market-versus-fundamental contradiction, main-business-anchored chain position, why now, old pressure and marginal change, peer choice, causal breakpoint, Bull/Base/Bear, valuation scenarios, five-state decision, and exactly three verification variables. Scores and technicals remain in the audit layer; technicals only address timing. Inline the complete merged report.",
            "For sector selection, use analyze_logic.py --kind sector --phase baseline to create the shared sector Logic Case and return Top 6 for light screening. Ask the user to select at most three candidates before --phase deep. Sector logic and sector_state are research priorities, never stock five-state decisions.",
            "Never alter research_score, evidence coverage, valuation scenario numbers, source status, or Hard Caps. Never store secrets or private portfolio details.",
            '"""',
            "",
            "[[skills.config]]",
            f"path = {_toml_string(str(companion_skill / 'SKILL.md'))}",
            "enabled = true",
        ]
    )


def install_codex(home: Path, *, force: bool, moda_root: Path | None = None) -> dict[str, Any]:
    skills_dir = home / ".agents" / "skills"
    companion_skill = _copy_tree(COMPANION_ROOT, skills_dir / "moda-companion", force=force)
    moda_skill = find_moda_source(moda_root)
    updater = install_internal_updater(companion_skill, moda_skill)
    memory_path = home / ".moda-companion" / "memory.json"
    runtime_config = write_runtime_config(companion_skill, moda_skill, memory_path)
    method_memory = install_method_memory(companion_skill, memory_path)
    hook = install_codex_hook(home, updater, moda_skill)
    agent_path = home / ".codex" / "agents" / "moda-companion.toml"
    if agent_path.exists() and not force:
        raise FileExistsError(f"Agent 配置已存在：{agent_path}。确认更新时使用 --force")
    agent_path.parent.mkdir(parents=True, exist_ok=True)
    agent_path.write_text(render_codex_agent(companion_skill), encoding="utf-8")
    return {
        "companion_skill": str(companion_skill),
        "moda_skill": str(moda_skill),
        "runtime_config": str(runtime_config),
        "method_memory": method_memory,
        "agent": str(agent_path),
        "updater": str(updater),
        "hook": str(hook),
    }


def render_claude_agent() -> str:
    return """---
name: moda-companion
description: 莫大方法研究、A股分析和版本管理的唯一入口。
tools: Read, Grep, Glob, Bash
skills:
  - moda-companion
memory: user
---

读取并严格遵守 moda-companion Skill。你不是莫大本人。

个股和板块统一运行 moda-companion/scripts/analyze_logic.py。先用 `--phase baseline` 建立最小事实包和可证伪 Logic Case，再按逻辑箭头用 `--phase evidence` 定向补证；事实与推断分开，搜索命中只算候选证据，禁止把 `collector_only` 报告当成结论。

具体 A 股只有在 Logic Case 成立后才进入深度采集，并同时提交 `--logic-json` 与 `--judgment-json` 完成 Agent Judgment V4。判断必须覆盖市场与基本面阶段矛盾、主营产业链位置、为什么现在看、过去压力与边际变化、同行选择、因果断点、Bull/Base/Bear、历史估值赔率、五态和恰好三项验证变量。评分和技术指标只留在审计层，技术面只决定时点；不得修改研究分、覆盖率、估值情景、来源状态或 Hard Cap。聊天中原样输出完整合并报告。

用户说“选股 XX 板块”时先运行 `analyze_logic.py <板块> --kind sector --phase baseline`，形成共享板块 Logic Case 和前六轻筛。轻筛不跑完整个股流水线、不展示研究分、不输出五态。展示前六后让用户选择最多三家，确认后才用 `--phase deep --candidate <代码>` 深研。sector_state 仅表示研究优先级，不是个股五态或交易建议。
"""


def install_claude(home: Path, *, force: bool, moda_root: Path | None = None) -> dict[str, Any]:
    skills_dir = home / ".claude" / "skills"
    companion_skill = _copy_tree(COMPANION_ROOT, skills_dir / "moda-companion", force=force)
    moda_source = find_moda_source(moda_root)
    moda_skill = _copy_moda_skill(moda_source, companion_skill / "_runtime" / "moda-v4", force=True)
    updater = install_internal_updater(companion_skill, moda_source)
    memory_path = home / ".moda-companion" / "memory.json"
    runtime_config = write_runtime_config(companion_skill, moda_skill, memory_path)
    method_memory = install_method_memory(companion_skill, memory_path)
    agent_path = home / ".claude" / "agents" / "moda-companion.md"
    if agent_path.exists() and not force:
        raise FileExistsError(f"Agent 配置已存在：{agent_path}。确认更新时使用 --force")
    agent_path.parent.mkdir(parents=True, exist_ok=True)
    agent_path.write_text(render_claude_agent(), encoding="utf-8")
    return {
        "companion_skill": str(companion_skill),
        "moda_skill": str(moda_skill),
        "runtime_config": str(runtime_config),
        "method_memory": method_memory,
        "agent": str(agent_path),
        "updater": str(updater),
    }


def install_openminis(
    skills_dir: Path,
    memory_dir: Path,
    *,
    force: bool,
    moda_root: Path | None = None,
) -> dict[str, Any]:
    companion_skill = _copy_tree(COMPANION_ROOT, skills_dir / "moda-companion", force=force)
    moda_source = find_moda_source(moda_root)
    moda_skill = _copy_moda_skill(moda_source, companion_skill / "_runtime" / "moda-v4", force=True)
    updater = install_internal_updater(companion_skill, moda_source)
    memory_path = memory_dir / "moda-companion-memory.json"
    runtime_config = write_runtime_config(companion_skill, moda_skill, memory_path)

    import importlib.util

    soul_module_path = COMPANION_ROOT / "adapters" / "openminis" / "install_soul.py"
    spec = importlib.util.spec_from_file_location("moda_companion_soul_installer", soul_module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载 OpenMinis SOUL 安装器")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    soul_result = module.install_soul(
        memory_dir,
        companion_skill / "adapters" / "openminis" / "SOUL.md",
        force=force,
    )
    method_memory = install_method_memory(companion_skill, memory_path)
    return {
        "companion_skill": str(companion_skill),
        "moda_skill": str(moda_skill),
        "runtime_config": str(runtime_config),
        "soul": soul_result,
        "soul_activated": True,
        "method_memory": method_memory,
        "updater": str(updater),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Install the single-entry Moda Companion with internal collector and updater")
    parser.add_argument("platform", choices=["codex", "claude", "openminis"])
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--moda-root", type=Path)
    parser.add_argument("--skills-dir", type=Path, default=Path("/var/minis/skills"))
    parser.add_argument("--memory-dir", type=Path, default=Path("/var/minis/memory"))
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    if args.platform == "codex":
        result: Any = install_codex(args.home.expanduser(), force=args.force, moda_root=args.moda_root)
    elif args.platform == "claude":
        result = install_claude(args.home.expanduser(), force=args.force, moda_root=args.moda_root)
    else:
        result = install_openminis(
            args.skills_dir,
            args.memory_dir,
            force=args.force,
            moda_root=args.moda_root,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
