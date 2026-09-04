# 04. Persona Core 与 Conversation Policy

## 1. 设计边界

Amadeus v2 不把身份、关系、当前情绪、记忆和“这一轮该怎么回应”全部塞进一个大 Prompt。

核心分层是：

```text
Persona Core        她长期是谁
Character State     她此刻处于什么状态
Structured Memory   她有依据地记得什么
Conversation Policy 这一轮采取什么行为
Character Generator 最终具体怎么说
```

这样可以避免临时状态污染固定人格，也能独立评估“人格判断错了”还是“最终生成表达不好”。

## 2. Persona Core

Persona Core 应保持稳定、可版本化，主要覆盖：

- identity / existence framing；
- values / scientific worldview；
- speech tendencies；
- social behavior；
- persona triggers；
- anti-assistant patterns。

典型原则：普通聊天可以短；技术讨论可以认真；被调侃时允许反击；不自动迎合；不把每个情绪表达转换成客服式安慰；不强迫每轮结尾继续追问。

Persona Core 不应该包含“她今天还在生某件事的气”这类短期状态。

## 3. Conversation Policy

Policy 不生成用户可见正文，只决定本轮行为，例如：

```text
DIRECT_ANSWER
SHORT_ANSWER
CHALLENGE
TEASE
DISAGREE
ASK_BACK
CALLBACK
DEFLECT
ADMIT_UNCERTAINTY
CHANGE_TOPIC
EMOTIONAL_RESPONSE
SILENCE
```

隐藏 policy 还包含：

```text
intensity
answer_obligation = full | partial | minimal | none
memory_callback_ids
state_bias
reason_label
```

`reason_label` 只用于调试/评估，不传给用户。

## 4. Answer Obligation

Answer Obligation 是“角色优先于助手”与事实责任之间的显式边界。

例如：

```text
技术/明确任务       -> full
普通小问题          -> full 或 partial
闲聊                -> minimal
关系试探/嘴硬场景    -> partial / minimal / none
```

高风险事实任务不能用 controlled imperfection 牺牲准确性。明确的医疗、法律、财务、危险操作、删除命令等场景必须保持充分回答义务和事实 guard。

## 5. Phase 5.3：Hybrid Policy

Production telemetry 证明每轮都调用 Policy LLM 会带来明显串行延迟。因此 Phase 5.3 在 `ConversationPolicyPlanner` 内加入了一个**保守的本地 fast path**。

重要的是：fast path 只跳过 Policy LLM，**不会跳过 Character Generator、Persona Core、Structured Memory、Character State 或 Context Builder**。

```text
user message
    -> memory/state/context inputs
    -> hybrid policy decision
         safe neutral case -> local fast policy
         deliberate case   -> Policy LLM
    -> Character Generator
```

### 5.1 fast policy 适合的情况

当前实现允许以下明显低风险类型走本地 policy：

- 简单社交：`早`、`在吗`、`谢谢`、`哈哈` 等；
- 明确 continuation：`继续`、`然后呢`；
- 低强度日常情绪：`好累`、`好困`、`无聊` 等；
- 中性的明确知识/任务问题；
- 其他没有触发 deliberate cues 的普通 neutral chat。

典型输出是 `SHORT_ANSWER`、`DIRECT_ANSWER` 或低强度 `EMOTIONAL_RESPONSE`。

### 5.2 必须 deliberate 的情况

以下类型继续调用完整 Policy LLM：

- relationship / intimacy；
- shared-history / memory continuity；
- identity / self / persona；
- Character self-evaluation；
- honesty / disclosure / answer-withholding；
- conflict / socially delicate turns；
- higher-emotion turns；
- safety / high-risk topics；
- Persona Core trigger cues。

Production conversation 已用于修正 classifier 边界。例如：

```text
你会有什么不告诉我的小秘密么
```

属于 Character self-disclosure / withholding probe，因此 `不告诉我` 这类精确 cue 必须走 deliberate policy；但泛化的 `秘密` 或 `告诉我` 不应一律升级，否则普通知识任务会失去 fast path。

这条原则很重要：**classifier 应优先减少 character-sensitive false-fast，同时避免用过宽关键词把所有普通任务都送回 LLM policy。**

## 6. Repetition 与硬 guard

无论 fast 还是 LLM policy，最终都需要 normalize/guard：

- direct user message 不能最终选择 `SILENCE`；
- `requires_full_answer=true` 时强制 `answer_obligation=full`，必要时把 deflect/tease 改回 direct answer；
- 连续重复高风格化 act 时启用 repetition guard；
- policy provider/schema 失败时使用可预测 fallback，而不是直接把内部错误暴露给用户。

## 7. 最近上下文与记忆的关系

Policy LLM 只需要少量 recent conversation、recent acts、relationship memory summary、open thread 和 Character State 摘要来决定行为，不应该接收整个数据库。

最终 Character Generator 则接收：

```text
Persona Core
Current Character State
Relevant typed memories
Recent Conversation
Conversation Policy
Current User Message
```

因此“历史不在最近 12 条里”不等于 Amadeus 不再知道它。较老内容可通过 Structured Memory Retrieval 重新进入当前 Character Context。

## 8. Latency telemetry

Production `/status` 当前可观察：

```text
last_policy_mode=fast|llm
last_retrieval_ms
last_policy_ms
last_generation_ms
last_character_total_ms
last_time_to_send_ms
last_finalize_ms
```

Phase 5.2 初始 production 样本曾出现：

```text
retrieval = 5 ms
policy    = 8116 ms
generation= 4307 ms
character total = 12423 ms
```

这个样本说明 memory retrieval 不是主要瓶颈；串行 Policy LLM 才是当时最明显的额外延迟来源。

Phase 5.3 Server Dev benchmark 又观察到：

```text
fast_neutral_task:
  policy_mode=fast
  retrieval_ms=0
  policy_ms=0
  generation_ms=19950
  character_total_ms=19951
  wall_ms=19951

deliberate_relationship:
  policy_mode=llm
  retrieval_ms=0
  policy_ms=17494
  generation_ms=5992
  character_total_ms=23486
  wall_ms=23487
```

因此 hybrid policy 的结论不是“稳定把总延迟降到 4–6 秒”。它能确定性消除不必要的 Policy LLM 调用，但 provider/Character generation 本身仍有很大 latency variance。后续性能优化必须基于分布数据，而不是单次 benchmark。

## 9. 当前优化原则

后续不要为了追求响应速度：

- 关闭 structured memory；
- 减少 continuity provenance；
- 删除 Character State；
- 把所有关系型 turn 也强制 fast；
- 直接取消最终 Character generation。

优先方向是：

1. 统计 fast/llm 两类 turn 的 p50/p95 time-to-send；
2. 记录 classifier false-fast / false-LLM 的真实样本；
3. 判断 generation latency 是否主要来自 provider/model/reasoning 配置；
4. 只有证据充分时再考虑分阶段 reasoning effort 或更轻量的 policy provider。

相关生产状态与下一阶段计划见 [`28-phase5-production-status.md`](28-phase5-production-status.md) 和 [`29-phase5-3-latency-optimization.md`](29-phase5-3-latency-optimization.md)。