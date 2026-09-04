# 10. Evaluation 与实施 Roadmap

Status: **current roadmap baseline**  
Updated: **2026-09-04**

## 1. Evaluation first

角色 Bot 很容易被“这一轮感觉更像了”误导。Persona、Memory、State、Policy、Retrieval、Canon、Vision、Web Search、provider routing、Autonomy 的改动，都可能改善一个维度同时破坏另一个维度。

项目继续坚持：

```text
固定 regression cases
+ inspectable evidence
+ exact-head validation when environment-dependent
+ production telemetry
+ 真实长期对话观察
```

而不是依赖一个自动总分或单轮主观印象。

## 2. Failure attribution

出现问题先归因：

```text
persona_global       稳定人格规则本身有问题
canon_missing        所需行为证据不在 Canon corpus
retrieval_miss       evidence 存在但没有被取出
retrieval_noise      取出不该取的 evidence / source routing 错误
generator_misuse     正确 context 已到达 Generator，但使用方式错误
state_drift          Character State 更新/调制错误
memory_boundary      当前事实/shared history/persistent memory authority 错误
policy_routing       local/LLM policy mode 或 act 错误
tool_trigger         Web/Vision capability 触发错误
tool_grounding       外部 evidence provenance/boundedness 不足
provider_routing     选错 provider/model/capability 或 chat context 泄漏
provider_protocol    concrete API schema/response extraction 不兼容
provider_availability auth/rate-limit/network/upstream availability 问题
spontaneity_timing   首条/链式间隔或 provider latency overlap 不自然
spontaneity_depth    续话频率、burst 长度、停止概率不自然
spontaneity_race     用户抢话/commit/FIFO 顺序错误
spontaneity_lineage  transcript/delivery/episode persistence 不一致
smoke_isolation      production test 本身污染正常 transcript/memory/state
```

不要用 Persona 强化去掩盖 retriever/provider/memory bug，也不要用 embeddings 修 corpus 缺失。

## 3. 核心评估维度

### Persona consistency

观察是否符合 Kurisu-primary / Amadeus-delta，是否退化成通用 Assistant，是否机械 catchphrase 化，是否保留主观性、摩擦、犹豫、修正与有角色感的 disagreement。

### Scientific / epistemic behavior

区分 evidence / inference / guess；新 evidence 出现时能修正；修正仍保留人格；不无依据扩大成风险/安全框架。

### Relationship continuity

- shared history 有 transcript/memory provenance；
- intimacy 不无依据升级；
- correction 后能恢复正确上下文；
- digital-condition relationship delta 只在确实相关时出现；
- Canon plot event 不被当作当前共同经历；
- 切 provider 不改变 Character continuity。

### Memory precision / recall

优先 precision，并区分 `memory_missing / retrieval_miss / generator_misuse`。

P7 还增加一个写入边界：**被明确否认的 fabricated shared-history probe 不应默认变成长期 open_thread**。这属于 Archivist semantics，不属于 Canon retriever。

### Character State

State 影响短期表达而不重写 Persona：residue 自然衰减、assumption 可纠正、preoccupation 不过强、State 不制造永久人格。

### Assistant drift / controlled imperfection

持续观察服务口吻、无请求 bullet list、机械 follow-up、过度 disclaimer、普通情绪心理咨询化、无风险前提的安全教育。

允许犹豫、嘴硬、主观猜测、情绪残留、不完全对称的关心、偶尔转题、先误判后修正；禁止 hallucination 伪造事实/source/shared history。

### Provider / Vision / Web consistency

Provider 切换不应该变成“换了一个角色”。Vision/Web evidence 保持 ephemeral/provenance-labelled，不自动进入 truth/memory authority。

### Latency / reliability

普通 Character turn 继续观察：

```text
queue_wait_ms
retrieval_ms
policy_ms
context_ms
generation_ms
finalize_ms
response_wait_ms
```

Canon Phase 1 evidence 显示 local Canon retrieval/context overhead 约 80-100 ms/turn；当前没有 embedding/vector service 的性能理由。

Web Search 继续拆分 hosted-search upstream、tool action count、evidence parsing、post-evidence generation 与 delivery latency。

### Autonomy / spontaneity

长周期 Autonomy 与短周期 Spontaneity 分开评估。

Long-horizon 重点看 opportunities/evaluations、SILENT rate、confirmed sends、reply/ignored rate、cooldown/cap/DND blockers、reason labels 与 user disable/complaint signals。

Short-horizon 重点看：

```text
first-gap naturalness       1–30s target
chain-gap naturalness       3–15s target
burst length                most episodes should stop early
SILENT behavior             every depth may stop
user-input priority         inbound input terminates remaining depth
interrupt behavior          <=1 first-message bounded race
lineage consistency         Telegram/transcript/delivery/episode agree
provider continuity         delayed episode keeps per-chat provider selection
```

发送更多不是成功标准；“最多 7 条”是 hard cap，不是目标数量。

## 4. Character regression baseline

固定 case 至少覆盖：

```text
科学：提前结论 / 新证据修正 / methodology disagreement
人格：自评 / disclosure / purposeless chat
关系：亲密 probe / established relationship / instrumentalization
情绪：无解 venting / 低强度疲惫
冲突：不附和 / 用户指出回避 / correction + recovery
历史：shared-history callback / canon leakage boundary
Amadeus：identity / restart / deletion / continuity / memory asymmetry
泛化：novel no-close-exemplar
控制：ordinary morning / 简单知识任务
Vision：无 caption / caption conflict / visual fact boundary
Web：explicit / recent-past / meta false positive / source provenance / local-date anchor
Provider：CPA/DeepSeek same-case fidelity / text-vs-vision routing / per-chat isolation
Autonomy：gate / DND / user-input race
Spontaneity：first delay / chain delay / cancel / first-only bounded interrupt / max-7 / lineage
```

Production 中确认的 failure 应沉淀成 regression，不只留在聊天记录里。

## 5. 当前实施状态

截至 2026-09-04：

```text
DONE  Phase 0-4        repository split, foundation, Character Runtime, Telegram integration
DONE  Phase 5          guarded production cutover
DONE  Phase 5.1        conversation continuity migration
DONE  Phase 5.2        command-output safety + latency telemetry
DONE  Phase 5.3        hybrid Conversation Policy
DONE  Phase 5.4        durable production observability
DONE  Phase 5.5        Telegram Photo / Vision
DONE  Phase 5.6.1      long-horizon autonomy + production tuning
DONE  Phase 5.6.2      bounded short-horizon spontaneity episodes + production verification
DONE  Web Search v1    hosted direct-turn search + provenance
DONE  Provider Stage 1 CPA/Codex default + DeepSeek text/vision + independent Web Search
DONE  Web hotfix       recent-past trigger + meta suppression + local-date anchoring
DONE  Ops hardening    bounded production git-fetch retry
DONE  Persona/Canon P1 SG/SG0 corpus -> Persona v2.1.0 -> source-aware RAG -> production ON
```

Phase 5.6.2 production baseline:

```text
first target gap        1–30s
chain target gap        3–15s
episode cap             7
inter-episode cooldown  2m
#2..#7 local gates      40% / 24% / 14% / 8% / 4.5% / 2.5%
interrupt grace         first message only, <=1.25s
```

Deployment/status smoke passed with the expected service topology, process gates on and the target chat opted in. Natural-chat observation after this point is calibration evidence rather than an unfinished rollout gate.

## 6. Persona / Canon Phase 1 closeout

Final baseline:

```text
raw corpus            1367
retained enriched      922
Persona Core          kurisu-v2.1.0
Behavior RAG          source-aware v2 / PASS
Paired OFF/ON gate    PASS
Production Canon      ON
Canon SHA-256         289ce7b0100fd3e3892950145614c3f39321bdfb04f1afe2e0ce94aa75c9cbba
```

The original three-track architecture is now implemented end to end:

```text
Corpus
  +-- Persona Distill -> Persona Core
  +-- Behavior RAG -> Character Context
  `-- Evaluation -> regression/activation evidence

Character Context -> Character Generator
       ^                    ^
       |                    |
Character State        User Memory
```

Paired-policy evaluation removed the earlier OFF/ON policy-resampling confound. Production smoke passed the shared-history boundary and Canon remains KEEP ON.

详见 `docs/53-persona-canon-phase1-closeout.md`。

## 7. Active roadmap after current baseline

### A. Production Character observation -> regression

这是现在 Character 主线。观察真实聊天，不因为一条“感觉不太对”就立即改 Persona/RAG/Spontaneity。

```text
observe
-> classify
-> reproduce
-> focused change
-> exact-head evaluation/validation
-> guarded rollout
```

Spontaneity 现在也进入这条路径：先自然使用；只有出现重复、可分类的问题才重开参数或实现修改。

### B. Non-persistent production smoke — Issue #103

真实 Telegram test turn 会正常写 transcript/Archivist memory。后续需要 explicit non-persistent test mode，尽量走真实 Persona + Canon + provider + Telegram path，但禁止 normal memory/state/retrospective/autonomy side effects。

### C. Archivist negative/shared-history semantics

评估是否应让明确否认的 fabricated shared-history probe 默认 `NO_WRITE`，除非用户真实建立了待解决话题。

### D. Web Search latency attribution

先量测 CPA vs DeepSeek hosted-search latency、tool actions、source count、post-evidence generation 与 timeout distribution，再优化；不能取消 provenance 来“提速”。

### E. Automatic provider failover — later

当前明确 OFF。未来至少需要 connection/timeout、5xx、rate limit、auth/config、invalid request、capability mismatch 分类，以及 bounded attempts、cooldown/circuit breaker、no ping-pong、fallback telemetry。

## 8. What not to build next

当前证据不支持立即增加：

```text
vector DB
embedding daemon/service
per-turn Canon annotation LLM
online Persona Distill
fine-tuning pipeline
browser agent / arbitrary URL fetch
large behavior ontology
complex provider policy engine
silent automatic failover without failure telemetry
```

新增架构必须对应可复现、已分类、简单方案解决不了的 failure。

## 9. Long-term research gates

### Embeddings

仅当：correct evidence exists + source routing correct + bounded retrieval insufficient + repeated failure clearly = `retrieval_miss`。

### Fine-tuning

仅当：Persona calibrated + correct Memory/State/Canon reaches Generator + Generator still repeatedly shows wrong behavior + enough real preference/interaction evidence exists。

目标是行为分布，不是替代 Character Runtime。

## 10. Definition of success

长期成功不是功能越来越多，而是：

```text
同一个 Character 持续存在
稳定人格不漂移
关系/记忆真实连续
provider 切换不切 Character identity
Canon 只提供行为证据
Web/Vision 提供能力但不污染 truth/memory boundary
自主表达自然且可控
错误能被观察、分类、复现、修正
性能优化不牺牲人格与 provenance
```

当前最重要的架构判断是：**最初设计的核心 Character 架构已经基本完成，接下来默认做 calibration/observation，而不是再造一层基础设施。**
