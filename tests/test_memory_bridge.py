"""记忆互通的最小测试：抽出真实方法跑，不碰浏览器/数据库。"""
from __future__ import annotations

import types
import unittest

from test_event_runtime import Entry, METHODS, extract  # noqa: F401

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]

BRIDGE_METHODS = METHODS | {"_bridge_people", "_bridge_prompt_text", "_qq_of", "_identity_map"}


class FakeMemory:
    def __init__(self, rows=None, host=None):
        self.rows = rows or {}
        self.host = host or {}

    def recent(self, person, limit=6):
        return list(self.rows.get(person, []))[:limit]

    def from_host(self, qq_id, limit=10):
        return list(self.host.get(qq_id, []))[:limit]


class FakeStore:
    def __init__(self, rows=None):
        self.rows = rows or []

    async def recent(self, limit=5, only_done=True):
        return self.rows[:limit]


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    def make(self, settings, memory=None, store=None):
        Subject = extract(ROOT / "main.py", names=BRIDGE_METHODS, Entry=Entry)
        app = Subject()
        app.settings = settings
        app.memory = memory or FakeMemory()
        app.store = store or FakeStore()
        return app

    def test_people_unique_in_order(self):
        app = self.make(types.SimpleNamespace(
            dm_chat_allow="大肥鱼Yume, yezheyue", dm_targets="yezheyue", dm_self_names="大肥鱼Yume"))
        self.assertEqual(app._bridge_people(), ["大肥鱼Yume", "yezheyue"])

    async def test_prompt_has_chat_and_videos(self):
        settings = types.SimpleNamespace(
            dm_chat_allow="yezheyue", dm_targets="", dm_self_names="",
            memory_bridge_lines=6, memory_bridge_items=4)
        memory = FakeMemory(rows={"yezheyue": ["你：吃了吗", "她：刚吃完"]})
        store = FakeStore([{"title": "测试视频", "summary": "讲的是窗口为何跑到屏外"}])
        app = self.make(settings, memory=memory, store=store)
        text = await app._bridge_prompt_text()
        self.assertIn("抖音私信里和「yezheyue」", text)
        self.assertIn("刚吃完", text)
        self.assertIn("窗口为何跑到屏外", text)

    async def test_zero_lines_and_items_disable(self):
        settings = types.SimpleNamespace(
            dm_chat_allow="yezheyue", dm_targets="", dm_self_names="",
            memory_bridge_lines=0, memory_bridge_items=0)
        app = self.make(settings, memory=FakeMemory(rows={"yezheyue": ["x"]}),
                        store=FakeStore([{"title": "t", "summary": "s"}]))
        self.assertEqual(await app._bridge_prompt_text(), "")

    async def test_empty_when_nothing_known(self):
        settings = types.SimpleNamespace(
            dm_chat_allow="yezheyue", dm_targets="", dm_self_names="",
            memory_bridge_lines=6, memory_bridge_items=4)
        app = self.make(settings)
        self.assertEqual(await app._bridge_prompt_text(), "")

    def test_qq_of_exact_and_fuzzy(self):
        settings = types.SimpleNamespace(dm_identity="yezheyue=1931855425")
        app = self.make(settings)
        self.assertEqual(app._qq_of("yezheyue"), "1931855425")
        self.assertEqual(app._qq_of("yezheyue酱"), "1931855425")
        self.assertEqual(app._qq_of("路人"), "")

    def test_qq_of_ignores_non_digits(self):
        settings = types.SimpleNamespace(dm_identity="yezheyue=abc")
        app = self.make(settings)
        self.assertEqual(app._qq_of("yezheyue"), "")


if __name__ == "__main__":
    unittest.main()