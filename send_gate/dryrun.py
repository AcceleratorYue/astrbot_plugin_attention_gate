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
    FOLLOWUP,
    PROACTIVE,
    USER_FACING_KINDS,
    RequestInfo,
    classify_request,
)

ROOT_EXTRA = "_send_gate_root_token"
ROOT_TICKET_EXTRA = "_send_gate_root_ticket"
ACTIVE_CANDIDATE_EXTRA = "send_gate_active_candidate"
DECISION_BUDGET_EXTRA = "send_gate_decision_budget"
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
        completeness_getter: Callable[..., Any] | None = None,
        revalidate_getter: Callable[..., Any] | None = None,
        active_judge: Callable[[Any, Any, Any], Any] | None = None,
        candidate_dropped: Callable[[Any], Any] | None = None,
    ) -> None:
        self._config = config
        self._config_getter = config_getter
        self._data_dir_getter = data_dir_getter
        self._logger = logger
        self._onnx = onnx
        self._completeness_getter = completeness_getter
        self._revalidate_getter = revalidate_getter
        self._active_judge = active_judge
        self._candidate_dropped = candidate_dropped
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
        if info.is_private and not self._bool("send_gate_observe_private", True):
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
        if info.is_private and not self._bool("send_gate_enforce_private", False):
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
            active = lane is not None and now - lane.first_at <= window
            if enforce:
                if (
                    active
                    and lane is not None
                    and info.kind
                    in (
                        PROACTIVE,
                        FOLLOWUP,
                    )
                ):
                    # 同一发送者的主动/跟进候选如果是在已有 root 窗口内产生的，
                    # 说明它已经被群消息缓冲合并，不能再作为第二个请求发出。
                    self._drop_candidate(event)
                    try:
                        event.stop_event()
                    except Exception:
                        pass
                    self._record(
                        stage="waiting",
                        action="duplicate_nonmergeable_stop",
                        info=info,
                        window_count=lane.count,
                        window_age_ms=max(0, int((now - lane.first_at) * 1000)),
                        would_wait_ms=0,
                        enforce=True,
                    )
                    return
                if active and lane is not None and info.kind not in COMPLETENESS_KINDS:
                    # poke 不混入普通聊天窗口，也不 stop；让它作为独立请求排队。
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
                    ticket = {
                        "token": token,
                        "key": key,
                        "first_at": now,
                        "last_at": lane.last_at,
                        "root_message_id": info.message_id,
                        "root_text": info.text,
                        "root_kind": info.kind,
                        "root_images": list(lane.root_images),
                    }
                    try:
                        event.set_extra(ROOT_EXTRA, token)
                        event.set_extra(ROOT_TICKET_EXTRA, ticket)
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
                ticket: Any = None
                try:
                    token = str(event.get_extra(ROOT_EXTRA, "") or "")
                    ticket = event.get_extra(ROOT_TICKET_EXTRA)
                except Exception:
                    token = ""
                    ticket = None
                candidate = bool(event.get_extra(ACTIVE_CANDIDATE_EXTRA))

                # root 可能在前一个 LLM 请求释放 session lock 后很久才进入
                # on_llm_request。此时 lane 已被 cleanup 移除，但 root token
                # 仍然存在；必须用 token 中的时间戳恢复 lane，不能把它当作
                # “未管理请求”直接放行，否则主动候选会绕过 Jev 和引用。
                if token and (lane is None or lane.token != token):
                    recovered = self._recover_lane(
                        event, info, key, token, ticket, candidate, now
                    )
                    if recovered is not None:
                        lane = recovered
                        self._record(
                            stage="llm",
                            action="late_root_recovered",
                            info=info,
                            window_count=lane.count,
                            window_age_ms=max(0, int((now - lane.first_at) * 1000)),
                            would_wait_ms=0,
                            prompt_chars=len(str(getattr(req, "prompt", "") or "")),
                            image_count=len(getattr(req, "image_urls", None) or ()),
                            enforce=True,
                        )

                if lane is None or not token or lane.token != token:
                    if candidate:
                        # 主动候选没有可恢复的 root 票据时必须 fail closed，
                        # 绝不能在没做主动决策的情况下交给主模型。
                        self._drop_candidate(event)
                        self._stop_event(event)
                        self._record(
                            stage="llm",
                            action="candidate_unmanaged_stop",
                            info=info,
                            window_count=lane.count if lane else 1,
                            window_age_ms=(
                                max(0, int((now - lane.first_at) * 1000)) if lane else 0
                            ),
                            would_wait_ms=0,
                            prompt_chars=len(str(getattr(req, "prompt", "") or "")),
                            image_count=len(getattr(req, "image_urls", None) or ()),
                            enforce=True,
                        )
                        # lane 属于同 key 的另一个 root，不能在这里关闭。
                        return
                    # 非候选请求没有我们的 root token，通常是其它插件
                    # 构造/停止过的请求，保持原有的不阻塞行为。
                    self._record(
                        stage="llm",
                        action="real_release_unmanaged",
                        info=info,
                        window_count=lane.count if lane else 1,
                        window_age_ms=(
                            max(0, int((now - lane.first_at) * 1000)) if lane else 0
                        ),
                        would_wait_ms=0,
                        prompt_chars=len(str(getattr(req, "prompt", "") or "")),
                        image_count=len(getattr(req, "image_urls", None) or ()),
                        enforce=True,
                    )
                    return

                lane.root_seen_llm = True
                try:
                    candidate = bool(event.get_extra(ACTIVE_CANDIDATE_EXTRA))
                    grace = float(
                        self._int(
                            "send_gate_late_decision_grace_seconds",
                            8,
                            0,
                        )
                    )
                    window = self._int("send_gate_max_window_seconds", 20, 1)
                    grace = min(grace, float(window))
                    waited_ms = await self._wait_for_window(lane)
                    if candidate:
                        if self._active_judge is None:
                            # 候选存在但插件没有提供决策器：宁可拦截，
                            # 也不能发出未经决策的主动回复。
                            self._drop_candidate(event)
                            self._stop_event(event)
                            self._record(
                                stage="llm",
                                action="active_decision_drop",
                                info=info,
                                window_count=lane.count,
                                window_age_ms=max(
                                    0,
                                    int((time.time() - lane.first_at) * 1000),
                                ),
                                would_wait_ms=waited_ms,
                                enforce=True,
                                onnx_score=lane.onnx_score,
                            )
                            self._close_lane(key, lane)
                            return
                        remaining = max(0.0, self._deadline(lane) - time.time())
                        budget = max(remaining, grace) if grace > 0 else remaining
                        if budget <= 0:
                            # 宽限为 0 时不启动注定被取消的 Jev，直接 fail closed。
                            self._drop_candidate(event)
                            self._stop_event(event)
                            self._record(
                                stage="llm",
                                action="active_decision_drop",
                                info=info,
                                window_count=lane.count,
                                window_age_ms=max(
                                    0,
                                    int((time.time() - lane.first_at) * 1000),
                                ),
                                would_wait_ms=waited_ms,
                                enforce=True,
                                onnx_score=lane.onnx_score,
                            )
                            self._close_lane(key, lane)
                            return
                        try:
                            event.set_extra(DECISION_BUDGET_EXTRA, budget)
                        except Exception:
                            pass
                        try:
                            accepted = await asyncio.wait_for(
                                self._active_judge(event, req, lane),
                                timeout=max(0.5, budget),
                            )
                        except asyncio.CancelledError:
                            # 统一交给外层 CancelledError 分支回滚并清理 lane。
                            raise
                        except Exception:
                            accepted = False
                        if not accepted:
                            self._drop_candidate(event)
                            self._stop_event(event)
                            self._record(
                                stage="llm",
                                action="active_decision_drop",
                                info=info,
                                window_count=lane.count,
                                window_age_ms=max(
                                    0,
                                    int((time.time() - lane.first_at) * 1000),
                                ),
                                would_wait_ms=waited_ms,
                                enforce=True,
                                onnx_score=lane.onnx_score,
                            )
                            self._close_lane(key, lane)
                            return
                    if (
                        lane.root_kind == PROACTIVE
                        and not candidate
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
                                        lane.root_text,
                                        self._lane_text(lane),
                                        remaining,
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
                                            lane.root_text,
                                            self._lane_text(lane),
                                            remaining,
                                        ),
                                        timeout=remaining,
                                    )
                                except Exception:
                                    keep = None
                            if keep is None:
                                policy = "drop"
                        if keep is False or (keep is None and policy == "drop"):
                            self._stop_event(event)
                            self._record(
                                stage="llm",
                                action="revalidate_drop",
                                info=info,
                                window_count=lane.count,
                                window_age_ms=max(
                                    0,
                                    int((time.time() - lane.first_at) * 1000),
                                ),
                                would_wait_ms=waited_ms,
                                enforce=True,
                                onnx_score=lane.onnx_score,
                            )
                            self._close_lane(key, lane)
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
                        window_age_ms=max(0, int((time.time() - lane.first_at) * 1000)),
                        would_wait_ms=waited_ms,
                        prompt_chars=len(str(getattr(req, "prompt", "") or "")),
                        image_count=len(getattr(req, "image_urls", None) or ()),
                        merged_messages=merged,
                        enforce=True,
                        onnx_score=lane.onnx_score,
                        jev_score=lane.jev_score,
                    )
                    try:
                        event.set_extra("send_gate_released", True)
                    except Exception:
                        pass
                    self._close_lane(key, lane)
                    return
                except asyncio.CancelledError:
                    # 任务取消时也要回滚主动候选预占的冷却并停止事件，
                    # 避免 AstrBot 吞掉异常后继续发送未决策候选。
                    self._drop_candidate(event)
                    self._stop_event(event)
                    self._close_lane(key, lane)
                    raise

            if not self._observe_for(event, info):
                return
            window_count = lane.count if lane is not None else 1
            window_age_ms = max(0, int((now - lane.first_at) * 1000)) if lane else 0
            min_wait = self._int("send_gate_min_wait_seconds", 5, 0)
            max_wait = self._int("send_gate_max_wait_seconds", 20, 0)
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
                self._close_lane(key, lane)
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("[send_gate] observe_llm 失败: %s", exc)
            # 任何未预期异常都不能让主动候选无决策放行。
            try:
                if event is not None and event.get_extra(ACTIVE_CANDIDATE_EXTRA):
                    self._drop_candidate(event)
                    self._stop_event(event)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # 执行辅助
    # ------------------------------------------------------------------

    def start_onnx_warmup(self) -> Any:
        """启动可选 ONNX 后台预热；没有实现时返回 None。"""
        if self._onnx is None:
            return None
        starter = getattr(self._onnx, "start_warmup", None)
        if callable(starter):
            return starter()
        return None

    def cancel_onnx_warmup(self) -> None:
        """插件卸载/重载时停止可选 ONNX 后台任务。"""
        if self._onnx is None:
            return
        canceller = getattr(self._onnx, "cancel_warmup", None)
        if callable(canceller):
            canceller()

    def _recover_lane(
        self,
        event: Any,
        info: RequestInfo,
        key: str,
        token: str,
        ticket: Any,
        candidate: bool,
        now: float,
    ) -> _Lane | None:
        """用 root token/ticket 恢复被 cleanup 移除的 lane。

        root 进入 on_llm_request 前可能被前一个请求占住 session lock 很久；
        lane 会按 max_window 被清理，但请求仍带着 root token。恢复时保留
        原始 first_at，这样不会重新等待一个完整窗口，同时仍能执行决策。
        """
        existing = self._lanes.get(key)
        if existing is not None and existing.token and existing.token != token:
            # 同一 key 已经有新的 root，旧请求不能覆盖它。
            return None
        first_at = now
        last_at = now
        root_text = str(info.text or "")
        root_kind = str(info.kind or "")
        root_message_id = str(info.message_id or "")
        root_images = self._image_sources(event)
        if isinstance(ticket, dict) and str(ticket.get("token") or "") == token:
            first_at = self._to_float(ticket.get("first_at"), now)
            last_at = self._to_float(ticket.get("last_at"), now)
            root_text = str(ticket.get("root_text") or root_text)
            root_kind = str(ticket.get("root_kind") or root_kind)
            root_message_id = str(ticket.get("root_message_id") or root_message_id)
            images = ticket.get("root_images")
            if isinstance(images, list):
                root_images = [str(x) for x in images if str(x)]
        elif isinstance(event.get_extra(ACTIVE_CANDIDATE_EXTRA), dict):
            payload = event.get_extra(ACTIVE_CANDIDATE_EXTRA) or {}
            first_at = self._to_float(payload.get("created_at"), now)
            last_at = first_at
            root_text = str(payload.get("root_text") or root_text)
            root_message_id = str(payload.get("message_id") or root_message_id)
        elif candidate:
            # 有候选标记但没有票据：至少在晚到状态下立即决策，不重等窗口。
            first_at = now - self._int("send_gate_max_window_seconds", 20, 1) - 1
            last_at = first_at
        lane = _Lane(
            key=key,
            first_at=first_at,
            last_at=last_at,
            count=1,
            root_message_id=root_message_id,
            last_kind=root_kind,
            token=token,
            root_text=root_text,
            root_kind=root_kind,
            root_images=root_images,
        )
        for item in self._recent_after(key, root_message_id):
            self._append_follower(lane, item)
        self._lanes[key] = lane
        self._logger.info(
            "[send_gate] root 晚到，已恢复 %s 的 lane（age=%.1fs, candidate=%s）",
            key,
            max(0.0, now - first_at),
            candidate,
        )
        return lane

    def _close_lane(self, key: str, lane: _Lane) -> None:
        """只关闭自己持有的 lane，避免旧请求误删同 key 的新 root。"""
        if self._lanes.get(key) is lane:
            self._lanes.pop(key, None)

    def _drop_candidate(self, event: Any) -> None:
        """通知宿主回滚主动候选的预占冷却；普通请求不得触发回滚。"""
        if self._candidate_dropped is None:
            return
        try:
            if not event.get_extra(ACTIVE_CANDIDATE_EXTRA):
                return
        except Exception:
            return
        try:
            self._candidate_dropped(event)
        except Exception as exc:  # noqa: BLE001
            self._logger.debug("[send_gate] 回滚主动候选失败: %s", exc)

    @staticmethod
    def _stop_event(event: Any) -> None:
        try:
            event.stop_event()
        except Exception:
            pass

    @staticmethod
    def _to_float(value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

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
                        self._completeness_getter(text, remaining),
                        timeout=remaining,
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
                "请合并理解，不要逐条重复回答]\n" + "\n".join(lines)
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
            message = (
                getattr(getattr(event, "message_obj", None), "message", None) or []
            )
            result: list[str] = []
            for part in message:
                if str(getattr(part, "type", "")) != "Image":
                    continue
                source = getattr(part, "url", None) or getattr(part, "file", None) or ""
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
                lane.last_at = max([lane.first_at] + [item.ts for item in kept])
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
            key for key, lane in self._lanes.items() if now - lane.last_at > cleanup
        ]
        max_window = self._int("send_gate_max_window_seconds", 20, 1)
        abandoned = [
            key
            for key, lane in self._lanes.items()
            if not lane.root_seen_llm and now - lane.first_at > max_window
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
