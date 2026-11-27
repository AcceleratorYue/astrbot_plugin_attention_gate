"""统一消息发送门控。

dry_run=true：
- 只记录，不 stop、不 sleep、不改 req。

send_gate_enforce=true：
- 普通聊天/私聊/主动回复/poke/follow-up 进入真实窗口
- 群聊普通后续消息由群消息事件缓冲，不要求它自己产生 LLM 请求
- root 在 on_llm_request 中等待安静窗口
- follower 在 on_waiting_llm_request 中被 stop
- root 合并 follower 文本/图片后释放原请求
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as _dt
import json
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Callable

from .request_kind import (
    CHAT,
    CHAT_PRIVATE,
    PROACTIVE,
    USER_FACING_KINDS,
    RequestInfo,
    classify_request,
)

ROOT_EXTRA = "_send_gate_root_token"
ENFORCE_KINDS = USER_FACING_KINDS
COMPLETENESS_KINDS = {CHAT, CHAT_PRIVATE}


@dataclasses.dataclass
class _Buffered:
    text: str
    images: list[str]
    message_id: str
    ts: float


@dataclasses.dataclass
class _Lane:
    key: str
    first_at: float
    last_at: float
    count: int
    root_message_id: str
    last_kind: str
    token: str = ""
    messages: list[_Buffered] = dataclasses.field(default_factory=list)
    root_text: str = ""
    root_kind: str = ""
    root_images: list[str] = dataclasses.field(default_factory=list)
    onnx_score: float | None = None
    jev_score: float | None = None
    jev_checked: bool = False
    root_seen_llm: bool = False


class SendGateObserver:
    def __init__(
        self,
        *,
        config: Any,
        data_dir_getter: Callable[[], Path],
        logger: Any,
        config_getter: Callable[[str, Any], Any] | None = None,
        onnx: Any = None,
        completeness_getter: Callable[[str], Any] | None = None,
        revalidate_getter: Callable[[str, str], Any] | None = None,
    ) -> None:
        self._config = config
        self._config_getter = config_getter
        self._data_dir_getter = data_dir_getter
        self._logger = logger
        self._onnx = onnx
        self._completeness_getter = completeness_getter
        self._revalidate_getter = revalidate_getter
        self._lanes: dict[str, _Lane] = {}
        self._recent: dict[str, deque[_Buffered]] = {}
        self._non_dry_run_warned = False

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------

    def _cfg(self, key: str, default: Any = None) -> Any:
        if self._config_getter is not None:
            try:
                return self._config_getter(key, default)
            except Exception:
                return default
        try:
            return self._config.get(key, default)
        except Exception:
            return default

    def _bool(self, key: str, default: bool) -> bool:
        value = self._cfg(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "是", "开"}
        return default if value is None else bool(value)

    def _int(self, key: str, default: int, minimum: int = 0) -> int:
        try:
            return max(minimum, int(self._cfg(key, default)))
        except (TypeError, ValueError, OverflowError):
            return default

    def _observe_groups(self) -> set[str]:
        raw = self._cfg("send_gate_enabled_groups", [])
        if not isinstance(raw, list):
            raw = [raw]
        return {str(item).strip() for item in raw if str(item).strip()}

    def _enforce_groups(self) -> set[str]:
        raw = self._cfg("send_gate_enforce_groups", [])
        if not isinstance(raw, list):
            raw = [raw]
        return {str(item).strip() for item in raw if str(item).strip()}

    def _base_enabled(self) -> bool:
        return self._bool("send_gate_enable", False)

    def _dryrun_enabled(self) -> bool:
        return self._bool("send_gate_dry_run", True)

    def _enforce_enabled(self) -> bool:
        return self._bool("send_gate_enforce", False)

    def _observe_for(self, event: Any, info: RequestInfo) -> bool:
        if not self._base_enabled():
            return False
        if info.is_private and not self._bool(
            "send_gate_observe_private", True
        ):
            return False
        if not info.is_private:
            groups = self._observe_groups()
            if groups and self._safe_group_id(event) not in groups:
                return False
        return True

    def _enforce_for(self, event: Any, info: RequestInfo) -> bool:
        if not self._base_enabled() or not self._enforce_enabled():
            return False
        # dry-run 优先级高于 enforce：只观察，绝不 stop/延迟。
        if self._dryrun_enabled():
            return False
        if info.kind not in ENFORCE_KINDS:
            return False
        if info.is_private and not self._bool(
            "send_gate_enforce_private", False
        ):
            return False
        if not info.is_private:
            groups = self._enforce_groups()
            if groups and self._safe_group_id(event) not in groups:
                return False
        return True

    # ------------------------------------------------------------------
    # 观察 / 执行入口
    # ------------------------------------------------------------------

    async def observe_waiting(self, event: Any) -> None:
        try:
            info = classify_request(event)
            if not self._base_enabled():
                return
            if not self._observe_for(event, info) and not self._enforce_for(
                event, info
            ):
                return
            if info.kind not in USER_FACING_KINDS:
                if self._observe_for(event, info):
                    self._record(
                        stage="waiting",
                        action="observe_ignore",
                        info=info,
                        window_count=0,
                        window_age_ms=0,
                        would_wait_ms=0,
                    )
                return

            enforce = self._enforce_for(event, info)
            now = time.time()
            self._cleanup(now)
            key = self._lane_key(info)
            window = self._int("send_gate_max_window_seconds", 20, 1)
            lane = self._lanes.get(key)
            active = (
                lane is not None
                and now - lane.first_at <= window
            )
            if enforce:
                if active and lane is not None and info.kind not in COMPLETENESS_KINDS:
                    # poke/proactive/follow-up 不混入普通聊天窗口，也不 stop；
                    # 让它作为独立请求等待 session lock。
                    self._record(
                        stage="waiting",
                        action="bypass_nonmergeable",
                        info=info,
                        window_count=lane.count,
                        window_age_ms=max(0, int((now - lane.first_at) * 1000)),
                        would_wait_ms=0,
                        enforce=True,
                    )
                    return
                if active and lane is not None:
                    lane.last_kind = info.kind
                    self._append_follower(
                        lane,
                        _Buffered(
                            text=info.text[:1000],
                            images=self._image_sources(event),
                            message_id=info.message_id,
                            ts=now,
                        ),
                    )
                    try:
                        event.stop_event()
                    except Exception:
                        pass
                    action = "merged_stop"
                else:
                    token = uuid.uuid4().hex
                    lane = _Lane(
                        key=key,
                        first_at=now,
                        last_at=now,
                        count=1,
                        root_message_id=info.message_id,
                        last_kind=info.kind,
                        token=token,
                        root_text=info.text,
                        root_kind=info.kind,
                        root_images=self._image_sources(event),
                    )
                    for item in self._recent_after(key, info.message_id):
                        self._append_follower(lane, item)
                    self._lanes[key] = lane
                    try:
                        event.set_extra(ROOT_EXTRA, token)
                    except Exception:
                        pass
                    action = "root_enforce"
                self._record(
                    stage="waiting",
                    action=action,
                    info=info,
                    window_count=lane.count,
                    window_age_ms=max(0, int((now - lane.first_at) * 1000)),
                    would_wait_ms=0,
                    enforce=True,
                )
                return

            # 非强制类型：保留 dry-run 观察
            if not self._observe_for(event, info):
                return
            if active and lane is not None:
                lane.count += 1
                lane.last_at = now
                lane.last_kind = info.kind
                action = "would_merge"
            else:
                lane = _Lane(
                    key=key,
                    first_at=now,
                    last_at=now,
                    count=1,
                    root_message_id=info.message_id,
                    last_kind=info.kind,
                )
                self._lanes[key] = lane
                action = "would_root"
            self._record(
                stage="waiting",
                action=action,
                info=info,
                window_count=lane.count,
                window_age_ms=max(0, int((now - lane.first_at) * 1000)),
                would_wait_ms=0,
            )
        except Exception as exc:  # noqa: BLE001
            self._logger.debug("[send_gate] observe_waiting 失败: %s", exc)

    async def observe_group_message(self, event: Any, accept: Any = None) -> None:
        """记录所有群消息，供 root 在等待期间合并。

        群聊里的后续消息通常不会单独产生 LLM 请求，因此不能只依赖
        on_waiting_llm_request。这里在消息事件阶段先把它们放进 recent
        buffer；root 建立窗口时再取走 root 之后的补充消息。
        """
        try:
            if not self._base_enabled():
                return
            if not self._enforce_enabled() and not self._dryrun_enabled():
                return
            info = classify_request(event)
            if info.is_private or info.kind not in USER_FACING_KINDS:
                return
            if accept is not None:
                try:
                    if not accept(event):
                        return
                except Exception:
                    return
            now = time.time()
            self._cleanup(now)
            key = self._lane_key(info)
            item = _Buffered(
                text=info.text[:1000],
                images=self._image_sources(event),
                message_id=info.message_id,
                ts=now,
            )
            recent = self._recent.get(key)
            if recent is None:
                recent = deque(maxlen=50)
                self._recent[key] = recent
            recent.append(item)
            lane = self._lanes.get(key)
            if lane is not None and now - lane.first_at <= self._int(
                "send_gate_max_window_seconds", 20, 1
            ):
                self._append_follower(lane, item)
                self._record(
                    stage="waiting",
                    action="group_buffered",
                    info=info,
                    window_count=lane.count,
                    window_age_ms=max(0, int((now - lane.first_at) * 1000)),
                    would_wait_ms=0,
                    enforce=True,
                )
        except Exception as exc:  # noqa: BLE001
            self._logger.debug("[send_gate] observe_group_message 失败: %s", exc)

    async def observe_llm(self, event: Any, req: Any) -> None:
        try:
            info = classify_request(event)
            if not self._base_enabled():
                return
            if not self._observe_for(event, info) and not self._enforce_for(
                event, info
            ):
                return
            enforce = self._enforce_for(event, info)
            now = time.time()
            self._cleanup(now)
            key = self._lane_key(info)
            lane = self._lanes.get(key)

            if enforce:
                token = ""
                try:
                    token = str(event.get_extra(ROOT_EXTRA, "") or "")
                except Exception:
                    token = ""
                if lane is None or not token or lane.token != token:
                    # 根请求没有建立 lane（可能被其他插件 stop），不阻塞。
                    self._record(
                        stage="llm",
                        action="real_release_unmanaged",
                        info=info,
                        window_count=lane.count if lane else 1,
                        window_age_ms=(
                            max(0, int((now - lane.first_at) * 1000))
                            if lane
                            else 0
                        ),
                        would_wait_ms=0,
                        prompt_chars=len(str(getattr(req, "prompt", "") or "")),
                        image_count=len(getattr(req, "image_urls", None) or ()),
                        enforce=True,
                    )
                    return

                lane.root_seen_llm = True
                waited_ms = await self._wait_for_window(lane)
                if (
                    lane.root_kind == PROACTIVE
                    and lane.messages
                    and self._revalidate_getter is not None
                    and self._bool("send_gate_revalidate_proactive", True)
                ):
                    keep = None
                    remaining = max(0.0, self._deadline(lane) - time.time())
                    if remaining > 0:
                        try:
                            keep = await asyncio.wait_for(
                                self._revalidate_getter(
                                    lane.root_text, self._lane_text(lane)
                                ),
                                timeout=remaining,
                            )
                        except Exception:
                            keep = None
                    policy = str(
                        self._cfg(
                            "send_gate_revalidate_failure_policy",
                            "keep_with_guard",
                        )
                        or "keep_with_guard"
                    )
                    if keep is None and policy == "retry_once_then_drop":
                        remaining = max(0.0, self._deadline(lane) - time.time())
                        if remaining > 0:
                            try:
                                keep = await asyncio.wait_for(
                                    self._revalidate_getter(
                                        lane.root_text, self._lane_text(lane)
                                    ),
                                    timeout=remaining,
                                )
                            except Exception:
                                keep = None
                        if keep is None:
                            policy = "drop"
                    if keep is False or (keep is None and policy == "drop"):
                        try:
                            event.stop_event()
                        except Exception:
                            pass
                        self._record(
                            stage="llm",
                            action="revalidate_drop",
                            info=info,
                            window_count=lane.count,
                            window_age_ms=max(
                                0, int((time.time() - lane.first_at) * 1000)
                            ),
                            would_wait_ms=waited_ms,
                            enforce=True,
                            onnx_score=lane.onnx_score,
                        )
                        self._lanes.pop(key, None)
                        return
                    guard = keep is None and policy == "keep_with_guard"
                else:
                    guard = False
                merged = await self._merge_into_request(req, lane, guard=guard)
                self._record(
                    stage="llm",
                    action="real_release",
                    info=info,
                    window_count=lane.count,
                    window_age_ms=max(
                        0, int((time.time() - lane.first_at) * 1000)
                    ),
                    would_wait_ms=waited_ms,
                    prompt_chars=len(str(getattr(req, "prompt", "") or "")),
                    image_count=len(getattr(req, "image_urls", None) or ()),
                    merged_messages=merged,
                    enforce=True,
                    onnx_score=lane.onnx_score,
                    jev_score=lane.jev_score,
                )
                self._lanes.pop(key, None)
                return

            if not self._observe_for(event, info):
                return
            window_count = lane.count if lane is not None else 1
            window_age_ms = (
                max(0, int((now - lane.first_at) * 1000)) if lane else 0
            )
            min_wait = self._int("send_gate_min_wait_seconds", 5, 0)
            max_wait = self._int("send_gate_max_wait_seconds", 10, 0)
            if lane is not None:
                quiet_for = now - lane.last_at
                would_wait = max(0.0, min_wait - quiet_for)
                would_wait = min(float(max_wait), would_wait)
            else:
                would_wait = float(min_wait)
            self._record(
                stage="llm",
                action="would_release",
                info=info,
                window_count=window_count,
                window_age_ms=window_age_ms,
                would_wait_ms=int(would_wait * 1000),
                prompt_chars=len(str(getattr(req, "prompt", "") or "")),
                image_count=len(getattr(req, "image_urls", None) or ()),
            )
            if lane is not None:
                self._lanes.pop(key, None)
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("[send_gate] observe_llm 失败: %s", exc)

    # ------------------------------------------------------------------
    # 执行辅助
    # ------------------------------------------------------------------

    def _deadline(self, lane: _Lane) -> float:
        max_wait = self._int("send_gate_max_wait_seconds", 20, 0)
        max_window = self._int("send_gate_max_window_seconds", 20, 1)
        deadline = lane.first_at + max_window
        if max_wait > 0:
            deadline = min(deadline, lane.first_at + max_wait)
        return deadline

    async def _wait_for_window(self, lane: _Lane) -> int:
        min_wait = self._int("send_gate_min_wait_seconds", 5, 0)
        low_wait = self._int("send_gate_onnx_low_wait_seconds", 6, 0)
        deadline = self._deadline(lane)
        started = time.time()
        extended = False
        while True:
            now = time.time()
            if now >= deadline:
                break
            release_at = min(lane.last_at + min_wait, deadline)
            if now < release_at:
                await asyncio.sleep(min(0.05, max(0.01, release_at - now)))
                continue

            if not extended and self._onnx is not None:
                text = self._lane_text(lane)
                remaining = max(0.01, deadline - time.time())
                try:
                    score = await asyncio.wait_for(
                        self._onnx.score(text), timeout=remaining
                    )
                except Exception:
                    score = None
                if score is not None:
                    lane.onnx_score = score
                    low_threshold = float(
                        self._cfg("send_gate_onnx_low_threshold", 0.35)
                    )
                    if score < low_threshold and low_wait > 0:
                        extended = True
                        extra_until = min(
                            lane.last_at + max(min_wait, low_wait), deadline
                        )
                        if extra_until > time.time():
                            await asyncio.sleep(
                                min(0.05, max(0.01, extra_until - time.time()))
                            )
                            continue

            if (
                not lane.jev_checked
                and self._completeness_getter is not None
                and lane.root_kind in (CHAT, CHAT_PRIVATE)
                and self._bool("send_gate_jev_enable", False)
            ):
                lane.jev_checked = True
                text = self._lane_text(lane)
                remaining = max(0.01, deadline - time.time())
                try:
                    jev_value = await asyncio.wait_for(
                        self._completeness_getter(text), timeout=remaining
                    )
                except Exception:
                    jev_value = None
                if jev_value is not None:
                    lane.jev_score = float(jev_value)
                    threshold = float(
                        self._cfg("send_gate_jev_complete_threshold", 0.65)
                    )
                    if lane.jev_score < threshold:
                        extra_wait = self._int(
                            "send_gate_jev_incomplete_wait_seconds", 2, 0
                        )
                        remaining = deadline - time.time()
                        if remaining > 0 and extra_wait > 0:
                            await asyncio.sleep(min(float(extra_wait), remaining))
                            continue
            break
        return max(0, int((time.time() - started) * 1000))

    def _lane_text(self, lane: _Lane) -> str:
        parts = [str(lane.root_text or "").strip()]
        for item in lane.messages:
            text = str(item.text or "").strip()
            if text:
                parts.append(text)
        return "\n".join(x for x in parts if x)[:3000]

    async def _merge_into_request(
        self, req: Any, lane: _Lane, *, guard: bool = False
    ) -> int:
        if not lane.messages:
            return 0
        lines: list[str] = []
        for item in lane.messages[:12]:
            text = str(item.text or "").strip()
            if not text:
                continue
            lines.append(f"- {text[:500]}")
        if lines:
            base = str(getattr(req, "prompt", "") or "")
            block = (
                "\n\n[同一用户在等待期间继续发来的补充内容，"
                "请合并理解，不要逐条重复回答]\n"
                + "\n".join(lines)
            )
            if guard and lane.root_kind == PROACTIVE:
                block += (
                    "\n[发送门控提醒] 如果新消息已经明显偏离原主动目标，"
                    "或无法确认该接哪句话，请调用 keep_silent，不要强行回复。"
                )
            try:
                req.prompt = base + block
            except Exception:
                pass

        incoming: list[str] = []
        for item in lane.messages:
            for url in item.images:
                if url and url not in incoming:
                    incoming.append(url)
        if incoming:
            current = list(getattr(req, "image_urls", None) or ())
            for url in incoming:
                if url not in current:
                    current.append(url)
            try:
                req.image_urls = current[:8]
            except Exception:
                pass
        return len(lines)

    @staticmethod
    def _image_sources(event: Any) -> list[str]:
        try:
            message = getattr(getattr(event, "message_obj", None), "message", None) or []
            result: list[str] = []
            for part in message:
                if str(getattr(part, "type", "")) != "Image":
                    continue
                source = (
                    getattr(part, "url", None)
                    or getattr(part, "file", None)
                    or ""
                )
                if source:
                    result.append(str(source))
            return result
        except Exception:
            return []

    def drop_buffered_message(self, session_id: str, message_id: str) -> bool:
        """兼容 recall_cancel：从 lane/recent 中移除被撤回的消息。"""
        needle = str(session_id or "")
        target = str(message_id or "")
        if not needle or not target:
            return False
        removed = False
        for key, lane in list(self._lanes.items()):
            if needle not in key:
                continue
            kept = [item for item in lane.messages if item.message_id != target]
            if len(kept) != len(lane.messages):
                removed = True
                lane.messages = kept
                lane.count = max(1, len(kept) + 1)
                lane.last_at = max(
                    [lane.first_at] + [item.ts for item in kept]
                )
        for key, items in list(self._recent.items()):
            if needle not in key:
                continue
            kept = [item for item in items if item.message_id != target]
            if len(kept) != len(items):
                removed = True
                self._recent[key] = deque(kept, maxlen=50)
        return removed

    def _append_follower(self, lane: _Lane, item: _Buffered) -> bool:
        if item.message_id and any(
            old.message_id == item.message_id for old in lane.messages
        ):
            return False
        lane.messages.append(item)
        lane.count += 1
        lane.last_at = max(lane.last_at, item.ts)
        return True

    def _recent_after(self, key: str, message_id: str) -> list[_Buffered]:
        recent = self._recent.get(key)
        if not recent:
            return []
        items = list(recent)
        index = None
        for i, item in enumerate(items):
            if message_id and item.message_id == message_id:
                index = i
                break
        if index is None:
            return []
        return items[index + 1 :]

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_group_id(event: Any) -> str:
        try:
            return str(event.get_group_id() or "")
        except Exception:
            return ""

    @staticmethod
    def _lane_key(info: RequestInfo) -> str:
        return f"{info.umo or info.sender_id or 'unknown'}|{info.sender_id}"

    def _cleanup(self, now: float) -> None:
        cleanup = self._int("send_gate_cleanup_seconds", 180, 10)
        expired = [
            key
            for key, lane in self._lanes.items()
            if now - lane.last_at > cleanup
        ]
        max_window = self._int("send_gate_max_window_seconds", 20, 1)
        abandoned = [
            key
            for key, lane in self._lanes.items()
            if not lane.root_seen_llm
            and now - lane.first_at > max_window
        ]
        for key in abandoned:
            self._lanes.pop(key, None)
        for key in expired:
            self._lanes.pop(key, None)
            self._recent.pop(key, None)
        recent_expired = [
            key
            for key, items in self._recent.items()
            if items and now - items[-1].ts > cleanup
        ]
        for key in recent_expired:
            self._recent.pop(key, None)

    def _record(
        self,
        *,
        stage: str,
        action: str,
        info: RequestInfo,
        window_count: int,
        window_age_ms: int,
        would_wait_ms: int,
        prompt_chars: int = 0,
        image_count: int = 0,
        merged_messages: int = 0,
        enforce: bool = False,
        onnx_score: float | None = None,
        jev_score: float | None = None,
    ) -> None:
        record = {
            "ts": time.time(),
            "time": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "stage": stage,
            "action": action,
            "kind": info.kind,
            "source": info.source,
            "umo": info.umo,
            "group_id": self._safe_group_id_from_umo(info.umo),
            "sender_id": info.sender_id,
            "is_private": info.is_private,
            "message_id": info.message_id,
            "window_count": window_count,
            "window_age_ms": window_age_ms,
            "would_wait_ms": would_wait_ms,
            "prompt_chars": prompt_chars,
            "image_count": image_count,
            "merged_messages": merged_messages,
            "dry_run": self._dryrun_enabled(),
            "enforce": bool(enforce),
        }
        if onnx_score is not None:
            record["onnx_score"] = round(float(onnx_score), 4)
        if jev_score is not None:
            record["jev_score"] = round(float(jev_score), 4)
        if self._bool("send_gate_log_content", False):
            record["content_preview"] = str(info.text or "")[:80]
        try:
            directory = self._data_dir_getter()
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"send_gate_dryrun_{_dt.date.today():%Y%m%d}.jsonl"
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as exc:  # noqa: BLE001
            self._logger.debug("[send_gate] 写入发送门控日志失败: %s", exc)

    @staticmethod
    def _safe_group_id_from_umo(umo: str) -> str:
        try:
            parts = str(umo).split(":")
            return parts[-1] if len(parts) >= 3 else ""
        except Exception:
            return ""

    def stats(self) -> dict[str, int]:
        return {
            "lanes": len(self._lanes),
            "active_roots": sum(1 for lane in self._lanes.values() if lane.count),
        }
