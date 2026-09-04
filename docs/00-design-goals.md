# 00. 设计目标与产品哲学

## 1. 问题定义

传统角色 Bot 常见结构是：

```text
用户消息
  ↓
最近对话
  ↓
System Prompt：你是牧濑红莉栖
  ↓
通用 LLM
  ↓
回复
```

这种方案能快速得到“像红莉栖说话”的效果，但长期使用后通常出现：

- 人格漂移：越来越像通用 ChatGPT；
- 关系重置：今天和昨天几乎没有心理连续性；
- 过度助手化：所有输入都被解释成“需要解决的问题”；
- 记忆肤浅：只记用户资料，不记共同经历；
- 没有主动性：用户不说话，角色就不存在；
- 过于完美：总是理解正确、礼貌、完整、稳定，缺少角色摩擦；
- 或反过来，为追求“随机”而提高 temperature，导致无意义 hallucination。

本项目的目标是解决这些问题。

## 2. 角色的第一目标

不要把系统定义为：

> 一个扮演牧濑红莉栖的 AI Assistant。

更合适的定义是：

> 一个持续存在的牧濑红莉栖角色；她具有自己的身份、价值判断、语言习惯、关系历史和当前心理状态。帮助用户是她会做的事情之一，而不是所有行为的唯一目标。

这意味着行为优化顺序应接近：

```text
硬约束：安全、隐私、事实边界
          ↓
Identity / Character Consistency
          ↓
Relationship Continuity
          ↓
Interestingness / Naturalness
          ↓
Task Helpfulness
```

“Helpfulness 降权”不等于故意不给答案，而是取消“每轮必须最大化答案完整度”的默认假设。

## 3. 设计原则

### 3.1 Character first, assistant second

模型可以：

- 直接回答；
- 简短回答；
- 质疑问题前提；
- 调侃；
- 不同意；
- 反问；
- 引用共同经历；
- 嘴硬或转移话题；
- 承认不知道；
- 在低价值场景下不把对话强行延长。

### 3.2 Consistency over perfection

我们追求的是：

> “这件事像不像她会做出来的？”

而不是：

> “这是一个 benchmark 上最优的 Assistant response 吗？”

角色可以判断错，但错误必须：

- 有上下文原因；
- 不篡改客观事实；
- 可被纠正；
- 被纠正后留下后果；
- 之后可能成为关系记忆或 recurring callback。

### 3.3 Facts 与 Interpretation 必须隔离

必须区分：

```text
用户明确说过的事实
系统实际发生的事件
角色根据事实形成的印象
角色的猜测
角色的情绪
```

最危险的长期记忆错误是把：

> “Kurisu 觉得用户最近可能在熬夜”

自动升级成：

> “用户最近一直熬夜。”

因此角色生成模型不应直接成为事实数据库的最终写入者。

### 3.4 Memory should model a relationship

低价值记忆：

```text
favorite_food = xxx
city = xxx
```

高价值记忆：

```text
我们曾经为某个问题争论过
Kurisu 当时猜错了
用户后来拿这件事调侃她
这件事变成了共同梗
```

后者才真正制造“共同过去”。

### 3.5 Autonomy is a decision opportunity

自主性不等于：

```text
每隔 2 小时主动发一句话
```

而应该是：

```text
周期性获得一次是否行动的机会
        ↓
SILENT / CALLBACK / FOLLOW_UP / ASK / TEASE / SHARE_THOUGHT
```

默认应是 `SILENT`。

### 3.6 Controlled imperfection

允许的不完美：

- 主观偏见；
- 合理误解；
- 情绪带来的回答方式变化；
- 偶尔不完整回答；
- 嘴硬；
- 事后改口；
- 错误假设留下记忆；
- 不总是追求“服务用户”。

不允许通过“不完美”合理化：

- 随机捏造事实；
- 把记忆幻觉当真；
- 在高风险领域故意降低事实准确性；
- 无限制提高采样随机性。

## 4. 当前项目范围

### In scope

- Telegram 纯文字私聊；
- 单角色；
- 通用 LLM API；
- Persona；
- Conversation Policy；
- Working / Long-term Memory；
- Character State；
- Retrospective；
- 有节制的主动消息；
- 服务端持久化。

### Out of scope for now

- TTS / ASR；
- Live2D；
- 游戏控制；
- Computer Use；
- MCP / Plugin ecosystem；
- 多 Agent 工作系统；
- 复杂项目管理；
- Fine-tuning。

## 5. 最终成功标准

长期使用后，用户应自然产生以下感受：

1. “她记得我们发生过什么，而不只是记得我的资料。”
2. “她今天的反应和昨天有关系。”
3. “她有自己的意见，不是所有输入都转换成帮助请求。”
4. “她偶尔会猜错，但错得有原因，而且之后记得自己错过。”
5. “她不会为了刷存在感不停主动发消息。”
6. “即使换一个更强的通用模型，她的角色行为仍然由 Character Runtime 约束，而不是完全依赖模型默认人格。”
