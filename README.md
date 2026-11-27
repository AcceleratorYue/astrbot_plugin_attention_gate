# 消息发送门控（astrbot_plugin_attention_gate）

项目仓库：<https://github.com/AcceleratorYue/astrbot_plugin_attention_gate>

AstrBot 的消息发送门控插件。它把“要不要主动接话”和“什么时候真正发送”拆成两层，并在发送前完成等待、合并与复核。

```text
主动决策层：
  要不要主动接话？接哪条？引用谁？

发送门控层：
  用户这一轮说完没有？
  同一用户的补充消息怎么合并？
  窗口内话题变了还要不要按原目标回复？
```

插件不生成回复正文，不直接发送消息，也不创建第二份 LLM 请求。人格、上下文、工具、长期记忆、静默规则等仍由 AstrBot 主线和其它插件处理。

---

## 一、功能

```text
群消息 / 私聊消息
→ 主动决策层判断是否值得主动回复
→ 已决定回复后进入统一发送窗口
→ ONNX 判断句尾节奏（可选）
→ Jev 判断这一轮是否说完（可选）
→ 合并同一用户补充消息和图片
→ 主动回复窗口内出现新消息时 revalidate
→ 释放原请求，交给 AstrBot 主模型管线
```

### 主动决策层

- **Jev 主判断**：输出是否点名、是否在跟别人说话、是否有接话钩子、是否答题求助、时机、风险、回复价值等。
- **人格兴趣层（全局）**：所有群共享，按需配置。
- **群聊话题层（独立）**：每个群一个方案，不串群。
- **突发回复欲望层**：不要求兴趣匹配，但要求高冲动、低风险、时机合适。
- **方向/接话硬门**：指向别人、答题求助、未说完、风险过高等直接拦下。
- **无随机概率**：按阈值和规则决定 act / pass。
- **Jev 不可用直接 pass**：不降级到其他模型。

默认不内置任何人格、兴趣词、示例群号或群话题。未配置时只使用通用人格判断，不会伪造兴趣匹配。

### 统一发送门控

- **普通聊天、私聊、主动回复、poke 进入真实窗口。**
- **群聊普通跟进由消息事件缓冲合并**，不要求它自己产生 LLM 请求。
- **同一用户连发合并**：follower 被 stop，root 合并文本和图片。
- **只释放原请求**：不创建新的 `request_llm`，不重复调用主模型。
- **ONNX 句尾节奏**：低分时延长等待。
- **Jev 完整性判断**：只问“这一轮是否说完”，不重复做主动门控。
- **主动回复 revalidate**：窗口内出现新消息时，重新确认是否仍适合按原目标回复。
- **共享决策窗口**：主动候选先等待/合并，再在窗口内执行主动 Jev 决策；act 才进入主模型。
- **候选失败关闭**：主动候选没有 root 票据、决策器缺失或决策失败时拦截，绝不在未决策的情况下放行。
- **冷却回滚**：候选被拒绝、超时、取消、重复拦截或提交失败时，恢复到进入候选前的冷却值。
- **重复保护**：原请求释放后立即关闭 lane；同一发送者的主动候选不会在窗口内二次发送。

### 其它

- **强制引用目标消息**：主动回复通过 `attention_quote_target` 引用候选消息。
- **回复密度限制**：默认 15 分钟最多 2 次主动回复。
- **回复后疲劳**：刚主动回复后提高接话阈值。
- **最小用户注意力**：同一用户近期反复触发时提高门槛。
- **未回复消息缓存**：让 Jev 看到近期没有被接上的内容。
- **模板列表配置**：兴趣、群话题、Jev 问题都可在面板逐条维护。
- **诊断日志**：记录发送窗口交接与模型返回状态，便于定位无响应。

---

## 二、安装

1. 把本目录放到：

```text
AstrBot/data/plugins/astrbot_plugin_attention_gate
```

2. 通过 AstrBot 插件管理安装/更新插件（或在 import 失败后重载）。AstrBot 会读取插件根目录的 `requirements.txt`，为当前运行环境安装 ONNX 相关依赖；普通重载在依赖已可导入时不会重复安装。

3. 在插件配置中填写 Jev 服务地址、模型和 API Key（如服务商要求）。

4. 可选：启用发送门控的 ONNX 句尾节奏。模型缺失时插件会自动从 ModelScope 下载；也可以手动放置模型，详见下文。

### 运行环境

```text
AstrBot >=4.29,<5
平台适配器：aiocqhttp（OneBot v11）
```

插件使用 AstrBot 的 `on_waiting_llm_request`、`on_llm_request`、`on_decorating_result` 等生命周期钩子；需要 AstrBot 4.29 及以上版本。

### 外部依赖

- **Jev 服务**：默认地址 `https://api.typesafe.ai/v1`，默认模型 `jev-latest`。也可以填写自托管服务、兼容中转服务或其它 Jev 部署；地址可填根地址、`/v1/systemone` 或 `/v1/decision`。
- **ONNX（可选）**：`onnxruntime`、`transformers`、`numpy`，由 `requirements.txt` 自动安装。
- **模型（可选）**：句尾节奏模型不随插件打包，首次启用时自动下载到插件数据目录；来源见“ONNX 模型”一节。

### 最小可用配置

```json
{
  "enable": true,
  "enabled_groups": [],
  "jev_provider_url": "https://api.typesafe.ai/v1",
  "model": "jev-latest",
  "api_key": "你的 Jev API Key",
  "reply_value_threshold": 1.65,
  "dry_run": false
}
```

`enabled_groups` 为空表示所有群生效。主动判断默认是 `dry_run: true`，确认日志符合预期后再关闭。

---

## 三、发送门控配置

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
  "send_gate_late_decision_grace_seconds": 8,

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
| `send_gate_dry_run` | 只观察，不 stop、不延迟、不改写请求；优先于 enforce |
| `send_gate_min_wait_seconds` | 最后一条消息后的最短安静时间 |
| `send_gate_max_wait_seconds` | 从 root 到达开始的等待上限；主动决策宽限另计 |
| `send_gate_max_window_seconds` | lane 合并/等待的硬上限；主动决策宽限另计 |
| `send_gate_late_decision_grace_seconds` | 等待结束后主动决策额外获得的时间；root 晚于窗口时也至少获得该宽限 |
| `send_gate_candidate_pending_timeout_seconds` | 候选预占冷却后，请求长时间未进入 LLM hook 时的恢复超时 |
| `send_gate_onnx_enable` | 启用 ONNX 句尾节奏 |
| `send_gate_onnx_model_type` | `small` 或 `normal` |
| `send_gate_onnx_model_path` | 自定义模型目录；留空使用插件数据目录 |
| `send_gate_onnx_auto_download` | 模型缺失时自动从 ModelScope 下载 |
| `send_gate_onnx_download_timeout_seconds` | 单个模型文件下载的 socket 超时 |
| `send_gate_onnx_low_threshold` | ONNX 低分阈值，低于该值时延长等待 |
| `send_gate_onnx_low_wait_seconds` | ONNX 低分时至少等待时间 |
| `send_gate_jev_enable` | 启用 Jev 完整性判断 |
| `send_gate_jev_complete_threshold` | Jev 完整阈值 |
| `send_gate_revalidate_proactive` | 主动回复出现补充消息时重新验证 |
| `send_gate_revalidate_failure_policy` | revalidate 失败策略：`keep` / `drop` / `keep_with_guard` / `retry_once_then_drop` |

### Jev 超时

`jev_timeout_seconds` 默认 8 秒，小于发送窗口的 20 秒。发送窗口内的 Jev 请求还会按当前剩余时间进一步收紧；不会出现“配置 25 秒、窗口只剩 0.01 秒”的必然取消。

### ONNX 模型

默认目录：

```text
data/plugin_data/astrbot_plugin_attention_gate/models/normal
```

模型来源（ModelScope，作者 advent259141）：

- Small（约 12MB）：<https://modelscope.cn/models/advent259141/astrbot_debouncer_small>
- Normal（约 154MB）：<https://modelscope.cn/models/advent259141/astrbot_debouncer_normal>

启用 `send_gate_onnx_enable` 后，如果默认目录缺少文件，插件会在后台从 ModelScope 下载，下载完成后校验文件大小与 SHA256 并自动加载；损坏文件会被重新下载。首次下载期间发送门控使用纯时间窗口，不会阻塞回复。

首次下载失败后约 10 分钟会在下一次真实发送窗口重试；期间使用纯时间窗口。离线部署时，从 ModelScope 仓库下载文件后，把上游的 `model/model.onnx` 放到本地的 `models/<small|normal>/model.onnx`，tokenizer 文件放到本地同名 `tokenizer/` 目录：

```text
model.onnx
tokenizer/tokenizer.json
tokenizer/tokenizer_config.json
tokenizer/special_tokens_map.json
tokenizer/vocab.txt
```

`send_gate_onnx_model_path` 只指向本插件可用的模型目录；插件不会读取、回退或依赖 `astrbot_plugin_debounce` 的模型目录。

模型文件不包含在插件发布包内；发布包体积限制为 16MB。自定义 `send_gate_onnx_model_path` 为相对路径时按 AstrBot 进程工作目录解析；`send_gate_onnx_download_timeout_seconds` 小于 10 时按 10 秒处理。

如果部署环境已经安装 `onnxruntime-directml`，请先确认 AstrBot 的依赖安装不会用 CPU 版 `onnxruntime` 覆盖它。

---

## 四、Jev 主动决策

插件用 Jev 的多个字段共同判断：

```text
is_mentioned
is_directed_elsewhere
has_reply_hook
is_answer_seeking
is_complete_turn
is_persona_fit
is_group_topic_fit
is_good_moment
is_risky
reply_value
is_continuation
is_impulse
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

## 五、发送窗口行为

### root / follower

```text
root：第一个进入窗口的请求
follower：同一发送者在窗口内继续发来的消息
```

处理：

```text
root 在 on_llm_request 中等待
普通群聊跟进消息由群消息事件缓冲，不要求它产生 LLM 请求
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

AstrBot 的 follow-up capture 会在 `OnWaitingLLMRequest` 之前把同发送者消息并入正在运行的 Agent 轮次。这类消息不会重新产生 LLM 请求，因此不会经过 send_gate；群聊中未产生独立请求的普通跟进由本插件的群消息缓冲覆盖。

### 群聊与私聊

```text
私聊：每条消息通常都会产生 LLM 请求，直接走 on_waiting 合并。
群聊：普通消息通常不产生 LLM 请求，额外由群消息事件缓冲合并。
```

群聊额外包含主动候选、主动 revalidate、poke 独立排队等逻辑。

### 共享决策窗口

```text
attention 只产出候选
→ send_gate 等待/合并
→ send_gate 内调用主动 Jev
→ act 才进入模型
→ reject 直接 stop
```

主动候选在窗口内进入模型前必须完成决策。若候选缺少 root 票据、决策器不可用或决策超时，插件会停止该候选并回滚预占冷却，而不是把占位提示词直接交给主模型。

### 晚到 root

root 可能在前一个 LLM 请求释放 session lock 后很久才进入 `on_llm_request`。此时原来的等待 lane 已被清理，但事件仍带 root token：

```text
用 root token/ticket 恢复 lane
保留原始 first_at，不再重新等待一个完整窗口
等待窗口结束后，主动决策最多额外获得 late_decision_grace 的时间
root 晚到时至少获得 late_decision_grace 的决策时间
决策失败/超时则 stop，并恢复冷却
普通聊天 root 合并缓冲后释放
```

这避免了“窗口已过期就无决策放行”和“冷却被白锁 120 秒”两种问题。

### ONNX + Jev

```text
等待安静
→ ONNX 判断句子完整度
→ Jev 判断这一轮是否说完
→ 完整则释放
→ 不完整则额外等待一小段（受 max_window 限制）
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

## 六、日志与诊断

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
  "action": "root_enforce|group_buffered|merged_stop|late_root_recovered|real_release|active_decision_drop|candidate_unmanaged_stop|revalidate_drop",
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

`decisions_*.jsonl` 会记录群号、发送者、消息内容和判断结果，用于复盘主动回复。请按部署环境的数据保留要求定期清理；发送门控日志默认不记录消息正文（`send_gate_log_content` 打开时才记录前 80 字）。

---

## 七、与其它插件

- **Context Aware**：可选共存；存在时自动注入场景，发送门控不接管其上下文。
- **LivingMemory**：继续注入记忆；发送门控只合并 prompt/图片。
- **quiet_hours**：静默硬门继续生效。
- **suanle_bushuo**：`keep_silent` 仍可最终否决。
- **smart_reply_tag**：普通引用继续工作；主动引用由本插件插入。
- **其它发送门控插件**：本插件不读取其目录、状态或模型。不要同时启用会 stop 同一请求的发送门控插件。

---

## 八、已知限制

- Jev 请求失败/超时/限流时，主动决策直接 pass；
- Jev 完整性失败时按时间窗口释放，不永久阻塞普通聊天；
- 主动候选决策失败时拦截并回滚冷却，不会无决策放行；
- `send_gate_max_wait_seconds` 与 `max_window` 取更小值作为等待上限；主动候选决策宽限另计，最长可能多出 `send_gate_late_decision_grace_seconds`；
- send_gate 真实窗口启用时，主动回复的 `active_reply_settle_seconds` 等待被跳过；
- AstrBot follow-up capture 在 OnWaitingLLMRequest 之前消费的消息不会重新进入 gate；
- 只有 `agent_runner.runner_type=local` 会触发 waiting hook；第三方 agent runner 下发送窗口不生效，主动回复退回非延迟判断；
- 图片只作为附件和合并内容，不做独立视觉理解；
- 重启 AstrBot 后内存缓冲和窗口状态会清空。

---

## 九、开发测试

测试需要 AstrBot 运行环境中的 Python（依赖 `astrbot` 包）：

```powershell
cd AstrBot/data/plugins/astrbot_plugin_attention_gate
python -m unittest discover -s tests -v
```

测试覆盖：

```text
请求分类、配置契约、Jev state 裁剪、dry-run、root/follower 合并、
主动候选共享决策、晚到 root 恢复、late decision、
取消/超时冷却回滚、revalidate、ONNX 模型来源与自动下载。
```

版本：

```text
v0.9.4
```

---

## 十、第三方与许可

- 插件代码：MIT License，见 [LICENSE](LICENSE)。
- AstrBot 插件框架：<https://github.com/AstrBotDevs/AstrBot>
- Jev / SystemOne：按所使用服务商的文档和条款配置。
- ONNX 句尾节奏模型：来自 ModelScope 的 `advent259141/astrbot_debouncer_small` 与 `advent259141/astrbot_debouncer_normal`（Apache License 2.0），版权归原作者；本插件只下载和加载，不重新分发模型文件。
- 设计思路参考：`Him666233/astrbot_plugin_group_chat_plus` 的群聊读空气/主动参与机制（AGPL-3.0），以及 `astrbot_plugin_debounce` 的发送窗口思路（AGPL-3.0）。当前插件为独立的 MIT 实现，不包含上述项目的代码，也不依赖其运行；如未来复用其代码或资源，需按 AGPL-3.0 处理并保留版权声明。
- 预筛为可选项：`prefilter_enabled` 默认开启，但 `embedding_provider_id` / `rerank_provider_id` 默认空，未配置时跳过向量/重排，直接交给 Jev。
