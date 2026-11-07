"""HTTP 增量（M1 私信 / M2 通知）的单测。

只喂合成数据：不连抖音、不开浏览器、不读线上游标。
"""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_event_runtime import METHODS, Entry, extract  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class FakeImApi:
    def __init__(self, convs):
        self.convs = convs
        self.calls = 0

    async def conversations(self, limit=None):
        self.calls += 1
        return self.convs


class FakeNoticeApi:
    def __init__(self, items):
        self.items_ = items
        self.calls = 0

    async def items(self, groups=None):
        self.calls += 1
        # 生产里返回的是整包 {counts, items, warnings}，这里照原样给，免得测试比生产宽松
        return {"counts": {700: len(self.items_), 960: 0, 401: 0, 601: 0},
                "items": list(self.items_), "warnings": []}


def conv(conv_id, messages, ctype=1):
    return {"conv_id": conv_id, "type": ctype, "peer_uid": "263", "messages": messages}


def msg(uid, text, ts, *, mine=False, kind="text", sid="s1"):
    return {"from_uid": uid, "mine": mine, "create_time": ts, "text": text,
            "kind": kind, "server_message_id": sid}


def notice(nid, *, needs_reply=True, cid="7700001", aweme="7701220343054541041",
           nick="yezheyue", ntype=45, ts=1793826991, text="@大肥鱼Yume"):
    return {"nid": nid, "needs_reply": needs_reply, "comment_id": cid,
            "root_comment_id": "", "aweme_id": aweme, "from_nick": nick,
            "from_uid": "2634023404581211", "type": ntype, "create_time": ts,
            "content": text}


class HttpDmScanTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.Subject = extract(ROOT / "main.py", names=METHODS, Entry=Entry)
        self.app = self.Subject()
        self.app.settings = types.SimpleNamespace(dm_http=True, notice_http=True, dm_http_limit=20)
        self.app.social = types.SimpleNamespace(_uid_cache={"yezheyue": {"uid": "263"}})
        self.app._http_state = {}

    async def test_only_new_incoming_messages_become_entries(self):
        self.app.imapi = FakeImApi([conv("0:1:263:433", [
            msg("263", "旧消息", 1000),
            msg("433", "我自己说的", 1100, mine=True),
        ])])
        await self.app._http_dm_scan(first=True)          # baseline 建游标
        self.app.imapi.convs[0]["messages"].append(msg("263", "新消息", 1200, sid="s2"))
        entries = await self.app._http_dm_scan(first=False)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].kind, "dm_text")
        self.assertEqual(entries[0].who, "yezheyue")
        self.assertEqual(entries[0].text, "新消息")
        self.assertTrue(entries[0].extra["protocol"])
        self.assertEqual(entries[0].extra["source"], "http")

    async def test_cursor_advances_so_nothing_is_replied_twice(self):
        self.app.imapi = FakeImApi([conv("c1", [msg("263", "一", 1000)])])
        self.assertEqual(len(await self.app._http_dm_scan(first=False)), 1)
        self.assertEqual(await self.app._http_dm_scan(first=False), [])
        self.app.imapi.convs[0]["messages"].append(msg("263", "二", 2000, sid="s9"))
        entries = await self.app._http_dm_scan(first=False)
        self.assertEqual([e.text for e in entries], ["二"])

    async def test_first_scan_marks_backlog_without_entries(self):
        self.app.imapi = FakeImApi([conv("c1", [msg("263", "积压", 1000)])])
        self.assertEqual(await self.app._http_dm_scan(first=True), [])
        self.assertEqual(self.app._http_state["im"]["c1"], 1000)
        self.app.imapi.convs[0]["messages"].append(msg("263", "新的", 1500, sid="s3"))
        entries = await self.app._http_dm_scan(first=True)
        # first=True 仍然只推进游标：baseline 不该把刚到的消息当成可以回的
        self.assertEqual(entries, [])
        self.assertEqual(self.app._http_state["im"]["c1"], 1500)

    async def test_unknown_sender_is_skipped_not_guessed(self):
        self.app.imapi = FakeImApi([conv("c1", [msg("999", "谁啊", 1000)])])
        self.assertEqual(await self.app._http_dm_scan(first=False), [])

    async def test_share_kind_is_kept(self):
        self.app.imapi = FakeImApi([conv("c1", [msg("263", "分享了一个作品", 1000, kind="share")])])
        entries = await self.app._http_dm_scan(first=False)
        self.assertEqual(entries[0].kind, "dm_share")

    async def test_api_failure_returns_empty_and_keeps_loop_alive(self):
        class Boom:
            async def conversations(self, limit=None):
                raise RuntimeError("IM 接口返回 too many requests")

        self.app.imapi = Boom()
        self.assertEqual(await self.app._http_dm_scan(first=False), [])

    async def test_claimed_so_dom_copy_is_skipped(self):
        self.app.imapi = FakeImApi([conv("c1", [msg("263", "你好", 1000)])])
        await self.app._http_dm_scan(first=False)
        dom_entry = types.SimpleNamespace(kind="dm_text", who="yezheyue", text="你好",
                                          extra={"from_uid": "263"})
        self.assertTrue(self.app._ws_claimed(dom_entry))


class HttpNoticeScanTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.Subject = extract(ROOT / "main.py", names=METHODS, Entry=Entry)
        self.app = self.Subject()
        self.app.settings = types.SimpleNamespace(dm_http=True, notice_http=True)
        self.app.social = types.SimpleNamespace(_uid_cache={"yezheyue": {"uid": "263"}})
        self.app._http_state = {}

    async def test_replyable_notices_carry_real_comment_id(self):
        self.app.noticeapi = FakeNoticeApi([
            notice("n1", cid="7704428225661879078"),
            notice("n2", needs_reply=False, cid="x"),
            notice("n3", ntype=31, cid="7704409979185677115", ts=1793822748, text="[看]"),
        ])
        entries = await self.app._http_notice_scan(first=False)
        self.assertEqual([e.key for e in entries], ["nt:n1", "nt:n3"])
        self.assertEqual(entries[0].extra["comment_id"], "7704428225661879078")
        self.assertEqual(entries[1].kind, "comment")
        self.assertEqual(entries[0].kind, "mention")
        self.assertEqual(entries[0].aweme_id, "7701220343054541041")

    async def test_seen_list_dedupes_across_rounds(self):
        self.app.noticeapi = FakeNoticeApi([notice("n1")])
        self.assertEqual(len(await self.app._http_notice_scan(first=False)), 1)
        self.assertEqual(await self.app._http_notice_scan(first=False), [])
        self.app.noticeapi.items_.append(notice("n2", cid="7700002"))
        entries = await self.app._http_notice_scan(first=False)
        self.assertEqual([e.key for e in entries], ["nt:n2"])

    async def test_first_scan_swallows_backlog(self):
        self.app.noticeapi = FakeNoticeApi([notice("n1")])
        self.assertEqual(await self.app._http_notice_scan(first=True), [])
        self.app.noticeapi.items_.append(notice("n2", cid="7700002"))
        self.assertEqual(await self.app._http_notice_scan(first=True), [])

    async def test_missing_aweme_is_not_turned_into_a_comment(self):
        self.app.noticeapi = FakeNoticeApi([notice("n1", aweme="")])
        self.assertEqual(await self.app._http_notice_scan(first=False), [])

    async def test_scan_dispatches_by_switches(self):
        self.app.imapi = FakeImApi([conv("c1", [msg("263", "你好", 1000)])])
        self.app.noticeapi = FakeNoticeApi([notice("n1")])
        entries = await self.app._http_scan(first=False, dm=True, notices=True)
        self.assertEqual(len(entries), 2)
        self.assertEqual(await self.app._http_scan(first=False, dm=False, notices=False), [])


if __name__ == "__main__":
    unittest.main()