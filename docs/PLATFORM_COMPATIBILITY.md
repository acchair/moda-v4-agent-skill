# 平台兼容说明

## 支持范围

| 环境 | 入口 | 说明 |
|---|---|---|
| Windows | `python tools/run_pipeline.py` | PowerShell 仅作为可选启动方式 |
| macOS | `python3 tools/run_pipeline.py` | 使用 POSIX 路径和 Python 标准库 |
| Linux | `python3 tools/run_pipeline.py` | 推荐的无桌面运行环境 |
| OpenMinis / Apple 移动端 | Agent 工作区中的 `python3` | 使用 OpenMinis Linux 沙箱；不依赖 PowerShell、浏览器状态或 Windows 用户目录 |

## 搜索回退

搜索后端按以下顺序尝试：

1. SearXNG
2. DuckDuckGo MCP
3. 公共 DuckDuckGo HTML

本地服务通过 `.env` 配置。公共回退默认开启，可设置 `MODA_PUBLIC_SEARCH=off` 关闭。公共搜索结果只作为 `网络命中（未核验）`，不会覆盖结构化财务、公告和行情数据。

## 移动端限制

- 不要求 PowerShell、`Get-NetTCPConnection` 或 `Start-Process`。
- 不要求本机浏览器登录状态或 Cookie。
- 数据请求失败时保留 `需人工确认`，不把失败转换为安全或利好。
- 适合在 OpenMinis 中输出 Markdown 报告；完整流水线可能受移动网络、后台执行时间和第三方接口限流影响。

## 300085 日志对应修复

- `social_sentiment`：每个平台请求 5 秒，总采集预算 35 秒；单个平台卡住不会拖住整个模块。
- `web_research`：没有本地 SearXNG 或 DDG MCP 时使用公共搜索回退。
- 行业景气和拥挤度：增加“软件服务、行业应用软件、计算机应用”等常见行业别名映射，减少跨数据源行业名称不一致造成的空结果。

## iSH HTTPS 兼容

- 新浪财报 HTTPS 出现 `UNEXPECTED_EOF` 或拒绝连接时，自动回退到同花顺 HTTPS 财报接口。
- 互动易优先使用 HTTPS；两条 HTTPS 路径都失败后，只读公开问答可回退到 HTTP，不携带 Cookie、Token 或登录信息。
- 东方财富股东户数直连失败后，AKShare/CNINFO 回退会正确传入股票代码。
- 没有可靠等价源的数据仍标记为失败或 `需人工确认`，不会把网络失败当成已核验空结果。
