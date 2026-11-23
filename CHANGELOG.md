# Changelog

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
