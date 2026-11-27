"""注意力门控的纯逻辑测试。"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from astrbot.api.message_components import Plain  # noqa: E402

from main import (  # noqa: E402
    ACTIVE_CANDIDATE_EXTRA,
    DEFAULT_GROUP_TOPIC_SCHEMES,
    DEFAULT_QUESTIONS,
    VERSION,
    AttentionGatePlugin,
    cosine_similarity,
    decide_answers,
    extract_nouls,
    impulse_cue,
    normalize_group_schemes,
    normalize_interest_groups,
    normalize_interests,
    parse_topic_lines,
    profile_text,
    question_schemes_to_questions,
)


class FakeConfig:
    def __init__(self, values: dict | None = None) -> None:
        self.values = values or {}

    def get(self, key: str, default=None):
        return self.values.get(key, default)


class FakeMessage:
    def __init__(
        self, text: str = "今天摸鱼好开心", message_id: str = "123456"
    ) -> None:
        self.text = text  # type: ignore[assignment]
        self.message_id = message_id
        self.raw_message = {"message_id": message_id}


class FakeEvent:
    def __init__(
        self, text: str = "今天摸鱼好开心", message_id: str = "123456"
    ) -> None:
        self.message_str = text
        self.message_obj = FakeMessage(text, message_id)
        self.is_at_or_wake_command = False
        self.is_wake = False
        self.unified_msg_origin = "napcat:GroupMessage:123456789"
        self.session_id = "session"
        self.bot = None
        self.extras: dict[str, object] = {}
        self.requests: list[object] = []
        self._messages = [Plain(text)]

    def is_private_chat(self) -> bool:
        return False

    def is_stopped(self) -> bool:
        return False

    def get_group_id(self) -> str:
        return "123456789"

    def get_self_id(self) -> str:
        return "10000"

    def get_sender_id(self) -> str:
        return "10001"

    def get_sender_name(self) -> str:
        return "张三"

    def get_platform_id(self) -> str:
        return "napcat"

    def get_messages(self):
        return self._messages

    def get_extra(self, key: str, default=None):
        return self.extras.get(key, default)

    def set_extra(self, key: str, value) -> None:
        self.extras[key] = value

    def request_llm(self, **kwargs):
        request = {"kwargs": kwargs}
        self.requests.append(request)
        return request

    def get_result(self):
        return None


class FakeConversationManager:
    async def get_curr_conversation_id(self, umo):
        return "cid"

    async def get_conversation(self, umo, cid):
        return object()


class FakeContext:
    def __init__(self) -> None:
        self.conversation_manager = FakeConversationManager()


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_act_sets_quote_target_and_request(self) -> None:
        plugin = object.__new__(AttentionGatePlugin)
        plugin.config = FakeConfig(
            {
                "enabled_groups": ["123456789"],
                "dry_run": False,
                "api_key": "test",
                "quote_target_enabled": True,
                "cooldown_seconds": 120,
                "hourly_limit": 15,
                "active_reply_settle_seconds": 0,
            }
        )
        plugin.context = FakeContext()
        plugin._states = {}
        plugin._warned_no_key = False
        plugin._data_dir = None
        plugin._ledger = lambda record: None

        async def fake_call_jev(state, questions, **kwargs):
            return {
                "answers": {
                    "is_persona_fit": {"type": "noul", "noul": 0.9},
                    "is_group_topic_fit": {"type": "noul", "noul": 0.8},
                    "is_good_moment": {"type": "noul", "noul": 0.9},
                    "is_risky": {"type": "noul", "noul": 0.1},
                    "is_continuation": {"type": "noul", "noul": 0.1},
                    "reply_value": {"type": "score", "score": 2.0},
                }
            }

        plugin._call_jev = fake_call_jev  # type: ignore[assignment]
        event = FakeEvent()
        requests = []
        async for request in plugin.on_group_message(event):
            requests.append(request)

        self.assertEqual(len(requests), 1)
        self.assertEqual(event.get_extra("attention_quote_target"), "123456")
        self.assertTrue(event.get_extra("active_reply_triggered"))
        state = plugin._state("123456789")
        self.assertGreater(state["cooldown_until"], 0)
        self.assertEqual(len(state["dispatches"]), 1)

    async def test_direct_message_is_buffered_but_not_judged(self) -> None:
        plugin = object.__new__(AttentionGatePlugin)
        plugin.config = FakeConfig(
            {
                "enabled_groups": ["123456789"],
                "dry_run": False,
                "api_key": "test",
            }
        )
        plugin.context = FakeContext()
        plugin._states = {}
        plugin._warned_no_key = False
        plugin._data_dir = None
        plugin._ledger = lambda record: None

        calls = []

        async def fake_call_jev(state, questions, **kwargs):
            calls.append(1)
            return {"answers": {}}

        plugin._call_jev = fake_call_jev  # type: ignore[assignment]
        event = FakeEvent()
        event.is_at_or_wake_command = True
        async for _ in plugin.on_group_message(event):
            pass
        self.assertEqual(calls, [])
        self.assertEqual(len(plugin._state("123456789")["buffer"]), 1)


class DecideTests(unittest.TestCase):
    def test_persona_interest_can_act(self) -> None:
        act, reason = decide_answers(
            {
                "is_persona_fit": 0.9,
                "is_group_topic_fit": 0.1,
                "is_good_moment": 0.8,
                "is_risky": 0.1,
                "is_continuation": 0.1,
            },
            1.8,
            continuation=False,
        )
        self.assertTrue(act)
        self.assertIn("value", reason)

    def test_group_topic_can_act(self) -> None:
        act, _ = decide_answers(
            {
                "is_persona_fit": 0.1,
                "is_group_topic_fit": 0.9,
                "is_good_moment": 0.8,
                "is_risky": 0.1,
            },
            1.8,
            continuation=False,
        )
        self.assertTrue(act)

    def test_both_interests_low_passes(self) -> None:
        act, _ = decide_answers(
            {
                "is_persona_fit": 0.1,
                "is_group_topic_fit": 0.1,
                "is_good_moment": 0.9,
                "is_risky": 0.1,
            },
            2.5,
            continuation=False,
        )
        self.assertFalse(act)

    def test_risk_passes(self) -> None:
        act, reason = decide_answers(
            {
                "is_persona_fit": 0.9,
                "is_group_topic_fit": 0.9,
                "is_good_moment": 0.9,
                "is_risky": 0.8,
            },
            3.0,
            continuation=False,
        )
        self.assertFalse(act)
        self.assertIn("risk", reason)

    def test_bad_moment_passes(self) -> None:
        act, reason = decide_answers(
            {
                "is_persona_fit": 0.9,
                "is_group_topic_fit": 0.9,
                "is_good_moment": 0.1,
                "is_risky": 0.1,
            },
            3.0,
            continuation=False,
        )
        self.assertFalse(act)
        self.assertIn("moment", reason)

    def test_directed_to_other_passes_even_with_high_value(self) -> None:
        act, reason = decide_answers(
            {
                "is_mentioned": 0.9,
                "is_persona_fit": 0.9,
                "is_group_topic_fit": 0.9,
                "is_good_moment": 0.9,
                "is_risky": 0.1,
                "is_directed_elsewhere": 0.9,
                "has_reply_hook": 0.3,
            },
            2.8,
            continuation=False,
        )
        self.assertFalse(act)
        self.assertIn("directed_elsewhere", reason)

    def test_low_reply_hook_passes(self) -> None:
        act, reason = decide_answers(
            {
                "is_persona_fit": 0.9,
                "is_group_topic_fit": 0.9,
                "is_good_moment": 0.9,
                "is_risky": 0.1,
                "has_reply_hook": 0.2,
            },
            2.8,
            continuation=False,
        )
        self.assertFalse(act)
        self.assertIn("no_reply_hook", reason)

    def test_high_value_with_hook_and_no_direction_acts(self) -> None:
        act, reason = decide_answers(
            {
                "is_persona_fit": 0.9,
                "is_group_topic_fit": 0.9,
                "is_good_moment": 0.9,
                "is_risky": 0.1,
                "is_directed_elsewhere": 0.2,
                "has_reply_hook": 0.8,
            },
            2.8,
            continuation=False,
        )
        self.assertTrue(act)
        self.assertIn("value", reason)

    def test_addressed_to_bot_by_name_bypasses_directed_gate(self) -> None:
        act, reason = decide_answers(
            {
                "is_persona_fit": 0.9,
                "is_group_topic_fit": 0.9,
                "is_good_moment": 0.9,
                "is_risky": 0.1,
                "is_directed_elsewhere": 0.95,
                "has_reply_hook": 0.2,
            },
            2.8,
            continuation=False,
            addressed_to_bot=True,
        )
        self.assertTrue(act)
        self.assertIn("value", reason)

    def test_answer_seeking_passes(self) -> None:
        act, reason = decide_answers(
            {
                "is_persona_fit": 0.9,
                "is_group_topic_fit": 0.9,
                "is_good_moment": 0.9,
                "is_risky": 0.1,
                "is_directed_elsewhere": 0.2,
                "has_reply_hook": 0.9,
                "is_answer_seeking": 0.9,
            },
            2.8,
            continuation=False,
        )
        self.assertFalse(act)
        self.assertIn("answer_seeking", reason)

    def test_answer_seeking_ignored_when_named(self) -> None:
        act, reason = decide_answers(
            {
                "is_persona_fit": 0.9,
                "is_group_topic_fit": 0.9,
                "is_good_moment": 0.9,
                "is_risky": 0.1,
                "is_directed_elsewhere": 0.2,
                "has_reply_hook": 0.9,
                "is_answer_seeking": 0.9,
            },
            2.8,
            continuation=False,
            addressed_to_bot=True,
        )
        self.assertTrue(act)
        self.assertIn("value", reason)

    def test_incomplete_turn_passes(self) -> None:
        act, reason = decide_answers(
            {
                "is_persona_fit": 0.9,
                "is_group_topic_fit": 0.9,
                "is_good_moment": 0.9,
                "is_risky": 0.1,
                "is_directed_elsewhere": 0.2,
                "has_reply_hook": 0.9,
                "is_answer_seeking": 0.1,
                "is_complete_turn": 0.2,
            },
            2.8,
            continuation=False,
        )
        self.assertFalse(act)
        self.assertIn("incomplete_turn", reason)

    def test_incomplete_turn_ignored_when_named(self) -> None:
        act, reason = decide_answers(
            {
                "is_persona_fit": 0.9,
                "is_group_topic_fit": 0.9,
                "is_good_moment": 0.9,
                "is_risky": 0.1,
                "is_directed_elsewhere": 0.2,
                "has_reply_hook": 0.9,
                "is_answer_seeking": 0.1,
                "is_complete_turn": 0.2,
            },
            2.8,
            continuation=False,
            addressed_to_bot=True,
        )
        self.assertTrue(act)
        self.assertIn("value", reason)

    def test_continuation_can_act_with_lower_value(self) -> None:
        act, _ = decide_answers(
            {
                "is_persona_fit": 0.7,
                "is_group_topic_fit": 0.2,
                "is_good_moment": 0.8,
                "is_risky": 0.1,
                "is_continuation": 0.9,
            },
            1.2,
            continuation=True,
        )
        self.assertTrue(act)

    def test_mention_can_act(self) -> None:
        act, _ = decide_answers(
            {
                "is_mentioned": 0.9,
                "is_persona_fit": 0.1,
                "is_group_topic_fit": 0.1,
                "is_good_moment": 0.8,
                "is_risky": 0.1,
            },
            1.2,
            continuation=False,
        )
        self.assertTrue(act)

    def test_low_value_passes(self) -> None:
        act, _ = decide_answers(
            {
                "is_persona_fit": 0.9,
                "is_group_topic_fit": 0.9,
                "is_good_moment": 0.9,
                "is_risky": 0.1,
            },
            0.8,
            continuation=False,
        )
        self.assertFalse(act)

    def test_impulse_can_act_without_interest(self) -> None:
        act, reason = decide_answers(
            {
                "is_persona_fit": 0.1,
                "is_group_topic_fit": 0.1,
                "is_good_moment": 0.8,
                "is_risky": 0.1,
                "is_impulse": 0.9,
            },
            1.4,
            continuation=False,
        )
        self.assertTrue(act)
        self.assertIn("impulse", reason)

    def test_impulse_high_risk_passes(self) -> None:
        act, _ = decide_answers(
            {
                "is_persona_fit": 0.1,
                "is_group_topic_fit": 0.1,
                "is_good_moment": 0.9,
                "is_risky": 0.4,
                "is_impulse": 0.9,
            },
            2.0,
            continuation=False,
        )
        self.assertFalse(act)


class ParsingTests(unittest.TestCase):
    def test_cosine_similarity(self) -> None:
        self.assertAlmostEqual(cosine_similarity([1.0, 0.0], [1.0, 0.0]), 1.0)
        self.assertAlmostEqual(cosine_similarity([1.0, 0.0], [0.0, 1.0]), 0.0)
        self.assertEqual(cosine_similarity([], [1.0]), 0.0)

    def test_impulse_cue(self) -> None:
        self.assertTrue(impulse_cue("哈哈哈哈这什么鬼"))
        self.assertTrue(impulse_cue("这也太离谱了吧？"))
        self.assertFalse(impulse_cue("今天开会讨论一下项目进度"))

    def test_normalize_interests(self) -> None:
        result = normalize_interests(
            [{"name": "游戏", "weight": "80", "keywords": ["MC", ""]}]
        )
        self.assertEqual(result[0]["name"], "游戏")
        self.assertEqual(result[0]["weight"], 80.0)
        self.assertEqual(result[0]["keywords"], ["MC"])
        self.assertIn("游戏", profile_text(result))

    def test_extract_nouls_and_score(self) -> None:
        nouls, score = extract_nouls(
            {
                "is_persona_fit": {"type": "noul", "noul": 0.7},
                "reply_value": {"type": "score", "score": 2},
                "ignore": {"type": "other"},
            }
        )
        self.assertEqual(nouls["is_persona_fit"], 0.7)
        self.assertEqual(score, 2.0)

    def test_interest_group_template_parsing(self) -> None:
        result = normalize_interest_groups(
            [
                {
                    "name": "鲸鱼",
                    "enabled": True,
                    "weight": 90,
                    "keywords": ["鲸", "摸鱼"],
                    "aliases": "鲸鱼=大肥鱼,小肥鱼",
                },
                {"name": "关闭方向", "enabled": False, "keywords": ["x"]},
            ]
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["aliases"]["鲸鱼"], ["大肥鱼", "小肥鱼"])

    def test_passive_interest_flag(self) -> None:
        result = normalize_interest_groups(
            [
                {
                    "name": "解题",
                    "enabled": True,
                    "keywords": ["题"],
                    "proactive": False,
                },
                {"name": "鲸鱼", "enabled": True, "keywords": ["鲸"]},
            ]
        )
        self.assertFalse(result[0]["proactive"])
        self.assertTrue(result[1]["proactive"])

    def test_passive_topic_flag(self) -> None:
        topics = parse_topic_lines(["游戏|85|MC,开黑", "群友日常|45|今天,上班|被动"])
        self.assertTrue(topics[0]["proactive"])
        self.assertFalse(topics[1]["proactive"])

    def test_group_topic_scheme_parsing(self) -> None:
        schemes = normalize_group_schemes(
            [
                {
                    "group_id": "123456789",
                    "enabled": True,
                    "topics": ["游戏|85|游戏,MC", "AI|75|模型,token"],
                }
            ]
        )
        topics = schemes["123456789"]["topics"]
        self.assertEqual(topics[0]["name"], "游戏")
        self.assertEqual(topics[1]["keywords"], ["模型", "token"])

    def test_topic_lines_ignore_bad_lines(self) -> None:
        topics = parse_topic_lines(["游戏|80|MC", "bad line", "# comment"])
        self.assertEqual(len(topics), 1)

    def test_question_template_parsing(self) -> None:
        questions = question_schemes_to_questions(
            [
                {
                    "id": "is_impulse",
                    "enabled": True,
                    "type": "noul",
                    "instructions": "是否想接？",
                    "true_criteria": "想",
                    "false_criteria": "不想",
                },
                {
                    "id": "reply_value",
                    "enabled": True,
                    "type": "score",
                    "instructions": "价值",
                    "score_levels": ["低", "中", "高"],
                },
            ]
        )
        self.assertEqual(questions["is_impulse"]["criteria"]["true"], "想")
        self.assertEqual(questions["reply_value"]["criteria"], ["低", "中", "高"])

    def test_questions_have_two_interest_layers(self) -> None:
        self.assertIn("is_persona_fit", DEFAULT_QUESTIONS)
        self.assertIn("is_group_topic_fit", DEFAULT_QUESTIONS)
        self.assertIn("is_impulse", DEFAULT_QUESTIONS)
        # 内置默认值不包含任何个人群号或话题模板，部署者按需配置。
        self.assertEqual(DEFAULT_GROUP_TOPIC_SCHEMES, [])


class EndpointTests(unittest.TestCase):
    def _endpoint(self, value: str) -> str:
        plugin = object.__new__(AttentionGatePlugin)
        plugin.config = FakeConfig({"jev_provider_url": value})
        return plugin._endpoint_url()

    def test_root_url_adds_systemone(self) -> None:
        self.assertEqual(
            self._endpoint("http://127.0.0.1:8080"),
            "http://127.0.0.1:8080/v1/systemone",
        )

    def test_v1_url_adds_systemone(self) -> None:
        self.assertEqual(
            self._endpoint("http://127.0.0.1:8080/v1"),
            "http://127.0.0.1:8080/v1/systemone",
        )

    def test_full_systemone_url_is_kept(self) -> None:
        url = "http://127.0.0.1:8080/v1/systemone"
        self.assertEqual(self._endpoint(url), url)

    def test_full_decision_url_is_kept(self) -> None:
        url = "http://127.0.0.1:8080/v1/decision"
        self.assertEqual(self._endpoint(url), url)


class ActiveCandidateCooldownTests(unittest.IsolatedAsyncioTestCase):
    def _plugin(
        self, cooldown_prev: float = 5.0
    ) -> tuple[AttentionGatePlugin, object, dict]:
        plugin = object.__new__(AttentionGatePlugin)
        plugin.config = FakeConfig({"cooldown_seconds": 120})
        plugin._states = {}
        plugin._data_dir = None
        plugin._ledger = lambda record: None
        event = FakeEvent()
        event.set_extra(
            ACTIVE_CANDIDATE_EXTRA,
            {
                "group_id": "123456789",
                "message_id": "123456",
                "cooldown_prev": cooldown_prev,
            },
        )
        state = plugin._state("123456789")
        state["cooldown_until"] = 999.0
        return plugin, event, state

    async def test_restore_is_idempotent(self) -> None:
        plugin, event, state = self._plugin()
        plugin._restore_active_candidate_cooldown(event)
        self.assertEqual(state["cooldown_until"], 5.0)
        state["cooldown_until"] = 123.0
        plugin._restore_active_candidate_cooldown(event)
        self.assertEqual(state["cooldown_until"], 123.0)

    async def test_plain_event_restore_is_noop(self) -> None:
        plugin = object.__new__(AttentionGatePlugin)
        plugin.config = FakeConfig({})
        plugin._states = {}
        plugin._ledger = lambda record: None
        state = plugin._state("123456789")
        state["cooldown_until"] = 100.0
        event = FakeEvent()
        plugin._restore_active_candidate_cooldown(event)
        self.assertEqual(state["cooldown_until"], 100.0)

    async def test_candidate_pending_timeout_restores_previous_cooldown(self) -> None:
        plugin = object.__new__(AttentionGatePlugin)
        plugin.config = FakeConfig({"send_gate_candidate_pending_timeout_seconds": 10})
        plugin._states = {}
        plugin._ledger = lambda record: None
        state = plugin._state("123456789")
        state["cooldown_until"] = 999.0
        state["active_candidate_pending"] = {
            "id": "p1",
            "at": 100.0,
            "prev": 5.0,
            "reserved_until": 999.0,
        }
        plugin._expire_candidate_pending(state, 1000.0)
        self.assertEqual(state["cooldown_until"], 5.0)
        self.assertIsNone(state["active_candidate_pending"])

    async def test_restore_does_not_clobber_newer_dispatch(self) -> None:
        plugin, event, state = self._plugin()
        payload = event.get_extra(ACTIVE_CANDIDATE_EXTRA)
        payload["cooldown_reserved_until"] = 999.0
        payload["cooldown_prev"] = 5.0
        event.set_extra(ACTIVE_CANDIDATE_EXTRA, payload)
        state["cooldown_until"] = 1234.0
        plugin._restore_active_candidate_cooldown(event)
        self.assertEqual(state["cooldown_until"], 1234.0)

    async def test_cancelled_judge_restores_cooldown(self) -> None:
        plugin, event, state = self._plugin(cooldown_prev=7.0)
        target = {"message_id": "123456", "sender_id": "10001", "content": "x"}
        plugin._find_buffer_record = lambda group_id, message_id: target  # type: ignore[assignment]
        plugin._judge = lambda *args, **kwargs: _raise_cancelled()  # type: ignore[assignment]
        with self.assertRaises(asyncio.CancelledError):
            await plugin._send_gate_active_judge(
                event, SimpleNamespace(prompt="x"), object()
            )
        self.assertEqual(state["cooldown_until"], 7.0)


async def _raise_cancelled():
    raise asyncio.CancelledError


class JevRequestTests(unittest.TestCase):
    def test_no_key_omits_authorization_and_uses_configured_model(self) -> None:
        import urllib.request
        from unittest.mock import patch

        plugin = object.__new__(AttentionGatePlugin)
        plugin.config = FakeConfig(
            {
                "jev_provider_url": "http://127.0.0.1:8080",
                "model": "NeoHorse-Jev-4B",
                "api_key": "",
            }
        )
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"answers": {}}'

        def fake_urlopen(request, timeout=0):
            captured["url"] = request.full_url
            captured["headers"] = dict(request.header_items())
            captured["body"] = request.data
            return Response()

        with patch.object(urllib.request, "urlopen", fake_urlopen):
            plugin._call_jev_sync({"state": "x"}, {"q": {"type": "noul"}})

        self.assertEqual(captured["url"], "http://127.0.0.1:8080/v1/systemone")
        self.assertNotIn("Authorization", captured["headers"])
        self.assertEqual(
            captured["headers"].get("User-agent"),
            f"astrbot-plugin-attention-gate/{VERSION}",
        )
        self.assertIn(b"NeoHorse-Jev-4B", captured["body"])
        payload = json.loads(captured["body"].decode("utf-8"))
        self.assertIsInstance(payload["state"], str)


class JevTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_gate_budget_clamps_configured_timeout(self) -> None:
        plugin = object.__new__(AttentionGatePlugin)
        plugin.config = FakeConfig({"jev_timeout_seconds": 25.0})
        captured = {}

        def fake_sync(state, questions, timeout=30.0):
            captured["timeout"] = timeout
            return {"answers": {}}

        plugin._call_jev_sync = fake_sync  # type: ignore[assignment]
        await plugin._call_jev({}, {}, timeout=1.5)
        self.assertGreater(captured["timeout"], 0)
        self.assertLessEqual(captured["timeout"], 1.5)
        # 默认 25 秒不能让发送窗口里的请求一直占到最后。
        self.assertLess(captured["timeout"], 25.0)


class PrefilterTests(unittest.IsolatedAsyncioTestCase):
    def _plugin(self) -> AttentionGatePlugin:
        plugin = object.__new__(AttentionGatePlugin)
        plugin.config = FakeConfig(
            {
                "prefilter_enabled": True,
                "embedding_threshold": 0.42,
                "embedding_low_threshold": 0.30,
                "impulse_enabled": True,
                "global_interests": [
                    {
                        "name": "测试兴趣",
                        "weight": 50,
                        "keywords": ["alpha"],
                        "proactive": True,
                    }
                ],
            }
        )
        plugin._msg_embedding_cache = {}
        plugin._profile_embedding_cache = {}
        return plugin

    async def test_embedding_low_skips_plain_chat(self) -> None:
        plugin = self._plugin()

        async def fake_msg(text):
            return [1.0, 0.0]

        async def fake_profiles(group_id, docs):
            return [[0.0, 1.0]]

        plugin._get_message_embedding = fake_msg  # type: ignore[assignment]
        plugin._get_profile_embeddings = fake_profiles  # type: ignore[assignment]
        ok, reason = await plugin._prefilter(
            "123456789",
            {"content": "zzzzz", "sender_name": "张三"},
        )
        self.assertFalse(ok)
        self.assertIn("prefilter_low", reason)

    async def test_embedding_high_passes(self) -> None:
        plugin = self._plugin()

        async def fake_msg(text):
            return [1.0, 0.0]

        async def fake_profiles(group_id, docs):
            return [[1.0, 0.0]]

        plugin._get_message_embedding = fake_msg  # type: ignore[assignment]
        plugin._get_profile_embeddings = fake_profiles  # type: ignore[assignment]
        ok, reason = await plugin._prefilter(
            "123456789",
            {"content": "zzzzz", "sender_name": "张三"},
        )
        self.assertTrue(ok)
        self.assertIn("embedding_high", reason)

    async def test_impulse_cue_bypasses_low_match(self) -> None:
        plugin = self._plugin()

        async def fake_msg(text):
            return [1.0, 0.0]

        async def fake_profiles(group_id, docs):
            return [[0.0, 1.0]]

        plugin._get_message_embedding = fake_msg  # type: ignore[assignment]
        plugin._get_profile_embeddings = fake_profiles  # type: ignore[assignment]
        ok, reason = await plugin._prefilter(
            "123456789",
            {"content": "zzzzz？？哈哈", "sender_name": "张三"},
        )
        self.assertTrue(ok)
        self.assertIn("impulse_cue_bypass", reason)

    async def test_profile_embedding_splits_batches(self) -> None:
        plugin = self._plugin()
        plugin.config = FakeConfig({"embedding_batch_size": 10})
        calls = []

        class Provider:
            async def get_embeddings(self, docs):
                calls.append(len(docs))
                return [[1.0, 0.0] for _ in docs]

        plugin._embedding_provider = lambda: Provider()  # type: ignore[assignment]
        docs = [f"doc{i}" for i in range(12)]
        vectors = await plugin._get_profile_embeddings("g", docs)
        self.assertIsNotNone(vectors)
        self.assertEqual(calls, [10, 2])
        self.assertEqual(len(vectors), 12)


class ProviderSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_forces_main_chat_provider(self) -> None:
        plugin = object.__new__(AttentionGatePlugin)
        plugin.config = FakeConfig({"use_main_chat_provider": True})

        class FakeContext:
            async def get_current_chat_provider_id(self, umo):
                return "main/model"

        class FakeEvent:
            unified_msg_origin = "napcat:GroupMessage:123456789"

            def __init__(self):
                self.extras = {}

            def set_extra(self, key, value):
                self.extras[key] = value

        plugin.context = FakeContext()
        event = FakeEvent()
        provider_id = await plugin._force_main_chat_provider(event)
        self.assertEqual(provider_id, "main/model")
        self.assertEqual(event.extras["selected_provider"], "main/model")


class PromptTests(unittest.TestCase):
    def test_dispatch_prompt_forbids_quote_tags(self) -> None:
        plugin = object.__new__(AttentionGatePlugin)
        plugin.config = FakeConfig(
            {
                "prompt_context_mode": "full",
                "context_window": 5,
            }
        )
        prompt = plugin._dispatch_prompt(
            "123456789",
            {"buffer": []},
            {"message_id": "123", "sender_name": "张三", "content": "测试"},
            "value(1.8)",
        )
        self.assertIn("不要输出 [quote]", prompt)
        self.assertIn("系统会自动引用目标消息", prompt)


class QuoteTests(unittest.IsolatedAsyncioTestCase):
    async def test_force_quote_target_inserts_reply(self) -> None:
        from astrbot.api.message_components import Reply

        plugin = object.__new__(AttentionGatePlugin)
        plugin.config = FakeConfig({"enable": True, "quote_target_enabled": True})

        class Result:
            def __init__(self):
                self.chain = [Plain("ok")]

        class Event:
            def __init__(self):
                self.extras = {"attention_quote_target": "123456"}
                self.result = Result()

            def get_extra(self, key, default=None):
                return self.extras.get(key, default)

            def get_result(self):
                return self.result

        event = Event()
        await plugin.force_quote_target(event)
        self.assertIsInstance(event.result.chain[0], Reply)
        self.assertEqual(str(event.result.chain[0].id), "123456")


class StateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plugin = object.__new__(AttentionGatePlugin)
        self.plugin.config = FakeConfig({"hourly_limit": 2, "cooldown_seconds": 120})
        self.plugin._states = {}

    def test_empty_whitelist_means_all_groups(self) -> None:
        self.plugin.config = FakeConfig({"enabled_groups": []})
        self.assertTrue(self.plugin._group_allowed("123456"))
        self.plugin.config = FakeConfig({"enabled_groups": ["123456789"]})
        self.assertTrue(self.plugin._group_allowed("123456789"))
        self.assertFalse(self.plugin._group_allowed("123456"))

    def test_missing_group_topics_removes_group_topic_question(self) -> None:
        self.plugin.config = FakeConfig(
            {
                "judge_questions": [
                    {"id": "is_persona_fit", "type": "noul", "instructions": "a"},
                    {"id": "is_group_topic_fit", "type": "noul", "instructions": "b"},
                ],
                "group_topic_schemes": [],
            }
        )
        questions = self.plugin._questions("1", "999999")
        self.assertIn("is_persona_fit", questions)
        self.assertNotIn("is_group_topic_fit", questions)

    def test_group_topics_keep_group_topic_question(self) -> None:
        self.plugin.config = FakeConfig(
            {
                "judge_questions": [
                    {"id": "is_persona_fit", "type": "noul", "instructions": "a"},
                    {"id": "is_group_topic_fit", "type": "noul", "instructions": "b"},
                ],
                "group_topic_schemes": [
                    {"group_id": "999999", "topics": ["游戏|80|游戏,MC"]}
                ],
            }
        )
        questions = self.plugin._questions("1", "999999")
        self.assertIn("is_group_topic_fit", questions)

    def test_hourly_limit(self) -> None:
        state = self.plugin._state("g")
        self.assertFalse(self.plugin._hour_limit_reached(state, 1000.0))
        state["dispatches"].extend([1000.0, 1001.0])
        self.assertTrue(self.plugin._hour_limit_reached(state, 1002.0))
        self.assertFalse(self.plugin._hour_limit_reached(state, 1001.0 + 3601.0))

    def test_cooldown(self) -> None:
        state = self.plugin._state("g")
        state["cooldown_until"] = 100.0
        self.assertTrue(self.plugin._in_cooldown(state, 99.0))
        self.assertFalse(self.plugin._in_cooldown(state, 100.0))

    def test_density_limit(self) -> None:
        self.plugin.config = FakeConfig(
            {"density_window_seconds": 900, "density_max_dispatches": 2}
        )
        state = self.plugin._state("g")
        state["dispatches"].extend([1000.0, 1100.0])
        self.assertTrue(self.plugin._density_limit_reached(state, 1200.0))
        self.assertFalse(self.plugin._density_limit_reached(state, 1901.0))

    def test_unanswered_cache(self) -> None:
        state = self.plugin._state("g")
        self.plugin._note_unanswered(
            state,
            {"message_id": "1", "sender_name": "张三", "content": "还没接的话"},
        )
        self.plugin._note_unanswered(
            state,
            {
                "message_id": "2",
                "sender_name": "机器人",
                "content": "自己",
                "is_self": True,
            },
        )
        self.assertEqual(len(state["unanswered"]), 1)
        self.assertEqual(state["unanswered"][0]["content"], "还没接的话")

    def test_post_reply_penalty(self) -> None:
        self.plugin.config = FakeConfig(
            {
                "reply_value_threshold": 1.8,
                "post_reply_penalty_seconds": 600,
                "post_reply_threshold_penalty": 0.2,
            }
        )
        state = self.plugin._state("g")
        state["last_proactive_at"] = __import__("time").time()
        th = self.plugin._thresholds_for_state(state)
        self.assertAlmostEqual(th["reply_value"], 2.0)

    def test_user_attention_penalty(self) -> None:
        self.plugin.config = FakeConfig(
            {
                "reply_value_threshold": 1.8,
                "user_attention_window_seconds": 1800,
                "user_repeat_penalty": 0.15,
            }
        )
        state = self.plugin._state("g")
        state["user_dispatches"]["u1"] = __import__("collections").deque(
            [__import__("time").time()]
        )
        th = self.plugin._thresholds_for_state(state, "u1")
        self.assertAlmostEqual(th["reply_value"], 1.95)
        self.assertAlmostEqual(
            self.plugin._thresholds_for_state(state, "u2")["reply_value"], 1.8
        )

    def test_jev_state_is_bounded(self) -> None:
        self.plugin.config = FakeConfig({"jev_max_state_chars": 2200})
        state = {
            "recent_conversation": [f"消息{i} " + "长" * 200 for i in range(20)],
            "recent_unanswered": [{"content": "长" * 200} for _ in range(6)],
            "target_message": {
                "content": "长" * 500,
                "turn": [{"content": "长" * 300} for _ in range(6)],
                "wait_batch": [{"content": "长" * 200} for _ in range(20)],
            },
        }
        result = self.plugin._fit_jev_state(state)
        self.assertLessEqual(
            len(__import__("json").dumps(result, ensure_ascii=False)), 2200
        )

    def test_target_turn_groups_same_sender(self) -> None:
        state = self.plugin._state("g")
        state["buffer"].extend(
            [
                {"message_id": "1", "sender_id": "a", "content": "前半句", "ts": 100.0},
                {"message_id": "2", "sender_id": "b", "content": "插话", "ts": 101.0},
                {"message_id": "3", "sender_id": "a", "content": "后半句", "ts": 102.0},
            ]
        )
        target = {"message_id": "3", "sender_id": "a", "content": "后半句", "ts": 102.0}
        turn = self.plugin._target_turn(state, target)
        self.assertEqual([x["content"] for x in turn], ["后半句"])


if __name__ == "__main__":
    unittest.main()
