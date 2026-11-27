"""dry-run 观察器：只记录，不改变真实请求。"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import json
import time
from pathlib import Path
from typing import Any, Callable

from .request_kind import (
    INTERNAL,
    UNKNOWN,
    USER_FACING_KINDS,
    RequestInfo,
    classify_request,
)


@dataclasses.dataclass
class _Lane:
    key: str
    first_at: float
    last_at: float
    count: int
    root_message_id: str
    last_kind: str
    last_llm_at: float = 0.0


class SendGateObserver:
    """发送门控的 dry-run 观察层。

    设计约束：
    - 不调用 event.stop_event()
    - 不修改 ProviderRequest
    - 不 sleep / 不延迟
    - 只维护内存 lane 和写 JSONL
    """

    def __init__(
        self,
        *,
        config: Any,
        data_dir_getter: Callable[[], Path],
        logger: Any,
    ) -> None:
        self._config = config
        self._data_dir_getter = data_dir_getter
        self._logger = logger
        self._lanes: dict[str, _Lane] = {}
        self._non_dry_run_warned = False

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------

    def _cfg(self, key: str, default: Any = None) -> Any:
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

    def _groups(self) -> set[str]:
        raw = self._cfg("send_gate_enabled_groups", [])
        if not isinstance(raw, list):
            raw = [raw]
        return {str(item).strip() for item in raw if str(item).strip()}

    def _enabled_for(self, event: Any, info: RequestInfo) -> bool:
        if not self._bool("send_gate_enable", False):
            return False
        if not self._bool("send_gate_dry_run", True):
            if not self._non_dry_run_warned:
                self._non_dry_run_warned = True
                self._logger.warning(
                    "[send_gate] dry_run=false 但真实发送门控尚未实现，本次跳过"
                )
            return False
        if info.is_private and not self._bool(
            "send_gate_observe_private", True
        ):
            return False
        if not info.is_private:
            groups = self._groups()
            group_id = self._safe_group_id(event)
            if groups and group_id not in groups:
                return False
        return True

    # ------------------------------------------------------------------
    # 观察入口
    # ------------------------------------------------------------------

    async def observe_waiting(self, event: Any) -> None:
        try:
            info = classify_request(event)
            if not self._enabled_for(event, info):
                return
            if info.kind not in USER_FACING_KINDS:
                self._record(
                    stage="waiting",
                    action="observe_ignore",
                    info=info,
                    window_count=0,
                    window_age_ms=0,
                    would_wait_ms=0,
                )
                return
            now = time.time()
            self._cleanup(now)
            key = self._lane_key(info)
            window = self._int("send_gate_max_window_seconds", 30, 1)
            lane = self._lanes.get(key)
            if lane is not None and now - lane.first_at <= window:
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

    async def observe_llm(self, event: Any, req: Any) -> None:
        try:
            info = classify_request(event)
            if not self._enabled_for(event, info):
                return
            now = time.time()
            self._cleanup(now)
            key = self._lane_key(info)
            lane = self._lanes.get(key)
            window_count = lane.count if lane is not None else 1
            window_age_ms = (
                max(0, int((now - lane.first_at) * 1000)) if lane else 0
            )
            min_wait = self._int("send_gate_min_wait_seconds", 2, 0)
            max_wait = self._int("send_gate_max_wait_seconds", 10, 0)
            if lane is not None:
                lane.last_llm_at = now
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
            # 原请求已经释放，后续同用户消息必须开启新窗口，
            # 不能再并入已经释放的 lane。
            if lane is not None:
                self._lanes.pop(key, None)
        except Exception as exc:  # noqa: BLE001
            self._logger.debug("[send_gate] observe_llm 失败: %s", exc)

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
        for key in expired:
            self._lanes.pop(key, None)

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
            "dry_run": True,
        }
        if self._bool("send_gate_log_content", False):
            record["content_preview"] = str(info.text or "")[:80]
        try:
            directory = self._data_dir_getter()
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"send_gate_dryrun_{_dt.date.today():%Y%m%d}.jsonl"
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as exc:  # noqa: BLE001
            self._logger.debug("[send_gate] 写入 dry-run 日志失败: %s", exc)

    @staticmethod
    def _safe_group_id_from_umo(umo: str) -> str:
        # umo 常见格式：napcat:GroupMessage:123456
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
