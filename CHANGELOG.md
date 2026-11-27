# Changelog

## 0.7.0 (2026-11-27)

### 真实发送窗口（灰度）
- 新增 `send_gate_enforce` / `send_gate_enforce_groups` / `send_gate_enforce_private`。
- 仅对普通群聊/私聊生效；主动回复、poke、follow-up 暂不强制。
- root 在 `on_llm_request` 内等待安静窗口，follower 在 `on_waiting_llm_request` 被 stop。
- root 合并 follower 文本和图片后释放原请求，不创建新请求。
- 原请求释放后关闭 lane，避免旧窗口继续吞消息。

## 0.6.2 (2026-11-27)

### dry-run 修正
- 原请求释放后立即关闭 lane，后续同用户消息会开启新窗口，不再并入已释放的旧窗口。
- 修正历史 dry-run 中“释放后仍出现 would_merge”和 window_age 超窗口的问题。

## 0.6.1 (2026-11-26)

### 配置页整理
- 配置项按【基础】【Jev】【人格与话题】【主动节奏】【判断阈值】【突发欲望】【预筛】【问题定义】【发送门控】重排。
- 仅调整 schema 顺序和描述前缀，不改变配置 key、默认值或读取逻辑。

## 0.6.0 (2026-11-26)

### 统一消息发送门控：dry-run 观察层
- 新增 `send_gate/` 模块和 `on_waiting_llm_request` / `on_llm_request` 观察 Hook。
- 当前只做 dry-run：不 stop、不 sleep、不改写 ProviderRequest。
- 记录 `would_root` / `would_merge` / `would_release` 到 `send_gate_dryrun_YYYYMMDD.jsonl`。
- 支持按群过滤、私聊开关、窗口长度、模拟等待和 lane 清理。
- 旧 `astrbot_plugin_debounce` 仍保留，不参与新观察层。

## 0.5.1 (2026-11-25)

### 修复
- 修复 Jev 422 `JEV_UPSTREAM_REJECTED`：限制 Jev state 字符数（默认 12000），自动裁剪最近对话、未回复消息、批次和轮次。
- 单条状态内容在进入 Jev 前截断，避免长上下文把状态推到上游输入上限。
- 新增 `jev_max_state_chars` 配置。

## 0.5.0 (2026-11-24)

### 借鉴 group_chat_plus 的优化
- P0：新增短窗口回复密度限制 `density_window_seconds` / `density_max_dispatches`。
- P0：新增未回复消息缓存 `unanswered`，并注入 Jev/生成 prompt。
- P1：等待窗口增加批次快照 `active_reply_wait_max_messages`，Jev 和生成 prompt 可看到同批消息。
- P1：新增主动回复后的动态阈值惩罚（疲劳窗口）。
- P2：新增最小用户注意力降权，同一用户近期反复触发时提高门槛。
- 所有新机制均为纯本地状态，不依赖 group_chat_plus。

## 0.4.1 (2026-11-24)

### 修复
- 主动判断增加 `active_reply_settle_seconds`，先等同一轮连续发言进入缓冲区。
- Jev 与生成 prompt 使用 `target turn`，同一发送者连续几句话一起判断。
- 新增 `is_complete_turn` 与 `complete_turn_threshold`，半句、补刀、孤立碎片直接 pass。
- 生成 prompt 明确要求先理解对话再回应完整意思，不要只抓单个词。

## 0.4.0 (2026-11-24)

### 新增
- 人格兴趣增加 `proactive` 开关：关闭后只作为正常聊天背景，不参与主动回复。
- “解题与知识问答”默认关闭主动参与，避免一有人提问就凑上去答题。
- 群话题支持行尾 `|被动`：只作为群聊背景，不参与主动触发；一次性事件、签到、纯图片、日常碎片已改为被动。
- 新增 `is_answer_seeking` 判断与 `answer_seeking_threshold`：非点名 Bot 的提问/求解直接 pass。
- 收窄群话题关键词，移除过于宽泛的“游戏”等词。

## 0.3.0 (2026-11-24)

### 新增
- 增加 `is_directed_elsewhere` 判断：一对一互聊、回应某个群友、指挥另一个 Bot 时直接 pass。
- 增加 `has_reply_hook` 判断：单字、复读、口号、纯图片、无上下文短句即使分值高也不接。
- 增加 `bot_names`：明确叫到 Bot 名字时视为对当前 Bot 说话，不被方向门误拦。
- 增加 `directed_elsewhere_threshold`、`directed_elsewhere_hook_max`、`reply_hook_threshold` 配置。
- 默认 `reply_value_threshold` 调整为 1.65。

## 0.2.6 (2026-11-24)

### 修复
- 主动回复判断前原子占住冷却，避免并发消息在 Jev 请求期间同时通过 120 秒冷却检查；判断为 pass/未发送时恢复原冷却。
- 每小时限额与冷却检查不再出现同秒多条 act。

## 0.2.5 (2026-11-24)

### 修复
- 主动回复显式设置 `selected_provider` 为当前会话主聊天模型，避免被 AstrBot 当作功能性请求路由到 `provider_settings.functional_provider_id`（此前实际使用 `starryapi_glm/glm-5.3-flash`）。
- 新增 `use_main_chat_provider` 配置，默认开启。
- 增加主聊天模型选择测试。

## 0.2.4 (2026-11-24)

### 修复
- 修复 DashScope text-embedding-v4 单批最多 10 条导致的 `prefilter_unavailable`：兴趣/群话题 embedding 现在按 `embedding_batch_size`（默认 10）分批请求。
- 增加分批 embedding 回归测试。

## 0.2.3 (2026-11-23)

### 提示词
- 主动回复 prompt 明确禁止输出 `[quote]`/`[引用]`/`[reply]`/`[quote:消息ID]`，引用由系统自动插入。
- 智能引用插件在 `active_reply_triggered` 轮次追加同样的禁止引用说明。
- 默认引用模板增加主动回复例外。
- 增加 prompt 回归测试。

## 0.2.2 (2026-11-23)

### 文档
- 增加 `patches/context_aware_attention_target.patch`，用于兼容 AstrBot Context Aware v3.7.1 的目标消息场景显示。
- 说明不打补丁时主动回复仍可正常工作，只是场景中不显示主动目标消息。

## 0.2.1 (2026-11-23)

### 改进
- `active_reply_triggered` 改为结构化 payload，附带判断原因、路径、目标消息和预筛来源。
- Context Aware 联动：支持 `attention_quote_target`，场景描述可显示主动回复正在接哪条消息。
- 新增 `prompt_context_mode`：检测到 Context Aware 时只发送目标与理由，避免重复注入最近上下文。
- 群聊入口增加 `event.is_stopped()` 检查，尊重 quiet_hours/黑名单等前置拦截。
- 主动回复引用改由插件自身插入，不再强依赖第三方引用插件补丁。

## 0.2.0 (2026-11-23)

### 新增
- 增加 `is_impulse` 第三层：不要求兴趣匹配，但要求高冲动、低风险、时机合适。
- 增加长时间未主动时的 `idle_state`：距上次主动秒数、消息数、是否有人接话。
- 增加 embedding/rerank 第一层预筛：
  - 关键词命中；
  - `dashscope_embedding` 向量匹配；
  - `bailian_rerank` 边界重排；
  - 冲动线索绕过低相关过滤；
  - Provider 不可用时回退给 Jev。
- 主动回复自带 `Reply` 引用插入，不再强依赖第三方引用插件补丁。
- 群聊白名单支持“填写时仅这些群生效，清空时全部群生效”。
- 未配置群话题的群自动移除 `is_group_topic_fit`，只走人格兴趣层。

### 配置
- 人格兴趣从 JSON 文本改为 `interest_groups` 模板列表。
- 群话题从 JSON 文本改为 `group_topic_schemes` 模板列表，每个群一条方案。
- Jev 问题改为 `judge_questions` 模板列表。
- 阈值改为独立数值字段。
- 增加预筛、冲动层配置字段。

### 修复
- Jev 服务支持 TypeSafe、TokenRhythm 平台和自托管 NeoHorse-Jev-4B。
- 兼容 `/v1/decision` 与 `/v1/systemone`。
- 无 API Key 时不再发送空 Authorization。
- 按平台文档区分可重试错误与不可重试错误。

## 0.1.0 (2026-11-23)

### 新增
- 初版注意力门控主动回复插件。
- Jev 判断、人格兴趣、群话题、dry-run、冷却与缓冲、每小时上限、引用目标。
