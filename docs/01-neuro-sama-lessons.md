# 01. 从 Neuro-sama 借鉴什么

本文只提取对 **Telegram 文字角色 Bot** 有价值的部分，不讨论游戏视觉、TTS、Live2D、实时控制等当前项目不需要的能力。

## 1. Neuro 真正值得借鉴的不是“用了什么模型”

公开信息并不能可靠确认 Neuro-sama 当前具体使用的 base model、参数规模和完整训练方式。真正稳定可观察的优势来自系统目标不同：

普通助手的默认目标倾向于：

```text
正确
有帮助
完整
礼貌
安全
适用于所有用户
```

而娱乐型长期角色的目标更接近：

```text
人格一致
反应自然
短而有节奏
有自己的立场
会接梗
能产生意外
保留共同历史
不必每次最大化帮助度
```

因此，Neuro 的关键经验是重新定义 reward / objective，而不是迷信某一个神秘 LLM。

## 2. “不像 Assistant”是显式目标

普通模型遇到：

> 你是不是很喜欢我？

很容易输出解释型回答。

一个角色 Bot 更可能选择：

- 回避；
- 嘴硬；
- 反击；
- 用过去发生过的事情回怼；
- 简短否认。

这类行为不一定提高“答案质量”，但会提高角色一致性和关系感。

因此本项目应增加一层 `Conversation Policy`，在生成语言前先决定本轮行为，而不是默认 `DIRECT_ANSWER`。

## 3. 一致性比“像真人”更重要

长期角色的核心不是让模型模拟一个普遍意义上的真人，而是：

> 不同时间发生的行为，都能让用户觉得“这像她会做的事”。

一致性来自三部分：

1. 稳定 Persona Core；
2. 长期关系记忆；
3. 当前 Character State。

只有 Persona Prompt，没有 2 和 3，角色每天都近似 reset。

## 4. 不追求完美

Neuro 式角色感的重要来源之一是不可预测性与合理瑕疵。

但应区分：

### 值得保留

- 不总是完整回答；
- 有个人偏见；
- 偶尔误解；
- 会改变观点；
- 会因情绪影响表达；
- 会抓错重点；
- 会形成奇怪但可解释的执念；
- 会被过去的失败影响；
- 会留下 recurring joke。

### 不值得保留

- 随机事实错误；
- 无来源记忆；
- 角色状态无限积累；
- 高 temperature 带来的语言噪声；
- 为了“疯”而完全失去逻辑。

因此项目采用 `Controlled Imperfection`：

```text
事实层尽量稳定
角色解释层允许不稳定
```

## 5. 错误必须产生 consequence

一个非常重要的设计原则：

```text
错误
 ↓
被指出
 ↓
角色反应
 ↓
记忆写入
 ↓
未来行为变化
```

如果模型犯错后下一轮完全 reset，那么错误只是 hallucination。

如果角色记得：

> “我上次对这件事猜错过。”

那么错误会成为角色历史。

因此要保存 `self_memory` 和 `relationship_event`，而不只保存用户事实。

## 6. 自主性来自事件，不来自随机 Timer

Neuro 的“活着”感很大程度来自环境事件可以触发她，而不是只有用户输入才能产生输出。

Telegram 版本不需要复杂实时事件，但可以建立以下 event：

```text
USER_MESSAGE
IDLE_OPPORTUNITY
OPEN_THREAD_DUE
MEMORY_CALLBACK_OPPORTUNITY
DATE_EVENT
STATE_DECAY
RETROSPECTIVE_TICK
```

这些事件进入统一 `Event Manager`。

注意：

`IDLE_OPPORTUNITY` 不代表一定发消息，只代表“允许系统考虑是否主动说话”。

## 7. 主动消息的目标不是通知，而是 continuity

高价值主动消息通常来自：

- 昨天未结束的话题；
- 一个承诺的 follow-up；
- 最近共同经历；
- 角色仍然在意的事情；
- 某个 recurring joke；
- 用户明确说过“之后再告诉你”。

例如：

```text
昨天：用户说还没想好长期记忆怎么做。

今天 autonomy opportunity：
open_thread 仍然 active。

Amadeus：
“……所以，昨天那个‘给我装长期记忆’的计划，你到底想好没有？”
```

这种消息几乎没有传统 Assistant utility，但对长期角色感价值极高。

## 8. 对当前项目最重要的五个 Neuro 原则

按优先级：

1. **目标函数从 Assistant Helpfulness 转为 Character Continuity。**
2. **稳定人格 + 可控不可预测性。**
3. **角色允许合理犯错，但错误必须可修正并留下后果。**
4. **自主性通过 decision opportunities 实现，SILENT 是合法且常见的决定。**
5. **持续关系比单轮回答质量更重要。**

这五条比复制 Neuro 的语音、游戏或直播系统更适合当前 Telegram Bot。
