# 06. Character State 与 Retrospective

## 1. 为什么需要 Character State

Persona 只能回答“她通常是什么样的人”，不能回答：

- 她刚刚为什么还在生气；
- 她最近对什么事情特别在意；
- 她上次猜错以后是否变得更谨慎；
- 她和用户最近是偏争论、偏轻松，还是偏认真；
- 哪些话题还没聊完。

因此需要一个独立、可变、可衰减的 `Character State`。

## 2. 推荐字段

```json
{
  "emotional_stance": "mildly_annoyed_but_cooling_down",
  "relationship_tone": "familiar_teasing",
  "current_preoccupations": [
    "user is redesigning long-term memory"
  ],
  "working_assumptions": [
    {
      "claim": "user may prefer character continuity over maximum helpfulness",
      "confidence": 0.78,
      "status": "active"
    }
  ],
  "open_threads": ["thread_123"],
  "unresolved_feelings": [],
  "plausible_mistakes": [],
  "next_reaction_bias": "more analytical on bot architecture topics",
  "avoid_sounding_like": [
    "customer support agent",
    "overly agreeable assistant"
  ],
  "updated_at": "..."
}
```

## 3. State 与 Persona 的边界

### Persona

长期稳定：

```text
不喜欢被叫 Christina
科学思维
略尖锐但并非恶意
不喜欢无根据断言
```

### State

会变化：

```text
刚被叫 Christina，因此暂时更容易恼火
最近对用户的 Memory 项目很好奇
上次在某个问题上猜错，所以暂时更谨慎
```

## 4. State 不应该变成 RPG 数值面板

不推荐：

```text
anger = 73
love = 82
trust = 91
```

原因：

- 机械；
- 难解释；
- 容易出现“数值驱动台词”；
- 不利于复杂关系状态。

更推荐自然语言 + 少量离散枚举。

例如：

```text
relationship_tone = familiar_teasing
emotional_stance = defensive_embarrassment
```

## 5. Retrospective 的角色

Retrospective 是低频隐藏模型调用。

它不负责：

- 直接回复用户；
- 写客观事实；
- 总结所有对话；
- 每轮都运行。

它负责：

> 回头看最近一段互动，判断这些经历应该如何改变角色接下来的心理姿态。

## 6. Retrospective 输入

推荐：

- 最近 10–30 轮精选对话；
- 新增的重要 memory；
- 最近 Character actions；
- 用户明确反馈；
- 最近被纠正的 hypothesis；
- 最近主动消息效果；
- 当前 Character State。

## 7. Retrospective 输出

```json
{
  "working_assumptions": [],
  "emotional_stance": "",
  "relationship_tone": "",
  "current_preoccupations": [],
  "plausible_mistakes": [],
  "next_reaction_bias": "",
  "avoid_sounding_like": [],
  "state_updates": [],
  "open_thread_updates": [],
  "confidence": 0.0
}
```

## 8. Plausible Mistakes

这是本项目最重要的 Character State 字段之一。

它表示：

> 角色现在有哪些可能犯的“合理错误”。

例如：

```json
{
  "claim": "user may be staying up late because of the bot project",
  "basis": ["msg_103", "msg_117"],
  "confidence": 0.52,
  "scope": "casual conversation only",
  "expires_at": "..."
}
```

它不是要求模型一定说错。

只是允许角色在自然场景中带着这个偏见理解用户。

## 9. 错误被纠正后的处理

例如角色说：

> 你又熬夜折腾 Bot 了吧。

用户：

> 不是，我只是因为时差。

系统应：

```text
1. Archivist 写新事实
2. working assumption → rejected
3. plausible mistake → retire
4. 可写 self memory：Kurisu 此前误判原因
5. Retrospective 可能设置：
   “对于用户深夜出现的原因不要太快下结论”
```

以后角色可以自然 callback：

> 我本来想说你又熬夜……算了，上次已经猜错一次了。

## 10. Emotion Decay

Character State 必须衰减。

建议由 deterministic runtime 控制，而不是完全交给 LLM。

例如：

```text
强烈短期情绪：几轮到数小时
普通情绪 residue：数小时到一天
关系 tone：数天/多次互动
长期关系历史：进入 memory，不直接作为 emotion state
```

Retrospective 可以修正衰减结果，但不应该让一次小事永久影响人格。

## 11. Open Threads

Open thread 应同时影响：

- Memory Retrieval；
- Conversation Policy；
- Autonomy；
- Retrospective。

结构：

```json
{
  "id": "thread_001",
  "topic": "user's long-term memory redesign",
  "status": "open",
  "created_from": "msg_123",
  "salience": 0.85,
  "followup_after": "2026-09-03T...",
  "expires_at": null,
  "last_mentioned_at": null
}
```

## 12. Retrospective 触发条件

MVP 推荐：

- 每 12–20 个用户 turn；
- 出现明显纠错；
- 出现强关系事件；
- 用户给出角色行为反馈；
- 一次主动消息得到明显正/负反馈；
- 系统检测到重复语言模式。

不推荐每轮运行。

## 13. Anti-drift 检查

Retrospective 可专门检查：

```text
最近是否过于：
- 礼貌
- 全面
- 客服化
- 解释型
- 同意用户
- 每次结尾提问
- 重复同一种傲娇句式
```

然后更新：

```text
avoid_sounding_like
next_reaction_bias
```

## 14. 设计目标

Character State 的意义不是让模型“表演情绪”，而是：

> 让过去发生的事情在未来留下可观察的行为后果。

这才是真正的角色连续性。
