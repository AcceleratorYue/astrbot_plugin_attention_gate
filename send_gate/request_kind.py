"""面向用户的请求分类。

dry-run 阶段只做观察，分类结果用于统计，不参与真实放行。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

PROACTIVE = "proactive"
POKE = "poke"
FOLLOWUP = "followup"
CHAT = "chat"
CHAT_PRIVATE = "chat_private"
INTERNAL = "internal"
UNKNOWN = "unknown"

USER_FACING_KINDS = {PROACTIVE, POKE, FOLLOWUP, CHAT, CHAT_PRIVATE}


@dataclass(frozen=True)
class RequestInfo:
    kind: str
    source: str
    sender_id: str
    umo: str
    message_id: str
    is_private: bool
    has_message: bool
    text: str = ""


def _safe_extra(event: Any, key: str, default: Any = None) -> Any:
    try:
        return event.get_extra(key, default)
    except Exception:
        return default


def _safe_call(event: Any, name: str, default: Any = None) -> Any:
    try:
        value = getattr(event, name)
        return value() if callable(value) else value
    except Exception:
        return default


def classify_request(event: Any) -> RequestInfo:
    """按额外标记和会话类型判断请求来源。

    这里不追求覆盖所有内部请求，只保证：
    - 主动接话 / poke / follow-up / 普通聊天能区分；
    - 明显没有 event/sender 的调用标记为 internal；
    - 其余标记为 unknown，后续用真实日志再校准。
    """

    if event is None:
        return RequestInfo(
            kind=INTERNAL,
            source="no_event",
            sender_id="",
            umo="",
            message_id="",
            is_private=False,
            has_message=False,
            text="",
        )

    sender_id = str(_safe_call(event, "get_sender_id", "") or "")
    umo = str(getattr(event, "unified_msg_origin", "") or "")
    message_obj = getattr(event, "message_obj", None)
    message_id = str(getattr(message_obj, "message_id", "") or "")
    has_message = message_obj is not None
    is_private = bool(_safe_call(event, "is_private_chat", False))
    text = str(_safe_call(event, "message_str", "") or "")

    if _safe_extra(event, "active_reply_triggered") or _safe_extra(
        event, "attention_gate_triggered"
    ):
        kind, source = PROACTIVE, "attention_gate"
    elif _safe_extra(event, "_poke_trigger", False):
        kind, source = POKE, "poke"
    elif _safe_extra(event, "_follow_up_captured") is not None:
        kind, source = FOLLOWUP, "followup"
    elif _safe_extra(event, "_send_gate_internal", False):
        kind, source = INTERNAL, "internal_mark"
    elif not has_message or not sender_id:
        kind, source = INTERNAL, "no_message_or_sender"
    elif is_private:
        kind, source = CHAT_PRIVATE, "private"
    else:
        kind, source = CHAT, "group"

    return RequestInfo(
        kind=kind,
        source=source,
        sender_id=sender_id,
        umo=umo,
        message_id=message_id,
        is_private=is_private,
        has_message=has_message,
        text=text,
    )
