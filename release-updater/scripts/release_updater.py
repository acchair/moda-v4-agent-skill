#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Any


REPO = "acchair/moda-v4-agent-skill"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
MANAGED_SCRIPT_MARKER = "release_updater.py"
MANAGED_COMMAND_MARKER = "session-start"
STATE_VERSION = 1
ALLOWED_DOWNLOAD_HOSTS = {
    "api.github.com",
    "github.com",
    "codeload.github.com",
    "objects.githubusercontent.com",
}
PRESERVED_PREFIXES = (
    ".git/",
    ".codex/",
    ".env",
    "knowledge/research/",
    "knowledge/output/",
    "output/",
)


class UpgradeBlocked(RuntimeError):
    pass


def source_skill_root() -> Path:
    return Path(__file__).resolve().parents[1]


def default_target() -> Path:
    explicit = os.environ.get("MODA_V4_ROOT")
    if explicit:
        return Path(explicit).expanduser().resolve()
    runtime_config = source_skill_root() / ".moda-companion-runtime.json"
    if runtime_config.is_file():
        try:
            configured = json.loads(runtime_config.read_text(encoding="utf-8")).get("moda_root")
            if configured:
                return Path(configured).expanduser().resolve()
        except (OSError, ValueError, TypeError):
            pass
    source_parent = source_skill_root().parent
    if (source_parent / "SKILL.md").is_file() and (source_parent / "tools").is_dir():
        return source_parent
    payload = load_state()
    remembered = payload.get("target")
    if remembered:
        return Path(str(remembered)).expanduser().resolve()
    current = Path.cwd().resolve()
    if (current / "SKILL.md").is_file() and (current / "tools").is_dir():
        return current
    return source_parent


def state_dir() -> Path:
    override = os.environ.get("MODA_RELEASE_UPDATER_STATE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "moda-release-updater"
    return Path.home() / ".cache" / "moda-release-updater"


def state_path() -> Path:
    return state_dir() / "state.json"


def lock_path() -> Path:
    return state_dir() / "check.lock"


def load_state() -> dict[str, Any]:
    path = state_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            payload.setdefault("version", STATE_VERSION)
            payload.setdefault("skipped_tags", [])
            return payload
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return {"version": STATE_VERSION, "skipped_tags": []}


def write_state(payload: dict[str, Any]) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


def today_text() -> str:
    return date.today().isoformat()


def checked_today(payload: dict[str, Any]) -> bool:
    return payload.get("last_check_date") == today_text()


def record_check(payload: dict[str, Any], *, status: str, error: str = "") -> None:
    payload["last_check_date"] = today_text()
    payload["last_check_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    payload["last_check_status"] = status
    if error:
        payload["last_error"] = error[:500]
    else:
        payload.pop("last_error", None)
    write_state(payload)


def acquire_lock() -> bool:
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            age = datetime.now().timestamp() - path.stat().st_mtime
            if age > 900:
                path.unlink(missing_ok=True)
                return acquire_lock()
        except OSError:
            pass
        return False
    with os.fdopen(descriptor, "w", encoding="ascii") as handle:
        handle.write(str(os.getpid()))
    return True


def release_lock() -> None:
    lock_path().unlink(missing_ok=True)


def request_json(url: str, timeout: int = 15) -> dict[str, Any] | None:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "moda-release-updater/1",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise RuntimeError(f"GitHub 检查失败：HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"GitHub 检查失败：{exc.reason if hasattr(exc, 'reason') else exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("GitHub 返回了无效的 Release 数据")
    return payload


def latest_release() -> dict[str, Any] | None:
    payload = request_json(API_URL)
    if not payload:
        return None
    tag = str(payload.get("tag_name") or "").strip()
    if not tag:
        raise RuntimeError("GitHub Release 缺少版本标签")
    if payload.get("draft") or payload.get("prerelease"):
        return None
    return payload


def run(command: list[str], *, cwd: Path, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=str(cwd),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def git(target: Path, *args: str, timeout: int = 120) -> str:
    result = run(["git", *args], cwd=target, timeout=timeout)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(detail or f"git {' '.join(args)} 执行失败")
    return result.stdout.strip()


def is_git_repo(target: Path) -> bool:
    return (target / ".git").exists()


def validate_tag(tag: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", tag):
        raise UpgradeBlocked("Release 标签格式不安全，已停止升级")
    return tag


def release_installed(target: Path, tag: str, payload: dict[str, Any]) -> bool:
    if payload.get("target") == str(target.resolve()) and payload.get("installed_tag") == tag:
        return True
    if not is_git_repo(target):
        return False
    try:
        return tag in git(target, "tag", "--points-at", "HEAD").splitlines()
    except RuntimeError:
        return False


def clean_release_text(text: str, limit: int = 4000) -> str:
    value = text.replace("\r\n", "\n").strip()
    value = re.sub(r"```.*?```", "[代码片段]", value, flags=re.S)
    value = re.sub(r"^#{1,6}\s*", "", value, flags=re.M)
    value = re.sub(r"\[([^]]+)]\([^)]*\)", r"\1", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    if not value:
        value = "该 Release 未提供改动说明。"
    return value[:limit] + ("\n…" if len(value) > limit else "")


def release_summary(release: dict[str, Any]) -> str:
    name = str(release.get("name") or release.get("tag_name") or "新版本")
    tag = str(release.get("tag_name") or "")
    published = str(release.get("published_at") or "")[:10] or "未知"
    body = clean_release_text(str(release.get("body") or ""))
    return f"版本：{name}（{tag}）\n发布日期：{published}\n\n改动摘要\n{body}"


def target_matches_repo(target: Path) -> bool:
    try:
        origin = git(target, "remote", "get-url", "origin")
    except RuntimeError:
        return False
    normalized = origin.lower().removesuffix(".git")
    return normalized.endswith(REPO.lower())


def install_requirements(target: Path) -> str:
    requirements = target / "requirements.txt"
    if not requirements.is_file():
        return "未发现 requirements.txt，已跳过依赖更新。"
    result = run(
        [sys.executable, "-m", "pip", "install", "-r", str(requirements)],
        cwd=target,
        timeout=900,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-1200:]
        raise RuntimeError(f"代码已更新，但依赖安装失败：\n{detail}")
    return "依赖已同步。"


def upgrade_git(target: Path, tag: str) -> str:
    if not target_matches_repo(target):
        raise UpgradeBlocked(f"目标目录的 origin 不是 {REPO}，已停止升级")
    dirty = git(target, "status", "--porcelain", "--untracked-files=all")
    if dirty:
        raise UpgradeBlocked("检测到未提交修改。为保护当前工作，本次没有覆盖文件；请先提交或妥善保存修改后再升级。")
    git(target, "fetch", "--force", "origin", f"refs/tags/{tag}:refs/tags/{tag}", timeout=300)
    head = git(target, "rev-parse", "HEAD")
    release_commit = git(target, "rev-parse", f"{tag}^{{commit}}")
    if head == release_commit:
        action = "当前代码已经是该 Release。"
    else:
        contains = run(["git", "merge-base", "--is-ancestor", release_commit, head], cwd=target)
        if contains.returncode == 0:
            action = "当前代码已包含该 Release，无需移动版本。"
        else:
            fast_forward = run(["git", "merge-base", "--is-ancestor", head, release_commit], cwd=target)
            if fast_forward.returncode != 0:
                raise UpgradeBlocked("当前分支与 Release 不能快进合并，已停止自动升级。")
            git(target, "merge", "--ff-only", tag, timeout=300)
            action = f"代码已升级到 {tag}。"
    dependency_result = install_requirements(target)
    return f"{action}\n{dependency_result}"


def safe_archive_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in ALLOWED_DOWNLOAD_HOSTS:
        raise UpgradeBlocked("Release 下载地址不在允许的 GitHub 域名中")
    return url


def preserved(relative: Path) -> bool:
    text = relative.as_posix()
    return any(text == prefix.rstrip("/") or text.startswith(prefix) for prefix in PRESERVED_PREFIXES)


def archive_root(extracted: Path) -> Path:
    candidates = [path for path in extracted.iterdir() if path.is_dir()]
    for candidate in candidates:
        if (candidate / "SKILL.md").is_file() and (candidate / "tools").is_dir():
            return candidate
    raise RuntimeError("Release 压缩包缺少 moda-v4 文件结构")


def upgrade_archive(target: Path, release: dict[str, Any], tag: str) -> str:
    url = safe_archive_url(str(release.get("zipball_url") or ""))
    with tempfile.TemporaryDirectory(prefix="moda-release-") as temporary_name:
        temporary = Path(temporary_name)
        archive = temporary / "release.zip"
        request = urllib.request.Request(url, headers={"User-Agent": "moda-release-updater/1"})
        with urllib.request.urlopen(request, timeout=120) as response:
            safe_archive_url(response.geturl())
            archive.write_bytes(response.read())
        extracted = temporary / "extracted"
        extracted.mkdir()
        with zipfile.ZipFile(archive) as package:
            base = extracted.resolve()
            for member in package.infolist():
                destination = (extracted / member.filename).resolve()
                if base not in destination.parents and destination != base:
                    raise UpgradeBlocked("Release 压缩包包含不安全路径")
            package.extractall(extracted)
        source = archive_root(extracted)
        backup = state_dir() / "backups" / f"{tag}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        copied = 0
        for source_file in source.rglob("*"):
            if not source_file.is_file():
                continue
            relative = source_file.relative_to(source)
            if preserved(relative):
                continue
            destination = target / relative
            if destination.exists():
                backup_file = backup / relative
                backup_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(destination, backup_file)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, destination)
            copied += 1
    dependency_result = install_requirements(target)
    return f"已从 Release 覆盖更新 {copied} 个程序文件。备份位于：{backup}\n{dependency_result}"


def copy_skill(source: Path, destination: Path) -> None:
    source = source.resolve()
    destination = destination.resolve()
    if source == destination:
        return
    temporary = destination.with_name(destination.name + ".installing")
    shutil.rmtree(temporary, ignore_errors=True)
    shutil.copytree(
        source,
        temporary,
        ignore=shutil.ignore_patterns("tests", "__pycache__", "*.pyc"),
    )
    if destination.exists():
        shutil.rmtree(destination)
    temporary.replace(destination)


def shell_command(parts: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(parts)
    return shlex.join(parts)


def managed_hook_group(group: Any) -> bool:
    if not isinstance(group, dict):
        return False
    for hook in group.get("hooks", []):
        if isinstance(hook, dict):
            command = str(hook.get("command") or "")
            if MANAGED_SCRIPT_MARKER in command and MANAGED_COMMAND_MARKER in command:
                return True
    return False


def install_hook(script: Path, target: Path, hooks_path: Path | None = None) -> Path:
    path = hooks_path or (Path.home() / ".codex" / "hooks.json")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            payload = {}
    except (FileNotFoundError, json.JSONDecodeError):
        payload = {}
    hooks = payload.setdefault("hooks", {})
    groups = hooks.setdefault("SessionStart", [])
    groups[:] = [group for group in groups if not managed_hook_group(group)]
    command = shell_command(
        [sys.executable, str(script.resolve()), "session-start", "--target", str(target.resolve())]
    )
    groups.append(
        {
            "matcher": "startup|resume|clear|compact",
            "hooks": [{"type": "command", "command": command, "timeout": 5}],
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return path


def install_global(target: Path, hooks_path: Path | None = None) -> dict[str, str]:
    del hooks_path
    installer = target.resolve() / "moda-companion" / "install.py"
    if not installer.is_file():
        raise UpgradeBlocked(f"缺少统一安装入口：{installer}")
    return {
        "status": "use_companion_installer",
        "message": "更新器已并入莫大 Agent，不再安装独立 moda-release-updater Skill。",
        "command": shell_command(
            [sys.executable, str(installer), "codex", "--moda-root", str(target.resolve())]
        ),
        "target": str(target.resolve()),
    }


def refresh_installed_skill(target: Path) -> None:
    updated_source = target / "moda-companion"
    if not updated_source.is_dir():
        return
    destinations = (
        Path.home() / ".agents" / "skills" / "moda-companion",
        Path.home() / ".claude" / "skills" / "moda-companion",
        Path("/var/minis/skills/moda-companion"),
    )
    for destination in destinations:
        if not destination.is_dir():
            continue
        for source_file in updated_source.rglob("*"):
            if not source_file.is_file():
                continue
            relative = source_file.relative_to(updated_source)
            if relative.as_posix() in {".moda-companion-runtime.json"} or "_runtime" in relative.parts:
                continue
            output = destination / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, output)
        if destination == destinations[0]:
            install_hook(destination / "scripts" / "release_updater.py", target)


def upgrade(target: Path, release: dict[str, Any], payload: dict[str, Any]) -> str:
    tag = validate_tag(str(release.get("tag_name") or ""))
    if is_git_repo(target):
        result = upgrade_git(target, tag)
    else:
        result = upgrade_archive(target, release, tag)
    payload["installed_tag"] = tag
    payload["target"] = str(target.resolve())
    payload["last_upgrade_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    payload["last_upgrade_status"] = "success"
    write_state(payload)
    refresh_installed_skill(target)
    return result


def save_skipped(payload: dict[str, Any], tag: str) -> None:
    skipped = {str(item) for item in payload.get("skipped_tags", [])}
    skipped.add(tag)
    payload["skipped_tags"] = sorted(skipped)
    write_state(payload)


def unskip(tag: str) -> bool:
    payload = load_state()
    skipped = {str(item) for item in payload.get("skipped_tags", [])}
    existed = tag in skipped
    skipped.discard(tag)
    payload["skipped_tags"] = sorted(skipped)
    write_state(payload)
    return existed


def skip_version(tag: str) -> bool:
    normalized = validate_tag(tag)
    payload = load_state()
    skipped = {str(item) for item in payload.get("skipped_tags", [])}
    existed = normalized in skipped
    save_skipped(payload, normalized)
    return not existed


def upgrade_now(target: Path, expected_tag: str) -> dict[str, str]:
    release = latest_release()
    if not release:
        raise UpgradeBlocked("GitHub 尚无可安装的正式 Release")
    tag = validate_tag(str(release.get("tag_name") or ""))
    expected = validate_tag(expected_tag)
    if tag != expected:
        raise UpgradeBlocked(f"最新版本已从 {expected} 变为 {tag}，请重新确认后再升级")
    payload = load_state()
    payload["latest_tag"] = tag
    payload["latest_release_name"] = str(release.get("name") or tag)
    payload["latest_release_summary"] = release_summary(release)
    write_state(payload)
    detail = upgrade(target, release, payload)
    return {"status": "upgraded", "tag": tag, "detail": detail}


def show_release_prompt(target: Path, release: dict[str, Any], payload: dict[str, Any]) -> str:
    try:
        import tkinter as tk
        from tkinter import messagebox, scrolledtext, ttk
    except ImportError as exc:
        raise RuntimeError("系统缺少图形提示组件 tkinter") from exc

    tag = str(release.get("tag_name") or "")
    choice = "no"
    root = tk.Tk()
    root.title("Moda v4 发现新版本")
    root.geometry("720x540")
    root.minsize(620, 460)
    root.columnconfigure(0, weight=1)
    root.rowconfigure(1, weight=1)

    heading = ttk.Label(root, text="发现可用的新版本", font=("Microsoft YaHei UI", 15, "bold"))
    heading.grid(row=0, column=0, sticky="w", padx=20, pady=(18, 10))
    summary = scrolledtext.ScrolledText(root, wrap=tk.WORD, font=("Microsoft YaHei UI", 10))
    summary.insert("1.0", release_summary(release))
    summary.configure(state="disabled")
    summary.grid(row=1, column=0, sticky="nsew", padx=20)

    status = ttk.Label(root, text="是否立即升级？")
    status.grid(row=2, column=0, sticky="w", padx=20, pady=(12, 6))
    buttons = ttk.Frame(root)
    buttons.grid(row=3, column=0, sticky="e", padx=20, pady=(0, 18))

    def finish(value: str) -> None:
        nonlocal choice
        choice = value
        if value == "skip":
            save_skipped(payload, tag)
            root.destroy()
            return
        if value == "no":
            root.destroy()
            return
        for button in buttons.winfo_children():
            button.configure(state="disabled")
        status.configure(text="正在升级，请稍候…")
        root.update_idletasks()
        try:
            detail = upgrade(target, release, payload)
        except UpgradeBlocked as exc:
            payload["last_upgrade_status"] = "blocked"
            payload["last_error"] = str(exc)
            write_state(payload)
            messagebox.showwarning("Moda v4 未升级", str(exc), parent=root)
        except Exception as exc:
            payload["last_upgrade_status"] = "error"
            payload["last_error"] = str(exc)[:500]
            write_state(payload)
            messagebox.showerror("Moda v4 升级失败", str(exc), parent=root)
        else:
            messagebox.showinfo("Moda v4 升级完成", detail, parent=root)
        root.destroy()

    ttk.Button(buttons, text="是", command=lambda: finish("yes"), width=12).grid(row=0, column=0, padx=(0, 8))
    ttk.Button(buttons, text="跳过本版", command=lambda: finish("skip"), width=12).grid(row=0, column=1, padx=(0, 8))
    ttk.Button(buttons, text="否", command=lambda: finish("no"), width=12).grid(row=0, column=2)
    root.protocol("WM_DELETE_WINDOW", lambda: finish("no"))
    root.lift()
    root.attributes("-topmost", True)
    root.after(500, lambda: root.attributes("-topmost", False))
    root.mainloop()
    return choice


def perform_check(target: Path, *, prompt: bool, force: bool = False) -> dict[str, Any]:
    payload = load_state()
    if not force and checked_today(payload):
        tag = str(payload.get("latest_tag") or "")
        skipped = {str(item) for item in payload.get("skipped_tags", [])}
        if tag and tag not in skipped and not release_installed(target, tag, payload):
            return {
                "status": "update_available",
                "tag": tag,
                "summary": str(payload.get("latest_release_summary") or payload.get("latest_release_name") or tag),
                "cached": True,
            }
        return {"status": "already_checked", "date": today_text()}
    try:
        release = latest_release()
    except Exception as exc:
        record_check(payload, status="error", error=str(exc))
        return {"status": "error", "error": str(exc)}
    record_check(payload, status="ok")
    if not release:
        return {"status": "no_releases"}
    tag = str(release.get("tag_name") or "")
    payload = load_state()
    payload["latest_tag"] = tag
    payload["latest_release_name"] = str(release.get("name") or tag)
    payload["latest_release_summary"] = release_summary(release)
    write_state(payload)
    if tag in {str(item) for item in payload.get("skipped_tags", [])}:
        return {"status": "skipped", "tag": tag}
    if release_installed(target, tag, payload):
        return {"status": "current", "tag": tag}
    if prompt:
        choice = show_release_prompt(target, release, payload)
        return {"status": "prompted", "tag": tag, "choice": choice}
    return {"status": "update_available", "tag": tag, "summary": release_summary(release)}


def spawn_worker(target: Path) -> bool:
    command = [sys.executable, str(Path(__file__).resolve()), "worker", "--target", str(target.resolve())]
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "cwd": str(target.resolve()),
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NO_WINDOW
        )
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(command, **kwargs)
        return True
    except OSError:
        release_lock()
        return False


def session_start(target: Path) -> dict[str, Any]:
    try:
        sys.stdin.read()
    except OSError:
        pass
    if checked_today(load_state()):
        return {"continue": True, "suppressOutput": True}
    if not acquire_lock():
        return {"continue": True, "suppressOutput": True}
    spawn_worker(target)
    return {"continue": True, "suppressOutput": True}


def worker(target: Path) -> None:
    try:
        perform_check(target, prompt=False)
    finally:
        release_lock()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check and safely install moda-v4 GitHub Releases")
    commands = parser.add_subparsers(dest="command", required=True)

    install_parser = commands.add_parser("install")
    install_parser.add_argument("--target", type=Path, default=default_target())

    session_parser = commands.add_parser("session-start")
    session_parser.add_argument("--target", type=Path, required=True)

    worker_parser = commands.add_parser("worker")
    worker_parser.add_argument("--target", type=Path, required=True)

    check_parser = commands.add_parser("check-now")
    check_parser.add_argument("--target", type=Path, default=default_target())
    check_parser.add_argument("--no-prompt", action="store_true")
    check_parser.add_argument("--force", action="store_true")

    upgrade_parser = commands.add_parser("upgrade-now")
    upgrade_parser.add_argument("--target", type=Path, default=default_target())
    upgrade_parser.add_argument("--tag", required=True)

    commands.add_parser("status")
    skip_parser = commands.add_parser("skip")
    skip_parser.add_argument("--tag", required=True)
    unskip_parser = commands.add_parser("unskip")
    unskip_parser.add_argument("--tag", required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.command == "install":
        result: Any = install_global(args.target.expanduser().resolve())
    elif args.command == "session-start":
        result = session_start(args.target.expanduser().resolve())
    elif args.command == "worker":
        worker(args.target.expanduser().resolve())
        return
    elif args.command == "check-now":
        result = perform_check(
            args.target.expanduser().resolve(),
            prompt=not args.no_prompt,
            force=args.force,
        )
    elif args.command == "upgrade-now":
        result = upgrade_now(args.target.expanduser().resolve(), args.tag)
    elif args.command == "skip":
        result = {"tag": args.tag, "added": skip_version(args.tag)}
    elif args.command == "unskip":
        result = {"tag": args.tag, "removed": unskip(args.tag)}
    else:
        result = load_state()
        result["state_path"] = str(state_path())
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
