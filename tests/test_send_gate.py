"""send_gate dry-run 观察层的纯逻辑测试。"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from astrbot.core.star.star_handler import (  # noqa: E402
    EventType,
    star_handlers_registry,
)

import main as plugin_main  # noqa: E402  (register hooks)
from send_gate import SendGateObserver, classify_request  # noqa: E402


class FakeLogger:
    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass


class FakeMessage:
    def __init__(self, text: str, message_id: str) -> None:
        self.message_str = text
        self.message_id = message_id


class FakeEvent:
    def __init__(
        self,
        text: str = "hello",
        *,
        sender: str = "u1",
        group: str = "10001",
        msg_id: str = "m1",
        private: bool = False,
        extras: dict | None = None,
    ) -> None:
        self.message_str = text
        self.message_obj = FakeMessage(text, msg_id)
        self._sender = sender
        self._group = group
        self._private = private
        self._extras = extras or {}
        self.unified_msg_origin = (
            f"test:PrivateMessage:{sender}" if private else f"test:GroupMessage:{group}"
        )
        self.stopped = False

    def get_sender_id(self):
        return self._sender

    def get_group_id(self):
        return "" if self._private else self._group

    def is_private_chat(self):
        return self._private

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def set_extra(self, key, value):
        self._extras[key] = value

    def stop_event(self):
        self.stopped = True

    def is_stopped(self):
        return self.stopped


class FakeConfig:
    def __init__(self, values: dict | None = None) -> None:
        self.values = values or {}

    def get(self, key, default=None):
        return self.values.get(key, default)


def run(coro):
    return asyncio.run(coro)


class RequestKindTests(unittest.TestCase):
    def test_proactive(self):
        info = classify_request(FakeEvent(extras={"active_reply_triggered": {"x": 1}}))
        self.assertEqual(info.kind, "proactive")
        self.assertEqual(info.source, "attention_gate")

    def test_poke_and_followup(self):
        self.assertEqual(
            classify_request(FakeEvent(extras={"_poke_trigger": True})).kind,
            "poke",
        )
        self.assertEqual(
            classify_request(FakeEvent(extras={"_follow_up_captured": {}})).kind,
            "followup",
        )

    def test_chat_and_private(self):
        self.assertEqual(classify_request(FakeEvent()).kind, "chat")
        self.assertEqual(classify_request(FakeEvent(private=True)).kind, "chat_private")

    def test_internal(self):
        self.assertEqual(classify_request(None).kind, "internal")
        event = FakeEvent()
        event.message_obj = None
        self.assertEqual(classify_request(event).kind, "internal")


class HookRegistrationTests(unittest.TestCase):
    def test_dry_run_hooks_registered(self):
        waiting = star_handlers_registry.get_handlers_by_event_type(
            EventType.OnWaitingLLMRequestEvent
        )
        llm = star_handlers_registry.get_handlers_by_event_type(
            EventType.OnLLMRequestEvent
        )
        waiting_names = {h.handler_name for h in waiting}
        llm_names = {h.handler_name for h in llm}
        adapter = star_handlers_registry.get_handlers_by_event_type(
            EventType.AdapterMessageEvent
        )
        adapter_names = {h.handler_name for h in adapter}
        self.assertIn("on_send_gate_waiting", waiting_names)
        self.assertIn("on_send_gate_llm_request", llm_names)
        self.assertIn("on_send_gate_group_message", adapter_names)


class SchemaOrderTests(unittest.TestCase):
    def test_schema_starts_with_grouped_sections(self):
        import json

        schema = json.loads(
            (Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(
                encoding="utf-8"
            )
        )
        for key in (
            "enable",
            "api_key",
            "jev_timeout_seconds",
            "persona",
            "interest_groups",
            "group_topic_schemes",
            "send_gate_enable",
            "send_gate_onnx_auto_download",
            "send_gate_late_decision_grace_seconds",
        ):
            self.assertIn(key, schema)
        self.assertTrue(schema["enable"]["description"].startswith("【基础】"))
        self.assertTrue(schema["api_key"]["description"].startswith("【Jev】"))
        self.assertTrue(
            schema["send_gate_enable"]["description"].startswith("【发送门控】")
        )
        # 默认配置不得再携带作者的个人人格、兴趣词或示例群号。
        raw = json.dumps(schema, ensure_ascii=False)
        self.assertNotIn("鲸鲸", raw)
        self.assertEqual(schema["bot_names"]["default"], [])
        self.assertEqual(schema["interest_groups"]["default"], [])
        self.assertEqual(schema["group_topic_schemes"]["default"], [])
        self.assertIs(schema["api_key"].get("secret"), True)


class JevStateLimitTests(unittest.TestCase):
    def test_fit_jev_state_hard_limit(self):
        plugin = object.__new__(plugin_main.AttentionGatePlugin)
        plugin.config = FakeConfig({"jev_max_state_chars": 2000})
        state = {
            "recent_conversation": ["长" * 2000 for _ in range(20)],
            "recent_unanswered": [{"content": "长" * 2000} for _ in range(10)],
            "target_message": {
                "content": "长" * 2000,
                "turn": [{"content": "长" * 2000} for _ in range(6)],
                "wait_batch": [{"content": "长" * 2000} for _ in range(10)],
            },
        }
        result = plugin._fit_jev_state(state)
        self.assertLessEqual(
            len(__import__("json").dumps(result, ensure_ascii=False)), 2000
        )


class DryRunObserverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = FakeConfig(
            {
                "send_gate_enable": True,
                "send_gate_dry_run": True,
                "send_gate_enabled_groups": [],
                "send_gate_observe_private": True,
                "send_gate_max_window_seconds": 30,
                "send_gate_cleanup_seconds": 180,
                "send_gate_min_wait_seconds": 2,
                "send_gate_max_wait_seconds": 10,
                "send_gate_log_content": True,
            }
        )
        self.observer = SendGateObserver(
            config=self.config,
            data_dir_getter=lambda: Path(self.tmp.name),
            logger=FakeLogger(),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _records(self):
        path = Path(self.tmp.name) / (
            "send_gate_dryrun_"
            + __import__("datetime").date.today().strftime("%Y%m%d")
            + ".jsonl"
        )
        if not path.exists():
            return []
        return [
            json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
        ]

    def test_root_then_merge_without_stopping(self):
        first = FakeEvent(text="第一句", msg_id="m1")
        second = FakeEvent(text="第二句", msg_id="m2")
        run(self.observer.observe_waiting(first))
        run(self.observer.observe_waiting(second))
        self.assertFalse(first.stopped)
        self.assertFalse(second.stopped)
        records = self._records()
        self.assertEqual([r["action"] for r in records], ["would_root", "would_merge"])
        self.assertEqual(records[1]["window_count"], 2)
        self.assertEqual(records[1]["content_preview"], "第二句")

    def test_llm_observation_does_not_mutate_req(self):
        event = FakeEvent(text="完整消息", msg_id="m3")
        req = SimpleNamespace(prompt="original prompt", image_urls=["a.jpg"])
        run(self.observer.observe_waiting(event))
        run(self.observer.observe_llm(event, req))
        self.assertEqual(req.prompt, "original prompt")
        self.assertEqual(req.image_urls, ["a.jpg"])
        self.assertFalse(event.stopped)
        records = self._records()
        self.assertEqual(records[-1]["stage"], "llm")
        self.assertEqual(records[-1]["prompt_chars"], len("original prompt"))

    def test_enforce_root_follower_and_merge(self):
        self.config.values.update(
            {
                "send_gate_enforce": True,
                "send_gate_dry_run": False,
                "send_gate_enforce_groups": [],
                "send_gate_enforce_private": False,
                "send_gate_min_wait_seconds": 0,
                "send_gate_max_window_seconds": 1,
            }
        )
        root = FakeEvent(text="根消息", msg_id="r1")
        follower = FakeEvent(text="补充消息", msg_id="f1")
        req = SimpleNamespace(prompt="根消息", image_urls=[])
        run(self.observer.observe_waiting(root))
        run(self.observer.observe_waiting(follower))
        self.assertFalse(root.stopped)
        self.assertTrue(follower.stopped)
        run(self.observer.observe_llm(root, req))
        self.assertIn("补充消息", req.prompt)
        self.assertFalse(root.stopped)
        # lane released, next message becomes a new root
        after = FakeEvent(text="释放后消息", msg_id="a1")
        run(self.observer.observe_waiting(after))
        self.assertEqual(self.observer.stats()["lanes"], 1)

    def test_enforce_completeness_callback(self):
        calls = []

        async def completeness(text, timeout=None):
            calls.append(text)
            return 0.0

        observer = SendGateObserver(
            config=FakeConfig(
                {
                    "send_gate_enable": True,
                    "send_gate_enforce": True,
                    "send_gate_dry_run": False,
                    "send_gate_enforce_groups": [],
                    "send_gate_enforce_private": False,
                    "send_gate_min_wait_seconds": 0,
                    "send_gate_max_window_seconds": 1,
                    "send_gate_jev_enable": True,
                    "send_gate_jev_complete_threshold": 0.65,
                    "send_gate_jev_incomplete_wait_seconds": 0,
                }
            ),
            data_dir_getter=lambda: Path(self.tmp.name),
            logger=FakeLogger(),
            completeness_getter=completeness,
        )
        root = FakeEvent(text="没说完", msg_id="r2")
        req = SimpleNamespace(prompt="没说完", image_urls=[])
        run(observer.observe_waiting(root))
        run(observer.observe_llm(root, req))
        self.assertEqual(calls, ["没说完"])

    def test_revalidate_drop_blocks_release(self):
        async def revalidate(root_text, full_text, timeout=None):
            return False

        observer = SendGateObserver(
            config=FakeConfig(
                {
                    "send_gate_enable": True,
                    "send_gate_enforce": True,
                    "send_gate_dry_run": False,
                    "send_gate_enforce_groups": [],
                    "send_gate_enforce_private": False,
                    "send_gate_min_wait_seconds": 0,
                    "send_gate_max_window_seconds": 1,
                    "send_gate_revalidate_proactive": True,
                }
            ),
            data_dir_getter=lambda: Path(self.tmp.name),
            logger=FakeLogger(),
            revalidate_getter=revalidate,
        )
        root = FakeEvent(
            text="原目标",
            msg_id="p1",
            extras={"active_reply_triggered": {"x": 1}},
        )
        follower = FakeEvent(text="新话题", msg_id="p2")
        req = SimpleNamespace(prompt="原目标", image_urls=[])
        run(observer.observe_waiting(root))
        run(observer.observe_waiting(follower))
        run(observer.observe_llm(root, req))
        self.assertTrue(root.stopped)
        self.assertEqual(req.prompt, "原目标")

    def test_group_followups_buffered_before_root_request(self):
        self.config.values.update(
            {
                "send_gate_enforce": True,
                "send_gate_dry_run": False,
                "send_gate_enforce_groups": [],
                "send_gate_min_wait_seconds": 0,
                "send_gate_max_window_seconds": 1,
            }
        )
        root = FakeEvent(text="第一句", msg_id="g1", group="20002")
        follow = FakeEvent(text="后续一句", msg_id="g2", group="20002")
        req = SimpleNamespace(prompt="第一句", image_urls=[])
        run(self.observer.observe_group_message(root, accept=lambda e: True))
        run(self.observer.observe_group_message(follow, accept=lambda e: True))
        run(self.observer.observe_waiting(root))
        run(self.observer.observe_llm(root, req))
        self.assertIn("后续一句", req.prompt)
        self.assertFalse(follow.stopped)

    def test_drop_buffered_message(self):
        self.config.values.update(
            {
                "send_gate_enforce": True,
                "send_gate_dry_run": False,
                "send_gate_enforce_groups": [],
            }
        )
        root = FakeEvent(text="第一句", msg_id="d1", group="40004")
        follow = FakeEvent(text="撤回这句", msg_id="d2", group="40004")
        run(self.observer.observe_group_message(root, accept=lambda e: True))
        run(self.observer.observe_group_message(follow, accept=lambda e: True))
        self.assertTrue(self.observer.drop_buffered_message("40004", "d2"))
        self.assertFalse(self.observer.drop_buffered_message("40004", "missing"))

    def test_active_candidate_judge_accept(self):
        self.config.values.update(
            {
                "send_gate_enforce": True,
                "send_gate_dry_run": False,
                "send_gate_min_wait_seconds": 0,
                "send_gate_max_window_seconds": 1,
            }
        )
        seen = {"called": False}

        async def judge(event, req, lane):
            seen["called"] = True
            return True

        observer = SendGateObserver(
            config=self.config,
            data_dir_getter=lambda: Path(self.tmp.name),
            logger=FakeLogger(),
            active_judge=judge,
        )
        event = FakeEvent(
            text="候选",
            msg_id="c1",
            extras={
                "active_reply_triggered": {},
                "send_gate_active_candidate": {"message_id": "c1"},
            },
        )
        req = SimpleNamespace(prompt="候选", image_urls=[])
        run(observer.observe_waiting(event))
        run(observer.observe_llm(event, req))
        self.assertTrue(seen["called"])
        self.assertFalse(event.stopped)

    def test_active_candidate_judge_drop(self):
        self.config.values.update(
            {
                "send_gate_enforce": True,
                "send_gate_dry_run": False,
                "send_gate_min_wait_seconds": 0,
                "send_gate_max_window_seconds": 1,
            }
        )

        async def judge(event, req, lane):
            return False

        observer = SendGateObserver(
            config=self.config,
            data_dir_getter=lambda: Path(self.tmp.name),
            logger=FakeLogger(),
            active_judge=judge,
        )
        event = FakeEvent(
            text="候选",
            msg_id="c2",
            extras={
                "active_reply_triggered": {},
                "send_gate_active_candidate": {"message_id": "c2"},
            },
        )
        run(observer.observe_waiting(event))
        run(observer.observe_llm(event, req=SimpleNamespace(prompt="x", image_urls=[])))
        self.assertTrue(event.stopped)

    def test_active_candidate_without_root_ticket_fails_closed(self):
        self.config.values.update(
            {
                "send_gate_enforce": True,
                "send_gate_dry_run": False,
                "send_gate_min_wait_seconds": 0,
                "send_gate_max_window_seconds": 1,
            }
        )
        dropped = []

        def drop(event):
            dropped.append(event)

        observer = SendGateObserver(
            config=self.config,
            data_dir_getter=lambda: Path(self.tmp.name),
            logger=FakeLogger(),
            candidate_dropped=drop,
        )
        event = FakeEvent(
            text="候选",
            msg_id="c-no-token",
            extras={
                "active_reply_triggered": {},
                "send_gate_active_candidate": {
                    "group_id": "10001",
                    "message_id": "c-no-token",
                    "cooldown_prev": 0,
                },
            },
        )
        req = SimpleNamespace(prompt="候选", image_urls=[])
        run(observer.observe_llm(event, req))
        self.assertTrue(event.stopped)
        self.assertEqual(dropped, [event])
        actions = [r["action"] for r in self._records()]
        self.assertIn("candidate_unmanaged_stop", actions)
        self.assertNotIn("real_release_unmanaged", actions)

    def test_late_active_candidate_recovers_lane_and_calls_judge(self):
        self.config.values.update(
            {
                "send_gate_enforce": True,
                "send_gate_dry_run": False,
                "send_gate_min_wait_seconds": 0,
                "send_gate_max_wait_seconds": 1,
                "send_gate_max_window_seconds": 1,
                "send_gate_late_decision_grace_seconds": 1,
            }
        )
        seen = []

        async def judge(event, req, lane):
            seen.append(1)
            return True

        observer = SendGateObserver(
            config=self.config,
            data_dir_getter=lambda: Path(self.tmp.name),
            logger=FakeLogger(),
            active_judge=judge,
        )
        event = FakeEvent(
            text="候选",
            msg_id="late-candidate",
            group="60006",
            extras={
                "active_reply_triggered": {},
                "send_gate_active_candidate": {
                    "group_id": "60006",
                    "message_id": "late-candidate",
                    "root_text": "候选",
                    "created_at": time.time() - 30,
                    "cooldown_prev": 0,
                },
            },
        )
        req = SimpleNamespace(prompt="候选", image_urls=[])
        run(observer.observe_waiting(event))
        for lane in observer._lanes.values():
            lane.first_at -= 30
            lane.last_at -= 30
        ticket = event.get_extra("_send_gate_root_ticket")
        ticket["first_at"] -= 30
        ticket["last_at"] -= 30
        run(observer.observe_llm(event, req))
        self.assertEqual(seen, [1])
        self.assertFalse(event.stopped)
        actions = [r["action"] for r in self._records()]
        self.assertIn("late_root_recovered", actions)
        self.assertIn("real_release", actions)
        self.assertNotIn("real_release_unmanaged", actions)

    def test_late_normal_root_recovers_lane_and_merges_followup(self):
        self.config.values.update(
            {
                "send_gate_enforce": True,
                "send_gate_dry_run": False,
                "send_gate_min_wait_seconds": 0,
                "send_gate_max_wait_seconds": 1,
                "send_gate_max_window_seconds": 1,
            }
        )
        root = FakeEvent(text="第一句", msg_id="late-root", group="60007")
        follow = FakeEvent(text="补充一句", msg_id="late-follow", group="60007")
        req = SimpleNamespace(prompt="第一句", image_urls=[])
        run(self.observer.observe_group_message(root, accept=lambda e: True))
        run(self.observer.observe_waiting(root))
        for lane in self.observer._lanes.values():
            lane.first_at -= 30
            lane.last_at -= 30
        ticket = root.get_extra("_send_gate_root_ticket")
        ticket["first_at"] -= 30
        ticket["last_at"] -= 30
        run(self.observer.observe_group_message(follow, accept=lambda e: True))
        run(self.observer.observe_llm(root, req))
        self.assertIn("补充一句", req.prompt)
        self.assertFalse(root.stopped)
        actions = [r["action"] for r in self._records()]
        self.assertIn("late_root_recovered", actions)
        self.assertIn("real_release", actions)

    def test_active_candidate_cancel_restores_cooldown(self):
        self.config.values.update(
            {
                "send_gate_enforce": True,
                "send_gate_dry_run": False,
                "send_gate_min_wait_seconds": 0,
                "send_gate_max_wait_seconds": 1,
                "send_gate_max_window_seconds": 1,
                "send_gate_late_decision_grace_seconds": 1,
            }
        )
        dropped = []

        def drop(event):
            dropped.append(event)

        async def judge(event, req, lane):
            raise asyncio.CancelledError

        observer = SendGateObserver(
            config=self.config,
            data_dir_getter=lambda: Path(self.tmp.name),
            logger=FakeLogger(),
            active_judge=judge,
            candidate_dropped=drop,
        )
        event = FakeEvent(
            text="候选",
            msg_id="cancel-candidate",
            group="60008",
            extras={
                "active_reply_triggered": {},
                "send_gate_active_candidate": {
                    "group_id": "60008",
                    "message_id": "cancel-candidate",
                    "root_text": "候选",
                    "created_at": time.time() - 30,
                    "cooldown_prev": 0,
                },
            },
        )
        req = SimpleNamespace(prompt="候选", image_urls=[])
        run(observer.observe_waiting(event))
        for lane in observer._lanes.values():
            lane.first_at -= 30
            lane.last_at -= 30
        with self.assertRaises(asyncio.CancelledError):
            run(observer.observe_llm(event, req))
        self.assertEqual(dropped, [event])

    def test_followup_duplicate_does_not_rollback_cooldown(self):
        self.config.values.update(
            {
                "send_gate_enforce": True,
                "send_gate_dry_run": False,
                "send_gate_min_wait_seconds": 0,
                "send_gate_max_window_seconds": 10,
            }
        )
        dropped = []

        def drop(event):
            dropped.append(event)

        observer = SendGateObserver(
            config=self.config,
            data_dir_getter=lambda: Path(self.tmp.name),
            logger=FakeLogger(),
            candidate_dropped=drop,
        )
        root = FakeEvent(text="root", msg_id="follow-root", group="60010")
        followup = FakeEvent(
            text="follow-up",
            msg_id="follow-followup",
            group="60010",
            extras={"_follow_up_captured": {"x": 1}},
        )
        run(observer.observe_waiting(root))
        run(observer.observe_waiting(followup))
        self.assertTrue(followup.stopped)
        self.assertEqual(dropped, [])

    def test_duplicate_candidate_in_active_lane_restores_cooldown(self):
        self.config.values.update(
            {
                "send_gate_enforce": True,
                "send_gate_dry_run": False,
                "send_gate_min_wait_seconds": 0,
                "send_gate_max_window_seconds": 10,
            }
        )
        dropped = []

        def drop(event):
            dropped.append(event)

        observer = SendGateObserver(
            config=self.config,
            data_dir_getter=lambda: Path(self.tmp.name),
            logger=FakeLogger(),
            candidate_dropped=drop,
        )
        root = FakeEvent(text="root", msg_id="dup-root", group="60009")
        candidate = FakeEvent(
            text="候选",
            msg_id="dup-candidate",
            group="60009",
            extras={
                "active_reply_triggered": {},
                "send_gate_active_candidate": {
                    "group_id": "60009",
                    "message_id": "dup-candidate",
                    "cooldown_prev": 0,
                },
            },
        )
        run(observer.observe_waiting(root))
        run(observer.observe_waiting(candidate))
        self.assertTrue(candidate.stopped)
        self.assertEqual(dropped, [candidate])

    def test_duplicate_proactive_in_active_lane_is_stopped(self):
        self.config.values.update(
            {
                "send_gate_enforce": True,
                "send_gate_dry_run": False,
                "send_gate_min_wait_seconds": 0,
                "send_gate_max_window_seconds": 10,
            }
        )
        root = FakeEvent(text="root", msg_id="r1", group="50005")
        dup = FakeEvent(
            text="follow-up",
            msg_id="r2",
            group="50005",
            extras={"active_reply_triggered": {"x": 1}},
        )
        run(self.observer.observe_waiting(root))
        run(self.observer.observe_waiting(dup))
        self.assertTrue(dup.stopped)
        actions = [r["action"] for r in self._records()]
        self.assertIn("duplicate_nonmergeable_stop", actions)

    def test_release_closes_lane(self):
        first = FakeEvent(text="第一句", msg_id="m1")
        second = FakeEvent(text="释放后的新消息", msg_id="m2")
        run(self.observer.observe_waiting(first))
        run(
            self.observer.observe_llm(first, SimpleNamespace(prompt="p", image_urls=[]))
        )
        run(self.observer.observe_waiting(second))
        actions = [r["action"] for r in self._records()]
        self.assertEqual(actions, ["would_root", "would_release", "would_root"])

    def test_disabled_is_noop(self):
        self.config.values["send_gate_enable"] = False
        event = FakeEvent()
        run(self.observer.observe_waiting(event))
        self.assertEqual(self._records(), [])
        self.assertFalse(event.stopped)

    def test_group_filter(self):
        self.config.values["send_gate_enabled_groups"] = ["99999"]
        event = FakeEvent(group="10001")
        run(self.observer.observe_waiting(event))
        self.assertEqual(self._records(), [])

    def test_window_expiry_creates_new_root(self):
        self.config.values["send_gate_max_window_seconds"] = 1
        first = FakeEvent(msg_id="old")
        run(self.observer.observe_waiting(first))
        # Simulate expiry without sleeping by editing lane state.
        for lane in self.observer._lanes.values():
            lane.first_at -= 5
            lane.last_at -= 5
        second = FakeEvent(msg_id="new")
        run(self.observer.observe_waiting(second))
        actions = [r["action"] for r in self._records()]
        self.assertEqual(actions, ["would_root", "would_root"])


if __name__ == "__main__":
    unittest.main()
