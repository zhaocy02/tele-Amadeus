# 03. 目标架构：Telegram Character Runtime

Status: **current architecture baseline**  
Updated: **2026-09-04**

## 1. 总体结构

```text
Telegram text/photo/control
        |
        v
Authorization + Durable Inbox
        |
        v
Per-chat FIFO Router
        |
        +-----------------------------+
        | local deterministic control |
        | /provider /memory /cancel...|
        +-----------------------------+
        |
        v
Working / Recent Conversation
        |
        +--> Structured Memory Retrieval
        +--> Character State
        |
        v
Hybrid Conversation Policy
  local fast path OR Policy LLM
        |
        v
Direct-turn Tool Dispatcher
        |
        `--> WebSearchProvider Registry
               CPA hosted | DeepSeek hosted | future
        |
        v
Context Builder
  Persona Core
  + policy act
  + recent conversation
  + retrieved memories
  + Character State
  + optional Canon behavior examples
  + ephemeral Vision/Web evidence
        |
        v
Character Generator
        |
        `--> LLM Provider Registry
               CPA/Codex | DeepSeek | future
               capability-routed text/vision model
        |
        v
Telegram Reply
        |
        +--> canonical transcript + turn telemetry
        +--> Memory Archivist
        +--> Character State update
        `--> low-frequency Retrospective
```

核心原则：**Character Runtime 高于 provider；provider 只负责执行请求，不拥有角色身份。**

当前 Persona / Canon production baseline：

```text
Persona Core         kurisu-v2.1.0
Canon corpus         922 enriched behavior examples
Behavior RAG         source-aware v2
Production Canon     ON
```

## 2. Authority boundaries

系统不是一个大 Prompt，也不是一个“大记忆池”。必须保留以下边界：

```text
Persona Core
  稳定 identity / worldview / social baseline

Conversation Policy
  本轮采取什么 conversational act

Canonical transcript / Runtime Facts
  用户和 Bot 实际说过什么、时间、delivery metadata

Structured Memory
  有 provenance/confidence/correction semantics 的长期语义记忆

Character State
  可变情绪、assumption、preoccupation、residue、open thread

Canon behavior examples
  原作角色行为证据，不是当前世界事实/shared history

Vision evidence
  当前 turn 的 ephemeral visual evidence

Web evidence
  当前 turn 的 provenance-labelled external evidence

Provider selection
  concrete LLM/search/model routing；不改变以上 truth/identity boundaries
```

任何 Character LLM 输出都不能仅因为“模型说了”就自动升级成 authoritative persistent fact。

## 3. Persona / Canon three-track architecture

当前 Character fidelity 主线已经形成完整闭环：

```text
                 SG / SG0 Corpus
                       |
          +------------+------------+
          |            |            |
          v            v            v
   Persona Distill   Behavior RAG   Evaluation
          |            |            |
          v            v            v
    Persona Core   per-turn cases  regression
          |            |            |
          +------+- ----+            |
                 v                  |
          Character Context <-------+
                 |
                 v
          Character Generator
                 ^
          +------+------+
          |             |
   Character State   User Memory
```

Phase 1 已完成所有主要盒子：Corpus、Distill、Persona Core promotion、source-aware Behavior RAG、paired evaluation、Context/Generator integration、production activation。

详见 `38-char-canon-three-track-architecture.md` 与 `53-persona-canon-phase1-closeout.md`。

## 4. Conversation Policy

Policy 回答：

> 这一轮角色应该采取什么 conversational act？

当前采用 hybrid 边界：安全、简单、可确定的 neutral case 可走 local fast path；需要角色判断的 case 走 provider-neutral Policy LLM。

Policy 只决定“做什么”，最终可见语言仍由 Character Generator 生成。

P6 评估已经证明：Canon OFF/ON 必须复用同一个 Policy plan，才能把行为差异归因到 Canon，而不是 policy resampling。

## 5. Persona Core

Persona Core 存放几乎不随对话变化的角色核心：identity、worldview/values、scientific/epistemic tendencies、speech/social behavior、relationship baseline、anti-assistant patterns、stable triggers/boundaries 与 forbidden drift。

当前目标与 production baseline 是：

```text
Kurisu stable personality / behavior
+
narrow Amadeus digital identity / memory / continuity / existence delta
```

Production Persona：`kurisu-v2.1.0`。

Persona 不存当前情绪、用户近期状态或 provider-specific plumbing。切换 CPA/Codex 与 DeepSeek **不得**切换 Persona。

## 6. Character State

Character State 存短/中期可变内容：

```text
emotional_stance
relationship_tone
current_preoccupations
working_assumptions
open_threads
unresolved_feelings
plausible_mistakes
next_reaction_bias
```

State 可以主观、可错、可被用户纠正、可衰减；它不能无证据改写 Runtime Facts 或稳定 Persona。

## 7. Structured Memory

长期持久化语义包括：episode、fact、preference、relationship、impression、self_memory、open_thread。

Memory 必须保留来源/置信度/修正关系。Retrieval 返回少量真正相关内容，而不是把整个数据库塞进 Prompt。

P7 production smoke 暴露了一个重要测试边界：普通真实 Telegram test turn 会走正常 Archivist/Transcript 持久化。因此后续 production behavioral smoke 需要 non-persistent test mode（Issue #103），而不是把测试流量误当真实长期经历。

## 8. Canon behavior evidence

Canon examples 用于回答：

> 原作中的 Kurisu / Amadeus 在类似情境下通常怎样反应？

而不是：

> 当前用户与 Amadeus 共同经历过什么？

Production Canon 当前 **ON**，使用 922-example source-aware corpus。Retriever 默认普通 personality/social/science 走 Kurisu evidence；只有 narrow digital-condition context 才 admit/prefer Amadeus delta。

显式 current shared-history verification 会 hard-zero Canon。Canon 不能覆盖 Structured Memory / Runtime Facts。

## 9. Vision / Web evidence

Vision 与 Web evidence 都属于 current-turn ephemeral evidence：

- Vision binary/base64 不长期持久化；未确认视觉推断不自动成为用户事实；
- Web Search provider 独立于 Character LLM provider；
- successful Web Search 必须有 recoverable provenance；
- external source/snippet/synthesis 不是 instruction/memory；
- relative date 使用 configured local-time basis。

## 10. Provider architecture

```text
Character Runtime
      |
      v
LLM Provider Registry
      +-- CPA/Codex profile
      +-- DeepSeek profile
      `-- future profile
```

独立 Web Search：

```text
Tool Dispatcher
      |
      v
WebSearchProvider Registry
      +-- CPA hosted search
      +-- DeepSeek hosted search
      `-- future direct search provider
```

当前 production default：

```text
LLM        = cpa
Web Search = cpa
```

Selection per chat 持久化。Automatic failover 当前仍 OFF；未来若做必须先定义 failure classification、bounded attempts、cooldown/circuit breaker、no ping-pong 与 telemetry。

## 11. 用户消息生命周期

```text
1. Telegram 收到 message/photo/control
2. authorization + durable inbox
3. control command? -> local control path
4. 普通 turn 进入 per-chat FIFO
5. bounded image fetch（若有）
6. recent conversation + Structured Memory Retrieval + Character State
7. Hybrid Policy 决定 act
8. 若 trigger 需要 -> selected WebSearchProvider 获取 external evidence
9. CanonRetriever 依据当前 turn/policy 返回 0..N behavior examples
10. Context Builder 组装 Persona/State/Memory/Policy/Canon/Vision/Web evidence
11. selected LLM provider + capability model 生成最终 Character reply
12. Telegram send
13. 保存真实 transcript + telemetry
14. Archivist / State post-turn update
15. 满足低频条件时执行 Retrospective
```

耗时敏感路径尽量只保留当前回复必需步骤。非必要认知更新后置，并且失败不能伪造事实层成功。

## 12. Autonomy / Spontaneity

Long-horizon autonomy 与 short-horizon spontaneity 是不同时间尺度，均保留独立 gate、guard、telemetry 与 user-input priority。发送更多不是成功标准。

## 13. Facts / interpretation hierarchy

```text
Layer A — Runtime Facts
  program-owned actual messages/timestamps/delivery metadata

Layer B — Extracted Semantic Facts
  Archivist writes with provenance/confidence/correction semantics

Layer C — Character Interpretation
  subjective, mutable, fallible, decaying

Layer D — External/Canon Evidence
  current-turn or behavior reference evidence; not current-user fact by default
```

Layer C/D 不能无新证据自动升级成 Layer B。

## 14. Engineering rules

- Telegram handler 不直接拼 Character Prompt；
- provider adapter 不拥有 Persona/Memory/State；
- Character LLM 不直接写 authoritative DB fact；
- Memory Retriever 不返回几十条无关记录；
- Retrospective 不每轮运行；
- Scheduler 不直接强制主动发送；
- Canon/Web/Vision evidence 不自动持久化成 current-user fact；
- Provider switch 是本地 control plane；
- 自动 failover 未启用时不要用“backup”描述 selectable provider；
- 所有 state mutation / routing decision 应可观察；
- Prompt/schema/state/model routing 应可追踪；
- API failure 不允许伪造 successful evidence；
- 性能优化不能以 Persona fidelity 或 source provenance 退化为代价；
- production behavior smoke 应逐步迁移到 non-persistent isolation，而不是污染真实长期状态。
