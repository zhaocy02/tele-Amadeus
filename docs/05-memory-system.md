# 05. 分层 Memory System

## 1. Memory 目标

长期记忆的目标不是“永远记住所有聊天内容”，而是支持：

- 关系连续性；
- 对用户偏好的稳定理解；
- 对共同经历的 callback；
- 对过去错误和纠正的记忆；
- 对未完话题的继续；
- 对角色自身行为的连续认知。

## 2. 推荐 Memory 类型

### 2.1 Working Memory

最近 15–30 轮完整对话。

用途：代词解析、连续问答、短期语境。

### 2.2 Episodic Memory

“发生过什么”。

例：

- 昨晚讨论过 Neuro-sama；
- 用户说正在设计长期记忆；
- Kurisu 因某个玩笑生气；
- 两人争论过某个模型架构。

### 2.3 Semantic Fact

相对稳定、明确的用户事实。

必须尽量来自用户明确表达，并保留 provenance。

### 2.4 Preference

长期偏好：

- 回复长度；
- 喜欢/不喜欢的话题；
- 交互方式；
- 对角色行为的偏好。

### 2.5 Relationship Event

两人共同历史中的高价值事件。

例如：

```text
Kurisu 曾错误判断 X，用户之后反复拿这件事开玩笑。
```

这是角色感最重要的 memory 类型之一。

### 2.6 Impression / Hypothesis

角色对用户形成的主观判断。

例：

> 用户似乎最近对 AI 人格连续性非常执着。

必须标记为 interpretation，而不是 fact。

### 2.7 Self Memory

角色自己做过什么：

- 曾经坚持过某个观点；
- 曾经猜错；
- 曾主动提过某件事；
- 曾给用户起某个称呼；
- 曾拒绝回答；
- 曾经道歉或嘴硬。

### 2.8 Open Thread

未来值得继续的话题：

```text
用户说“明天告诉你结果”
```

可带 due_at / expires_at。

### 2.9 Emotional Residue

不是长期人格，而是短期留下来的角色情绪痕迹。

这类内容更适合 Character State，但可以由重要 episode 重新激活。

## 3. 推荐基础字段

所有长期 memory 至少应包含：

```json
{
  "id": "mem_...",
  "type": "episode|fact|preference|relationship|impression|self_memory|open_thread",
  "content": "...",
  "confidence": 0.82,
  "salience": 0.65,
  "status": "active",
  "source_message_ids": [123, 124],
  "created_at": "...",
  "updated_at": "...",
  "last_recalled_at": null
}
```

可选字段：

```text
expires_at
supersedes_id
contradicts_id
embedding
entities
tags
```

## 4. Fact 与 Impression 分离

这是强制性原则。

错误示例：

```text
用户连续两晚很晚说话
↓
Memory: “用户经常熬夜工作”
```

正确做法：

```text
Runtime fact:
用户在两个深夜时间点发消息

Impression:
Kurisu 怀疑用户可能又在熬夜折腾项目
confidence=0.55
```

之后用户说明是时差：

```text
impression.status = rejected
```

不要删除历史；可保留“她曾经这样误判过”。

## 5. Memory Archivist

每轮对话后由低温隐藏模型判断是否写入。

建议输出：

```json
{
  "decision": "NO_WRITE|CREATE|UPDATE|REJECT|SUPERSEDE",
  "items": []
}
```

普通闲聊应大量 `NO_WRITE`。

否则系统会快速积累垃圾记忆。

## 6. 写入门槛

建议提高以下内容的 salience：

- 用户明确说“记住”；
- 长期偏好；
- 关系事件；
- 争论与纠正；
- recurring joke；
- 承诺和 open thread；
- 用户对 Bot 行为的反馈；
- 角色自己明显犯错并被指出。

降低：

- 一次性闲聊；
- 无长期意义的事实；
- 模型自己的推测；
- 重复内容；
- 已被更可靠 memory 覆盖的内容。

## 7. Retrieval 不应只看 Embedding

建议综合评分：

```text
score =
  semantic_similarity * w1
+ recency * w2
+ salience * w3
+ relationship_relevance * w4
+ open_thread_bonus * w5
+ confidence * w6
- contradiction_penalty
- stale_penalty
```

第一版甚至可以不做 embedding，先用：

- tags；
- entity/token overlap；
- 最近时间；
- salience；
- memory type；

验证架构是否有效。

## 8. Retrieval 数量

不要 top-20 全塞给模型。

推荐：

```text
0–2 semantic facts
0–2 relationship memories
0–2 impressions
0–1 self memory
0–2 open threads
```

总数通常 3–7 条足够。

## 9. Memory Correction

当用户纠正：

> 我不是因为项目熬夜，是因为时差。

系统应该：

1. 写入明确事实；
2. 找到相关 impression；
3. 将旧 impression 标为 rejected/superseded；
4. 可产生 self/relationship memory：Kurisu 上次猜错；
5. future retrieval 不再把旧猜测作为事实使用。

## 10. Forget / Expire / Decay

不是所有 memory 都永久存在。

建议：

### 永久或非常慢衰减

- 明确用户长期偏好；
- 高价值共同经历；
- 明确要求保留的信息。

### 中等衰减

- impression；
- relationship tone；
- 普通 episode。

### 快速衰减

- 临时情绪；
- “今天要做 X”；
- 未确认猜测。

## 11. Shared History 优先

对角色感而言，检索优先级通常应为：

```text
共同经历 / recurring callback
>
纯用户资料
```

因为：

> “你喜欢 X”

的角色价值往往低于：

> “上次你因为 X 和我争了半天，最后还不是证明我说对了。”

## 12. 数据库建议

MVP：SQLite 足够。

建议表：

```text
messages
memory_items
memory_sources
character_state
retrospectives
autonomy_events
```

如果后续需要并发、多用户、复杂检索，再迁移 PostgreSQL。

## 13. Memory 的最终目标

不是让 Bot 表演：

> “我记得你喜欢咖啡。”

而是让记忆自然改变行为：

```text
过去发生的事情
        ↓
改变当前 State / Policy / Retrieval
        ↓
影响现在的反应
```

最好的一类记忆甚至不需要显式说“我记得”。
