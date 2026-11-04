"""Pure SYNTHETIC event-runtime regression tests; no browser, AstrBot or network.

AST extracts the *actual* selected production methods, not copies of their logic.
All pages, CDP frames, events, model decisions and store rows below are synthetic.
The decoder itself is separately exercised by test_wsproto.py.
"""
from __future__ import annotations

import ast
import asyncio
import base64
import json
from pathlib import Path
import time
import types
import unittest
from unittest.mock import AsyncMock, Mock

ROOT = Path(__file__).resolve().parents[1]

class Entry:
    """生产端 douyin.inbox.Entry 的本地替身：只有属性和 describe。"""

    def __init__(self, kind="", key="", aweme_id="", who="", text="", when="", src="", extra=None):
        self.kind = kind
        self.key = key
        self.aweme_id = aweme_id
        self.who = who
        self.text = text
        self.when = when
        self.src = src
        self.extra = dict(extra or {})

    def describe(self):
        return f"{self.kind}:{self.who}:{self.text}"




def extract(path, class_name=None, names=None, **namespace):
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    source = next(n for n in tree.body if isinstance(n, ast.ClassDef)
                  and (class_name is None or n.name == class_name)) if class_name else None
    if source is None:
        source = next(n for n in tree.body if isinstance(n, ast.ClassDef)
                      and any(isinstance(m, ast.AsyncFunctionDef) and m.name == "_one_event_round"
                              for m in n.body))
    body = source.body if names is None else [n for n in source.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]

    class StripImports(ast.NodeTransformer):
        def visit_ImportFrom(self, node):
            return None if node.module == "wsproto" else node

    cls = ast.ClassDef(name="Subject", bases=[], keywords=[], body=body, decorator_list=[])
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), cls], type_ignores=[])
    module = ast.fix_missing_locations(StripImports().visit(module))
    ns = dict(time=time, asyncio=asyncio, json=json, logger=Mock(), **namespace)
    exec(compile(module, str(path), "exec"), ns)
    return ns["Subject"]


def result(events=(), *, dirty=True, fingerprint="synthetic-frame"):
    return types.SimpleNamespace(events=list(events), dirty=dirty, raw_frame_id=fingerprint,
                                 diagnostics=["synthetic"])


def event(key="evt1", *, kind="dm", uid="123", channel="im", text="hello"):
    return dict(event_id=key, raw_frame_id="synthetic-frame", type=kind, from_uid=uid,
                from_nick="Alice", content=text, timestamp=1, aweme_id="", comment_id="",
                extra={"channel": channel, "conversation_type": 1})


def entry(kind="dm_text", key="dom1", who="Alice", text="hello", **extra):
    return types.SimpleNamespace(kind=kind, key=key, who=who, text=text,
                                 aweme_id="1234567890123456", when="now", src="synthetic",
                                 extra=dict(extra))


class Emitter:
    def __init__(self):
        self.handlers = {}
    def on(self, name, callback):
        self.handlers.setdefault(name, []).append(callback)
    def remove_listener(self, name, callback):
        self.handlers[name].remove(callback)
    def emit(self, name, payload=None):
        for callback in list(self.handlers.get(name, [])):
            callback(payload)


class Store:
    def __init__(self):
        self.rows = {}
        self.done = []
    async def inbox_add(self, kind, key, *, src="", payload=""):
        identity = (kind, key)
        if identity in self.rows:
            return False
        self.rows[identity] = dict(key=key, src=src, payload=payload, pending=True)
        return True
    async def inbox_done(self, kind, key, line):
        self.done.append((kind, key, line))
        if (kind, key) in self.rows:
            self.rows[(kind, key)]["pending"] = False
    async def inbox_pending(self, kind, limit=20):
        return [r.copy() for (k, _), r in self.rows.items() if k == kind and r["pending"]][:limit]


class WatcherTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.decoder = Mock(return_value=result())
        self.Subject = extract(ROOT / "douyin/im.py", "ImWatcher", decode_frame=self.decoder)
        self.page = Emitter()
        self.cdp = Emitter()
        self.cdp.send = AsyncMock()
        self.cdp.detach = AsyncMock()
        self.page.context = types.SimpleNamespace(new_cdp_session=AsyncMock(return_value=self.cdp))
        self.w = self.Subject(self.page)

    def socket(self, url="wss://frontier-im.douyin.com/ws?token=SYNTHETIC"):
        sock = Emitter()
        sock.url = url
        self.page.emit("websocket", sock)
        return sock

    async def test_cdp_attach_enable_once_and_close(self):
        await self.w.start()
        await self.w.start()
        self.page.context.new_cdp_session.assert_awaited_once_with(self.page)
        self.cdp.send.assert_awaited_once_with("Network.enable")
        self.assertIn("Network.webSocketFrameReceived", self.cdp.handlers)
        await self.w.close()
        self.cdp.detach.assert_awaited_once()
        self.assertEqual(self.page.handlers["websocket"], [])

    async def test_future_ws_and_cdp_duplicate_enqueue_once(self):
        self.decoder.side_effect = lambda _: result([event()])
        sock = self.socket()
        sock.emit("framereceived", b"SYNTHETIC")
        self.w._cdp_created({"requestId": "r", "url": sock.url})
        self.w._cdp_frame({"requestId": "r", "response": {"opcode": 2,
            "payloadData": base64.b64encode(b"SYNTHETIC").decode()}})
        self.assertEqual(len(self.w.events), 1)
        self.assertEqual(self.w.signals, 1)
        self.assertEqual(self.w.urls, ["im"])
        self.assertNotIn("token", str(self.w.stats()))
        self.assertTrue(self.w.alive())

    async def test_existing_socket_decodable_message_is_observed(self):
        self.decoder.return_value = result([event()])
        self.w._cdp_frame({"requestId": "already-open", "response": {"opcode": 2,
            "payloadData": base64.b64encode(b"SYNTHETIC").decode()}})
        self.assertEqual(len(self.w.events), 1)
        self.assertIn("unknown", self.w.routes)
        self.assertTrue(self.w.dirty)

    async def test_unknown_telemetry_does_not_fake_business_liveness(self):
        self.decoder.return_value = result()
        self.w._cdp_frame({"requestId": "telemetry", "response": {"opcode": 1,
            "payloadData": "SYNTHETIC unknown"}})
        self.assertFalse(self.w.alive())
        self.assertEqual(self.w.last_rx, 0)
        self.assertFalse(self.w.dirty)

    async def test_known_notice_unknown_bytes_request_dom(self):
        sock = self.socket("wss://frontier-pc.douyin.com/ws")
        sock.emit("framereceived", b"short")
        self.assertTrue(self.w.dirty)
        self.assertEqual(self.w.routes, {"notice"})
        self.assertEqual(len(self.w.events), 0)

    async def test_heartbeats_do_not_request_dom(self):
        sock = self.socket()
        for payload in (b"hi", "ping", "pong", b""):
            sock.emit("framereceived", payload)
        self.decoder.assert_not_called()
        self.assertFalse(self.w.dirty)
        self.assertTrue(self.w.alive())

    async def test_host_spoof_and_unrelated_url_ignored(self):
        self.socket("wss://frontier-im.douyin.com.evil.example/ws").emit("framereceived", b"X")
        self.socket("wss://telemetry.example/ws").emit("framereceived", b"X")
        self.decoder.assert_not_called()
        self.assertFalse(self.w.alive())

    async def test_invalid_cdp_base64_is_ignored(self):
        self.w._cdp_created({"requestId": "r", "url": "wss://frontier-im.douyin.com"})
        self.w._cdp_frame({"requestId": "r", "response": {"opcode": 2, "payloadData": "!!!"}})
        self.decoder.assert_not_called()

    async def test_queue_dedup_and_public_user_throttle_not_im(self):
        self.assertTrue(self.w.enqueue(event()))
        self.assertFalse(self.w.enqueue(event()))
        self.assertTrue(self.w.enqueue(event("c1", kind="comment", channel="notice")))
        self.assertFalse(self.w.enqueue(event("c2", kind="comment", channel="notice")))
        self.assertTrue(self.w.enqueue(event("i2", kind="comment", channel="im")))
        self.w.release(event("c1", kind="comment", channel="notice"))
        self.assertTrue(self.w.enqueue(event("c2", kind="comment", channel="notice")))

    async def test_releasing_im_event_does_not_unlock_public_comment_user(self):
        public = event("public", kind="comment", channel="notice")
        self.assertTrue(self.w.enqueue(public))
        self.w.release(event("private", kind="dm", channel="im"))
        self.assertFalse(self.w.enqueue(event("second", kind="comment", channel="notice")))
        self.w.release(public)
        self.assertTrue(self.w.enqueue(event("second", kind="comment", channel="notice")))

    async def test_full_queue_preserves_old_events_and_requests_dom(self):
        for i in range(256):
            self.assertTrue(self.w.enqueue(event(str(i))))
        self.assertFalse(self.w.enqueue(event("overflow")))
        self.assertEqual(self.w.events[0]["event_id"], "0")
        self.assertTrue(self.w.dirty)
        self.assertEqual(self.w.dropped, 1)

    async def test_disconnect_fallback_thresholds(self):
        self.w.created_at = time.monotonic() - 301
        self.w.disconnected_at = self.w.created_at
        self.assertFalse(self.w.fallback_due(time.monotonic() - 599, 600))
        self.assertTrue(self.w.fallback_due(time.monotonic() - 601, 600))
        self.w.disconnected_at = time.monotonic() - 299
        self.assertFalse(self.w.fallback_due(time.monotonic() - 601, 600))


METHODS = {"_one_event_round", "_handle_mention", "_mention_work", "_entry_event",
           "_send_comment_reply", "_confirm_comment_replies", "_event_loop", "_wait_signal",
           "_attach_watcher", "_on_page_ready", "_attach_event_page",
           "_ws_claimed", "_ws_mark", "_ws_self_echo", "_matches_push"}


class MainRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.Subject = extract(ROOT / "main.py", names=METHODS, Entry=Entry)
        self.app = self.Subject()
        self.app.settings = types.SimpleNamespace(action_mode="ask", event_ws=True)
        self.app.browser = types.SimpleNamespace(page=AsyncMock(return_value="PAGE"))
        self.app.inbox = types.SimpleNamespace(collect=AsyncMock(return_value=[]))
        self.app.social = types.SimpleNamespace(_uid_cache={}, reply_comment=AsyncMock())
        self.app.store = Store()
        self.app.watcher = types.SimpleNamespace(drain=Mock(return_value=([], {"im"})),
            release=Mock(), restore=Mock())
        self.app.agent = types.SimpleNamespace(handle_douyin_event=AsyncMock(return_value={"reply": True, "text": "reply"}))
        self.app.limits = types.SimpleNamespace(check_async=AsyncMock(return_value=""), hit_async=AsyncMock())
        self.app._handle_dm_text = AsyncMock(return_value="回了：dm")
        self.app._handle_dm_share_event = AsyncMock(return_value="回了：share")
        self.app._notify_text = AsyncMock()
        self.app._last_umo = ""
        self.app._mention_seen = {}
        self.app._confirm_lock = asyncio.Lock()
        self.app._comment_confirm_lock = asyncio.Lock()

    async def test_ws_im_only_reads_dm_notice_only_reads_mentions(self):
        await self.app._one_event_round("ws")
        self.app.inbox.collect.assert_awaited_with("PAGE", include_dm=True, include_mentions=False)
        self.app.watcher.drain.return_value = ([], {"notice"})
        await self.app._one_event_round("ws")
        self.app.inbox.collect.assert_awaited_with("PAGE", include_dm=False, include_mentions=True)

    async def test_baseline_marks_backlog_without_drain_or_send(self):
        self.app.inbox.collect.return_value = [entry()]
        await self.app._one_event_round("baseline")
        self.app.watcher.drain.assert_not_called()
        self.app._handle_dm_text.assert_not_awaited()
        self.assertEqual(self.app.store.done, [("dm_text", "dom1", "backlog")])
        self.assertTrue(self.app._dm_warmed)

    async def test_dom_routes_and_persistent_dedup(self):
        self.app._handle_mention = AsyncMock(return_value="回了：comment")
        self.app.inbox.collect.return_value = [entry(), entry("dm_share", "s"), entry("comment", "c")]
        await self.app._one_event_round("poll")
        await self.app._one_event_round("poll")
        self.app._handle_dm_text.assert_awaited_once()
        self.app._handle_dm_share_event.assert_awaited_once()
        self.app._handle_mention.assert_awaited_once()

    async def test_unmatched_raw_dm_is_promoted_not_dropped(self):
        """界面没读到时，协议层解出来的私信要自己走一遍链路，不能只剩一条证据。"""
        raw = event()
        self.app.watcher.drain.return_value = ([raw], {"im"})
        self.app.social._uid_cache = {"Alice": {"uid": "123"}}
        await self.app._one_event_round("ws")
        self.app._handle_dm_text.assert_awaited_once()
        handled = self.app._handle_dm_text.await_args[0][0]
        self.assertTrue(handled.extra["protocol"])
        self.assertEqual(handled.extra["source"], "ws")
        self.assertEqual(handled.who, "Alice")
        self.assertEqual(handled.text, raw["content"])
        self.app._handle_dm_share_event.assert_not_awaited()
        # 已经提升成条目，就不再另写一条「等身份」的证据
        self.assertNotIn(("ws_evidence", "evt1"), self.app.store.rows)
        self.app.watcher.release.assert_called_once_with(raw)

    async def test_matching_raw_enriches_one_dom_and_not_replayed(self):
        e = entry()
        raw = event()
        self.app.watcher.drain.return_value = ([raw], {"im"})
        self.app.inbox.collect.return_value = [e]
        await self.app._one_event_round("ws")
        self.assertEqual(e.extra["event_id"], "evt1")
        self.assertEqual(e.extra["from_uid"], "123")
        await self.app._one_event_round("poll")
        self.app._handle_dm_text.assert_awaited_once()

    async def test_own_message_pushed_back_is_not_promoted(self):
        """自己发出去的话被推回来一条，不能当新私信处理（否则自己回自己，无限循环）。"""
        raw = event(text="我发出去的")
        raw["extra"]["conversation_id"] = "0:1:4330304429101319:4330304429101319"
        self.app.watcher.drain.return_value = ([raw], {"im"})
        self.app.settings.dm_self_names = "大肥鱼Yume"
        self.app.social._uid_cache = {"大肥鱼Yume": {"uid": "123"}}
        await self.app._one_event_round("ws")
        self.app._handle_dm_text.assert_not_awaited()
        self.assertIn(("ws_evidence", "evt1"), self.app.store.rows)
        self.assertEqual(self.app.store.done[-1][-1], "self_echo")

    async def test_self_name_without_conversation_id_still_blocked(self):
        raw = event(text="又是我")
        self.app.settings.dm_self_names = "大肥鱼Yume"
        self.app.social._uid_cache = {"大肥鱼Yume": {"uid": "123"}}
        self.app.watcher.drain.return_value = ([raw], {"im"})
        await self.app._one_event_round("ws")
        self.app._handle_dm_text.assert_not_awaited()

    async def test_same_public_user_only_one_handler_in_round(self):
        self.app._handle_mention = AsyncMock(return_value="queued")
        self.app.inbox.collect.return_value = [entry("comment", "c1", from_uid="u"), entry("mention", "c2", from_uid="u")]
        await self.app._one_event_round("poll")
        self.app._handle_mention.assert_awaited_once()
        self.assertIn(("mention", "c2", "same_user_pending"), self.app.store.done)

    async def test_group_raw_without_dom_does_not_send_private_or_public_reply(self):
        raw = event(kind="mention")
        raw["extra"]["conversation_type"] = 2
        self.app.watcher.drain.return_value = ([raw], {"im"})
        await self.app._one_event_round("ws")
        self.app._handle_dm_text.assert_not_awaited()
        self.app.agent.handle_douyin_event.assert_not_awaited()
        self.app.social.reply_comment.assert_not_awaited()
        self.app.watcher.release.assert_called_once_with(raw)

    async def test_duplicate_nickname_matches_do_not_inject_protocol_identity(self):
        raw = event()
        self.app.watcher.drain.return_value = ([raw], {"im"})
        first, second = entry(key="a"), entry(key="b")
        self.app.inbox.collect.return_value = [first, second]
        await self.app._one_event_round("ws")
        self.assertNotIn("protocol", first.extra)
        self.assertNotIn("protocol", second.extra)
        self.assertIn(("ws_evidence", "evt1"), self.app.store.rows)

    async def test_mention_guard_released_after_exception(self):
        self.app._mention_work = AsyncMock(side_effect=RuntimeError("synthetic"))
        with self.assertRaises(RuntimeError):
            await self.app._handle_mention("PAGE", entry("comment", from_uid="u"), "")
        self.assertEqual(self.app._mention_seen, {})

    async def test_ask_queue_has_no_public_write(self):
        e = entry("comment", comment_id="cid", from_uid="u")
        await self.app._mention_work("PAGE", e, "")
        self.app.social.reply_comment.assert_not_awaited()
        rows = await self.app.store.inbox_pending("comment_reply")
        self.assertEqual(len(rows), 1)
        payload = json.loads(rows[0]["payload"])
        self.assertEqual(payload["comment_id"], "cid")
        await self.app._mention_work("PAGE", entry("comment", "second", from_uid="u"), "")
        self.assertEqual(len(await self.app.store.inbox_pending("comment_reply")), 1)

    async def queue(self, key="q", age=0):
        await self.app.store.inbox_add("comment_reply", key, payload=json.dumps(dict(
            created_at=time.time() - age, comment_id="cid", aweme_id="aid", text="reply", uid="u")))

    async def test_confirm_success_finishes_and_second_confirm_no_send(self):
        await self.queue()
        self.app._send_comment_reply = AsyncMock(return_value="回了：reply")
        await self.app._confirm_comment_replies()
        await self.app._confirm_comment_replies()
        self.app._send_comment_reply.assert_awaited_once()
        self.assertEqual(await self.app.store.inbox_pending("comment_reply"), [])

    async def test_confirm_failure_and_rate_limit_keep_queue(self):
        await self.queue()
        self.app._send_comment_reply = AsyncMock(return_value="评论暂缓：quota")
        await self.app._confirm_comment_replies()
        self.assertEqual(len(await self.app.store.inbox_pending("comment_reply")), 1)
        self.assertEqual(self.app.store.done, [])

    async def test_confirm_ttl_expiry_no_send(self):
        await self.queue(age=43201)
        self.app._send_comment_reply = AsyncMock()
        await self.app._confirm_comment_replies()
        self.app._send_comment_reply.assert_not_awaited()
        self.assertEqual(await self.app.store.inbox_pending("comment_reply"), [])

    async def test_confirm_off_retains_pending(self):
        await self.queue()
        self.app.settings.action_mode = "off"
        self.app._send_comment_reply = AsyncMock()
        await self.app._confirm_comment_replies()
        self.app._send_comment_reply.assert_not_awaited()
        self.assertEqual(len(await self.app.store.inbox_pending("comment_reply")), 1)

    async def test_comment_missing_id_never_uses_top_level_comment(self):
        line = await self.app._send_comment_reply(dict(aweme_id="a", comment_id="", text="x"))
        self.assertIn("id", line)
        self.app.social.reply_comment.assert_not_awaited()
        self.app.limits.hit_async.assert_not_awaited()

    async def test_comment_confirmed_write_charges_quota_only_on_success(self):
        self.app.social.reply_comment.return_value = types.SimpleNamespace(ok=False, detail="synthetic")
        payload = dict(aweme_id="a", comment_id="c", text="x")
        await self.app._send_comment_reply(payload)
        self.app.limits.hit_async.assert_not_awaited()
        self.app.social.reply_comment.return_value.ok = True
        await self.app._send_comment_reply(payload)
        self.app.limits.hit_async.assert_awaited_once_with("comment")

    async def test_im_comment_never_routes_to_public_reply(self):
        self.app.settings.action_mode = "auto"
        self.app.social.reply_comment.return_value = types.SimpleNamespace(ok=True)
        await self.app._mention_work("PAGE", entry("comment", channel="im", route="dm", comment_id="c"), "")
        self.app.social.reply_comment.assert_not_awaited()

    async def test_collect_error_restores_drained_events(self):
        raw = event()
        self.app.watcher.drain.return_value = ([raw], {"im"})
        self.app.inbox.collect.side_effect = RuntimeError("synthetic collect failure")
        with self.assertRaises(RuntimeError):
            await self.app._one_event_round("ws")
        self.app.watcher.restore.assert_called_once()

    async def test_concurrent_confirmation_sends_once(self):
        await self.queue()
        async def send(payload):
            await asyncio.sleep(0.01)
            return "回了：reply"
        self.app._send_comment_reply = AsyncMock(side_effect=send)
        await asyncio.gather(self.app._confirm_comment_replies(), self.app._confirm_comment_replies())
        self.app._send_comment_reply.assert_awaited_once()


class BrowserAttachTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_page_hook_runs_before_handle_returns(self):
        subject = extract(ROOT / "douyin/browser.py", "DouyinBrowser", names={"_page", "page"})()
        page = types.SimpleNamespace(is_closed=lambda: False)
        context = types.SimpleNamespace(pages=[page], new_page=AsyncMock())
        subject._ensure = AsyncMock(return_value=context)
        subject.on_page_ready = AsyncMock()
        self.assertIs(await subject.page(), page)
        subject.on_page_ready.assert_awaited_once_with(page)
        context.new_page.assert_not_awaited()

    async def test_new_page_hook_runs_before_handle_returns(self):
        subject = extract(ROOT / "douyin/browser.py", "DouyinBrowser", names={"_page", "page"})()
        page = object()
        context = types.SimpleNamespace(pages=[], new_page=AsyncMock(return_value=page))
        subject._ensure = AsyncMock(return_value=context)
        subject.on_page_ready = AsyncMock()
        self.assertIs(await subject.page(), page)
        subject.on_page_ready.assert_awaited_once_with(page)
        context.new_page.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
