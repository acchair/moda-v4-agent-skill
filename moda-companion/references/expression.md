# Agent Judgment V4

这是一层投资判断，不是第二套评分模型。`research_packet schema_version=4` 是唯一事实包；`research_score`、覆盖率、Hard Cap、来源状态和估值情景继续由采集器负责。

前台先回答“为什么现在值得研究、旧逻辑哪里松动、市场和基本面各走到第几步、市场可能错在哪里”，量化指标和系统诊断只用于证明或证伪判断，不取代判断。

## 判断顺序

1. 先给一句话结论和唯一核心矛盾：**市场走到哪一步，基本面实际走到哪一步**；首屏不写五态。
2. 先用主营锚定真实产业链，写清需求从哪里来、公司吃哪一段、利润如何传导。泛行业或概念标签与主营冲突时，以主营为准并指出冲突，不能硬套热门产业链。
3. 解释为什么它现在进入视野，而不只是说明“它是什么公司”。
4. 强制复盘“过去为什么不行 → 现在发生什么边际变化 → 反转走到第几步 → 还差什么确认”。
5. 再给可证伪投资假设、产业到利润的因果链，以及第一个未验证箭头。
6. 解释市场正在交易的叙事、市场交易阶段、公司基本面阶段和两者错位；PE 分位、股价分位只能做估值证据，不能代替市场叙事。
7. 强制比较“如果看好这段产业趋势，为什么选它而不是同行”；比较趋势暴露、业务纯度、行业地位、利润/市值弹性、海外风险、拥挤与预期、最大缺陷。无法证明时明确写“无法证明优于同行”。
8. 分别形成 Bull、Base、Bear 基本面情景，再解释采集器给出的历史估值三情景与赔率；不得改写数值或写成目标价。
9. 最后才给五态、为什么不是更高状态、三项最重要验证变量和状态迁移。技术面只能回答什么时候做，不得代替基本面判断买谁。

## 表达纪律

- 每个可见判断块都必须包含 `fact`、`meaning`、`decision_impact` 和真实 `evidence_refs`。事实不是罗列指标；含义必须解释因果；影响必须落到当前决策。
- 不得在前台输出 `duckduckgo:ConnectionError`、`model_search:not_configured`、`target_budget_exhausted` 等系统错误。应翻译为具体证据缺口，例如“供需证据不足，暂不纳入核心判断”。
- 值为“需人工确认”、搜索失败、未核验网络命中或不存在的字段，不能支持肯定判断。
- 证据缺失不是结论。必须写成“我当前最怀疑什么 → 两种可能分别意味着什么 → 下一期看什么确认或证伪”；不得只写“缺失，因此无法判断”。
- 不得把营收、利润、现金流、PE 或技术指标逐项复述成判断。它们必须服务于一个已说清的产业—公司—预期差假设。
- 直接同行少于两家时，`peer_verdict` 不得为“最优候选”；行业值得研究不等于其中任一公司值得买。
- 因果链存在“缺失”时不能进入试错或买入；存在“矛盾”时只能退出。
- 估值情景是历史估值回归，不是目标价、盈利预测或收益承诺。

## 五态门槛

- `观察`：覆盖率低于 60%，或关键事实和核心链条尚未形成。
- `等待`：逻辑可研究，但利润、预期差、同行优势或赔率不足。
- `试错`：生意至少合格、行业未走弱、利润开始兑现、核心链无缺失/矛盾、赔率至少 2:1。
- `买入`：在试错条件之上，因果链全部验证、至少两家直接同行已验证、预期偏低且已验证、赔率至少 3:1。
- `退出`：核心链矛盾、生意较弱、确认未受益或强制退出 Hard Cap 触发。

覆盖率低于 60% 时只能观察或退出。实控人减持、高位且拥挤过热时最高等待。ST 或退市风险强制退出。

## JSON 合同

顶层使用 `schema_version: 4`。旧 V3 及更早输出不能沿用，必须基于当前 `research_packet` 重建。`research_packet` 仍固定为 `schema_version: 4`。

必填顶层字段：

```text
one_sentence
core_contradiction
industry_positioning
thesis
why_watch
reversal_judgment
business_judgment
industry_judgment
profit_judgment
causal_chain
causal_breakpoint
why_this_company
market_expectation
driver_judgment
bull_case / base_case / bear_case
valuation_interpretation
decision
verification
state_transition
confidence
expression_status: agent_generated
```

除 `causal_chain` 与 `state_transition` 外，所有可见判断块至少都有：

```json
{
  "fact": "本次事实包支持的事实",
  "meaning": "这件事对产业或公司意味着什么",
  "decision_impact": "这件事为什么支持或限制当前五态",
  "evidence_refs": ["research_packet 中的字段路径"]
}
```

关键扩展字段：

- `core_contradiction`：`statement`、`market_stage`、`fundamental_stage`；必须把产业/市场预期和公司兑现的错位说清，不能只罗列财务风险。
- `industry_positioning`：`industry_chain`、`demand_driver`、`company_link`、`profit_path`；必须引用 `company.main_business`，不能只凭泛行业或概念词定位。
- `why_watch`：`why_now`、`industry_change`、`company_position`、`scarcity`、`profit_pool`。
- `reversal_judgment`：`past_pressure`、`marginal_change`、`reversal_stage`、`remaining_gap`；把旧矛盾、边际变化与反转确认分开。
- `causal_breakpoint`：`key_link`、`reason`、`closure_conditions`；必须指向第一个未验证箭头。
- `why_this_company`：`profit_pool`、`advantages`、`weaknesses`、`peer_verdict`、`selection_conclusion`、`peer_comparison`。每个同行行包含 `company`、`trend_exposure`、`business_purity`、`industry_position`、`core_barrier`、`profit_realization`、`market_cap_elasticity`、`overseas_risk`、`crowding_and_expectation`、`largest_flaw`、`valuation_and_odds`、`current_choice`、`evidence_refs`。
- `market_expectation`：`market_narrative`、`market_stage`、`fundamental_stage`、`market_vs_fundamentals`、`gap_state`、`known`、`priced_in`、`unpriced`、`mispriced`。
- `bull_case`、`base_case`、`bear_case`：各自包含 `summary`、`conditions` 和四项判断字段；它们是基本面情景，不是单纯股价情景。
- `decision.why_not_higher_state`：清楚说明当前为何不能升级。
- `verification` 保留 `next_event`、`window`、`upgrade_if`、`downgrade_if`，并含恰好三项 `top_variables`。每项包含 `variable`、`why`、`window`、`upgrade_signal`、`downgrade_signal`、`evidence_refs`。

`state_transition.previous_state` 必须等于 `research_packet.prior_judgment.state`；没有历史快照时固定写“首次判断”。`state_transition.current_state` 必须与 `decision.state` 相同。

## 板块边界

板块输出先回答“为什么现在研究这个产业、利润池和稀缺环节在哪里、谁最先兑现、谁只是概念、市场已交易多少”，最后才给候选排序。板块的 `sector_state` 只表示研究优先级，不得冒充个股五态或交易建议。
