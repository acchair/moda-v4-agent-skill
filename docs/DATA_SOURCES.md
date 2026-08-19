# moda-v4 数据源接口目录

> 扫描基线：当前工作区已接入的数据获取层（2026-08-17）。
>
> 目标：以后遇到新的选股需求，先在“选股需求反查表”定位数据，再查看对应接口、真实上游、降级链和证据边界。
> 范围：只列出 moda-v4 已接入或已预留适配的外部数据能力；不等于 AKShare、efinance 等第三方库的全部 API。这里的“全部接口”指当前代码中对外的非下划线数据获取、搜索、解析与健康检查入口；本地清洗、评分和渲染辅助函数不重复列出。

## 先看这三条

1. 接口不同不一定代表数据源独立。本目录优先标注真实上游，例如东财 F10 直连和 <code>AKShare.stock_zygc_em</code> 都是东方财富。
2. 能取到数据不代表能直接作为选股事实。A/B/C 级决定其能否确认、交叉验证或仅作线索。
3. 新需求先复用本目录已有接口；没有匹配项时，再按本文末尾模板接入，不能把搜索摘要或空结果当作事实。

---

## 1. 目录使用方式

### 1.1 状态和证据等级

| 标记 | 含义 | 可以做什么 | 不可以做什么 |
|---|---|---|---|
| A | 法定披露、交易所、政府/监管/统计等一手公开来源 | 确认公司事实、风险事件和正式披露 | 将未覆盖的时间窗推断为没有风险 |
| B | 行情、财经数据和聚合平台 | 行情/指标计算、交叉验证、辅助结构化判断 | 伪装成法定披露，或把同源封装当双重确认 |
| C | 搜索结果、论坛、社交平台、热榜和媒体线索 | 找候选事件、观察热度、提示待核验风险 | 进入确认器、覆盖结构化证据或直接计分 |
| L | 本地静态名单、缓存、历史报告 | 提高覆盖和运行效率 | 代替实时官方名单或新鲜市场数据 |
| T | 健康检查、缓存、限流、清洗等工具 | 诊断、保护和质量控制 | 产出选股证据 |

搜索后端本身不决定证据等级：DuckDuckGo Lite、DeepSeek、OpenAI、模型搜索返回的链接，仍按目标网页所属的 A/B/C 来源判定。

### 1.2 全局返回状态

| 字段 | 含义 |
|---|---|
| <code>source_chain</code> | 实际尝试过的来源、每一跳结果和错误 |
| <code>fetch_state=ok</code> | 主源成功 |
| <code>fetch_state=fallback_ok</code> | 主源失败、备用成功 |
| <code>fetch_state=empty</code> | 请求成功且确实无记录 |
| <code>fetch_state=failed</code> | 请求失败、超时或无可用来源 |
| <code>fetch_state=stale</code> | 只能读取旧缓存；只展示，不按新鲜数据使用 |

### 1.3 统一约束

- <code>tools/data_call.py</code> 负责墙钟超时、回退链和状态记录。
- 日 K 会经过 <code>tools/providers/kline_quality.py::validate_kline_frame()</code>：统一字段、剔除异常 OHLCV、检查重复/时效、标记零成交和极端复权收益。
- 质押和解禁只有在请求成功且结果确为空时才可记为 0；失败、超时、字段缺失均为未知。
- 公告必须分页到目标日期边界或末页，才允许判断该窗口未发现风险；未覆盖不是安全证明。
- 网页补缺、论坛和模型搜索必须保留真实 URL；没有 URL 的模型回答直接丢弃。
- 东方财富直连统一经 <code>tools/providers/eastmoney_transport.py</code>：跨进程串行、默认最少间隔 1.1 秒并带 0–0.15 秒抖动。AKShare 内部请求不一定经过该层，不能误写成“全站已限流”。
- 代码进入东财/腾讯直连前必须是六位当前代码；43/83/87 开头的北交所历史代码直接拒绝，避免把可能陈旧的报价当作现价。腾讯报价出现“零成交且最新价等于昨收”时标记为疑似陈旧，不进入实时行情回退结果。

---

## 2. 选股需求反查表

| 未来想解决的问题 | 首选数据 | 入口模块 / 接口 | 真实上游 | 使用边界 |
|---|---|---|---|---|
| 找近期强势、低位、回撤、量价结构 | 前复权日 K、成交量额、换手 | <code>finance_data.fetch_kline_daily</code> | TDX → BaoStock → 东财 → 新浪 → 腾讯 | 三年价格分位至少 720 根有效日 K |
| 看日内强弱、盘口、成交和主力流 | 实时报价、分时、逐笔、当日资金流 | <code>easy_tdx_provider.fetch_realtime_quote</code>、<code>fetch_tick_chart</code>、<code>fetch_transactions</code>、<code>fetch_capital_flow</code> | TDX | 盘中快照，不替代历史资金流 |
| 看近期资金持续性 | 120 日/分钟资金流、行业资金流 | <code>a_stock_data_provider.stock_fund_flow_120d</code>、<code>fund_flow_minute</code>、<code>industry_prosperity.collect_market_signal</code> | 东财；申万/东财 | 分钟流没有语义等价备用；120 日回退 TDX 当前流时须标注口径不同 |
| 看 PE/PB 是否低于历史 | 五年 PE/PB 和历史分位 | <code>finance_data.fetch_historical_valuation</code> | 东财 → 百度 | 百度只补 PE/PB，不补股本/市值 |
| 看总股本、流通股本和市值 | 股本、市值历史序列 | <code>finance_data.fetch_historical_valuation</code> | 东财 | 当前仅东财可补这组字段 |
| 看营收、利润、现金流和资产负债 | 三大报表、FCF、净债务、短债覆盖 | <code>finance_data.fetch_financial_report</code> | 新浪 → 同花顺 | easy-tdx/Sina 与 AKShare/Sina 同源；同花顺才是异源备用 |
| 交叉核验财务摘要 | ROE、毛利率、负债率、CFO/净利、股本 | <code>finance_data.fetch_baostock_financial_summary</code> | BaoStock | B 级交叉核验/缺字段补缺，不替代完整三表 |
| 判断主营、产品/地区收入、海外收入 | 主营构成、收入占比、毛利率；异源主营描述 | <code>business_data.collect_business_context</code> | 东方财富 F10 + 同花顺主营介绍；CNINFO 公司概况 | 东财分部收入是主事实；同花顺只做主营语义交叉核验，不替代收入/毛利结构 |
| 看公司身份、行业、法人和披露主营 | 公司概况、上市日、经营范围 | <code>business_data.fetch_company_profile_cninfo</code> | CNINFO/巨潮 | A 级公司档案；不是实控人、子公司或客户集中度总表 |
| 找直接同行或同一申万行业成分 | 申万成分、主营相似度、产业链位置；行业横截面排名 | <code>finance_data.fetch_company_and_peers</code>、<code>collect_direct_peers</code>、<code>fetch_industry_peer_snapshot</code>、<code>sector_screening.collect_peer_scale_snapshots</code> | TDX + 申万宏源研究 + 东财同行比较 | 东财规模接口只返目标排名；增长/杜邦/估值只返小样本，均不能跳过主营/产业链核验 |
| 看公告、减持、回购、股权激励、扩产、订单、审计风险 | 公告标题、日期、PDF URL、年报正文 | <code>announcements.fetch_announcements</code>、<code>extract_latest_annual_report</code> | CNINFO/巨潮 | 公告页数/日期覆盖不足时不反推无风险 |
| 看互动易中的订单、产能、客户问答 | 互动易问答 | <code>announcements.fetch_irm_qa</code> | CNINFO 互动易 | 公司问答是公开线索，不替代法定披露 |
| 看股东户数、前十大股东、基金持仓 | 户数变化、前十、基金持仓比例 | <code>market_events.collect</code>、<code>fetch_top_holders</code>、<code>fetch_fund_holding</code> | 东财 / 新浪 | 前十股东的直连与 AKShare 备用同为新浪 |
| 看北向持股和 20 日变化 | 港交所通道持股 | <code>market_events.fetch_northbound_holding</code> | 东财 | 适用于持股记录，不等于已停止的北向盘中实时数据 |
| 看质押、解禁、两融、大宗、龙虎榜、分红 | 对应事件明细 | <code>market_events.collect</code>、<code>a_stock_data_provider</code> 对应接口 | 东财 / 新浪 / CNINFO | 详见第 5 节逐项表，失败必须保留未知 |
| 看研报覆盖和标题线索 | 研报标题、日期、PDF 链接 | <code>a_stock_data_provider.research_reports</code> | 东财研报 | 默认回看 730 日；标题只能作为线索 |
| 找概念题材、所属板块、板块成分 | 概念板块、TDX 板块、申万成分 | <code>concept_blocks</code>、<code>fetch_belong_boards</code>、<code>fetch_board_members</code> | 东财 / TDX / 申万宏源研究 | 概念归属不能单独证明主营受益 |
| 看行业拥挤是否过热 | 申万二级拥挤度或成交活跃代理 | <code>congestion.collect</code> | 乐咕 → 申万宏源研究二级指数代理 | 代理不是原指标；过期数据不计分、不触发 Hard Cap |
| 看行业财务景气和边际改善 | 行业营收、利润、ROE 中位数 | <code>industry_prosperity.collect</code> | 乐咕 | 当前主要是申万一级中位数，不能冒充完整二级景气 |
| 看行业相对收益、成交和资金流 | 申万指数、沪深 300、行业资金流 | <code>industry_prosperity.collect_market_signal</code> | 申万宏源研究 / 新浪 / 东财 | 只验证市场定价，不替代基本面 |
| 看商品涨价、基差、库存、供需方向 | 现货、基差、库存 | <code>scoring.supply_demand.collect</code> | AKShare 接入的商品数据 | 至少两类独立证据同向才确认供需方向 |
| 看宏观 LPR、PMI、PPI 与政策 | 宏观指标、政策标题 | <code>macro_policy.collect</code> | 当前 AKShare 宏观实现为东财聚合；gov.cn | 宏观为背景；单条政策标题不能直接改评分 |
| 看市场人气 | 个股人气排名、排名变化 | <code>popularity.collect</code>、<code>hot_rank</code> | 东财 | 人气不是基本面证据 |
| 看社交热度、传播和异常推广 | 六平台热榜、主题、传播速度 | <code>social_sentiment.collect</code> | 微博、知乎、百度、抖音、头条、B 站 | 只能证明可见度，不能证明公司事实 |
| 看个股讨论情绪 | 雪球、股吧帖子与互动 | <code>stock_discussion.collect</code> | 雪球 / 东财股吧 → 搜索补缺 | 双源且至少 10 条才算样本充分；搜索结果只作线索 |
| 看财经快讯情绪 | 金十、东财、同花顺快讯 | <code>news_sentiment.collect</code> | 三个快讯站点 | 舆情信号，不替代公告或财报 |
| 补产业趋势、国产替代、供需、风险等缺口 | 真实网页正文和 URL | <code>web_research.collect</code> | 搜索栈返回的目标站点 | 必须来源匹配、正文可读、对象/判断词命中 |
| 做板块研究和候选池 | 板块六断面、主题候选、广搜审计 | <code>web_research.collect_sector_evidence</code>、<code>sector_broad_research.collect_sector_broad_evidence</code>、<code>sector_screening.resolve_sector_universe</code> | 搜索栈 + 东财行业/概念 + 申万 + 本地候选池 | 主题概念只建候选池；板块可研究不等于任一个股可以买 |
| 股票名称/代码解析 | 代码、简称 | <code>stock_resolver.resolve_stock_input</code> | 本地索引 → efinance → CNINFO → 本地分类表 | 有歧义会报错，禁止猜测 |
| 判断接口能否使用 | 连通性与依赖状态 | <code>source_health.collect</code>、<code>search_stack.check_stack</code> | 各源健康探针 | 诊断工具，不是数据源 |

---

## 3. 行情、报价、技术与板块接口

### 3.1 正常流水线的行情链

| 数据 | 生产链 | 关键输出 | 证据级别 / 限制 |
|---|---|---|---|
| 共享日 K | 当日持久缓存 → easy-tdx/TDX → BaoStock | OHLC、成交量/额、换手、涨跌幅 | 缓存至少 60 根、最新数据不超过 14 天；全失败才读旧缓存并标 stale |
| 财务/技术模块日 K | 共享缓存 → TDX → BaoStock → AKShare/东财 → AKShare/新浪 → 腾讯 | 标准化日 K | BaoStock 为 B 级备用；腾讯为最后备用 |
| 实时报价 | TDX → efinance → 腾讯 | 现价、涨跌、换手、量比、PE/PB、市值、成交额等 | 非交易时段可能空；不把旧盘中价当实时价 |
| 技术指标 | 基于上述日 K 本地计算 | MA、OBV、BIAS、MACD、BOLL、ATR、DMI、RSI、WR、日线分型/笔/中枢 | AlphaSorosAnalyzer 是计算器，不是新数据源；部分公式为工程近似 |

### 3.2.1 东方财富调用治理与 a-stock-data 参考

本项目参考了 [simonlin1212/a-stock-data](https://github.com/simonlin1212/a-stock-data) 的“直连上游、来源优先级、代码标准化、限流与陈旧报价保护”思路，但**没有把它作为生产依赖，也没有把其封装接口当作独立数据源**。

| 可借鉴点 | moda-v4 落地 | 边界 |
|---|---|---|
| 东财是易限流的末级来源 | <code>eastmoney_transport</code> 为当前直连的 F10、DataCenter、个股人气、东财快讯/股吧和 <code>a_stock_data_provider</code> 共享跨进程节流时钟 | AKShare 内部东财调用不保证经过此层；仍应优先 TDX、BaoStock、CNINFO、同花顺等更适合该字段的上游 |
| 代码须显式规范化 | 东财/腾讯适配器接受 SH/SZ/BJ 前后缀并归一为六位代码；拒绝历史北交所代码 | 名称解析仍须经过本地索引或 CNINFO 等身份核验，不能仅凭代码样式推断公司 |
| 报价要防陈旧 | 腾讯末级报价校验返回代码，并将零成交且平盘的结果标为疑似陈旧 | 非交易时段的正常静态报价不是“实时”；行情失败必须保留失败状态 |
| 聚合库不是独立来源 | AKShare、efinance、a-stock-data 风格适配器均只负责传输/清洗 | 评分和交叉验证按真实上游判定，不能因 Python 包不同而算多源 |

### 3.2 可直接调用的行情与板块接口

| 模块 / 函数 | 数据能力 | 真实上游 | 主备 / 注意事项 |
|---|---|---|---|
| <code>easy_tdx_provider.fetch_kline</code> | 日、周、月、季和 1/5/15/30/60 分钟 K；前/后/不复权 | 通达信行情服务器 | 底层主行情接口 |
| <code>easy_tdx_provider.fetch_kline_daily</code> | 日 K 快捷入口 | 通达信 | 正常流水线优先使用 |
| <code>easy_tdx_provider.fetch_realtime_quote</code> | 实时报价、换手、量比、估值、市值、资金字段 | 通达信 | 实时快照 |
| <code>easy_tdx_provider.fetch_tick_chart</code> | 分时图数据 | 通达信 | 盘中数据 |
| <code>easy_tdx_provider.fetch_transactions</code> | 逐笔成交，默认最多 2,000 条 | 通达信 | 盘中数据 |
| <code>easy_tdx_provider.fetch_capital_flow</code> | 当前资金流 | 通达信 | 不等价于 120 日历史资金流 |
| <code>easy_tdx_provider.fetch_company_info</code> | 证券基础信息 | 通达信 | 无内部备用 |
| <code>easy_tdx_provider.fetch_belong_boards</code> | 个股所属板块 | 通达信 | 板块候选，不等于申万行业确认 |
| <code>easy_tdx_provider.fetch_board_list</code> | 板块列表 | 通达信 | 可建立板块池 |
| <code>easy_tdx_provider.fetch_board_members</code> | 板块成分股 | 通达信 | 同行候选池 |
| <code>easy_tdx_provider.fetch_board_ranking</code> | 板块排行 | 通达信 | 板块强弱辅助 |
| <code>tencent_provider.fetch_realtime_quote</code> | 实时报价 | 腾讯 qt.gtimg.cn | 实时报价末级备用；代码不一致或疑似陈旧报价会被拒绝/标记 |
| <code>tencent_provider.fetch_kline_daily</code> | 前复权日 K | 腾讯 ifzq.gtimg.cn | 日 K 最末级备用 |
| <code>baostock_provider.fetch_kline_daily</code> | 沪深 A 股日 K，前/后/不复权 | BaoStock | B 级备用；当前不支持北交所代码 |
| <code>efinance.provider.fetch_quote_history</code> | 历史行情 | efinance 封装的行情源 | 名称/行情备用能力；不在共享日 K 主链中 |
| <code>efinance.provider.fetch_realtime_quotes</code> | 实时报价 | efinance | 实时报价第二跳 |
| <code>finance_data.fetch_kline_daily</code> | 标准化日 K | 见第 3.1 节 | 对外统一日 K 入口 |
| <code>finance_data.fetch_kline_quarterly</code> | 季度 OHLC、量额、涨跌 | 本地日 K 聚合 | 派生数据，不是外部接口 |
| <code>finance_data.fetch_spot</code> | 统一实时报价 | TDX → efinance → 腾讯 | 对外统一报价入口 |
| <code>tdx.analyzer.fetch_daily_kline</code> | 技术分析用日 K | 共享 K → TDX → 东财 → 新浪 | 单独运行且不传共享 K 时不含 BaoStock |

---

## 4. 公司、财务、估值与同行接口

| 模块 / 函数 | 数据能力与字段 | 真实上游 / 降级链 | 证据边界 |
|---|---|---|---|
| <code>finance_data.fetch_financial_report(code, report_type)</code> | 利润表 lrb、资产负债表 fzb、现金流量表 llb；最多 20 期；营收、利润、EPS、现金、应收、存货、债务、经营现金流、资本开支等 | easy-tdx/Sina → AKShare/Sina → AKShare/同花顺 | 前两跳同源；同花顺是异源备用 |
| <code>easy_tdx_provider.fetch_financial_report</code> | 新浪三张财务报表的原始获取入口 | easy-tdx SinaClient | 供上一行作为主跳使用；不与 AKShare/Sina 视为独立确认 |
| <code>finance_data.fetch_baostock_financial_summary</code> | 净利润、ROE、毛利率、资产负债率、CFO/净利、总/流通股本等摘要 | BaoStock | B 级同报告期交叉核验和有限补缺；不替代完整三表 |
| <code>finance_data.fetch_historical_valuation</code> | 五年 PE-TTM、PB、总/流通股本、总/流通市值和历史分位 | 东财 stock_value_em → 百度 stock_zh_valuation_baidu | 百度仅补 PE/PB；股本/市值缺失仍为未知 |
| <code>finance_data.fetch_company_and_peers</code> | 行业、板块、同行候选、PE/PB/EPS/市值 | TDX 所属板块 + 申万二级成分股 | 未通过申万成分与主营核验的同行仅是候选 |
| <code>finance_data.collect_direct_peers</code> | 主营相似度、产业链位置、收入/利润/现金流和估值对比 | 申万二级成分 + 主营 + 三表 | 二次加工，不是独立数据源 |
| <code>finance_data.fetch_industry_peer_snapshot</code> | 目标公司的行业规模/营收/净利排名，增长/杜邦/估值的行业均值/中值和少量样本 | AKShare 四个东财同行比较接口 | B 级轻量横截面；只用于候选排序与 V4 相对位置，不是完整同行池；预测期字段不入事实包 |
| <code>business_data.fetch_business_data</code> | 报告期、产品/行业/地区主营、收入、收入占比、毛利率、海外收入占比 | 东方财富 F10 直连 → AKShare stock_zygc_em | 两跳完全同源；不构成独立交叉验证 |
| <code>business_data.fetch_company_profile_cninfo</code> | 公司名、行业、法人、上市日、主营、经营范围 | AKShare stock_profile_cninfo → CNINFO/巨潮 | A 级公司身份事实；失败/空值不补猜 |
| <code>business_data.fetch_business_intro_ths</code> | 主营业务、产品类型/名称、经营范围 | AKShare stock_zyjs_ths → 同花顺 | B 级异源主营描述；不含收入/毛利分部 |
| <code>business_data.collect_business_context</code> | 把东财分部、CNINFO 公司档案和同花顺主营介绍写入 V4 事实包 | 上述三源并行 | 东财失败时可显示同花顺主营补缺，并明确无收入/毛利分部数据 |
| <code>business_data.build_structured</code> | 主营表转换为结构化主营/海外收入字段 | 本地转换 | 不新增外部事实 |
| <code>baostock_provider.fetch_financial_summary</code> | 财务摘要原始数据表 | BaoStock | 同上，仅 B 级备用/校验 |

### 4.1 财务派生字段

以下字段由财报/估值原始数据在本地计算，不是新接口：自由现金流、净债务、净现金率、短债覆盖、负债率、同比/环比、估值分位和历史分位数。新选股条件若依赖这些字段，优先确认原始三表与估值序列是否完整，再增加计算逻辑。

---

## 5. 公告、股东、筹码与公司事件接口

### 5.1 披露、互动易和年报

| 模块 / 函数 | 数据能力 | 真实上游 / 降级链 | 证据边界 |
|---|---|---|---|
| <code>announcements.fetch_announcements(code, name, days)</code> | 公告日期、标题、类型、公告 URL、PDF URL | easy-tdx/CNINFO → AKShare/CNINFO | 都是巨潮传输封装；每页 100 条、最多 5 页；正常流水线用 180 天 |
| <code>announcements.fetch_irm_qa</code> | 问题、回答、提问/更新时间、提问者 | AKShare/互动易 → CNINFO HTTPS → CNINFO HTTP | 同一互动易来源；可作公司答复线索，不替代法定披露 |
| <code>announcements.extract_latest_annual_report</code> | 年报 PDF 正文及当前可提取字段：控制链、境外收入、经营现金流和少量特定业务字段 | CNINFO 年报 PDF | 最多 80 页、35 万字符；不是通用年报结构化引擎 |
| <code>web_research.fetch_pdf_document</code> | 允许公网 URL 的 HTML/PDF 正文 | 目标网页 | 最多 4 次重定向；HTML 600KB、PDF 10MB、30 页、12 万字符；它是取证工具 |
| <code>announcements.fetch_pdf_document</code> | 上一行 PDF 正文能力的公告模块转发入口 | 目标网页 | 与 web_research 同一读取器，不是独立来源 |
| <code>easy_tdx_provider.fetch_announcements</code> | CNINFO 公告原始表 | easy-tdx CninfoClient | A 级披露通道 |
| <code>a_stock_data_provider.research_reports</code> | 研报标题、日期、PDF URL | 东财研报 API | 默认最多 2 页、回看 730 日；标题只作线索 |

公告文本还会本地识别减持、回购、股权激励、扩产、订单、审计、质押等事件。识别规则属于 <code>announcement_rules.py</code>，不是外部数据接口；有日期和金额/数量/产能/同比/项目动作等硬细节才可作为已确认事件。

### 5.2 市场事件逐项表

本节中的 <code>provider</code> 均指 <code>tools/providers/a_stock_data_provider.py</code>。

| 选股数据 | 主接口 | 主源 → 备用 | 主要字段 / 限制 |
|---|---|---|---|
| 东财通用报表 | <code>provider.eastmoney_datacenter</code> | 东财 DataCenter | 通用原始入口；调用方必须指定报表名、筛选与分页；全局约 0.35 秒节流 |
| 股东户数 | <code>market_events.collect</code> / <code>provider.holder_num_change</code> | 东财 RPT_HOLDERNUMLATEST → CNINFO stock_hold_num_cninfo | 截止日、当前/上期户数、变化率 |
| 未来解禁 | <code>provider.lockup_expiry</code> | 东财 RPT_LIFT_STAGE → 新浪 stock_restricted_release_queue_sina | 默认未来 180 天；解禁日、股数、比例、类型 |
| 概念板块 | <code>provider.concept_blocks</code> | 东财 → TDX 板块 | 仅赛道/题材候选，不能证明主营 |
| 股权质押 | <code>market_events.fetch_pledge</code> | 东财 RPTA_APP_ACCUMDETAILS → CNINFO stock_cg_equity_mortgage_cninfo | 质押方、比例、解押状态；空值规则见第 1.3 节 |
| 前十大股东 | <code>market_events.fetch_top_holders</code> | 新浪网页 → AKShare/Sina 同页 | 股东名、排名、持股比例；两跳同源 |
| 基金持仓 | <code>market_events.fetch_fund_holding</code> | 新浪 stock_institute_hold_detail | 最近已完成季度、机构数、持股比例及变化 |
| 沪深港通持股 | <code>market_events.fetch_northbound_holding</code> | 东财 stock_hsgt_individual_em | 日期、持股数量/市值、占 A 股比例、20 日变化 |
| 龙虎榜 | <code>provider.dragon_tiger_board</code> | 东财 DataCenter | 默认回看 180 日 |
| 融资融券 | <code>provider.margin_trading</code> | 东财 DataCenter | 两融明细 |
| 大宗交易 | <code>provider.block_trade</code> | 东财 DataCenter | 默认回看 180 日 |
| 分红送配 | <code>provider.dividend_history</code> | 东财 DataCenter | 历史分红送配 |
| 120 日资金流 | <code>provider.stock_fund_flow_120d</code> | 东财 120 日资金流 → TDX 当前资金流 | 回退时粒度不等价，必须标注 |
| 分钟资金流 | <code>provider.fund_flow_minute</code> | 东财分钟资金流 | 无语义等价备用 |
| 个股新闻 | <code>provider.stock_news</code> | AKShare/东财新闻 → 东财搜索 API | 同源回退，不构成双源确认 |
| 市场热度榜 | <code>provider.hot_rank</code> | 东财 | 不等同于个股基本面 |
| 事件汇总 | <code>provider.collect_market_events</code> / <code>market_events.collect</code> | 汇总以上结构化接口 | 面向流水线；汇总本身不新增数据源 |

---

## 6. 行业、供需、宏观与政策接口

| 模块 / 函数 | 数据能力 | 真实上游 / 降级链 | 证据边界 |
|---|---|---|---|
| <code>congestion.collect</code> | 申万二级行业拥挤度、换手率分位、成交额分位、热度和行业映射 | 乐咕申万二级拥挤度 → 申万宏源研究二级指数成交额/量滚动分位代理 | 乐咕按上海自然日缓存；代理是口径替代；过期不计分、不触发 Hard Cap |
| <code>industry_prosperity.fetch_legulegu_tables</code> | 行业营收、营收总额、营业利润、利润总额、ROE、净利润同比和边际变化中位数 | 乐咕 | B 级；当前以申万一级行业为主 |
| <code>industry_prosperity.collect_market_signal</code> | 行业 20/60 日收益、相对沪深 300、成交活跃、5 日资金流 | 申万宏源研究行业指数 + 新浪沪深 300 + 东财行业资金流 | 仅市场验证层 |
| <code>industry_prosperity.collect_supply_signal</code> | 商品/宏观报告中的供需证据 | 已生成的商品供需、宏观模块报告 | 本函数不发起新的网络采集 |
| <code>industry_prosperity.collect_web_signal</code> | 财务确认、供需先行、市场验证三层网页旁证 | 搜索栈 | 需两个独立域名；仍是未核验旁证 |
| <code>industry_prosperity.collect</code> | 行业映射、财务/供需/市场三层交叉结果 | 汇总以上数据 | 不能用概念板块填补行业财务缺口 |
| <code>scoring.supply_demand.collect(context)</code> | 商品现货、基差、库存、期货快照 | AKShare 商品现货/库存主接口 → 期货快照/库存等价接口 | 至少两类独立证据同向才确认趋紧/趋松 |
| <code>macro_policy.collect(industry)</code> | LPR、制造/非制造 PMI、PPI 及环比变化；近期行业政策标题 | AKShare 宏观接口（当前实现为东财 DataCenter 聚合） + gov.cn | 宏观数据为 B 级聚合；政策标题仅作背景，不是完整政策库 |
| <code>easy_tdx_provider.fetch_board_list/member/ranking</code> | TDX 板块、成分和排行 | 通达信 | 可作板块/同行候选 |
| <code>finance_data.fetch_company_and_peers</code> | 申万二级成分校验后的同行候选 | 申万宏源研究 index_component_sw | 仍需主营/产业链复核 |

### 6.1 AKShare 函数名与真实站点对照

| AKShare 函数 | 当前真实上游 |
|---|---|
| stock_irm_cninfo、stock_zh_a_disclosure_report_cninfo、stock_hold_num_cninfo、stock_cg_equity_mortgage_cninfo | CNINFO/巨潮 |
| stock_profile_cninfo | CNINFO/巨潮公司概况 |
| stock_zh_a_hist、stock_value_em、stock_hsgt_individual_em、stock_sector_fund_flow_rank、stock_zh_scale_comparison_em、stock_zh_growth_comparison_em、stock_zh_dupont_comparison_em、stock_zh_valuation_comparison_em | 东方财富 |
| stock_zh_a_daily、stock_financial_report_sina、stock_restricted_release_queue_sina、stock_main_stock_holder、stock_institute_hold_detail、stock_zh_index_daily | 新浪 |
| stock_zyjs_ths、stock_financial_benefit_new_ths、stock_financial_cash_new_ths、stock_financial_debt_new_ths | 同花顺 |
| stock_zh_valuation_baidu | 百度 |
| sw_index_first_info、sw_index_second_info | 乐咕乐股 |
| index_realtime_sw、index_hist_sw、index_component_sw | 申万宏源研究 |
| macro_china_lpr、macro_china_pmi、macro_china_ppi | 当前 AKShare 版本实际使用东财 DataCenter 聚合 |

### 6.2 AKShare 中高价值候选复核（2026-08-17）

本表专门对应“公司画像、同行、主营、研发、股东、风险和兑现链”的选股需求。状态为“已进入 V4”表示已写进采集报告并暴露给 Agent Judgment V4；不等于自动加分或直接触发 Hard Cap。

| 需求 | AKShare 接口 / 实际上游 | 状态 | V4 使用边界 |
|---|---|---|---|
| 行业内规模、营收、盈利位置 | <code>stock_zh_scale_comparison_em</code> / 东财 HSF10 | 已进入 V4 | 只返回目标公司一行及行业排名，优先用排名，不是同行名单 |
| 行业增长、质量、估值横截面 | <code>stock_zh_growth_comparison_em</code>、<code>stock_zh_dupont_comparison_em</code>、<code>stock_zh_valuation_comparison_em</code> / 东财 HSF10 | 已进入 V4 | 返回行业均值/中值、目标和少量排序样本；只作轻量候选/相对位置。<code>25E–27E</code>预测字段不进入事实包；估值接口字段变化会单项降级 |
| 公司身份与披露主营 | <code>stock_profile_cninfo</code> / CNINFO | 已进入 V4 | 公司名、行业、法人、上市日、主营/范围可引用；不是实控人、客户或子公司总表 |
| 东财分部收入的异源主营核验 | <code>stock_zyjs_ths</code> / 同花顺 | 已进入 V4 | 主营/产品/范围仅做语义交叉核验；东财 F10 仍是收入占比和毛利率主事实 |
| 研发费用、财务质量和员工规模 | <code>stock_financial_benefit_new_ths</code>、<code>stock_financial_abstract_new_ths</code>、<code>stock_financial_analysis_indicator_em</code> / 同花顺、东财 | 待正式接入 | 有研发费用、周转/偿债比率、部分员工字段；必须统一单位、报告期、单季/累计口径并与三表对齐 |
| 董监高/股东增减持 | <code>stock_share_hold_change_sse/szse/bse</code>、<code>stock_management_change_ths</code>、<code>stock_shareholder_change_ths</code> / 交易所、同花顺 | 待正式接入 | 交易所优先、同花顺补充；未命中绝不解释为无减持或无治理风险 |
| 前十大股东/股东户数变化 | <code>stock_gdfx_top_10_em</code>、<code>stock_gdfx_free_top_10_em</code>、<code>stock_zh_a_gdhs_detail_em</code> / 东财 | 部分已有等价链 | 当前主链已有新浪/东财；若接入仅作同源结构化补充，不可重复计证据 |
| 重大合同、子公司兑现线索 | <code>stock_zdhtmx_em</code> / 东财 | 待正式接入 | 可提供签署主体、关联关系、对手方、金额和收入占比；能验证一条合同链，不构成完整子公司版图或客户集中度 |
| 股本结构与变动原因 | <code>stock_zh_a_gbjg_em</code> / 东财 | 待正式接入 | 可补股本变化明细；现有股本历史已覆盖常用总/流通股本，需先校验单位和报告期 |
| 质押/解禁明细 | <code>stock_gpzy_individual_pledge_ratio_detail_em</code>、<code>stock_restricted_release_queue_em</code> / 东财 | 维持现有链 | 当前质押/解禁已有东财与 CNINFO/新浪链；本轮质押明细接口不稳定，不新增依赖 |
| 担保、诉讼等披露风险 | <code>stock_cg_guarantee_cninfo</code>、<code>stock_cg_lawsuit_cninfo</code> / CNINFO | 待正式接入 | 全市场汇总接口，必须按日期缓存后再过滤；不能逐股高频全量拉取 |

当前版本中没有稳定、按公司返回的 AKShare 结构化接口：完整子公司/参股公司财务表、前五大客户及集中度、关联交易明细总表、完整现任董监高履历、研发人员数。它们仍需年报/公告解析或独立数据源，不能因为有相近接口就写成已覆盖。

板块轻筛的正常 CLI 会对主营/生存初筛后的最多 30 家候选抓取 <code>stock_zh_scale_comparison_em</code>，只作完全相同轻筛条件下的末级排序，并在表格显示营收/净利/市值行业排名。可用 <code>--no-peer-snapshot</code> 关闭；它不跑完整个股流水线，也不改变研究分、Hard Cap 或五态。

---

## 7. 人气、社交、新闻与搜索接口

| 模块 / 函数 | 数据能力 | 真实上游 / 降级链 | 证据边界 |
|---|---|---|---|
| <code>popularity.collect</code> | 个股人气排名、总样本数、归一化热度、排名变化、榜单时间 | 东财 stockrank/getCurrentLatest，经统一东财节流 | 没有语义等价备用；人气只用于预期/拥挤观察，不等于基本面 |
| <code>social_sentiment.collect</code> | 热榜排名、个股命中、跨平台主题、传播速度、推广/谣言词 | 微博、知乎、百度、抖音、头条、B 站公开热榜 | 5 分钟缓存、7 天传播历史；只证明可见度和异常传播风险 |
| <code>stock_discussion.collect</code> | 雪球/股吧帖子、作者、互动、情绪、样本状态 | 雪球搜索接口 + 东财股吧（经统一节流）→ 搜索栈 | 双源且至少 10 条才充分；单源/少样本降级；搜索只作未核验线索 |
| <code>news_sentiment.collect</code> | 与公司别名匹配的财经快讯和词典情绪 | 金十、东财快讯（经统一节流）、同花顺快讯 | 三源并发、10 分钟缓存；只作舆情辅助 |
| <code>social_sentiment._collect_news</code> | 结构化快讯为空时的公司新闻候选 URL | <code>news_sentiment.collect</code> → 搜索栈补缺 | 搜索候选明确标“未核验”，不改变结构化新闻情绪/计数，也不直接计分 |
| <code>web_research.collect</code> | F1–F5 缺口的查询词、URL、正文、来源角色和核验结果 | DuckDuckGo Lite → DeepSeek Web Search → OpenAI Web Search → 带引用模型搜索 | 对全部未解决 F1–F5 先做首轮；单目标最多 3 个查询、75 秒总预算；F6 不走网页补分 |
| <code>web_research.collect_sector_evidence</code> | 行业趋势、供需、利润池、稀缺环节、利润兑现、市场定价六断面 | 同一搜索栈；有板块轻筛结果时复用 AKShare 板块实体与 F10 主营 | 45 秒总预算；只产出可追溯证据状态 |
| <code>sector_broad_research.collect_sector_broad_evidence</code> | 板块实体解析、动态查询计划、宽搜 URL 库、正文与计数审计 | 先复用 AKShare/东财行业或概念成分股与 F10 主营，再走 DuckDuckGo Lite / 可选 Brave → DeepSeek / OpenAI Web Search | 默认最多 100 个去重 URL、读取前 100 个正文；达到多类候选来源覆盖可提前结束；只形成待核验材料，不作板块结论 |
| <code>sector_search_planner.OVERSEAS_INTELLIGENCE_SOURCE_CATALOG</code> + <code>overseas_event_radar</code> | 海外隔夜增量扫描、事件分级与“海外事件→A股待核验链” | 按板块仅选择 4 个监管/一手/专业/财经站点，再由同一搜索栈取得 URL 和正文 | 不是逐站数据接口或全站扫描；只有正文匹配的事件才展示，且不得直接写成 A股受益、订单或利润事实 |
| <code>sector_screening.collect_sector_market_snapshot</code> | 板块现价、60 日区间、资金流、成分股涨跌广度、领涨领跌、TDX 板块排行 | AKShare/东方财富 + efinance 批量行情 + easy-tdx | 市场状态层；聚合源不作为独立交叉验证，不参与公司排序或五态 |
| <code>search_stack.check_stack</code> | DuckDuckGo Lite 和联网模型配置可用性 | 公共探针 / 仅配置检查 | T 级诊断，不产出研究证据 |

### 7.1 网页搜索的硬门槛

- 模型搜索支持 OpenAI Responses web_search，或通过 MODA_MODEL_SEARCH_URL 接入带 URL 的 DeepSeek/其他搜索网关；普通 Chat API 的无引用文本不能使用。
- 所有网页候选必须通过：来源策略匹配、正文读取成功、对象匹配、判断词匹配。
- 卡位/国产替代等关键链路还要求公司法定披露和产业权威来源双侧、不同域名确认。
- 搜索失败、未匹配、候选未核验是三种不同状态，均不能被解读成正面或负面公司事实。
- 板块或题材广搜必须先做实体解析：通过 AKShare/东方财富行业板块、概念板块和申万回退确定板块名称、类型、成分股与覆盖状态；有 baseline 轻筛时继续复用代表公司 F10 主营、业务分部和产业链位置。解析失败要显示为覆盖失败，不能直接按用户字面词臆测产业链。
- 实体解析后动态拆成“定义边界、市场规模、产业链、技术路线、供需、竞争/国产化、利润池、公司兑现、市场定价、反证”查询；再按主营关键词选择软件、材料、设备、医药、消费或通用业务模型的指标。陌生板块走同一通用拆分，不默认套用英伟达、高通等固定公司域名。
- 板块广搜按“板块专属权威来源、实体校准、政策统计、法定披露、技术标准、市场事实、补充媒体”排队；论坛、社交和搜索摘要不占确认优先级。即使开放查询先凑够 URL 数，也必须完成最低查询数和至少三个研究维度后才能提前停止。广搜报告必须同时展示 AKShare 实体解析结果、查询维度覆盖、原始结果数、去重 URL、正文尝试/可读数和可用证据数。
- 若需要稳定完成 100+ URL 的广搜，应配置 <code>BRAVE_SEARCH_API_KEY</code>（分页搜索，密钥只放本机环境）；未配置时系统仅用公开后端，并保留“目标未达”的真实计数，不应把 15 条等稀疏结果说成 100 条。

### 7.1.1 海外增量到 A 股的固定手法

- 板块/概念完成本地实体解析后，先执行一个按行业选源的“海外增量雷达”，再进入行业结构和 A 股公司兑现检索。它扫描的不是普通海外新闻，而是 III 期/关键临床读出、监管审批、并购授权、订单采购、业绩指引/资本开支、技术验证/融资及股价异动原因。
- 事件优先级固定为：III 期/关键临床读出（P1）→ 监管审批重大节点（P2）→ 大额并购或授权（P3）→ 订单、业绩或资本开支（P4）→ 技术验证或融资（P5）→ 股价异动/普通新闻（P6）。这只是海外事件的研究优先级，不是 A 股交易优先级。
- 每条正文核验事件必须标为：情绪催化、产业催化、订单催化或利润催化。订单/利润催化只获得“优先验证 A 股业绩桥”的资格；仍需 F10 同维度收入暴露、订单/产能、收入/毛利/现金流和同行比较，才可进入候选排序或深研。
- 输出的因果链只能写成“海外事件 → 改变的产业变量 → 待核验的 A 股供给环节”。禁止由“美股公司上涨”跳到“中国版公司受益”，也禁止把概念名称、搜索标题或同源转载写成收入和利润。

### 7.1.2 全球情报源库（按需选择，不全量扫站）

| 场景 | 优先监管/一手层 | 行业/财经发现层 |
|---|---|---|
| 宏观金融 | Federal Reserve、FRED、BEA、BLS | Reuters、Bloomberg、FT、WSJ、CNBC、Nikkei |
| 生命科学 | FDA、ClinicalTrials.gov、SEC | Fierce Biotech、Endpoints、BioPharma Dive、Reuters |
| 半导体/AI | SEC、公司 IR/财报 | TrendForce、SemiAnalysis、Digitimes、Reuters |
| 能源/金属/化工 | EIA、SEC | S&P Global、Argus、Fastmarkets、Reuters |
| 军工航天 | U.S. DoD、SEC | Defense News、Breaking Defense、Reuters |
| 航运物流 | MARAD、SEC | Lloyd's List、Splash247、Reuters |
| 农业食品 | USDA、FAO、SEC | Agricensus、Reuters |

目录只登记发现入口；付费墙、无法读取正文、对象不匹配或只是搜索摘要，都要在审计中保留为覆盖/候选状态，不能补成事实。

### 7.2 板块专属来源不是自动事实

- EDA 已登记 SEMI/ESD Alliance、新华网、中证鹏元、中商产业研究、交易所/CNINFO 和上市公司年报等搜索入口；半导体材料已登记 SEMI、中国石化联合会及细分材料研究站点。登记只提高发现优先级，不绕过正文和口径核验。
- <code>semi.org</code>、<code>cpcic.org</code> 按行业权威来源处理；<code>news.cn</code> 按权威财经媒体处理；<code>cspengyuan.com</code>、<code>seccw.com</code>、<code>siscmag.com</code>、<code>nepconasia.com</code>、<code>fsemi.tech</code>、<code>infoobs.com</code> 按行业研究 B 级处理。
- 市场规模必须记录年份、地区、币种、是否包含半导体 IP/服务等统计边界；国产化率、细分材料占比等缺乏统一口径的数据至少需要一个权威锚点和一个独立来源交叉核验。公司收入、产品和研发数据优先回到 CNINFO/交易所法定年报，媒体转载只作定位和交叉检查。

---

## 8. 名称解析、板块候选和本地参考数据

| 模块 / 函数 | 能力 | 数据来源 | 限制 |
|---|---|---|---|
| <code>stock_resolver.resolve_stock_input(value, name)</code> | 代码/简称解析，返回代码和名称 | 本地名称索引 → efinance.search_stock → CNINFO 公司档案 → 本地分类表 | 名称冲突或未确认时直接报错，不猜测 |
| <code>stock_resolver.lookup_local</code> | 模糊查本地已确认名称索引 | 本地缓存 / 历史报告 | L 级索引，不代替官方状态 |
| <code>efinance.provider.search_stock</code> | 股票名称搜索 | efinance | 名称解析备用 |
| <code>sector_screening.resolve_sector_universe</code> | 板块/概念候选股票池 | 行业模式：东财行业成分股 → 申万二级成分股 → 东财概念成分股 → 本地候选池；概念模式：东财概念成分股 → 本地概念字段 | 显式“概念/题材”或已登记主题别名进入概念模式；概念模式不拿行业成分股替代，避免把行业归属当成概念受益 |
| <code>sector_screening.collect_business_snapshots</code> | 候选公司主营快照 | 复用 business_data.fetch_business_data | 东财 F10 直连按单线程执行；AKShare 备用仍为同源 |
| <code>sector_screening._concept_exposure</code> | 多业务公司的概念收入暴露分层 | 东财 F10 产品/行业主营分部 | 同一披露维度内才合计收入占比，不能跨“产品/行业/地区”相加；核心主业（≥50%）/重要业务（20%–50%）可进入概念优先深研，边际受益、收入待核验、纯题材只保留为非优先候选 |
| <code>classification_db.lookup</code> | 专精特新、行业龙头、核心供应商本地名单命中 | 本地 CSV | 静态参考表；未命中不代表不是，需权威来源补核 |

---

## 9. 预留但未接入生产流水线的接口

| 适配器 | 当前状态 | 可请求的 kind | 接入前必须确认 |
|---|---|---|---|
| <code>tools/providers/axdata_provider.py::fetch(kind, code, **kwargs)</code> | 仅预留；需 MODA_AXDATA=1 且安装 AxData，当前没有生产调用方 | finance、profit_cashflow、balance、valuation、valuation_series、valuation_band、share_capital、shareholder_changes | 真实上游、许可、Python 兼容性、字段口径、与既有来源是否同源、测试和降级位置 |

因此 AxData 不能写成当前已接入的数据源，也不能在报告中形成证据，除非完成正式接入与验证。

---

## 10. 工具性组件（不产出选股证据）

| 组件 | 职责 |
|---|---|
| <code>tools/data_call.py</code> | 墙钟超时、回退链、状态记录 |
| <code>tools/daily_cache.py</code> | 上海自然日缓存、原子写入、锁和失败降级 |
| <code>tools/providers/kline_quality.py</code> | K 线统一、质量/新鲜度校验 |
| <code>tools/providers/eastmoney_transport.py</code> | 东方财富直连的跨进程节流、基础请求头与主机校验 |
| <code>tools/akshare/anti_rate_limit.py</code>、<code>tools/data_patch.py</code> | 请求限流、站点请求头和可选代理补丁 |
| <code>tools/source_health.py</code> | 核心、社交、快讯来源的健康检查 |
| <code>tools/search_stack.py</code> | 搜索后端健康检查与本地 MCP 启动 |
| <code>tools/scoring/announcement_rules.py</code> | 公告文本事件分类 |
| <code>tools/scoring/search_rules.py</code> | 搜索缺口和来源策略 |
| <code>tools/scoring/evidence.py</code>、<code>model.py</code>、<code>grader.py</code>、<code>thesis.py</code> | 证据合并、评分、校验和报告渲染 |

常用诊断命令：

<pre><code>python tools/source_health.py --group all
python tools/source_health.py --group social --json
python tools/search_stack.py check</code></pre>

健康检查只告诉我们当前能不能连，不能证明某条业务事实成立。

---

## 11. 同源关系：不能当成独立确认

| 看上去是两条链 | 实际关系 |
|---|---|
| 东方财富 F10 直连 vs AKShare.stock_zygc_em | 同源：东方财富 F10 |
| easy-tdx/CNINFO 公告 vs AKShare/CNINFO 公告 | 同源：CNINFO/巨潮，只是传输封装不同 |
| AKShare 互动易 vs CNINFO HTTPS/HTTP 直连 | 同源：CNINFO 互动易 |
| 新浪前十大股东直连 vs AKShare.stock_main_stock_holder | 同源：新浪同页 |
| easy-tdx/Sina 财报 vs AKShare/Sina 财报 | 同源：新浪 |
| sw_index_first_info / sw_index_second_info vs 乐咕行业页 | 同源：乐咕乐股 |
| AKShare 宏观函数的重试链 | 当前实现同源：东财 DataCenter 聚合 |
| 东财个股新闻 vs 东财搜索 API 回退 | 同源：东方财富 |

真正的独立交叉验证应来自不同的原始披露主体或不同数据提供方，例如新浪三表与同花顺三表、东财估值与百度估值、TDX 行情与 BaoStock 行情。

---

## 12. 新选股需求的接入流程

### 12.1 先判断现有数据是否已覆盖

1. 在第 2 节按问题找到首选数据和接口。
2. 查第 3–9 节确认真实上游、字段、窗口和证据等级。
3. 若已有数据只是口径不同，优先新增本地计算或展示字段，不重复接一个同源接口。
4. 若确实缺数据，先确定它服务于事实确认、交叉验证、行情计算还是线索发现。

### 12.2 新接口登记模板

新增接口时，请在本文件增加一行，并至少填写：

| 字段 | 必填内容 |
|---|---|
| 选股问题 | 例如找行业产能利用率拐点 |
| 模块 / 函数 | 实际调用入口和参数 |
| 真实上游 | 原始披露主体或网站，不只写 SDK 名称 |
| 主备顺序 | 主源、备用、超时和空值规则 |
| 关键输出 | 字段、单位、时间频率、历史窗口 |
| 证据等级 | A/B/C/L/T，以及能否进入评分 |
| 同源关系 | 与现有接口是否同源 |
| 新鲜度与缓存 | 更新频率、过期阈值、缓存位置 |
| 不能证明什么 | 防止误用的边界 |
| 验证 | 接口测试、异常/空值测试和来源状态测试 |

### 12.3 目前明确没有统一结构化接口的需求

以下需求不能因为当前表里没有就由搜索摘要或猜测填补：产业渗透率、CR3、行业整体扩产周期、产能利用率、完整行业资本开支、私域讨论热度、北向盘中实时流、所有年报字段的通用结构化抽取。需要时应接入明确的原始/授权数据源，并按上表登记。

---

## 13. 模块与报告标记对照

| 数据模块 | 主要报告标记 / 输出目录 |
|---|---|
| 财务、估值、日 K、同行 | moda_metrics / knowledge/research/finance_data/；含 <code>industry_peer_snapshot</code> |
| 主营构成 | moda_business / knowledge/research/business_data/；含 <code>company_profile</code>、<code>business_intro_ths</code>、<code>business_crosscheck</code> |
| 公告、互动易、年报 | moda_announcements / knowledge/research/announcements/ |
| 股东、解禁、质押、北向、基金 | moda_market_events / knowledge/research/market_events/ |
| 人气 | moda_popularity / knowledge/research/popularity/ |
| 社交热榜与内嵌快讯/讨论 | moda_social_sentiment / knowledge/research/social_sentiment/ |
| 单独运行的个股讨论 | moda_stock_discussion / knowledge/research/stock_discussion/ |
| 行业拥挤度 | moda_congestion / knowledge/research/congestion/ |
| 商品供需 | moda_supply_demand / knowledge/research/supply_demand/ |
| 宏观和政策 | moda_macro_policy / knowledge/research/macro_policy/ |
| 行业景气 | moda_industry_prosperity / knowledge/research/industry_prosperity/ |
| 网页补缺 | moda_web_research / knowledge/research/web_research/ |
| 技术派生指标 | moda_technical / knowledge/research/tdx_analysis/ |

<code>source_chain</code>、<code>fetch_state</code>、源数据日期和缓存状态应随报告一起复核。出现搜索失败、候选未核验、需人工确认时，优先看它们属于哪一种状态，再决定是否补数据；它们不是公司正负面事实。
