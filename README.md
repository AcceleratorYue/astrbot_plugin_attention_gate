# 消息发送门控（astrbot_plugin_attention_gate）

项目仓库：<https://github.com/AcceleratorYue/astrbot_plugin_attention_gate>

基于 **Jev（NeoHorse-Jev-4B / TypeSafe SystemOne）** 的 AstrBot 消息发送门控插件。

插件由两层组成：

```text
主动决策层：
  要不要主动接话？接哪条？引用谁？

发送门控层：
  什么时候真正发？
  同一用户连发怎么合并？
  窗口内话题变了还要不要发？
```

目标：

```text
先判断有没有发言冲动
再等用户把这一轮说完
最后只调用一次正式主模型
```

---

## 一、功能

```text
群消息 / 私聊消息
→ 主动决策层判断是否值得主动回复
→ 已决定回复后进入统一发送窗口
→ ONNX 判断句子节奏
→ Jev 判断这一轮是否说完
→ 合并同一用户补充消息和图片
→ 主动回复窗口内出现新消息时 revalidate
→ 释放原请求，交给 AstrBot 主模型管线
```

插件不生成正文、不直接发送消息，也不创建第二份 LLM 请求。  
人格、上下文、工具、长期记忆、静默规则等仍由 AstrBot 主线和其它插件处理。

---

## 二、核心能力

### 主动决策层

- **Jev 主判断**：输出意图、兴趣匹配、时机、风险、回复价值。
- **人格兴趣层（全局）**：所有群共享，模板列表维护。
- **群聊话题层（独立）**：每个群一个方案，不串群。
- **突发回复欲望层**：不要求兴趣匹配，但要求高冲动、低风险、时机合适。
- **方向/接话硬门**：指向别人、答题求助、未说完、风险过高等直接拦下。
- **无随机概率**：按阈值和规则决定 act / pass。
- **Jev 不可用直接 pass**：不降级到其他模型。

### 统一发送门控

- **普通聊天、私聊、主动回复、poke 进入真实窗口。**
- **群聊普通跟进由消息事件缓冲合并**，不要求它自己产生 LLM 请求。
- **同一用户连发合并**：follower 被 stop，root 合并文本和图片。
- **只释放原请求**：不创建新的 `request_llm`，不重复调用主模型。
- **ONNX 句尾节奏**：低分时延长等待。
- **Jev 完整性判断**：只问“这一轮是否说完”，不重复做主动门控。
- **主动回复 revalidate**：窗口内出现新消息时，重新确认是否仍适合按原目标回复。
- **共享决策窗口**：主动候选先等待/合并，再在窗口内执行主动 Jev 决策。
- **失败最终释放**：Jev/ONNX 异常不会永久卡住请求。
- **重复保护**：原请求释放后立即关闭 lane；同一发送者的主动候选不会在窗口内二次发送。

### 其它

- **强制引用目标消息**：主动回复通过 `attention_quote_target` 引用候选消息。
- **回复密度限制**：15 分钟最多 2 次主动回复。
- **回复后疲劳**：刚主动回复后提高接话阈值。
- **最小用户注意力**：同一用户近期反复触发时提高门槛。
- **未回复消息缓存**：让 Jev 看到近期没有被接上的内容。
- **模板列表配置**：兴趣、群话题、Jev 问题都可在面板逐条维护。
- **诊断日志**：记录发送窗口交接与模型返回状态，便于定位无响应。

---

## 三、安装

1. 把本目录放到：

```text
AstrBot/data/plugins/astrbot_plugin_attention_gate
```

2. 重载插件。
3. 打开插件配置，按板块填写：

```text
【基础】启用、dry-run、群白名单、Bot 称呼、其它 Bot
【Jev】API Key、模型、服务地址、状态长度
【人格与话题】人格兴趣、群话题
【主动节奏】额度、冷却、密度、上下文、疲劳
【判断阈值】兴趣/话题/时机/风险/价值硬门
【突发欲望】冲动层
【预筛】Embedding / Rerank
【问题定义】Jev 结构化问题
【发送门控】真实窗口、ONNX、Jev 完整性、revalidate
```

### 最小可用配置

```json
{
  "enable": true,
  "enabled_groups": [],
  "jev_provider_url": "https://tokenrhythm.studio/v1/decision",
  "model": "NeoHorse-Jev-4B",
  "api_key": "sk_你的TokenRhythm密钥",
  "reply_value_threshold": 1.65,
  "dry_run": false
}
```

`enabled_groups` 为空表示所有群生效。

---

## 四、发送门控配置

推荐运行配置：

```json
{
  "send_gate_enable": true,
  "send_gate_enforce": true,
  "send_gate_enforce_groups": [],
  "send_gate_enforce_private": true,
  "send_gate_dry_run": false,

  "send_gate_min_wait_seconds": 5,
  "send_gate_max_wait_seconds": 20,
  "send_gate_max_window_seconds": 20,

  "send_gate_onnx_enable": true,
  "send_gate_onnx_low_wait_seconds": 6,
  "send_gate_jev_enable": true,
  "send_gate_jev_complete_threshold": 0.65,
  "send_gate_revalidate_proactive": true,
  "send_gate_revalidate_failure_policy": "keep_with_guard"
}
```

### 字段含义

| 字段 | 含义 |
|---|---|
| `send_gate_enable` | 启用发送门控 |
| `send_gate_enforce` | 开启真实窗口；关闭时只观察 |
| `send_gate_enforce_groups` | 真实窗口生效群，空=全部 |
| `send_gate_enforce_private` | 真实窗口处理私聊 |
| `send_gate_dry_run` | 只观察，不 stop、不延迟、不改写请求 |
| `send_gate_min_wait_seconds` | 最后一条消息后的最短安静时间 |
| `send_gate_max_wait_seconds` | 从 root 到达开始的总等待上限 |
| `send_gate_max_window_seconds` | 硬窗口上限 |
| `send_gate_onnx_enable` | 启用 ONNX 句尾节奏 |
| `send_gate_onnx_low_wait_seconds` | ONNX 低分时至少等待时间 |
| `send_gate_jev_enable` | 启用 Jev 完整性判断 |
| `send_gate_jev_complete_threshold` | Jev 完整阈值 |
| `send_gate_revalidate_proactive` | 主动回复出现补充消息时重新验证 |
| `send_gate_revalidate_failure_policy` | revalidate 失败策略：`keep` / `drop` / `keep_with_guard` / `retry_once_then_drop` |

### ONNX 模型

默认使用：

```text
data/plugin_data/astrbot_plugin_attention_gate/models/normal
```

也可以通过 `send_gate_onnx_model_path` 指定其它目录。  
模型或依赖缺失时会自动降级为纯时间窗口，不会阻塞回复。

---

## 五、Jev 主动决策

插件用 Jev 的多个字段共同判断：

```text
is_mentioned
is_directed_elsewhere
has_reply_hook
is_answer_seeking
is_complete_turn
persona_fit
group_topic_fit
good_moment
risk
reply_value
continuation
impulse
```

核心规则：

```text
先看方向与硬门
再看兴趣/话题
再看时机与风险
最后看回复价值
```

只有同时满足：

```text
所有硬门通过
+ 价值达到阈值
+ 风险可接受
+ 时机合适
```

才会进入主动回复。

---

## 六、发送窗口行为

### root / follower

```text
root：第一个进入窗口的请求
follower：同一发送者在窗口内继续发来的消息
```

处理：

```text
root 在 on_llm_request 中等待
普通群聊跟进消息由群消息事件缓冲，不要求它自己产生 LLM 请求
follower 请求在 on_waiting_llm_request 中被 stop
root 合并 follower 文本和图片
root 释放原请求
```

同一个用户连续发消息时，不会产生第二个正式 LLM 请求。

### 请求类型

```text
普通群聊：真实窗口
私聊：真实窗口
主动回复：真实窗口
poke：真实窗口
群聊普通跟进：由群消息事件缓冲合并
框架 follow-up capture：若平台已消费，不会重新进入 gate
```

AstrBot 的 follow-up capture 会在 `OnWaitingLLMRequest` 之前把同发送者消息并入正在运行的 Agent 轮次。  
这类消息不会重新产生 LLM 请求，因此不会经过 send_gate；群聊中未产生独立请求的普通跟进由本插件的群消息缓冲覆盖。

### 群聊与私聊

```text
私聊：每条消息通常都会产生 LLM 请求，直接走 on_waiting 合并。
群聊：普通消息通常不产生 LLM 请求，额外由群消息事件缓冲合并。
```

群聊额外包含主动候选、主动 revalidate、poke 独立排队等逻辑。

### 共享决策窗口

主动回复采用共享决策窗口：

```text
attention 只产出候选
→ send_gate 等待/合并
→ send_gate 内调用主动 Jev
→ act 才进入模型
→ reject 直接 stop
```

### ONNX + Jev

```text
等待安静
→ ONNX 判断句子完整度
→ Jev 判断这一轮是否说完
→ 完整则释放
→ 不完整则继续等到 max_window
```

### 主动回复 revalidate

主动回复窗口内出现补充消息时调用 Jev：

```text
keep：继续按原目标回复
drop：停止原请求，不发送
keep_with_guard：保留原请求并加 keep_silent 护栏
retry_once_then_drop：裁剪 state 重试一次，仍失败再 drop
```

---

## 七、日志与诊断

主动决策日志：

```text
data/plugin_data/astrbot_plugin_attention_gate/decisions_YYYYMMDD.jsonl
```

发送门控日志：

```text
data/plugin_data/astrbot_plugin_attention_gate/send_gate_dryrun_YYYYMMDD.jsonl
```

关键字段：

```json
{
  "action": "root_enforce|group_buffered|merged_stop|real_release|revalidate_drop",
  "kind": "chat|chat_private|proactive|poke|followup",
  "window_count": 2,
  "merged_messages": 1,
  "onnx_score": 0.98,
  "jev_score": 0.99,
  "dry_run": false,
  "enforce": true
}
```

运行日志中的诊断：

```text
send_gate handoff
send_gate model response
```

用于判断请求是否真正交给模型，以及模型是否返回空回复。

---

## 八、与其它插件

- **Context Aware**：继续注入场景；发送门控不接管其上下文。
- **LivingMemory**：继续注入记忆；发送门控只合并 prompt/图片。
- **quiet_hours**：静默硬门继续生效。
- **suanle_bushuo**：`keep_silent` 仍可最终否决。
- **smart_reply_tag**：普通引用继续工作；主动引用由本插件插入。

---

## 九、已知限制

- Jev 请求失败/超时/限流时，主动决策直接 pass；
- Jev 完整性失败时最终会释放，不永久阻塞；
- `send_gate_max_wait_seconds` 与 `max_window` 同时作为总时长上限，取更小值；
- send_gate 真实窗口启用时，主动回复的 `active_reply_settle_seconds` 等待被跳过；
- AstrBot follow-up capture 在 OnWaitingLLMRequest 之前消费的消息不会重新进入 gate；
- root 在等待前一个 LLM 请求释放 session lock 时，`window_age` 可能很大，但进入 gate 后会立即释放；
- 图片只作为附件和合并内容，不做独立视觉理解；
- 重启 AstrBot 后内存缓冲和窗口状态会清空。

---

## 十、开发测试

```powershell
cd AstrBot/data/plugins/astrbot_plugin_attention_gate
python -m unittest discover -s tests -v
```

版本：

```text
v0.9.2
```
