# Changelog

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
