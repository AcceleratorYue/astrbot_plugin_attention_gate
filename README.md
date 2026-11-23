# 注意力主动回复门控（astrbot_plugin_attention_gate）

项目仓库：<https://github.com/AcceleratorYue/astrbot_plugin_attention_gate>

用 **Jev（TypeSafe SystemOne / NeoHorse-Jev-4B）** 判断群消息是否值得主动接话，替代 AstrBot 内置的随机概率主动回复。

## 它做什么

```text
群消息
→ 硬过滤（私聊 / @ / 回复 Bot / 指向别人 / 其他 Bot）
→ 缓冲区
→ Jev 判断
→ act / pass
→ act 时走 AstrBot 原生 LLM 管线
```

插件**不生成正文**，也不直接发送消息。人格、历史、分段、工具、长期记忆、静默规则等仍由 AstrBot 主线和其他插件处理。

## 关键特性

- **Jev 主判断**：不生成文字，只输出意图、兴趣匹配、时机、风险和回复价值。
- **人格兴趣层（全局）**：所有群共享，模板列表逐条维护。
- **群聊话题层（独立）**：每个群一个方案，不串群。
- **突发回复欲望层**：不要求兴趣匹配，但要求高冲动、低风险、时机合适。
- **无随机概率**：按阈值和风险规则决定 act / pass。
- **Jev 不可用直接 pass**：不降级到其他模型。
- **冷却与缓冲**：主动回复后冷却 120 秒，期间继续监听并把消息放进缓冲区；冷却结束后的下一条消息再判断。
- **单群每小时上限**：默认 15 次，按滚动一小时统计。
- **不自动续话**：Bot 不会主动连续自说自话；只有人类继续说话时才可能触发下一轮。
- **强制引用目标消息**：主动回复通过 `attention_quote_target` 让智能引用插件引用候选消息。

## 安装

1. 把本目录放到：

```text
AstrBot/data/plugins/astrbot_plugin_attention_gate
```

2. 重载插件。
3. 打开插件配置，填写：
   - `jev_provider_url`：Jev 服务地址；
   - `model`：模型 ID；
   - `api_key`：TokenRhythm 平台必填 `sk_` Key；自托管无鉴权可留空；
   - `enabled_groups`：填写示例群号；实际按你的群替换；
   - `enabled_groups` 清空后表示**所有群聊生效**；填写时只在这些群生效；
   - `dry_run`：先保持 `true`。

## 两种 Jev 服务接入方式

### A. TokenRhythm 平台（当前默认）

```json
{
  "jev_provider_url": "https://tokenrhythm.studio/v1/decision",
  "model": "NeoHorse-Jev-4B",
  "api_key": "sk_你的TokenRhythm密钥",
  "state_as_text": true
}
```

也可以使用：

```json
{
  "jev_provider_url": "https://tokenrhythm.studio/v1/systemone",
  "model": "NeoHorse-Jev-4B"
}
```

平台文档：<https://tokenrhythm.studio/docs/api-integration>

注意：TokenRhythm 要求 `Authorization: Bearer sk_...`，Key 需要填进插件配置。

### B. TypeSafe 官方 Jev

```json
{
  "jev_provider_url": "https://api.typesafe.ai/v1/systemone",
  "model": "jev-latest",
  "api_key": "<TypeSafe API Key>"
}
```

Key 申请：<https://console.typesafe.ai/keys>

### C. 自托管 NeoHorse-Jev-4B

NeoHorse-Jev-4B 同时提供：

```text
POST /v1/systemone
POST /v1/decision
GET  /health
```

本地无鉴权示例：

```json
{
  "jev_provider_url": "http://127.0.0.1:8080",
  "model": "NeoHorse-Jev-4B",
  "api_key": ""
}
```

如果服务端设置了 `NEOHORSE_API_KEY`，把同一个值填入 `api_key`。

插件会：

- 只填根地址时自动补 `/v1/systemone`；
- 填 `/v1` 时自动补 `/systemone`；
- 填完整 `/v1/systemone` 或 `/v1/decision` 时原样使用；
- 未填 API Key 时不发送 `Authorization` 头；
- 429/502/503/504/529 重试后仍失败，直接 pass；
- 401/403/413/422 不重试，直接 pass。

## 推荐配置

```json
{
  "enable": true,
  "dry_run": true,
  "enabled_groups": ["123456789", "123456788", "123456787", "123456786"],
  "hourly_limit": 15,
  "cooldown_seconds": 120,
  "quote_target_enabled": true
}
```

### dry-run

`dry_run=true` 时：

- 仍然调用 Jev；
- 只写入 `data/plugin_data/astrbot_plugin_attention_gate/decisions_YYYYMMDD.jsonl`；
- 不触发 LLM；
- 不占主动次数。

先观察 1~3 天，确认 `act` 的判断质量，再改为 `false`。

## 人格兴趣层与群话题层

两项都使用 WebUI 的「模板列表」配置形式，每个条目都能单独开关、修改，不再需要手写整段 JSON。

### 全局人格兴趣（interest_groups）

每个方向一条：

| 字段 | 说明 |
|---|---|
| 方向名称 | 例如「鲸鱼与摸鱼」 |
| 启用 | 单独关闭某个方向 |
| 权重 | 0~100，越大越容易被判断为相关 |
| 关键词 | 一行一个，可添加多个 |
| 同义词/别名 | 每行 `本体=别名1,别名2`，例如 `鲸鱼=大肥鱼,小肥鱼` |

所有群共用。

### 群话题层（group_topic_schemes）

每个群一个方案，互不影响：

| 字段 | 说明 |
|---|---|
| 群号 | 例如 `123456789` |
| 显示名称 | 方便识别 |
| 启用 | 关闭后该群不再使用话题层 |
| 话题列表 | 每行格式：`方向|权重|关键词1,关键词2` |

示例：

```text
游戏与联机|85|游戏,十点半,Minecraft,MC,VRChat,战雷,开黑
AI/插件/技术|75|模型,token,插件,提示词,部署,代码,桌宠
B站与视频|70|B站,视频,UP,评论区,弹幕,切片
玩梗与表情包|60|梗,表情包,图,斗嘴,吐槽,复读
群友日常|45|今天,上班,下班,睡觉,好累,刚刚
```

加一个新群时，只需在 `group_topic_schemes` 里新增一条方案，不会影响其他群。

**如果某个群没有填写方案或关键词，Jev 不会收到 `is_group_topic_fit` 问题，该群只走人格兴趣层。**

## 第一层：embedding / rerank 预筛

在调用 Jev 之前，先用已有兴趣方向做一次轻量匹配：

```text
关键词命中
或 embedding 相似度高
→ 继续调用 Jev
```

只有落在中间区间的消息才调用 rerank：

```text
embedding 分数在低阈值和高阈值之间
→ rerank 再判断一次
```

低相关消息默认不调用 Jev，直接写一条 `prefilter_low` 决策记录。

为了避免漏掉突发回复欲望，当：

```text
embedding 低于低阈值
且文本含明显情绪/吐槽/反问/笑声线索
且 impulse_enabled=true
```

会标记为 `impulse_cue_bypass`，仍然交给 Jev 判断。

图片/非文本消息直接放行，交给 Jev 结合上下文判断。

配置：

```json
{
  "prefilter_enabled": true,
  "embedding_provider_id": "dashscope_embedding",
  "rerank_provider_id": "bailian_rerank",
  "embedding_threshold": 0.42,
  "embedding_low_threshold": 0.30,
  "embedding_batch_size": 10,
  "rerank_threshold": 0.30
}
```

如果 embedding/rerank Provider 不可用，插件不会直接丢弃消息，而是标记 `prefilter_unavailable` 并交给 Jev，避免把主判断一起弄坏。

DashScope `text-embedding-v4` 单批最多 10 条，`embedding_batch_size` 用于分批；人格兴趣 + 群话题超过 10 条时会自动拆成多批。

## 第三层：突发回复欲望（is_impulse）

有些消息不属于长期兴趣，但就是让人想接一句。

新增字段：

| 字段 | 含义 |
|---|---|
| `is_impulse` | 看到这条消息是否产生即时想接、想吐槽、想笑、想安慰或想接梗的冲动 |

它不要求人格兴趣或群话题匹配，但要求：

```text
is_impulse 过线
且 reply_value 达到最低线
且 is_good_moment 过线
且风险低于冲动层专属上限
```

相关配置：

```json
{
  "impulse_enabled": true,
  "impulse_threshold": 0.72,
  "impulse_value_threshold": 1.20,
  "impulse_risk_max": 0.30
}
```

冲动层仍然是确定性的阈值判断，不加入随机数。

## Jev 判断字段

问题本身也改成模板列表 `judge_questions`，每个问题一条，可单独开关、修改文案和判定标准。

默认问题：

| 字段 | 含义 |
|---|---|
| `is_mentioned` | 是否点名/直接对 Bot 说话 |
| `is_persona_fit` | 是否符合全局人格兴趣 |
| `is_group_topic_fit` | 是否符合当前群话题 |
| `is_good_moment` | 现在插话是否自然 |
| `is_continuation` | 是否在接 Bot 刚才的话 |
| `is_impulse` | 是否有突发接话欲望 |
| `is_risky` | 是否有明显风险 |
| `reply_value` | 0~3 的主动回复价值 |

### 阈值

阈值不再是 JSON，而是独立数值字段：

```text
persona_fit_threshold
group_topic_fit_threshold
good_moment_threshold
risk_threshold
reply_value_threshold
continuation_value_threshold
mentioned_threshold
impulse_threshold
impulse_value_threshold
impulse_risk_max
```

### 决定规则

```text
is_risky 过高 → pass
is_good_moment 过低 → pass

冲动路径：
  is_impulse 高
  且 reply_value 达到冲动最低值
  且风险不超过冲动上限
  → act

兴趣路径：
  人格兴趣或群话题至少一个过线
  且 reply_value 过线
  → act

接话延续路径：
  正在接 Bot 的话
  且 continuation 概率与 reply_value 过线
  → act

其余 → pass
```

长时间没有主动发言时，Jev 会看到：

```text
距上次主动的秒数
距上次主动的消息条数
上次主动后是否有人接话
```

这些信息只帮助判断，不会因为“时间到了”自动发消息。

## 与上下文感知增强（Context Aware）联动

主动回复触发时会设置结构化标记：

```json
{
  "plugin": "astrbot_plugin_attention_gate",
  "reason": "value(1.55)",
  "path": "value",
  "target_message_id": "123456",
  "prefilter": "rerank_high(0.37)"
}
```

同时设置：

```text
active_reply_triggered
attention_quote_target
```

`astrbot_plugin_context_aware` 会把这类请求识别为 `TRIGGER_ACTIVE`，并在 `strict_mode` 下避免误以为群友在问 Bot。

如果 Context Aware 支持 `attention_quote_target`，场景中会明确写出“你这次主动接的是哪条消息”；没有该支持时，仍能正常识别主动触发，只是不会显示目标消息。

### 避免重复上下文

配置项：

```json
{
  "prompt_context_mode": "auto"
}
```

- `auto`：检测到 Context Aware 时，注意力插件只发送“目标消息 + 判断理由”，最近群聊交给 Context Aware；
- `full`：注意力插件自己附带最近消息；
- `target_only`：始终不附带最近消息。

### 主动回复模型

默认开启：

```json
"use_main_chat_provider": true
```

触发前会读取当前会话的主聊天模型，并设置：

```python
event.set_extra("selected_provider", provider_id)
```

否则 AstrBot 会把插件构造的 `ProviderRequest` 当作功能性请求，优先使用 `provider_settings.functional_provider_id`，导致主动回复落到另一个模型。

### 前置停止检查

入口会检查：

```python
event.is_stopped()
```

`quiet_hours`、黑名单或其他前置插件已经拦停时，不再调用预筛和 Jev。

### Context Aware 目标消息补丁

仓库提供针对 `astrbot_plugin_context_aware` v3.7.1 的可选补丁：

```text
patches/context_aware_attention_target.patch
```

应用后，Context Aware 的场景描述会显示：

```text
（你这次主动接的是 张三 的这条消息：「...」）
（主动判断依据：value(1.55)，路径 value）
```

不打补丁也不会破坏主动回复；只是场景里不会显示主动目标消息。

## 引用链路

主动回复触发时，插件会设置：

```python
event.set_extra("active_reply_triggered", True)
event.set_extra("attention_quote_target", target_message_id)
```

`on_decorating_result` 会读取该标记，并在发送前插入：

```python
Reply(id=target_message_id, chain=[])
```

因此主动回复会明确引用它正在回应的那条群消息，**不强制依赖第三方引用插件补丁**。  
主动回复 prompt 也会明确禁止模型输出 `[quote]` / `[引用]` / `[reply]` / `[quote:消息ID]`，避免和自动引用重复。

如果已安装并修补了 `astrbot_plugin_smart_reply_tag`，它会识别已有的 `Reply` 并跳过，不会重复引用。

要求：

- 目标消息必须来自当前群缓冲区；
- 目标消息 ID 必须存在；
- 目标消息不能是 Bot 自己的消息；
- 引用失败时正常发送正文，不阻断回复。

## 已知限制

- TypeSafe 官方 Jev 需要 API Key；自托管 NeoHorse-Jev-4B 默认不需要；
- Jev 请求失败/超时/限流时直接 pass；
- 第一版不自动学习兴趣；
- 第一版不做续话；
- 不处理私聊；
- 图片只以 `[图片]` 标记进入上下文；
- 如果 AstrBot 重启，内存缓冲和冷却状态会清空。

## 目录

```text
main.py
_conf_schema.json
metadata.yaml
README.md
tests/test_attention_gate.py
```

## 开发测试

```powershell
cd AstrBot/data/plugins/astrbot_plugin_attention_gate
python -m unittest discover -s tests -v
```
