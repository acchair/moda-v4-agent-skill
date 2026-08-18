#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SENSITIVE_KEYWORDS = {
    "password", "passwd", "密码", "token", "cookie", "api_key", "apikey", "secret",
    "private_key", "私钥", "身份证", "手机号", "phone", "email", "账户", "账号",
    "持仓数量", "成本价", "交易记录", "银行卡", "database_url",
}
SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]{12,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)
METHOD_MEMORY_KEY = "system.moda_selection_logic_v2"
METHOD_MEMORY_VALUE = {
    "version": 2,
    "identity": "基于莫大公开材料整理的方法框架，不代表本人或其最新观点",
    "decision_order": [
        "世界与国家方向", "产业系统变化", "产业链瓶颈", "公司受益与利润兑现",
        "市场预期", "当前价格与赔率", "五态决策", "验证与证伪",
    ],
    "opportunity_models": [
        "产业成长型", "卡脖子瓶颈型", "周期反转型", "困境反转型", "企业家成长型",
    ],
    "selection_filters": [
        "解决什么真实产业问题",
        "是否处于难替代、难扩产、认证周期长或供应商少的关键位置",
        "现金流、负债、治理和股东行为是否安全",
        "是否存在有公开证据支持的重新定价",
        "失败场景下当前价格是否仍有安全边际",
    ],
    "evidence_rules": {
        "一级": "年报、公告、财报、合同、客户认证、产能与经营数据",
        "二级": "行业报告、公司交流和权威媒体，只作解释补充",
        "三级": "论坛、股吧和市场传闻，只作待核验线索",
        "discipline": "未知必须标记需人工确认；三级信息不能成为投资依据",
    },
    "company_benefit_levels": ["A 已坐实", "B 高概率受益", "C 主题关联"],
    "decision_states": ["观察", "等待", "试错", "买入", "退出"],
    "hard_caps": {
        "ST或退市风险": "强制退出",
        "实控人减持": "最高等待",
        "高位且拥挤过热": "最高等待",
    },
    "non_negotiables": [
        "research_score 只是证据仪表盘，不映射买卖",
        "先建立可证伪投资假设和逐箭头因果链",
        "少于两家已验证直接同行不得宣称最优",
        "估值情景不是目标价或收益承诺",
        "试错只是研究验证状态，不代表仓位或自动交易",
    ],
}


def default_memory_path() -> Path:
    configured = os.environ.get("MODA_COMPANION_MEMORY", "").strip()
    if configured:
        return Path(configured).expanduser()
    runtime_config = Path(__file__).resolve().parents[1] / ".moda-companion-runtime.json"
    if runtime_config.is_file():
        try:
            runtime_path = json.loads(runtime_config.read_text(encoding="utf-8")).get("memory_path")
            if runtime_path:
                return Path(runtime_path).expanduser()
        except (OSError, ValueError, TypeError):
            pass
    return Path.home() / ".moda-companion" / "memory.json"


def _reject_sensitive(key: str, value: Any) -> None:
    normalized = key.strip().lower()
    text = json.dumps(value, ensure_ascii=False)
    if any(word in normalized for word in SENSITIVE_KEYWORDS):
        raise ValueError("拒绝保存敏感字段")
    if any(pattern.search(text) for pattern in SECRET_PATTERNS):
        raise ValueError("内容疑似包含密钥、Token 或私钥，已拒绝保存")


def load_memory(path: Path | None = None) -> dict[str, Any]:
    target = path or default_memory_path()
    if not target.exists():
        return {"version": 1, "entries": {}}
    payload = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("entries"), dict):
        raise ValueError("记忆文件格式无效")
    return payload


def _atomic_write(target: Path, payload: dict[str, Any]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)


def remember(key: str, value: Any, tags: list[str] | None = None, path: Path | None = None) -> dict[str, Any]:
    _reject_sensitive(key, value)
    target = path or default_memory_path()
    payload = load_memory(target)
    entry = {
        "value": value,
        "tags": sorted(set(tags or [])),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    payload["entries"][key] = entry
    _atomic_write(target, payload)
    return entry


def seed_method_memory(path: Path | None = None) -> dict[str, Any]:
    """Install the public method memory once without replacing user edits."""
    target = path or default_memory_path()
    payload = load_memory(target)
    existing = payload["entries"].get(METHOD_MEMORY_KEY)
    if existing is not None:
        return {"path": str(target), "key": METHOD_MEMORY_KEY, "created": False, "entry": existing}
    entry = {
        "value": METHOD_MEMORY_VALUE,
        "tags": ["公开方法", "系统预置", "选股逻辑", "V2"],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    payload["entries"][METHOD_MEMORY_KEY] = entry
    _atomic_write(target, payload)
    return {"path": str(target), "key": METHOD_MEMORY_KEY, "created": True, "entry": entry}


def forget(key: str, path: Path | None = None) -> bool:
    target = path or default_memory_path()
    payload = load_memory(target)
    removed = payload["entries"].pop(key, None) is not None
    if removed:
        _atomic_write(target, payload)
    return removed


def main() -> None:
    parser = argparse.ArgumentParser(description="Store only non-sensitive Moda Companion memories")
    parser.add_argument("--path", type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)
    save_parser = subparsers.add_parser("remember")
    save_parser.add_argument("key")
    save_parser.add_argument("value")
    save_parser.add_argument("--tag", action="append", default=[])
    forget_parser = subparsers.add_parser("forget")
    forget_parser.add_argument("key")
    subparsers.add_parser("seed-method")
    subparsers.add_parser("list")
    args = parser.parse_args()

    if args.command == "remember":
        result: Any = remember(args.key, args.value, args.tag, args.path)
    elif args.command == "forget":
        result = {"removed": forget(args.key, args.path)}
    elif args.command == "seed-method":
        result = seed_method_memory(args.path)
    else:
        result = load_memory(args.path)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
