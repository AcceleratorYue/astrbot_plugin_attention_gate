"""注意力门控主动回复插件。

只做“观察 / 判断 / 触发”，不直接发送消息，也不生成回复正文。

流程：
群消息 -> 硬过滤 -> 缓冲区 -> Jev 判断 -> act 时 yield request_llm()
主线的 persona / 历史 / 分段 / 引用 / reply_budget / suanle 等继续生效。

关键约束：
- 只做群聊；
- 单群每小时上限 + 主动冷却；
- 冷却期间继续缓冲，冷却结束后的下一条消息再判断；
- Jev 不可用直接 pass，不降级到其他模型；
- 主动回复时通过 attention_quote_target 让智能引用插件引用目标消息。
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import hashlib
import json
import math
import re
import time
import urllib.error
import urllib.request
from collections import OrderedDict, deque
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Image, Plain, Reply
from astrbot.api.star import Context, Star, StarTools

PLUGIN_NAME = "astrbot_plugin_attention_gate"
VERSION = "0.2.1"
LOG = f"[{PLUGIN_NAME}]"

DEFAULT_JEV_URL = "https://tokenrhythm.studio/v1/decision"
DEFAULT_JEV_MODEL = "NeoHorse-Jev-4B"
SYSTEMONE_SUFFIX = "/v1/systemone"
TRIGGER_QUOTE_EXTRA = "attention_quote_target"
TRIGGER_MARK_EXTRA = "attention_gate_triggered"
HOUR_SECONDS = 3600.0
BUFFER_MAX = 100

DEFAULT_PERSONA = (
    "我是鲸鲸，一只鲸鱼娘。性格活泼、嘴硬心软、会傲娇，但很少真的生气；"
    "我说话短，先给情绪反应，再给内容；我不会主动刷屏，也不会为了参与硬找话说。"
)

DEFAULT_GLOBAL_INTERESTS = [
    {
        "name": "解题与知识问答",
        "weight": 85,
        "keywords": ["题", "解题", "为什么", "原理", "算法", "答案"],
    },
    {
        "name": "吃与零食",
        "weight": 75,
        "keywords": ["吃", "白饭", "零食", "饭", "好吃", "奶茶"],
    },
    {
        "name": "鲸鱼与摸鱼",
        "weight": 90,
        "keywords": ["鲸", "鲸鱼", "大肥鱼", "小肥鱼", "摸鱼", "海"],
    },
    {
        "name": "被夸与互动",
        "weight": 60,
        "keywords": ["可爱", "厉害", "夸", "喜欢你", "抱抱"],
    },
    {
        "name": "情绪陪伴",
        "weight": 55,
        "keywords": ["累", "难过", "烦", "委屈", "安慰", "开心"],
    },
]

DEFAULT_GROUP_TOPIC_SCHEMES = [
    {
        "__template_key": "group",
        "group_id": "123456789",
        "name": "示例群 A",
        "enabled": True,
        "topics": [
            "游戏与联机|85|游戏,十点半,Minecraft,MC,VRChat,战雷,开黑",
            "AI/插件/技术|75|模型,token,插件,提示词,部署,代码,桌宠",
            "B站与视频|70|B站,视频,UP,评论区,弹幕,切片",
            "玩梗与表情包|60|梗,表情包,图,斗嘴,吐槽,复读",
            "群友日常|45|今天,上班,下班,睡觉,好累,刚刚",
        ],
    },
    {
        "__template_key": "group",
        "group_id": "123456788",
        "name": "示例群 B",
        "enabled": True,
        "topics": [
            "玩梗与复读|80|梗,复读,哈基米,玩梗,转人工",
            "脑筋急转弯与谜题|85|脑筋急转弯,谜题,为什么,答案,切比雪夫",
            "摸头与互动|70|摸头,抱抱,贴贴,打闹,打招呼",
            "角色扮演与图灵测试|65|角色扮演,图灵测试,扮演,AI,机器人",
            "图片与表情包|60|图,图片,表情包,截图,画图",
            "音乐与视频|55|歌,歌曲,音乐,视频,切片",
            "深夜闲聊|40|深夜,睡不着,熬夜,晚安",
        ],
    },
    {
        "__template_key": "group",
        "group_id": "123456787",
        "name": "示例群 C",
        "enabled": True,
        "topics": [
            "蔚蓝档案|90|蔚蓝档案,BA,老师,学生,阿罗娜",
            "总力战与攻略|85|总力战,凹分,凹轴,攻略,配队,轴",
            "抽卡与养成|80|抽卡,十连,出货,井,养成,角色",
            "签到与今日运势|55|签到,运势,今日运势,抽签",
            "群友日常|45|今天,日常,闲聊,好累,开心",
            "图片分享|50|图,图片,表情包,截图",
            "深夜闲聊|35|深夜,熬夜,晚安",
        ],
    },
    {
        "__template_key": "group",
        "group_id": "123456786",
        "name": "示例群 D",
        "enabled": True,
        "topics": [
            "今日运势与签到|60|签到,运势,今日运势,金币",
            "图片与斗图|55|图,图片,斗图,晒图,表情包",
            "深夜与晚安|50|深夜,熬夜,晚安,睡不着",
            "群友打闹|50|打闹,抱抱,摸头,玩笑,打招呼",
            "B站与视频|45|B站,视频,链接,UP,切片",
            "游戏与MC|45|游戏,MC,Minecraft,空气制黄金",
            "穿搭与日常|40|穿搭,衣服,作业,学校,吉他,画画",
            "新人欢迎|35|新人,欢迎,入群,自我介绍",
        ],
    },
]

DEFAULT_IMPULSE_SETTINGS = {
    "impulse_threshold": 0.72,
    "impulse_value_threshold": 1.20,
    "impulse_risk_max": 0.30,
}

DEFAULT_PREFILTER_SETTINGS = {
    "embedding_provider_id": "dashscope_embedding",
    "rerank_provider_id": "bailian_rerank",
    "embedding_threshold": 0.42,
    "embedding_low_threshold": 0.30,
    "rerank_threshold": 0.30,
}

_IMPULSE_CUE_RE = re.compile(
    r"[?？!！]|哈哈|笑死|离谱|救命|卧槽|绝了|服了|真的假的|无语|"
    r"绷|破防|呜呜|好耶|可爱|摸鱼|难绷|草|啊这|不是吧|这也"
)
_MEDIA_TEXT = {"[图片]", "[非文本消息]", "[图片消息]"}


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def impulse_cue(text: str) -> bool:
    return bool(_IMPULSE_CUE_RE.search(text or ""))

DEFAULT_QUESTIONS: dict[str, Any] = {
    "is_mentioned": {
        "type": "noul",
        "instructions": "目标消息是否在点名或直接对当前机器人说话？",
        "criteria": {
            "true": "叫了它的名字/常用称呼，或话头明显是对它说的",
            "false": "在说别人、普通群聊、或指向其他机器人",
        },
    },
    "is_persona_fit": {
        "type": "noul",
        "instructions": "目标消息是否符合机器人的全局人格兴趣？",
        "criteria": {
            "true": "话题落在全局人格兴趣中，例如解题、吃、鲸鱼/摸鱼、被夸、情绪陪伴，接话会自然有趣",
            "false": "话题与人格兴趣无关，接话会显得突兀",
        },
    },
    "is_group_topic_fit": {
        "type": "noul",
        "instructions": "目标消息是否符合当前群自己的话题画像？",
        "criteria": {
            "true": "话题落在当前群配置的话题方向中，例如游戏、AI/插件、B站、玩梗、群友日常",
            "false": "与当前群话题画像无关；不要用其他群的话题来判断",
        },
    },
    "is_good_moment": {
        "type": "noul",
        "instructions": "此刻机器人接一句话是否自然得体？",
        "criteria": {
            "true": "对话节奏允许加入，接话不会打断正在进行的一对一交流",
            "false": "群里正专注聊别的事，或两个人聊得正浓，插话会尴尬",
        },
    },
    "is_continuation": {
        "type": "noul",
        "instructions": "当前消息是否是在接机器人刚才说过的话？",
        "criteria": {
            "true": "延续机器人上一轮发言，像是回应、追问或顺着说；即使没有引用/@",
            "false": "没有和机器人上一轮发言形成连续对话",
        },
    },
    "is_impulse": {
        "type": "noul",
        "instructions": "看到这条消息时，是否产生一种即时想接一句、想吐槽、想笑、想安慰或想接梗的冲动？",
        "criteria": {
            "true": "明显好笑、突然的吐槽、情绪很强、反问、群友在互动、有自然话头、冷场里刚好能接一句",
            "false": "普通信息、纯陈述、复读、没人需要回应、接话会显得硬凑",
        },
    },
    "is_risky": {
        "type": "noul",
        "instructions": "机器人接这句话是否有明显风险？",
        "criteria": {
            "true": "涉及隐私、争吵、敏感身份、色情、违法、骚扰或明显不适合主动插话的场景",
            "false": "普通日常群聊，风险低",
        },
    },
    "reply_value": {
        "type": "score",
        "instructions": "综合来看，机器人主动回复这条消息的价值有多大？",
        "criteria": [
            "0 不该接：表情、复读、纯噪音、完全无关",
            "1 可接可不接：有点意思但错过也无所谓",
            "2 值得接：符合人格或群话题，且气氛合适",
            "3 很适合接：正中兴趣、时机好、接话会自然有趣",
        ],
    },
}

DEFAULT_THRESHOLDS = {
    "is_mentioned": 0.60,
    "is_persona_fit": 0.55,
    "is_group_topic_fit": 0.55,
    "is_good_moment": 0.45,
    "is_continuation": 0.50,
    "is_risky": 0.50,
    "reply_value": 1.50,
    "continuation_reply_value": 1.20,
    "is_impulse": 0.72,
    "impulse_reply_value": 1.20,
    "impulse_risk": 0.30,
}


def _now_text(ts: float | None = None) -> str:
    return _dt.datetime.fromtimestamp(ts or time.time()).strftime("%m-%d %H:%M")


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0, minimum: int = 0) -> int:
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return max(minimum, default)


def _parse_json_value(raw: Any, default: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def normalize_interests(value: Any) -> list[dict[str, Any]]:
    """把兴趣配置规范成 [{name, weight, keywords, aliases}]。"""
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        keywords = [str(k).strip() for k in item.get("keywords", []) if str(k).strip()]
        aliases_raw = item.get("aliases") or {}
        aliases: dict[str, list[str]] = {}
        if isinstance(aliases_raw, dict):
            for base, extra in aliases_raw.items():
                if isinstance(extra, list):
                    aliases[str(base)] = [str(x).strip() for x in extra if str(x).strip()]
        result.append(
            {
                "name": name,
                "weight": _as_float(item.get("weight"), 50.0),
                "keywords": keywords,
                "aliases": aliases,
            }
        )
    return result


def _split_keywords(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    if not isinstance(raw, str):
        return []
    return [x.strip() for x in re.split(r"[,，、]", raw) if x.strip()]


def normalize_interest_groups(value: Any) -> list[dict[str, Any]]:
    """模板列表格式的人格兴趣方向。"""
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        if item.get("enabled", True) is False:
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        aliases: dict[str, list[str]] = {}
        raw_aliases = item.get("aliases")
        if isinstance(raw_aliases, dict):
            for base, extra in raw_aliases.items():
                if isinstance(extra, list):
                    aliases[str(base)] = _split_keywords(extra)
        elif isinstance(raw_aliases, str):
            for line in raw_aliases.splitlines():
                if "=" not in line:
                    continue
                base, extra = line.split("=", 1)
                base = base.strip()
                if base:
                    aliases[base] = _split_keywords(extra)
        result.append(
            {
                "name": name,
                "weight": _as_float(item.get("weight"), 50.0),
                "keywords": _split_keywords(item.get("keywords")),
                "aliases": aliases,
            }
        )
    return result


def parse_topic_lines(lines: Any) -> list[dict[str, Any]]:
    """解析群话题多行文本：方向名|权重|关键词1,关键词2。"""
    if isinstance(lines, str):
        lines = lines.splitlines()
    if not isinstance(lines, list):
        return []
    topics: list[dict[str, Any]] = []
    for line in lines:
        text = str(line).strip()
        if not text or text.startswith("#"):
            continue
        parts = text.split("|", 2)
        if len(parts) < 2:
            continue
        name = parts[0].strip()
        if not name:
            continue
        weight = _as_float(parts[1].strip(), 50.0)
        keywords = _split_keywords(parts[2] if len(parts) > 2 else "")
        topics.append({"name": name, "weight": weight, "keywords": keywords, "aliases": {}})
    return topics


def normalize_group_schemes(value: Any) -> dict[str, dict[str, Any]]:
    """模板列表 -> {群号: {topics: [...]}}；同时兼容旧 group_profiles 字典。"""
    if isinstance(value, dict):
        result: dict[str, dict[str, Any]] = {}
        for group_id, profile in value.items():
            if not isinstance(profile, dict):
                continue
            topics = normalize_interests(profile.get("topics", []))
            result[str(group_id)] = {"topics": topics}
        return result
    if not isinstance(value, list):
        return {}
    result = {}
    for item in value:
        if not isinstance(item, dict):
            continue
        if item.get("enabled", True) is False:
            continue
        group_id = str(item.get("group_id") or "").strip()
        if not group_id:
            continue
        result[group_id] = {"topics": parse_topic_lines(item.get("topics", []))}
    return result


def question_schemes_to_questions(value: Any) -> dict[str, dict[str, Any]] | None:
    """模板列表 -> Jev questions 字典。"""
    if not isinstance(value, list):
        return None
    questions: dict[str, dict[str, Any]] = {}
    for item in value:
        if not isinstance(item, dict):
            continue
        if item.get("enabled", True) is False:
            continue
        qid = str(item.get("id") or "").strip()
        qtype = str(item.get("type") or "").strip()
        if not qid or qtype not in {"noul", "choice", "score"}:
            continue
        question: dict[str, Any] = {
            "type": qtype,
            "instructions": str(item.get("instructions") or "").strip(),
        }
        if qtype == "noul":
            criteria = {}
            if str(item.get("true_criteria") or "").strip():
                criteria["true"] = str(item["true_criteria"]).strip()
            if str(item.get("false_criteria") or "").strip():
                criteria["false"] = str(item["false_criteria"]).strip()
            if criteria:
                question["criteria"] = criteria
        elif qtype == "score":
            levels = _split_keywords(item.get("score_levels"))
            if len(levels) >= 2:
                question["criteria"] = levels
            else:
                continue
        elif qtype == "choice":
            choices = item.get("choice_options")
            if isinstance(choices, dict) and choices:
                question["criteria"] = choices
            else:
                continue
        questions[qid] = question
    return questions or None


def profile_text(interests: list[dict[str, Any]]) -> str:
    if not interests:
        return "（未配置）"
    lines = []
    for item in interests:
        words = "、".join(item["keywords"][:12]) or "（无关键词）"
        lines.append(f"- {item['name']}（权重 {item['weight']:.0f}）：{words}")
    return "\n".join(lines)


def decide_answers(
    nouls: dict[str, float],
    score: float | None,
    continuation: bool,
    thresholds: dict[str, float] | None = None,
) -> tuple[bool, str]:
    """纯函数：把 Jev 的答案映射成 act / pass。"""

    th = dict(DEFAULT_THRESHOLDS)
    if thresholds:
        th.update({k: _as_float(v, th[k]) for k, v in thresholds.items() if k in th})

    risk = _as_float(nouls.get("is_risky"), 0.0)
    moment = _as_float(nouls.get("is_good_moment"), 0.0)
    persona = _as_float(nouls.get("is_persona_fit"), 0.0)
    group_topic = _as_float(nouls.get("is_group_topic_fit"), 0.0)
    mentioned = _as_float(nouls.get("is_mentioned"), 0.0)
    continuation_p = _as_float(nouls.get("is_continuation"), 0.0)
    impulse = _as_float(nouls.get("is_impulse"), 0.0)
    value = _as_float(score, 0.0)

    if risk >= th["is_risky"]:
        return False, f"risk({risk:.2f})"
    if moment < th["is_good_moment"]:
        return False, f"moment({moment:.2f})"

    # 第三层：突发回复欲望。不需要兴趣匹配，但风险要求更严。
    if (
        impulse >= th["is_impulse"]
        and value >= th["impulse_reply_value"]
        and risk <= th["impulse_risk"]
    ):
        return True, f"impulse({impulse:.2f}, value={value:.2f})"

    interest_ok = persona >= th["is_persona_fit"] or group_topic >= th["is_group_topic_fit"]
    if not interest_ok and mentioned < th["is_mentioned"]:
        return False, "interest_low"

    if continuation and continuation_p >= th["is_continuation"] and value >= th["continuation_reply_value"]:
        return True, f"continuation(value={value:.2f}, p={continuation_p:.2f})"
    if value >= th["reply_value"]:
        return True, f"value({value:.2f})"
    if mentioned >= th["is_mentioned"] and value >= 1.2:
        return True, f"mention(value={value:.2f})"
    return False, f"value_low({value:.2f})"


def extract_nouls(answers: dict[str, Any]) -> tuple[dict[str, float], float | None]:
    nouls: dict[str, float] = {}
    score: float | None = None
    for key, value in (answers or {}).items():
        if not isinstance(value, dict):
            continue
        if value.get("type") == "noul" or "noul" in value:
            try:
                nouls[key] = float(value.get("noul"))
            except (TypeError, ValueError):
                pass
        if value.get("type") == "score" or "score" in value:
            try:
                score = float(value.get("score"))
            except (TypeError, ValueError):
                pass
    return nouls, score


class AttentionGatePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        self._data_dir: Path | None = None
        self._states: dict[str, dict[str, Any]] = {}
        self._msg_embedding_cache: OrderedDict[str, list[float]] = OrderedDict()
        self._profile_embedding_cache: dict[str, tuple[str, list[list[float]]]] = {}
        logger.info(
            "%s v%s 已加载 | dry_run=%s | groups=%s | model=%s | endpoint=%s",
            LOG,
            VERSION,
            self._bool("dry_run", True),
            self._groups() or "全部群聊",
            str(self._cfg("model", DEFAULT_JEV_MODEL) or DEFAULT_JEV_MODEL),
            self._endpoint_url(),
        )

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------

    def _cfg(self, key: str, default: Any = None) -> Any:
        if self.config is None:
            return default
        try:
            return self.config.get(key, default)
        except Exception:
            return default

    def _bool(self, key: str, default: bool) -> bool:
        value = self._cfg(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "是", "开"}
        return default if value is None else bool(value)

    def _json_cfg(self, key: str, default: Any) -> Any:
        return _parse_json_value(self._cfg(key, default), default)

    def _endpoint_url(self) -> str:
        """接受完整 SystemOne/Decision URL，也接受服务根地址。

        NeoHorse-Jev-4B 同时提供：
        - /v1/systemone
        - /v1/decision

        如果只填 http://127.0.0.1:8080，默认补 /v1/systemone。
        """
        raw = str(self._cfg("jev_provider_url", "") or DEFAULT_JEV_URL).strip()
        url = raw.rstrip("/")
        if url.endswith("/v1/systemone") or url.endswith("/v1/decision"):
            return url
        if url.endswith("/v1"):
            return url + "/systemone"
        return url + SYSTEMONE_SUFFIX

    def _groups(self) -> set[str]:
        raw = self._cfg("enabled_groups", [])
        if not isinstance(raw, list):
            raw = [raw]
        return {str(x).strip() for x in raw if str(x).strip()}

    def _group_allowed(self, group_id: str) -> bool:
        groups = self._groups()
        return not groups or group_id in groups

    def _thresholds(self) -> dict[str, float]:
        result: dict[str, float] = {}
        scalar_keys = {
            "persona_fit_threshold": "is_persona_fit",
            "group_topic_fit_threshold": "is_group_topic_fit",
            "good_moment_threshold": "is_good_moment",
            "risk_threshold": "is_risky",
            "reply_value_threshold": "reply_value",
            "continuation_value_threshold": "continuation_reply_value",
            "mentioned_threshold": "is_mentioned",
            "impulse_threshold": "is_impulse",
            "impulse_value_threshold": "impulse_reply_value",
            "impulse_risk_max": "impulse_risk",
        }
        for field, key in scalar_keys.items():
            value = self._cfg(field, None)
            if value is not None:
                result[key] = _as_float(value, DEFAULT_THRESHOLDS[key])
        old = self._json_cfg("thresholds", {})
        if isinstance(old, dict):
            for key, value in old.items():
                result.setdefault(str(key), _as_float(value))
        return result

    def _persona(self) -> str:
        text = str(self._cfg("persona", "") or "").strip()
        return text or DEFAULT_PERSONA

    def _global_interests(self) -> list[dict[str, Any]]:
        groups = self._cfg("interest_groups", None)
        if isinstance(groups, list):
            return normalize_interest_groups(groups)
        return normalize_interests(
            self._json_cfg("global_interests", DEFAULT_GLOBAL_INTERESTS)
        )

    def _group_profile(self, group_id: str) -> dict[str, Any]:
        schemes = self._cfg("group_topic_schemes", None)
        if isinstance(schemes, list):
            profiles = normalize_group_schemes(schemes)
        else:
            profiles = self._json_cfg(
                "group_profiles", normalize_group_schemes(DEFAULT_GROUP_TOPIC_SCHEMES)
            )
        if not isinstance(profiles, dict):
            return {}
        profile = profiles.get(group_id)
        if not isinstance(profile, dict):
            return {}
        return {"topics": normalize_interests(profile.get("topics", []))}

    def _data_dir_path(self) -> Path:
        if self._data_dir is None:
            self._data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
            self._data_dir.mkdir(parents=True, exist_ok=True)
        return self._data_dir

    def _ledger(self, record: dict[str, Any]) -> None:
        try:
            path = self._data_dir_path() / f"decisions_{_dt.date.today():%Y%m%d}.jsonl"
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as exc:  # noqa: BLE001
            logger.debug("%s 写入决策日志失败: %s", LOG, exc)

    # ------------------------------------------------------------------
    # 缓冲
    # ------------------------------------------------------------------

    def _state(self, group_id: str) -> dict[str, Any]:
        state = self._states.get(group_id)
        if state is None:
            state = {
                "buffer": deque(maxlen=BUFFER_MAX),
                "cooldown_until": 0.0,
                "dispatches": deque(maxlen=64),
                "last_bot_at": 0.0,
                "last_bot_sender": "",
                "last_dispatch_sender": "",
                "last_proactive_at": 0.0,
                "messages_since_proactive": 0,
                "last_proactive_got_reply": False,
                "awaiting_reply": False,
                "created_at": time.time(),
            }
            self._states[group_id] = state
        return state

    def _record(self, event: AstrMessageEvent, group_id: str) -> dict[str, Any] | None:
        message_obj = getattr(event, "message_obj", None)
        raw = getattr(message_obj, "raw_message", {}) or {}
        if not isinstance(raw, dict):
            raw = {}
        message_id = str(
            getattr(message_obj, "message_id", "")
            or raw.get("message_id")
            or raw.get("message_seq")
            or ""
        )
        if not message_id:
            return None
        sender_id = str(event.get_sender_id() or "")
        sender_name = event.get_sender_name() or sender_id
        text = self._flatten(event)
        if not text:
            text = "[非文本消息]"
        reply_to = ""
        for comp in event.get_messages() or []:
            if isinstance(comp, Reply):
                reply_to = str(getattr(comp, "id", "") or "")
                break
        return {
            "ts": time.time(),
            "time": _now_text(),
            "group_id": group_id,
            "message_id": message_id,
            "message_seq": str(raw.get("message_seq") or raw.get("real_seq") or ""),
            "sender_id": sender_id,
            "sender_name": sender_name,
            "content": text,
            "reply_to": reply_to,
            "is_self": sender_id == str(event.get_self_id() or ""),
            "is_bot": False,
        }

    def _append_buffer(self, group_id: str, record: dict[str, Any]) -> None:
        self._state(group_id)["buffer"].append(record)

    def _trim_dispatches(self, state: dict[str, Any], now: float) -> None:
        dispatches = state["dispatches"]
        while dispatches and now - dispatches[0] >= HOUR_SECONDS:
            dispatches.popleft()

    def _in_cooldown(self, state: dict[str, Any], now: float) -> bool:
        return now < _as_float(state.get("cooldown_until"), 0.0)

    def _hour_limit_reached(self, state: dict[str, Any], now: float) -> bool:
        self._trim_dispatches(state, now)
        limit = _as_int(self._cfg("hourly_limit", 15), 15, 1)
        return len(state["dispatches"]) >= limit

    # ------------------------------------------------------------------
    # 事件判断
    # ------------------------------------------------------------------

    @staticmethod
    def _flatten(event: AstrMessageEvent) -> str:
        parts: list[str] = []
        try:
            components = event.get_messages() or []
        except Exception:
            components = []
        for comp in components:
            if isinstance(comp, Plain):
                if comp.text:
                    parts.append(str(comp.text))
            elif isinstance(comp, At):
                parts.append(f"@{comp.qq}")
            elif isinstance(comp, Image):
                parts.append("[图片]")
            elif isinstance(comp, Reply):
                target = getattr(comp, "sender_id", "") or getattr(comp, "id", "")
                parts.append(f"[引用:{target}]")
        text = "".join(parts).strip()
        if not text:
            text = str(getattr(event, "message_str", "") or "").strip()
        return text[:2000]

    @staticmethod
    def _direct_to_bot(event: AstrMessageEvent, self_id: str) -> bool:
        if getattr(event, "is_at_or_wake_command", False):
            return True
        try:
            for comp in event.get_messages() or []:
                if isinstance(comp, At) and str(comp.qq) == self_id:
                    return True
                if isinstance(comp, Reply) and str(getattr(comp, "sender_id", "")) == self_id:
                    return True
        except Exception:
            return False
        return False

    @staticmethod
    def _directed_elsewhere(event: AstrMessageEvent, self_id: str) -> bool:
        try:
            for comp in event.get_messages() or []:
                if isinstance(comp, At) and str(comp.qq) != self_id:
                    return True
                if isinstance(comp, Reply) and str(getattr(comp, "sender_id", "")) != self_id:
                    return True
        except Exception:
            return False
        return False

    def _continuation(self, state: dict[str, Any], sender_id: str) -> bool:
        last = _as_float(state.get("last_bot_at"), 0.0)
        if not last:
            return False
        window = _as_int(self._cfg("continuation_window_seconds", 300), 300, 0)
        if window and time.time() - last > window:
            return False
        last_sender = str(state.get("last_bot_sender") or "")
        return bool(last_sender and last_sender == sender_id)

    # ------------------------------------------------------------------
    # Jev
    # ------------------------------------------------------------------

    def _build_state(
        self,
        event: AstrMessageEvent,
        group_id: str,
        state: dict[str, Any],
        target: dict[str, Any],
    ) -> dict[str, Any]:
        window = _as_int(self._cfg("context_window", 20), 20, 1)
        entries = list(state["buffer"])[-window:]
        lines: list[str] = []
        for item in entries:
            name = item.get("sender_name") or item.get("sender_id") or "?"
            if item.get("is_self"):
                name = f"{name}(我)"
            elif item.get("is_bot"):
                name = f"{name}(机器人)"
            lines.append(
                f"[#{item.get('message_id')}] {item.get('time')} {name}: {item.get('content')}"
            )
        continuation = self._continuation(state, str(event.get_sender_id() or ""))
        now = time.time()
        last_proactive = _as_float(state.get("last_proactive_at"), 0.0)
        since_last = now - (last_proactive or _as_float(state.get("created_at"), now))
        return {
            "bot_persona": self._persona(),
            "global_interest_profile": profile_text(self._global_interests()),
            "group_id": group_id,
            "group_topic_profile": profile_text(
                self._group_profile(group_id).get("topics", [])
            ),
            "recent_conversation": lines,
            "target_message": {
                "message_id": target.get("message_id", ""),
                "sender": target.get("sender_name", ""),
                "content": target.get("content", ""),
            },
            "continuation": "yes" if continuation else "no",
            "idle_state": {
                "seconds_since_last_proactive": max(0, int(since_last)),
                "messages_since_last_proactive": _as_int(
                    state.get("messages_since_proactive"), 0, 0
                ),
                "last_proactive_got_reply": bool(
                    state.get("last_proactive_got_reply", False)
                ),
            },
        }

    def _questions(self, self_id: str, group_id: str = "") -> dict[str, Any]:
        schemes = self._cfg("judge_questions", None)
        built = question_schemes_to_questions(schemes)
        if built:
            raw = built
        elif isinstance(schemes, list):
            raw = {}
        else:
            raw = self._json_cfg("questions", DEFAULT_QUESTIONS)
            if not isinstance(raw, dict):
                raw = DEFAULT_QUESTIONS
        raw = json.loads(json.dumps(raw))
        if not self._bool("impulse_enabled", True):
            raw.pop("is_impulse", None)
        # 未配置当前群话题层时，不把群话题判断送给 Jev，避免把“未配置”
        # 误判成“匹配”或“不匹配”；此时只看人格兴趣层。
        if group_id and not self._group_profile(group_id).get("topics"):
            raw.pop("is_group_topic_fit", None)
        return json.loads(json.dumps(raw).replace("{self_id}", self_id))

    def _call_jev_sync(self, state: dict[str, Any], questions: dict[str, Any]) -> dict[str, Any]:
        url = self._endpoint_url()
        api_key = str(self._cfg("api_key", "") or "").strip()
        # NeoHorse-Jev-4B 的 SystemOne 接口以文本 state 为主；
        # TypeSafe 也接受文本。默认转成 JSON 文本，兼容性最好。
        state_payload: Any = state
        if self._bool("state_as_text", True):
            state_payload = json.dumps(state, ensure_ascii=False, indent=2)
        body = json.dumps(
            {
                "model": str(self._cfg("model", DEFAULT_JEV_MODEL) or DEFAULT_JEV_MODEL),
                "state": state_payload,
                "questions": questions,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "astrbot-plugin-attention-gate/0.1.0",
        }
        # NeoHorse 本地服务默认无鉴权；只有配置了 key 才发送 Authorization。
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        last_error: Exception | None = None
        for attempt in range(3):
            request = urllib.request.Request(
                url,
                data=body,
                headers=headers,
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                body = ""
                try:
                    body = exc.read().decode("utf-8", "replace")[:300]
                except Exception:
                    pass
                last_error = RuntimeError(
                    f"HTTP {exc.code}: {body or exc.reason}"
                )
                # 平台文档：429 容量限流，502/503/504 上游或依赖故障可重试；
                # 401/403/413/422 等参数/权限错误不重试。
                if exc.code in (429, 502, 503, 504, 529):
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise last_error from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                last_error = exc
                # 平台明确提示：超时请求不要无条件重试，直接 fail closed。
                raise
        raise RuntimeError(f"Jev 请求失败: {last_error}")

    async def _call_jev(self, state: dict[str, Any], questions: dict[str, Any]) -> dict[str, Any]:
        timeout = _as_float(self._cfg("jev_timeout_seconds", 25.0), 25.0)
        return await asyncio.wait_for(
            asyncio.to_thread(self._call_jev_sync, state, questions),
            timeout=max(3.0, timeout),
        )

    def _profile_docs(self, group_id: str) -> list[str]:
        docs: list[str] = []
        for item in self._global_interests():
            words = "、".join(item.get("keywords", [])[:20])
            docs.append(f"{item['name']}：{words}")
        for item in self._group_profile(group_id).get("topics", []):
            words = "、".join(item.get("keywords", [])[:20])
            docs.append(f"当前群话题 {item['name']}：{words}")
        return [x for x in docs if x.strip()]

    def _keyword_hit(self, group_id: str, text: str) -> bool:
        lowered = (text or "").lower()
        if not lowered:
            return False
        interests = self._global_interests() + self._group_profile(group_id).get(
            "topics", []
        )
        for item in interests:
            for word in item.get("keywords", []):
                word = str(word).strip()
                if word and word.lower() in lowered:
                    return True
        return False

    def _embedding_provider(self):
        provider_id = str(
            self._cfg("embedding_provider_id", "dashscope_embedding") or ""
        ).strip()
        if not provider_id:
            return None
        provider = self.context.get_provider_by_id(provider_id)
        if provider is None or not hasattr(provider, "get_embedding"):
            return None
        return provider

    async def _get_message_embedding(self, text: str) -> list[float] | None:
        key = hashlib.sha1(text.encode("utf-8")).hexdigest()
        cached = self._msg_embedding_cache.get(key)
        if cached is not None:
            self._msg_embedding_cache.move_to_end(key)
            return cached
        provider = self._embedding_provider()
        if provider is None:
            return None
        try:
            vector = await asyncio.wait_for(
                provider.get_embedding(text),
                timeout=float(self._cfg("embedding_timeout_seconds", 8.0) or 8.0),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("%s 获取消息 embedding 失败: %s", LOG, exc)
            return None
        if not isinstance(vector, list) or not vector:
            return None
        self._msg_embedding_cache[key] = vector
        while len(self._msg_embedding_cache) > 256:
            self._msg_embedding_cache.popitem(last=False)
        return vector

    async def _get_profile_embeddings(
        self, group_id: str, docs: list[str]
    ) -> list[list[float]] | None:
        signature = json.dumps(docs, ensure_ascii=False)
        cached = self._profile_embedding_cache.get(group_id)
        if cached and cached[0] == signature:
            return cached[1]
        provider = self._embedding_provider()
        if provider is None:
            return None
        try:
            vectors = await asyncio.wait_for(
                provider.get_embeddings(docs),
                timeout=float(self._cfg("embedding_timeout_seconds", 8.0) or 8.0),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("%s 获取兴趣 embedding 失败: %s", LOG, exc)
            return None
        if not isinstance(vectors, list) or len(vectors) != len(docs):
            return None
        self._profile_embedding_cache[group_id] = (signature, vectors)
        return vectors

    async def _rerank_score(self, text: str, docs: list[str]) -> float | None:
        provider_id = str(
            self._cfg("rerank_provider_id", "bailian_rerank") or ""
        ).strip()
        if not provider_id:
            return None
        provider = self.context.get_provider_by_id(provider_id)
        if provider is None or not hasattr(provider, "rerank"):
            return None
        try:
            results = await asyncio.wait_for(
                provider.rerank(text, docs, top_n=1),
                timeout=float(self._cfg("rerank_timeout_seconds", 10.0) or 10.0),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("%s rerank 失败: %s", LOG, exc)
            return None
        if not results:
            return None
        try:
            return max(float(item.relevance_score) for item in results)
        except (TypeError, ValueError, AttributeError):
            return None

    async def _prefilter(
        self, group_id: str, record: dict[str, Any]
    ) -> tuple[bool, str]:
        if not self._bool("prefilter_enabled", True):
            return True, "prefilter_disabled"
        text = str(record.get("content") or "")
        if text in _MEDIA_TEXT:
            return True, "media_pass"
        if self._keyword_hit(group_id, text):
            return True, "keyword_hit"

        docs = self._profile_docs(group_id)
        if not docs:
            return True, "no_profiles"

        msg_vector = await self._get_message_embedding(text)
        profile_vectors = await self._get_profile_embeddings(group_id, docs)
        if msg_vector is None or not profile_vectors:
            # 预筛依赖的模型不可用时，不牺牲主判断：交给 Jev，并记录原因。
            return True, "prefilter_unavailable"

        score = max(cosine_similarity(msg_vector, vec) for vec in profile_vectors)
        high = _as_float(self._cfg("embedding_threshold", 0.42), 0.42)
        low = _as_float(self._cfg("embedding_low_threshold", 0.30), 0.30)

        if score >= high:
            return True, f"embedding_high({score:.2f})"
        if score < low and self._bool("impulse_enabled", True) and impulse_cue(text):
            return True, f"impulse_cue_bypass({score:.2f})"
        if score < low:
            return False, f"prefilter_low({score:.2f})"

        rerank = await self._rerank_score(text, docs)
        if rerank is None:
            return True, f"embedding_borderline_no_rerank({score:.2f})"
        threshold = _as_float(self._cfg("rerank_threshold", 0.30), 0.30)
        if rerank >= threshold:
            return True, f"rerank_high({rerank:.2f})"
        if self._bool("impulse_enabled", True) and impulse_cue(text):
            return True, f"impulse_cue_bypass(rerank={rerank:.2f})"
        return False, f"rerank_low({rerank:.2f})"

    async def _judge(
        self,
        event: AstrMessageEvent,
        group_id: str,
        state: dict[str, Any],
        target: dict[str, Any],
    ) -> tuple[bool, str, dict[str, Any]]:
        self_id = str(event.get_self_id() or "")
        questions = self._questions(self_id, group_id)
        if not questions:
            return False, "no_questions", {}
        state_view = self._build_state(event, group_id, state, target)
        try:
            response = await self._call_jev(
                state_view,
                questions,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s Jev 判断失败，直接 pass: %s", LOG, exc)
            return False, f"jev_error:{type(exc).__name__}", {}
        answers = response.get("answers", {}) if isinstance(response, dict) else {}
        if not isinstance(answers, dict):
            return False, "jev_bad_response", {}
        nouls, score = extract_nouls(answers)
        continuation = self._continuation(state, str(event.get_sender_id() or ""))
        act, reason = decide_answers(nouls, score, continuation, self._thresholds())
        return act, reason, {
            "nouls": nouls,
            "reply_value": score,
            "idle": state_view.get("idle_state", {}),
            "answers": answers,
        }

    # ------------------------------------------------------------------
    # 触发
    # ------------------------------------------------------------------

    async def _current_conversation(self, event: AstrMessageEvent):
        manager = getattr(self.context, "conversation_manager", None)
        if manager is None:
            return None, ""
        try:
            cid = await manager.get_curr_conversation_id(event.unified_msg_origin)
            if not cid:
                cid = await manager.new_conversation(
                    event.unified_msg_origin, event.get_platform_id()
                )
            if not cid:
                return None, ""
            conversation = await manager.get_conversation(event.unified_msg_origin, cid)
            return conversation, str(cid)
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s 获取对话失败: %s", LOG, exc)
            return None, ""

    def _use_target_only_prompt(self) -> bool:
        mode = str(self._cfg("prompt_context_mode", "auto") or "auto").strip()
        if mode == "target_only":
            return True
        if mode == "full":
            return False
        # auto: 运行目录中存在 context_aware 时，上下文交给它注入，避免重复。
        try:
            return (Path(__file__).resolve().parent.parent / "astrbot_plugin_context_aware").is_dir()
        except Exception:
            return False

    def _dispatch_prompt(
        self, group_id: str, state: dict[str, Any], target: dict[str, Any], reason: str
    ) -> str:
        window = _as_int(self._cfg("context_window", 20), 20, 1)
        lines = []
        for item in list(state["buffer"])[-window:]:
            name = item.get("sender_name") or item.get("sender_id") or "?"
            if item.get("is_self"):
                name = f"{name}(我)"
            lines.append(f"[#{item.get('message_id')}] {name}: {item.get('content')}")
        prompt = (
            f"这是你主动决定接的一句话，判断依据：{reason}。\n"
            f"目标消息 ID：{target.get('message_id')}，发送者：{target.get('sender_name')}。\n"
            f"目标内容：{target.get('content')}\n"
        )
        if self._use_target_only_prompt():
            prompt += (
                "群聊场景和最近对话由 Context Aware 负责注入；"
                "你只需要针对目标消息自然接一句。\n"
            )
        else:
            prompt += "最近群聊：\n" + "\n".join(lines) + "\n"
        return prompt + "不要长篇解释；没有表达欲就调用 keep_silent。"

    async def on_group_message(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[Any, None]:
        if not self._bool("enable", True):
            return
        if event.is_stopped():
            # quiet_hours / 黑名单 / 其他前置插件已经拦停，不再判断。
            return
        if event.is_private_chat():
            return
        group_id = str(event.get_group_id() or "")
        if not group_id or not self._group_allowed(group_id):
            return
        self_id = str(event.get_self_id() or "")
        sender_id = str(event.get_sender_id() or "")
        if not self_id or sender_id == self_id:
            return

        record = self._record(event, group_id)
        if record is None:
            return
        state = self._state(group_id)
        state["messages_since_proactive"] = _as_int(
            state.get("messages_since_proactive"), 0, 0
        ) + 1
        if state.get("awaiting_reply") and sender_id != self_id:
            state["last_proactive_got_reply"] = True
            state["awaiting_reply"] = False
        if self._direct_to_bot(event, self_id):
            # 明确对 Bot 说话：仍然进缓冲，正常回复交给 AstrBot 主线。
            self._append_buffer(group_id, record)
            return

        other_bots = {str(x) for x in (self._cfg("other_bot_ids", []) or [])}
        if sender_id in other_bots:
            record["is_bot"] = True
            self._append_buffer(group_id, record)
            return
        if self._directed_elsewhere(event, self_id) and self._bool(
            "skip_directed_messages", True
        ):
            self._append_buffer(group_id, record)
            return

        self._append_buffer(group_id, record)
        now = time.time()
        if self._in_cooldown(state, now):
            return
        if self._hour_limit_reached(state, now):
            return

        target = record
        prefilter_ok, prefilter_reason = await self._prefilter(group_id, record)
        if not prefilter_ok:
            self._ledger(
                {
                    "ts": time.time(),
                    "time": _now_text(),
                    "group": group_id,
                    "sender": record["sender_name"],
                    "sender_id": record["sender_id"],
                    "message_id": record["message_id"],
                    "content": record["content"],
                    "reason": prefilter_reason,
                    "act": False,
                    "dry_run": self._bool("dry_run", True),
                    "prefilter": prefilter_reason,
                }
            )
            return

        act, reason, detail = await self._judge(event, group_id, state, target)
        dry_run = self._bool("dry_run", True)
        ledger = {
            "ts": time.time(),
            "time": _now_text(),
            "group": group_id,
            "sender": record["sender_name"],
            "sender_id": record["sender_id"],
            "message_id": record["message_id"],
            "content": record["content"],
            "reason": reason,
            "act": act,
            "dry_run": dry_run,
            "prefilter": prefilter_reason,
            "judge": detail,
        }
        if not act:
            self._ledger(ledger)
            return
        if dry_run:
            ledger["action"] = "dry_run"
            self._ledger(ledger)
            logger.info("%s dry-run act: %s | %s", LOG, record["content"][:50], reason)
            return

        conversation, _cid = await self._current_conversation(event)
        if conversation is None:
            ledger["action"] = "skip_no_conversation"
            self._ledger(ledger)
            return

        state["cooldown_until"] = now + max(0, _as_int(self._cfg("cooldown_seconds", 120), 120, 0))
        state["dispatches"].append(now)
        state["last_dispatch_sender"] = record["sender_id"]
        state["last_proactive_at"] = now
        state["messages_since_proactive"] = 0
        state["last_proactive_got_reply"] = False
        state["awaiting_reply"] = True
        state["last_target_sender"] = record["sender_id"]

        path = reason.split("(", 1)[0]
        trigger_payload = {
            "plugin": PLUGIN_NAME,
            "reason": reason,
            "path": path,
            "target_message_id": str(record["message_id"]),
            "prefilter": prefilter_reason,
        }
        event.set_extra(TRIGGER_MARK_EXTRA, trigger_payload)
        event.set_extra("active_reply_triggered", trigger_payload)
        if self._bool("quote_target_enabled", True):
            event.set_extra(TRIGGER_QUOTE_EXTRA, str(record["message_id"]))

        prompt = self._dispatch_prompt(group_id, state, target, reason)
        ledger["action"] = "dispatch"
        ledger["cooldown_until"] = state["cooldown_until"]
        self._ledger(ledger)
        logger.info(
            "%s 触发主动回复: %s | %s | target=%s",
            LOG,
            record["sender_name"],
            reason,
            record["message_id"],
        )
        yield event.request_llm(
            prompt=prompt,
            session_id=event.session_id,
            conversation=conversation,
        )

    # ------------------------------------------------------------------
    # 机器人自己的发言写回缓冲，供 continuation 判断
    # ------------------------------------------------------------------

    @filter.on_decorating_result(priority=110000)
    async def force_quote_target(self, event: AstrMessageEvent) -> None:
        """让主动回复自带引用能力，不再强依赖第三方引用插件补丁。"""
        if not self._bool("enable", True) or not self._bool(
            "quote_target_enabled", True
        ):
            return
        target = str(event.get_extra(TRIGGER_QUOTE_EXTRA) or "").strip()
        if not target:
            return
        result = event.get_result()
        chain = getattr(result, "chain", None) if result else None
        if not chain:
            return
        if any(isinstance(comp, Reply) for comp in chain):
            return
        chain.insert(0, Reply(id=target, chain=[]))

    @filter.after_message_sent()
    async def on_after_message_sent(self, event: AstrMessageEvent) -> None:
        if not self._bool("enable", True) or event.is_private_chat():
            return
        if not (
            event.get_extra(TRIGGER_MARK_EXTRA)
            or getattr(event, "__is_llm_reply", False)
        ):
            return
        group_id = str(event.get_group_id() or "")
        if not group_id or not self._group_allowed(group_id):
            return
        result = event.get_result()
        chain = getattr(result, "chain", None) if result else None
        if not chain:
            return
        text_parts = [
            str(comp.text)
            for comp in chain
            if isinstance(comp, Plain) and getattr(comp, "text", "")
        ]
        text = "".join(text_parts).strip()
        if not text:
            return
        state = self._state(group_id)
        record = {
            "ts": time.time(),
            "time": _now_text(),
            "group_id": group_id,
            "message_id": "",
            "sender_id": str(event.get_self_id() or ""),
            "sender_name": "鲸鲸",
            "content": text[:2000],
            "reply_to": "",
            "is_self": True,
            "is_bot": False,
        }
        state["buffer"].append(record)
        state["last_bot_at"] = time.time()
        state["last_bot_sender"] = str(
            event.get_sender_id()
            or state.get("last_dispatch_sender")
            or ""
        )

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=-2)
    async def on_group_message_entry(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[Any, None]:
        async for request in self.on_group_message(event):
            yield request
