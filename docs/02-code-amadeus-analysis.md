# 02. Code-Amadeus/Amadeus 项目拆解

参考仓库：`Code-Amadeus/Amadeus`

本文重点不是复述全部功能，而是筛选对当前 Telegram 文字角色 Bot 有价值的机制。

## 1. 总体判断

Code-Amadeus 当前实际上存在两套成熟度不同的角色机制：

### A. Main Chat

核心仍接近：

```text
System Prompt
+ 最近若干轮对话
+ LLM
```

Main Chat 的角色 prompt 主要约束：

- 身份是牧濑红莉栖；
- 保持自然、机智、略傲娇；
- 被叫 Christina 时强烈否认；
- 维持语言与情绪标签；
- 工具/Provider 路由规则。

人格层本身并不复杂。

### B. VN Player

VN Player 是更值得借鉴的实验系统。它已经把“角色怎么理解世界”拆成多个层：

- short memory；
- scene summary；
- story summary log；
- evidence nodes；
- hypotheses；
- characters；
- relationships；
- traits observed；
- emotional readings；
- suspicion notes；
- open questions；
- retrospective bias。

这套结构比 Main Chat 更接近长期角色认知系统。

## 2. Main Chat 记忆：目前主要是 Rolling Window

`core/session_manager.py` 中 `ConversationHistory` 默认 `max_rounds=10`。

它会：

- 保存 user / assistant 对话；
- 超出窗口后只保留最后若干轮；
- 将 session 持久化为 JSON；
- 重新加载后恢复对话。

但它并不是完整长期语义记忆系统。

值得注意的是源码明确写到：当前 alpha 使用 bounded rolling window，并没有让可见回复模型在对话中做 in-band memory summary。

这意味着：

```text
session persistence ≠ semantic long-term memory
```

## 3. Roadmap 对 Memory 的判断非常值得采纳

项目 Roadmap 明确指出缺少：

- cross-session recall；
- small explicit profile；
- semantic memory；
- memory inspect / correct / forget / scope；
- persona continuity 测试；
- bad precedent propagation 防护。

尤其重要的一句设计思想是：

> 一个 generic vector store 或把不断增长的 transcript 塞进 prompt，本身并不构成真正的 memory feature。

这一点应直接作为本项目的 Memory 原则。

## 4. VN Player 的事实边界

VN Player 最值得借鉴的机制：

```text
observed_fact
candidate_fact / evidence
hypothesis / interpretation
summary
character model
```

它明确规定：

### observed_fact

由 runtime 写入，LLM 不能直接写。

意义：客观发生过什么，不能由角色模型自行决定。

### candidate_fact / evidence

必须能追溯到显示过的证据。

### hypothesis / interpretation

模型可以自由创建、修改、削弱、退休，但必须允许 confidence 和 revision。

### summary

用于压缩历史，不等同于事实证明。

### character model

单独维护人物：

```text
facts
relationships
traits_observed
suspicion_notes
emotional_readings
open_questions
```

这对 Telegram 版本非常有价值，因为我们也需要区分：

```text
用户明确说过什么
vs
Kurisu 如何理解用户
```

## 5. Revision History

VN Context Store 中 hypothesis 等条目不是简单覆盖，而是保留 `revision_history`。

这意味着模型可以：

```text
形成假设
→ 获得新证据
→ 修改假设
→ 保留过去错误版本
```

Telegram Bot 应借鉴这种思想，至少对于：

- impression；
- relationship interpretation；
- working assumption；
- uncertain preference；

保留状态变化。

不一定要永久保存全部版本，但至少需要：

```text
status = active / weakened / rejected / superseded
```

## 6. Immediate Persona 与 Reasoning Archivist 分离

VN Player 的 `immediate_prompt` 负责：

- 以 Kurisu 身份做即时反应；
- 简短；
- 可以沉默；
- 可以请求更多上下文；
- 可以更新非事实层状态。

而 `Reasoning Archivist` 明确要求：

> 不扮演 Kurisu；中立、简洁、结构化。

这个分离应直接迁移到 Telegram Bot：

```text
Character Model
负责“说话”

Memory Archivist
负责“记录”

Retrospective Model
负责“回顾与调整角色状态”
```

不要让一个模型一次完成所有事情。

## 7. Silence 是合法动作

VN Player 的 immediate decision 包含：

```text
silence
hold
speak
context_request
context_patch
```

并明确规定：

> 不需要对每一行都反应，沉默往往是正确的。

对于 Telegram 私聊，用户直接发消息时通常仍应回复，但这个思想应应用在：

- 主动消息；
- 低价值事件；
- 连续 autonomy tick；
- 用户只发无意义 reaction；
- 系统内部事件。

## 8. Retrospective Character Orientation

VN Player 的 retrospective 是最值得借鉴的模块之一。

它回顾：

- 已显示文本；
- Kurisu 最近反应；
- context patches；
- verifier feedback；

然后生成：

```text
working_assumptions
emotional_stance
uncertainty_style
plausible_mistakes
next_reaction_bias
avoid_sounding_like
```

这是一个关键设计：

> 系统不只保存“发生了什么”，还保存“这些事情现在让角色以什么姿态继续面对未来”。

这应成为 Telegram Character State 的核心来源。

## 9. Plausible Mistakes

VN Player 甚至显式允许 `plausible_mistakes`。

要求不是“随机犯错”，而是：

- 错误由已经看到的事实诱发；
- 逻辑上说得通；
- 范围有限；
- 之后可以被证据纠正。

这和本项目的 `Controlled Imperfection` 完全一致。

## 10. Attention Router

VN Player 还有一个 deterministic attention router：

它根据：

- 当前内容类型；
- 信息密度；
- 情绪强度；
- retrospective bias；
- cooldown；

决定哪些 lane 需要运行。

这给 Telegram Bot 一个重要工程启发：

> 不要每条消息都调用所有隐藏模型。

可以通过便宜规则决定：

```text
普通闲聊
→ Character + 轻量 Archivist

出现长期偏好 / 关系事件
→ Character + Archivist

发生争论 / 情绪变化 / 明显错误纠正
→ Character + Archivist + Retrospective candidate

累计 N 轮
→ Retrospective
```

这样能降低 API 成本和延迟。

## 11. 哪些部分值得直接借鉴

### 强烈建议借鉴

1. Fact / Hypothesis 分层；
2. Runtime-owned facts；
3. Character / Archivist 分离；
4. Retrospective Character Orientation；
5. `plausible_mistakes`；
6. Silence / Hold 作为合法决策；
7. Memory revision / confidence；
8. Attention routing / low-frequency reflection；
9. 不把 raw transcript 当长期 memory。

## 12. 哪些部分现在不要搬

当前 Telegram 阶段暂不需要：

- TTS / ASR；
- Emotion tag 系统；
- Live2D；
- Browser Provider；
- Work Ledger；
- AUIP；
- MCP；
- VN script matcher；
- Lookahead；
- 游戏 evidence verifier；
- 桌面 Agent；
- 多 Provider 工作执行。

这些会显著增加复杂度，却不会直接提升文字角色体验。

## 13. 结论

不要把 Code-Amadeus 当成需要 fork 的完整产品。

更合适的策略是：

```text
借它的认知架构思想
↓
重新实现一个轻量 Telegram Character Runtime
```

尤其应该把 VN Player 的：

```text
Fact Boundary
Memory Layers
Retrospective
Plausible Mistakes
Attention Routing
```

作为本项目的设计基础。
