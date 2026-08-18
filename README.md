# 🧭 moda-v4

![moda-v4](./ChatGPT%20Image%202026%E5%B9%B47%E6%9C%8825%E6%97%A5%2020_24_27.png)

面向 A 股研究的逻辑优先 Skill，支持可证伪假设、数据来源标注、保守硬约束和固定格式输出。

> 🧠 逻辑优先 · 🔎 来源可追溯 · 🧩 缺失数据不猜测 · 🛡️ Hard Cap 风险控制

## 🚀 快速安装

将下面的提示词交给Agent：

```text
请将 https://github.com/acchair/moda-v4-agent-skill 安装为单入口“莫大 Agent”：克隆仓库、安装 requirements.txt，然后运行 moda-companion/install.py 对应平台安装器。只暴露 moda-companion；moda-v4 采集器和版本更新器放在内部，不要安装成独立用户 Skill。
```

也可以手动安装：

```bash
git clone https://github.com/acchair/moda-v4-agent-skill.git
cd moda-v4-agent-skill
python -m pip install -r requirements.txt
python moda-companion/install.py codex --moda-root .
```

## 📈 使用方法

直接向 Agent 提供股票名称或六位股票代码，例如：

```text
$moda-companion 中国平安
莫大 Agent，看看 601318
```

Agent 会先确认股票代码，建立最小事实包和 Logic Case，再按逻辑缺口定向补证：

```bash
python moda-companion/scripts/analyze_logic.py 000001 --kind stock --phase baseline
```

只有逻辑成立且确需正式判断时，才进入完整证据流水线和 Judgment V4。运行数据与逻辑档案保存在 `knowledge/research/`。

重点报告：

```text
knowledge/research/finance_data/000001.md
knowledge/research/tdx_analysis/000001.md
knowledge/research/announcements/000001.md
knowledge/research/scoring/000001.md
```

## 💡 设计理念

以可追溯证据、可证伪因果链和保守风险约束为核心，让研究事实、投资判断与不确定性保持一致。完整原则见 [设计理念文档](./docs/DESIGN_PRINCIPLES.md)。

### 🧱 六层评分框架

| 因子 | 满分 | 子项 | 当前标准 |
|---|---:|---:|---|
| F1 产业趋势与资本开支 | 30 | 5 | 大时代赛道、上游/卖铲子、供需失衡、卡脖子/国产替代、资本开支浪潮；赛道须由申万行业映射与主营共同验证。 |
| F2 股东与筹码 | 15 | 5 | 第一大股东增减持、Top1 持股比例、股东户数趋势、前十大股东质量、质押/解禁风险。 |
| F3 生存能力与龙头 | 20 | 5 | 产业背景、龙头/核心供应商、财务安全、退市/审计/商誉风险、专精特新/单项冠军。 |
| F4 利润兑现路径 | 15 | 4 | 主营匹配产业链、利润分配位置、出口/海外收入、订单/产能兑现。 |
| F5 低位与困境反转 | 10 | 5 | 价格分位、PE/PB 相对位置、行业冰点/市场冷落、业绩拐点、预期差；低位提供赔率，不单独等于反转。 |
| F6 修正项 | 10 | 4 | 技术结构 4 分、机构方向 2 分、情绪/拥挤度 2 分、风口催化 2 分；已计入总分，不重复加分，也不使用网页补分。 |
| **合计** | **100** | **28** | **F1-F4 合计 80 分，F5 10 分，F6 10 分；研究分仅在已知证据范围内归一化。** |

研究分只用于比较已知证据，不映射交易评级。Agent Judgment V4 先解释“市场走到哪一步、基本面走到哪一步”、主营锚定的产业链、旧逻辑的边际变化与预期差，再使用 `观察 / 等待 / 试错 / 买入 / 退出` 五态；覆盖率低于 `60%` 时最高只能观察。

Hard Cap：

- ST 或退市风险：强制 `退出`。
- 控股股东或实控人减持：最高 `等待`。
- 三年价格分位 `>80%` 且新鲜有效的市场拥挤度 `>=80%`：最高 `等待`。

搜索结果按来源分级：巨潮资讯、沪深北交易所等法定信息披露正文可作为高确信度证据；雪球、东方财富、大智慧等金融论坛只用于收集线索，不参与确认或计分。

搜索补缺默认按 `SearXNG → DuckDuckGo MCP → DuckDuckGo HTML/Lite → 带引用模型搜索` 回退。首轮覆盖所有 F1-F5 缺口，并按公司、产业、产业链、利润兑现、市场预期选择来源类型；只有正文可读、对象匹配且来源合理的结果才成为未核验线索。模型搜索可使用 OpenAI Responses `web_search`，或通过 `MODA_MODEL_SEARCH_URL` 接入返回 URL 的 DeepSeek/其他搜索网关；普通 Chat API 生成的无引用文本不会进入证据。配置示例见 `.env.example`，真实 API Key 只放本地 `.env`，不要提交仓库。

### 🧾 输出格式

最终分析固定按以下顺序输出：

1. Agent Judgment V4：莫大判断、一句话结论、核心矛盾、产业链定位、为什么现在看、过去为何受压及边际变化、市场叙事/错位、同行选择、因果断点、Bull/Base/Bear、三情景赔率、五态与三项验证
2. 研究分、覆盖率与技术信号
3. easy-tdx 技术指标与缠论日线结构
4. 六层图形概览
5. 六层评分卡及 F1-F6 逐项诊断
6. 舆情、社交热榜与异常推广风险
7. Hard Cap 检查
8. 机构方法交叉验证
9. 睡得着检查
10. 动态纠错触发器
11. 数据覆盖、待确认项与免责声明

每个关键判断都标注实际数据来源。报告缺失、接口失败或无法交叉验证的内容统一标记为 `需人工确认`，不会自动转为负面结论；报告另外显示未知可得分上限、已确认扣分和当前五态。

### 🧠 莫大 Agent

仓库内置受莫大公开投资方法与表达方式启发的研究与陪伴型 Agent。它不是莫大本人，也不代表其当前观点。`moda-companion` 是唯一用户入口：内部 moda-v4 采集 `research_packet schema_version=4`，Agent 再形成 V4 投资判断：产业—公司—预期差假设、旧矛盾的边际变化、同行选择、因果断点、Bull/Base/Bear、历史估值赔率、五态和可证伪验证。研究分、覆盖率和 Hard Cap 保持可审计。

板块问题使用独立的事实包和候选比较：产业趋势、供需、利润池、稀缺环节、利润兑现、概念与已计价，最后才输出证据排序。板块状态仅表示“值得研究 / 等待验证 / 暂不优先”，不等同个股五态或交易建议。

```powershell
python moda-companion/install.py codex
```

安装器会预置公开的莫大选股方法记忆，包括判断顺序、五类机会、五问过滤器、证据纪律、五态与 Hard Cap；重复安装不会覆盖用户已有记忆。安装后可用于行业、投资方法、AI/科技话题和日常陪伴，不保存账户、成本、持仓、联系方式、Cookie、密码、Token 或 API Key。

### 📤 导出结果

需要保存完整答复时，将最终内容写入 UTF-8 文本并执行：

```powershell
python tools/export_skill_output.py --stock 000001 --name 平安银行 --input final.md
```

默认导出到 `knowledge/output/`。可通过环境变量 `MODA_OUTPUT_DIR` 更改目录。

## 🌐 平台兼容

| 平台 | 使用方式 | 状态 |
|---|---|---|
| Codex | `python moda-companion/install.py codex --moda-root .` | 推荐 |
| Claude Code | `python moda-companion/install.py claude --moda-root .` | 支持 |
| OpenMinis | `python3 moda-companion/install.py openminis --moda-root .` | 支持 |
| 其他支持 `SKILL.md` 的 Agent | 只暴露 `moda-companion`，将 moda-v4 作为内部运行时 | 需适配 |

### 📱 手机端

[![OpenMinis](https://openminis.app/icon-dark.png)](https://openminis.app/)

访问 [OpenMinis](https://openminis.app/) 下载手机端 Agent，即可安装并使用本 Skill。移动端不依赖 PowerShell、Windows 路径或本机浏览器登录状态；缺少本地搜索服务时自动降级到公共搜索，并保留来源状态。

OpenMinis 安装命令会自动备份原 `SOUL.md`、启用莫大 SOUL，并在 `/var/minis/memory/moda-companion-memory.json` 预置公开方法记忆，不需要安装后再手动启动人格。

Windows 使用 `python` 也可以。Apple/macOS 和 OpenMinis 使用 `python3`；Apple 移动端应将仓库放入 Agent 的工作区，由 Agent 的 Linux 沙箱执行。所有报告仍写入 `knowledge/research/`，不依赖 Windows 专用路径。

## 🗒️ 更新日志

完整变更记录见 [CHANGELOG.md](./CHANGELOG.md)。当前版本重点是逻辑优先主流程、Judgment V4、证据边界、板块轻筛和跨平台降级链。

## 🔒 隐私与安全

仓库不包含浏览器登录状态、Cookie、本机日志或历史分析报告。可选代理凭据只从环境变量读取，禁止写入代码、报告或提交记录。

## ❤️ 支持项目

如果这个项目对选股研究有帮助，可以前往[雪球主页](https://xueqiu.com/u/1500823973?scene=1036&share_uid=1500823973&share_type=weixin&data_type=link&data_model=utl&fix_uid=1500823973)支持作者。

[![支持作者](./_2026-07-31_000022_473.png)](https://xueqiu.com/u/1500823973?scene=1036&share_uid=1500823973&share_type=weixin&data_type=link&data_model=utl&fix_uid=1500823973)

## ⚠️ 免责声明

本项目仅供研究与学习，不构成任何投资建议。
