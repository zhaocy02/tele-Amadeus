# 08. 实施蓝图

Status: **current implementation baseline + restrained future blueprint**  
Updated: **2026-09-04**

项目已经越过早期 MVP。当前重点不是继续增加基础架构，而是维护已经落地的 Character Runtime，并用真实 production evidence 驱动小步改进。

## 1. 技术栈原则

继续保持简单：

```text
Python 3.11+
asyncio-based Telegram runtime
SQLite
Pydantic / typed schemas
provider-neutral Responses-compatible LLM layer
systemd user service
plain Git + focused PR workflow
```

当前不需要为了功能增长引入 Redis、Kafka、向量数据库、多 Agent framework、service mesh 或复杂 orchestration framework。

## 2. 当前模块职责

```text
amadeus_bot/
  app.py                    composition root
  config.py                 validated runtime config

  telegram/                 adapter/router/delivery/control
  runtime/                  coordinator/preferences/provider runtime
  character/                Persona/Policy/Context/Generator/State/Retrospective/Autonomy/Canon
  memory/                   structured memory/retrieval/archivist
  llm/                      concrete LLM adapter + provider registry/routing
  tools/                    WebSearchProvider + dispatcher
  tests/
```

真正重要的是职责边界，不是目录名称本身。

## 3. Character architecture is now complete at the core level

最开始规划的三路 Character 架构：

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

现在所有主要组件均已实现并接入 production：

```text
Corpus               DONE  1367 raw -> 922 enriched
Persona Distill      DONE  offline + human review
Persona Core         DONE  kurisu-v2.1.0
Behavior RAG         DONE  source-aware v2 / production ON
Evaluation           DONE  fixed cases + paired-policy OFF/ON harness
Character Context    DONE  Canon/Memory/State authority separated
Character Generator  DONE  production path
Character State      DONE  persistent subjective state + retrospective
User Memory          DONE  structured memory + canonical transcript
```

详见 `docs/53-persona-canon-phase1-closeout.md`。

## 4. Provider composition

LLM boundary：

```text
Character Runtime
  -> Provider Registry
       -> CPA/Codex (default)
       -> DeepSeek (selectable)
```

Web Search boundary 独立存在：

```text
Tool Dispatcher
  -> WebSearchProvider Registry
       -> CPA hosted search (default)
       -> DeepSeek hosted search (selectable)
```

LLM 与 Web provider per-chat 独立持久化。Automatic failover 当前 OFF。

## 5. Capability-aware routing

Provider profile 描述 provider identity、text model、vision model 与 capabilities。当前 DeepSeek text/vision routing 已通过 production verification。

Character Runtime 表达 capability requirement，不要求上层手动选择 concrete vision model。

## 6. User-turn execution

```text
durable user input
-> per-chat routing
-> recent context + memory retrieval + Character State
-> hybrid Conversation Policy
-> optional direct-turn Web Search
-> source-aware Canon retrieval
-> Context Builder
-> provider-neutral Character Generator
-> selected LLM/model
-> Telegram delivery
-> transcript/telemetry
-> post-reply Archivist/State/Retrospective
```

非必要后台认知更新尽量后置，避免拉高用户可感知延迟。

## 7. Durable state and authority

需要长期持久化的边界包括：

```text
canonical transcript
structured memory + provenance
Character State
runtime preferences / gates
provider per-chat preferences
telemetry / retrospective metadata
```

Canon/Web/Vision evidence 不应仅因为进入当前 turn context 就自动升级成真实用户事实。

## 8. Persona / Canon production baseline

```text
Persona Core         kurisu-v2.1.0
Canon corpus         922 enriched examples
Behavior RAG         source-aware v2
Production Canon     ON
Canon SHA-256        289ce7b0100fd3e3892950145614c3f39321bdfb04f1afe2e0ce94aa75c9cbba
```

Retriever 默认 ordinary personality/social/science 走 Kurisu evidence；narrow digital-condition context 才 admit/prefer Amadeus delta。显式 current shared-history verification hard-zeroes Canon。

Phase 1 latency evidence显示 local Canon retrieval/context overhead 约 80-100 ms/turn，不支持现在引入 embeddings/vector DB。

## 9. Structured Memory / Archivist

Memory 类型保持语义化：episode、fact、preference、relationship、impression、self_memory、open_thread。

Archivist 可以 `NO_WRITE`。普通闲聊不应因为“记忆系统存在”而强制写入。

P7 smoke 发现 denied shared-history probe 仍可能被 Archivist 写成 `open_thread`。这不是 Canon retrieval failure，而是 memory write semantics 的后续校准点。

## 10. Production smoke isolation

首次 Canon production smoke 证明真实 Telegram test turn 会走正常 transcript/Archivist persistence。两条 synthetic memory 已清理，Character State 未受影响。

后续应实现 Issue #103：**non-persistent production smoke mode**，要求尽量走真实 Persona + Canon + provider + Telegram path，但禁止正常长期 transcript/memory/state/retrospective/autonomy side effects。

## 11. Web Search / Vision

Web Search 与 Vision 已是独立 capability boundary：

- Web Search 需要 recoverable provenance；
- Web evidence 是 ephemeral/untrusted external data；
- Vision binary/base64 不长期持久化；
- 未确认 visual inference 不自动成为用户事实。

Web Search 当前主要 follow-up 是 latency attribution，而不是正确性架构重写。

## 12. Autonomy / Spontaneity

Autonomy 与 short-horizon spontaneity 已有独立 gate、cadence/cooldown/cap 与 telemetry。

原则保持：

```text
opportunity != send
SILENT is valid
user input/control has priority
```

不要为了“更主动”简单提高发送率。

## 13. Observability

至少可追踪：prompt/schema version、selected provider/model、routing mode、retrieved memory/canon ids、policy decision、Character State version、Web Search status、latency breakdown、Archivist writes、Retrospective diff、Autonomy/Spontaneity decision。

否则无法回答“为什么这一句不像她”或“为什么这次明显变慢”。

## 14. Production deployment

当前 production 是 systemd user service + guarded deploy helper：

```bash
cd /opt/amadeus-bot
bash scripts/deploy-prod.sh
```

Process feature rollout 使用 allow-listed helper；Canon 当前通过：

```bash
bash scripts/rollout-prod-feature.sh canon on|off
```

Routine docs-only同步可在确认无 runtime reload 需要时使用 `deploy-prod.sh --skip-restart`。

## 15. 当前优先级

```text
DONE  Character Runtime foundation
DONE  Structured Memory / State / Retrospective
DONE  production cutover
DONE  Vision
DONE  Web Search v1
DONE  autonomy/spontaneity implementation slices
DONE  CPA + DeepSeek multi-provider foundation
DONE  independent Web Search provider selection
DONE  Persona/Canon Phase 1 -> kurisu-v2.1.0 + source-aware Canon ON

NEXT  production Character observation -> regression maintenance
NEXT  Issue #103 non-persistent production smoke mode
NEXT  Web Search latency attribution + optimization
LATER bounded automatic provider failover
LATER direct third-party search provider if evidence justifies it
LATER per-cognitive-module provider routing if evaluation justifies it
LATER embeddings/fine-tuning only after repeated classified evidence
```

## 16. 不要过早优化

新增基础设施前先回答：

> 这是一个被真实 observation / regression 证明的问题吗？

当前 Character 架构已经足够完整。未来的默认策略应是**校准现有边界，而不是再造一套基础架构**。
