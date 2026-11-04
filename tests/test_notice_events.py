"""Synthetic unit fixtures only: no live browser, account, WS capture or DOM evidence."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
from pathlib import Path
import re
import shutil
import subprocess
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load_modules():
    # Isolated package avoids importing the plugin entrypoint or changing real modules.
    package = types.ModuleType("_synthetic_notice_tests")
    package.__path__ = [str(ROOT / "douyin")]
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("synthetic-notice-tests")
    im = types.ModuleType(package.__name__ + ".im")
    im.DirectMessages = lambda browser: None
    im.aweme_id_of = lambda text: ""
    im.AWEME_RE = re.compile(r"(?:modal_id=|/video/|/note/|aweme_id=)(\d{12,25})")
    feed = types.ModuleType(package.__name__ + ".feed")
    feed.AwemeItem = object
    feed.parse_aweme = lambda item: item
    modules = {package.__name__: package, "astrbot.api": api,
               im.__name__: im, feed.__name__: feed}
    with patch.dict(sys.modules, modules):
        loaded = []
        for name in ("notice", "inbox"):
            spec = importlib.util.spec_from_file_location(package.__name__ + "." + name,
                                                        ROOT / "douyin" / (name + ".py"))
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            loaded.append(module)
    return *loaded, modules


notice, inbox, MODULES = load_modules()


class SyntheticParsingTests(unittest.TestCase):
    def test_old_mention_shape(self):
        mt = notice.parse_row("yezheyue | 朋友 | 模型里面有敏感限制吗 @大肥鱼Yume | 在评论中提到了你05:29")
        self.assertEqual((mt.who, mt.text, mt.when, mt.kind),
                         ("yezheyue", "模型里面有敏感限制吗 @大肥鱼Yume", "05:29", "mention"))
        self.assertEqual((mt.uid, mt.comment_id, mt.aweme_id), ("", "", ""))

    def test_comments_replies_and_mentions(self):
        for raw, kind, body in [
            ("甲 评论了你的作品： 好看 刚刚", "comment", "好看"),
            ("甲 回复了你的评论： 同意 昨天", "comment", "同意"),
            ("甲 回复了你的： 同意 昨天", "comment", "同意"),
            ("甲 @乙 在评论中提到了你05:31", "mention", "@乙"),
        ]:
            with self.subTest(raw=raw):
                mt = notice.parse_row(raw)
                self.assertEqual((mt.who, mt.kind, mt.text), ("甲", kind, body))

    def test_missing_sender_and_missing_body(self):
        mt = notice.parse_row("回复了你的评论：你好 刚刚")
        self.assertEqual((mt.who, mt.text), ("", "你好"))
        self.assertEqual(notice.parse_row("甲 评论了你的作品 刚刚").text, "")
        self.assertIsNone(notice.parse_row("甲 点赞了你的作品 刚刚"))

    def test_body_clock_is_not_timestamp(self):
        mt = notice.parse_row("甲 12:30见 在评论中提到了你 刚刚")
        self.assertEqual((mt.text, mt.when), ("12:30见", "刚刚"))

    def test_ids_from_explicit_attrs(self):
        ids = notice._row_ids({"attrs": {"data-uid": "00123", "data-comment-id": "456",
                                        "data-aweme-id": "789", "data-event-id": "evt:a"}}, "甲")
        self.assertEqual(ids, {"uid": "00123", "comment_id": "456", "aweme_id": "789", "event_id": "evt:a"})

    def test_sender_link_not_mentioned_user(self):
        row = {"links": [{"text": "甲", "href": "https://www.douyin.com/user/123"},
                         {"text": "@乙", "href": "https://www.douyin.com/user/999"},
                         {"text": "作品", "href": "/video/789?comment_id=456"}]}
        self.assertEqual(notice._row_ids(row, "甲"),
                         {"uid": "123", "comment_id": "456", "aweme_id": "789", "event_id": ""})

    def test_sec_uid_and_foreign_host_not_trusted(self):
        row = {"text": "甲 123456789012345", "links": [
            {"text": "甲", "href": "https://www.douyin.com/user/MS4wLjABAAAA"},
            {"text": "作品", "href": "https://douyin.com.evil.test/video/789?comment_id=456"}]}
        self.assertTrue(all(not value for value in notice._row_ids(row, "甲").values()))

    def test_conflicting_ids_are_empty(self):
        row = {"attrs": {"data-uid": "123", "data-comment-id": "456"},
               "links": [{"text": "甲", "href": "/user/999?comment_id=888"}]}
        ids = notice._row_ids(row, "甲")
        self.assertEqual((ids["uid"], ids["comment_id"]), ("", ""))

    def test_child_user_must_match_sender(self):
        row = {"nodes": [{"text": "乙", "attrs": {"data-uid": "999"}},
                         {"text": "甲", "attrs": {"data-uid": "123"}}]}
        self.assertEqual(notice._row_ids(row, "甲")["uid"], "123")


class SyntheticEventTests(unittest.TestCase):
    def test_kind_mapping_and_raw_metadata(self):
        for kind, expected in (("dm", "dm_text"), ("share", "dm_share"),
                               ("mention", "mention"), ("comment", "comment")):
            event = dict(type=kind, event_id="evt-001", raw_frame_id="frame-2", from_uid="123",
                         from_nick="甲", content="内容", comment_id="456", timestamp=123.5,
                         aweme_id="789", extra={"evidence": "synthetic"})
            entry = inbox.Inbox.event_entry(event)
            self.assertEqual((entry.kind, entry.key, entry.who, entry.text),
                             (expected, "evt-001", "甲", "内容"))
            for name in ("event_id", "raw_frame_id", "from_uid", "comment_id", "timestamp"):
                self.assertEqual(entry.extra[name], event[name])
            self.assertFalse(entry.extra["pending_enrichment"])
            self.assertEqual(entry.extra["evidence"], "synthetic")
            self.assertEqual(event["extra"], {"evidence": "synthetic"})

    def test_missing_values_do_not_fabricate_identity_text_or_key(self):
        entry = inbox.Inbox.event_entry({"type": "dm", "raw_frame_id": "one-frame"})
        self.assertEqual((entry.key, entry.who, entry.text, entry.aweme_id), ("", "", "", ""))
        self.assertTrue(entry.extra["pending_enrichment"])
        self.assertEqual(entry.extra["missing_fields"], ["from_uid", "text", "event_id"])

    def test_raw_comment_id_fallback_and_time_alias(self):
        entry = inbox.Inbox.event_entry({"kind": "comment", "comment_id": "000456",
                                       "text": "你好", "time": 0})
        self.assertEqual((entry.key, entry.text, entry.when, entry.extra["timestamp"]),
                         ("000456", "你好", "0", 0))

    def test_repeated_text_distinct_raw_ids_remain_distinct(self):
        first = inbox.Inbox.event_entry({"type": "dm", "event_id": "1", "content": "你好"})
        second = inbox.Inbox.event_entry({"type": "dm", "event_id": "2", "content": "你好"})
        self.assertNotEqual(first.key, second.key)

    def test_unsupported_event(self):
        self.assertIsNone(inbox.Inbox.event_entry({"type": "heartbeat"}))
        self.assertIsNone(inbox.Inbox.event_entry(None))


class SyntheticAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_modal_id_is_not_attributed(self):
        class Page:
            url = "https://www.douyin.com/?modal_id=1234567890123"
            async def click(self, *args):
                pass
            async def wait_for_timeout(self, ms):
                pass
        page = Page()
        page.mouse = page
        notices = notice.Notices(None)
        closed = []
        async def close(page):
            closed.append(True)
        notices._close = close
        with patch.dict(sys.modules, MODULES):
            self.assertEqual(await notices.open_mention(page, notice.Mention(x=10, y=10)), "")
        self.assertEqual(closed, [True])

    async def test_changed_modal_and_explicit_id(self):
        class Page:
            url = "https://www.douyin.com/?modal_id=1234567890123"
            async def click(self, *args):
                self.url = "https://www.douyin.com/?modal_id=9876543210123"
            async def wait_for_timeout(self, ms):
                pass
        page = Page()
        page.mouse = page
        notices = notice.Notices(None)
        async def close(page):
            pass
        notices._close = close
        with patch.dict(sys.modules, MODULES):
            self.assertEqual(await notices.open_mention(page, notice.Mention(x=10, y=10)), "9876543210123")
        self.assertEqual(await notices.open_mention(None, notice.Mention(aweme_id="456")), "456")

    async def test_notice_rows_are_isolated_and_collect_preserves_old_key(self):
        class Page:
            async def query_selector(self, selector):
                return True
            async def evaluate(self, script):
                return {"ok": True, "rows": [
                    {"text": "甲 好看 评论了你的作品 刚刚", "attrs": {"data-uid": "123", "data-comment-id": "456", "data-aweme-id": "789"}},
                    {"text": "乙 @甲 在评论中提到了你 刚刚"}]}
        box = inbox.Inbox(None)
        async def no_id(page, mention):
            return ""
        box.notices.open_mention = no_id
        entries = await box.collect_mentions(Page())
        self.assertEqual((entries[0].kind, entries[0].key, entries[0].extra["from_uid"]), ("comment", "456", "123"))
        self.assertEqual((entries[1].kind, entries[1].key, entries[1].extra["from_uid"]), ("mention", "mt:乙:@甲", ""))
        self.assertEqual(entries[1].aweme_id, "")


@unittest.skipUnless(shutil.which("node"), "Node required for synthetic JS DOM fixture")
class SyntheticDOMTests(unittest.TestCase):
    def test_row_js_emits_attrs_links_and_skips_multirow_container(self):
        # Minimal DOM facade exercises actual ROW_JS, not a live/browser DOM.
        fixture = r'''
const rect = {x: 0, y: 0, width: 328, height: 696};
const root = {getBoundingClientRect: () => rect};
const make = (text, y, uid) => ({
  innerText: text, parentElement: root, className: 'row',
  attributes: [{name:'data-uid', value:uid}],
  getBoundingClientRect: () => ({x:0, y, width:328, height:70}),
  querySelectorAll: selector => selector === 'a[href]' ? [{
    href:'https://www.douyin.com/video/789?comment_id=456', innerText:'作品', attributes:[]
  }] : []
});
const first = make('甲 好看 评论了你的作品 刚刚', 0, '123');
const second = make('乙 @甲 在评论中提到了你 刚刚', 70, '999');
const merged = make(first.innerText + ' ' + second.innerText, 0, '777');
root.innerText = merged.innerText;
root.querySelectorAll = () => [merged, first, second];
global.document = {querySelectorAll: () => [root]};
'''
        script = fixture + "console.log(JSON.stringify((" + notice.ROW_JS + ")()));"
        result = subprocess.run([shutil.which("node"), "-e", script], capture_output=True,
                                text=True, encoding="utf-8", check=True)
        data = json.loads(result.stdout)
        self.assertEqual(len(data["rows"]), 2)
        self.assertEqual([r["attrs"]["data-uid"] for r in data["rows"]], ["123", "999"])
        self.assertEqual(data["rows"][0]["links"][0]["href"],
                         "https://www.douyin.com/video/789?comment_id=456")


if __name__ == "__main__":
    unittest.main()
