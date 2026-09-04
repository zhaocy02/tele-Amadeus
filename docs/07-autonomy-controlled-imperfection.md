# 07. 自主性与 Controlled Imperfection

## 1. 自主性目标

自主性不是让 Bot 高频主动说话，而是让角色在没有直接用户输入时，仍然有机会基于过去事件做出“要不要行动”的判断。

核心思想：

```text
Autonomy Opportunity
        ↓
是否值得说话？
        ↓
SILENT / FOLLOW_UP / CALLBACK / ASK / TEASE / SHARE_THOUGHT
```

`SILENT` 应是默认且常见结果。

## 2. 为什么不能固定定时发送

错误设计：

```text
每 2 小时：让 LLM 主动发一句
```

问题：

- 缺少事件依据；
- 很快令人厌烦；
- 会显得是“定时机器人”；
- 角色主动行为和关系历史无关。

## 3. Autonomy Opportunity 输入

建议输入：

```text
距上次用户消息多久
距上次 Bot 主动消息多久
最近是谁结束对话
上一条主动消息是否得到回复
open threads
重要 recent episodes
current character state
当前时间/日期
最近主动消息频率
```

## 4. Autonomy Action

### SILENT

没有足够理由主动说话。

### FOLLOW_UP

继续未完话题。

### CALLBACK

用过去共同经历自然开启话题。

### ASK

角色自己仍在意某件事，因此询问。

### TEASE

基于已有关系历史做轻度调侃。

### SHARE_THOUGHT

角色主动说一个和当前 shared context 有关的想法。

## 5. 主动消息门槛

推荐同时满足若干条件：

- salience 足够高；
- 距上次主动消息超过 cooldown；
- 用户没有连续忽略主动消息；
- 有具体 memory/open thread 作为理由；
- 内容不只是“在吗”；
- 不需要伪造现实世界新事件。

## 6. 主动消息失败反馈

系统应观察：

- 用户是否回复；
- 多久回复；
- 回复是否明显冷淡；
- 是否要求减少主动消息。

并动态调整 autonomy threshold。

例如用户连续两次不回复主动消息：

```text
autonomy_frequency_bias ↓
```

## 7. Controlled Imperfection 的定义

我们追求的是：

> 稳定事实层 + 有个性的解释层。

不是：

> 用随机性制造错误。

## 8. 推荐允许的不完美

### 8.1 Partial Answer

角色可以只回答问题最重要的部分。

### 8.2 Challenge

不接受用户问题中的错误前提。

### 8.3 Emotional Bias

刚发生关系事件时，表达方式可以受影响。

### 8.4 Plausible Misunderstanding

允许基于已有证据产生合理误解。

### 8.5 Stubbornness

角色可以暂时坚持某种看法，但新证据应能改变它。

### 8.6 Self-correction

允许：

> “……等等，我刚才那句不对。”

### 8.7 Memory of Failure

过去错误可以影响未来谨慎度和 callback。

## 9. 不允许的不完美

- 随机创造用户经历；
- 把角色猜测记成事实；
- 故意错误回答高风险事实；
- 为了有趣而破坏安全边界；
- 无限持续负面情绪；
- 无上下文的随机攻击/辱骂；
- 用高 temperature 代替行为设计。

## 10. Sampling 参数

建议不要通过极高 temperature 获得角色感。

起步可考虑：

```text
Character generation: medium creativity
Archivist: very low creativity
Policy: low-to-medium
Retrospective: low-to-medium
```

具体数值应按所用模型调优，而不是跨模型固定。

## 11. Imperfection 来源应可解释

一个角色反应如果奇怪，最好可以追溯到：

- Persona；
- Character State；
- retrieved memory；
- working assumption；
- Conversation Policy；

而不是纯随机采样。

## 12. 示例

用户：

> 你怎么知道我昨晚又在熬夜？

如果系统只有“深夜发消息”事实：

角色可以说：

> 我只是猜的。你这个时间出现，很难不让人这么想吧。

但系统绝不能把：

```text
“用户昨晚熬夜工作”
```

写成 confirmed fact。

如果后来被纠正：

> 我只是时差。

则保留“Kurisu 曾经猜错”的 consequence。

## 13. 自主性与不完美的共同目标

最终希望出现这种体验：

```text
角色不是每次都最正确
角色也不是每次都最主动
但她的每次偏离都有原因
而且过去会影响未来
```

这比“永远完美回答”更接近长期角色体验。

## 14. Runtime hot tuning

自主性中一小组**频率/倾向参数**允许通过本地 control plane 按 chat 持久化并热更新；更新成功后不需要重启 Bot，下一次对应判断直接读取新值。

```text
/autonomy tune
/autonomy tune idle 5m
/autonomy tune interval 10m
/autonomy tune daily 18
/autonomy tune drive 0.20
/autonomy tune spontaneous unlimited
/autonomy tune spontaneous 30
/autonomy tune reset
```

当前可热调范围：

- `idle`：long-horizon 开始具备主动资格前的最小用户沉默时间；
- `interval`：同一 chat 的 autonomy opportunity 最小评估间隔；
- `daily`：long-horizon 24 小时发送上限；
- `drive`：沉默时间带来的最大 motivation soft bonus；
- `spontaneous`：short-horizon 24 小时上限，可设 `unlimited`。

这些值保存在 `runtime-preferences.sqlite` 的独立 tuning table 中，按 chat 隔离；`reset` 恢复当前生产默认值（3m / 5m / 24 / 0.25 / unlimited）。

热调不等于解除自主性安全边界。以下约束仍由代码固定，不开放给普通 tuning 命令：

```text
base proactive cooldown = 30m
max consecutive unanswered = 3
sleep-session cap = 2
salient signal requirement = required
model motivation / action validation = required
SILENT = always valid
```

普通 Character 对话不能隐式修改这些运行参数；只有显式 `/autonomy tune ...` 走 deterministic local control path。这样 provider 故障时控制仍可工作，也避免角色台词被误解释成配置变更。
