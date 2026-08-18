---
name: moda-release-updater
description: moda-companion 内部使用的 moda-v4 GitHub Release 检查与升级实现。不要作为独立用户 Skill 安装；检查新版、立即升级、跳过或恢复版本统一从莫大 Agent 入口路由。
---

# Moda Companion 内部更新器

此目录保留更新实现和测试。用户可见入口只有 `moda-companion`，安装时将 `scripts/release_updater.py` 复制到其内部脚本目录。

使用 `scripts/release_updater.py` 管理 `acchair/moda-v4-agent-skill` 的正式 Release。

## 固定规则

- 会话启动检查必须在独立后台进程中运行，不阻塞当前 Agent。
- 每个本地自然日最多访问一次 GitHub Release API。
- 仅处理 GitHub `latest release`，不处理草稿和预发布版本。
- 发现新版时返回结构化的 Release 标签、发布日期和正文摘要，由宿主 Agent 提供“立即升级 / 跳过本版 / 稍后”。
- `是`：立即升级。Git 仓库存在未提交修改时停止升级，不覆盖用户文件。
- `跳过本版`：记录该 Release 标签，以后不再提示该版本。
- `否`：本次不升级；下一自然日检查到同一版本时可再次提示。
- 不输出或保存密码、Token、Cookie、密钥和其他敏感信息。

## 常用命令

```powershell
python scripts/release_updater.py status
python scripts/release_updater.py check-now --target "C:\path\to\moda-v4"
python scripts/release_updater.py upgrade-now --target "C:\path\to\moda-v4" --tag "v1.2.3"
python scripts/release_updater.py skip --tag "v1.2.3"
python scripts/release_updater.py unskip --tag "v1.2.3"
```

旧的 `install` 命令只返回统一入口提示。实际安装统一使用 `python moda-companion/install.py <platform>`；不得再创建用户级 `moda-release-updater` Skill。
