"""注意力门控的纯逻辑测试。"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from main import (  # noqa: E402
    AttentionGatePlugin,
    DEFAULT_GROUP_TOPIC_SCHEMES,
    DEFAULT_QUESTIONS,
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
from astrbot.api.message_components import Plain  # noqa: E402


class FakeConfig:
    def __init__(self, values: dict | None = None) -> None:
        self.values = values or {}

    def get(self, key: str, default=None):
        return self.values.get(key, default)


class FakeMessage:
    def __init__(self, text: str = "今天摸鱼好开心", message_id: str = "123456") -> None:
        self.text = text  # type: ignore[assignment]
        self.message_id = message_id
        self.raw_message = {"message_id": message_id}


class FakeEvent:
    def __init__(self, text: str = "今天摸鱼好开心", message_id: str = "123456") -> None:
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
            }
        )
        plugin.context = FakeContext()
        plugin._states = {}
        plugin._warned_no_key = False
        plugin._data_dir = None
        plugin._ledger = lambda record: None

        async def fake_call_jev(state, questions):
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

        async def fake_call_jev(state, questions):
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
        self.assertIn("123456789", {x["group_id"] for x in DEFAULT_GROUP_TOPIC_SCHEMES})


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
        self.assertIn(b"NeoHorse-Jev-4B", captured["body"])
        payload = json.loads(captured["body"].decode("utf-8"))
        self.assertIsInstance(payload["state"], str)


class PrefilterTests(unittest.IsolatedAsyncioTestCase):
    def _plugin(self) -> AttentionGatePlugin:
        plugin = object.__new__(AttentionGatePlugin)
        plugin.config = FakeConfig(
            {
                "prefilter_enabled": True,
                "embedding_threshold": 0.42,
                "embedding_low_threshold": 0.30,
                "impulse_enabled": True,
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


if __name__ == "__main__":
    unittest.main()
