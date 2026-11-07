"""两条只读 HTTP 通道的合成测试（不联网、不碰浏览器、不发任何东西）。

这里验的是**我们自己的**编解码与整形：
    · 请求信封里 cmd / body 摆的位置对不对
    · 响应解析遇到截断 / 垃圾字节会不会炸
    · 会话 id 里「对方是谁」取得对不对
    · 通知条目里 cid / parent_id / aweme_id 有没有被认出来
真正「抖音那边到底回什么」不在这份测试里 —— 那是探针（/dy imapi、/dy noticeapi）的事。
"""
from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from douyin import imapi  # noqa: E402
from douyin import noticeapi  # noqa: E402


class EnvelopeTest(unittest.TestCase):
    def test_envelope_carries_cmd_and_body(self):
        body = imapi._enc_ld(imapi.CMD_INIT, b"")
        env = imapi._parse(imapi._envelope(imapi.CMD_INIT, body))
        self.assertEqual(imapi._one(env, 1), imapi.CMD_INIT)
        self.assertEqual(imapi._text(imapi._one(env, 3)), "0.1.8")
        self.assertEqual(imapi._one(env, 8), body)
        self.assertEqual(imapi._text(imapi._one(env, 7)), "0d50935:feat/pc-im-group")
        # 15 号是重复 kv，一个都不能少
        self.assertGreaterEqual(len(env.get(15) or []), 10)

    def test_varint_roundtrip_keeps_19_digit_ids(self):
        big = 7704390662654182971
        parsed = imapi._parse(imapi._enc_v(3, big))
        self.assertEqual(imapi._one(parsed, 3), big)

    def test_parse_survives_truncated_and_garbage(self):
        raw = imapi._envelope(imapi.CMD_INIT, imapi._enc_ld(imapi.CMD_INIT, b""))
        for cut in (1, 7, len(raw) // 2, len(raw) - 1):
            imapi._parse(raw[:cut])            # 不抛就算过
        imapi._parse(b"\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\x01")
        imapi._parse(b"")


class MessageTextTest(unittest.TestCase):
    def test_plain_text(self):
        out = imapi.message_text('{"text": " 收得到吗 "}')
        self.assertEqual(out["text"], "收得到吗")
        self.assertEqual(out["kind"], "text")

    def test_share_card_is_marked(self):
        out = imapi.message_text('{"aweType": 800, "content_title": "[视频]"}')
        self.assertEqual(out["kind"], "share")
        self.assertEqual(out["text"], "[视频]")

    def test_empty_and_broken_content(self):
        self.assertEqual(imapi.message_text("{}")["kind"], "unsupported")
        self.assertEqual(imapi.message_text("not json")["kind"], "raw")
        self.assertEqual(imapi.message_text("")["kind"], "unsupported")


class PeerUidTest(unittest.TestCase):
    def test_picks_the_other_side(self):
        self.assertEqual(
            imapi.peer_uid("0:1:2634023404581211:4330304429101319", "4330304429101319"),
            "2634023404581211",
        )

    def test_self_chat_keeps_one_side(self):
        self.assertEqual(
            imapi.peer_uid("0:1:4330304429101319:4330304429101319", "4330304429101319"),
            "4330304429101319",
        )

    def test_group_and_junk(self):
        self.assertEqual(imapi.peer_uid("", "1"), "")
        # 会话 id 里少于 8 位的段不当作 uid（防脏数据冒充），这是有意的
        self.assertEqual(imapi.peer_uid("0:2:123:456", "123"), "")
        self.assertEqual(
            imapi.peer_uid("0:2:7625369585096688164:4330304429101319", "4330304429101319"),
            "7625369585096688164",
        )


class _FakeBrowser:
    def __init__(self, cookies):
        self._cookies = cookies

    async def raw_cookies(self):
        return self._cookies


class CookieGateTest(unittest.IsolatedAsyncioTestCase):
    def test_missing_sessionid_raises_both_channels(self):
        browser = _FakeBrowser([{"name": "ttwid", "value": "x", "domain": ".douyin.com"}])
        with self.assertRaises(imapi.ImApiError):
            asyncio.get_event_loop().run_until_complete(imapi.ImApi(browser)._cookie_header())
        with self.assertRaises(noticeapi.NoticeApiError):
            asyncio.get_event_loop().run_until_complete(
                noticeapi.NoticeApi(browser)._cookie_header())

    async def test_cookie_header_only_takes_douyin_domain(self):
        browser = _FakeBrowser([
            {"name": "sessionid", "value": "s1", "domain": ".douyin.com"},
            {"name": "other", "value": "keep-out", "domain": ".example.com"},
        ])
        header = await imapi.ImApi(browser)._cookie_header()
        self.assertIn("sessionid=s1", header)
        self.assertNotIn("keep-out", header)

    async def test_probe_reports_failure_instead_of_raising(self):
        browser = _FakeBrowser([])
        data = await imapi.ImApi(browser).probe()
        self.assertFalse(data["ok"])
        self.assertIn("error", data)
        data2 = await noticeapi.NoticeApi(browser).probe()
        self.assertFalse(data2["ok"])


class NoticeNormalizeTest(unittest.TestCase):
    def _item(self, **extra):
        blk = {
            "user_info": {"nickname": "小张", "uid": "123"},
            "comment": {"cid": "7700000000000000001", "text": "在吗", "user": {"nickname": "小张", "uid": "123"}},
            "parent_id": "7600000000000000002",
            "aweme_id": "7500000000000000003",
            "content": "",
        }
        blk.update(extra)
        return {"type": 35, "nid": 99, "create_time": 1700000000, "comment": blk}

    def test_cid_and_root_and_aweme(self):
        rec = noticeapi.NoticeApi.normalize(self._item(), 700)
        self.assertEqual(rec["comment_id"], "7700000000000000001")
        self.assertEqual(rec["root_comment_id"], "7600000000000000002")
        self.assertEqual(rec["aweme_id"], "7500000000000000003")
        self.assertEqual(rec["from_nick"], "小张")
        self.assertTrue(rec["has_cid"])
        self.assertTrue(rec["needs_reply"])
        self.assertEqual(rec["url"], "https://www.douyin.com/video/7500000000000000003")

    def test_like_notice_does_not_need_reply(self):
        item = {"type": 34, "nid": 1, "create_time": 1,
                "digg": {"user_info": {"nickname": "路人", "uid": "9"}, "aweme_id": "7500000000000000004"}}
        rec = noticeapi.NoticeApi.normalize(item, 960)
        self.assertFalse(rec["needs_reply"])
        self.assertFalse(rec["has_cid"])
        self.assertEqual(rec["from_nick"], "路人")

    def test_at_notice_without_cid_is_marked(self):
        item = {"type": 45, "nid": 2, "create_time": 2,
                "at": {"user_info": {"nickname": "老王", "uid": "8"}, "content": "@你 看看这个",
                       "aweme_id": "7500000000000000005"}}
        rec = noticeapi.NoticeApi.normalize(item, 401)
        self.assertTrue(rec["needs_reply"])
        self.assertFalse(rec["has_cid"])
        self.assertEqual(rec["content"], "@你 看看这个")

    def test_level1_comment_supplies_root(self):
        item = {"type": 31, "nid": 3, "create_time": 3,
                "comment": {"user_info": {"nickname": "甲", "uid": "1"},
                            "comment": {"cid": "7700000000000000009", "text": "嗯"},
                            "level1_comment": {"cid": "7600000000000000010",
                                               "user": {"nickname": "乙"}, "text": "根"}}}
        rec = noticeapi.NoticeApi.normalize(item, 601)
        self.assertEqual(rec["comment_id"], "7700000000000000009")
        self.assertEqual(rec["root_comment_id"], "7600000000000000010")
        self.assertEqual(rec["root_author"], "乙")


if __name__ == "__main__":
    unittest.main()