"""Pure stdlib tests. ALL fixtures here are SYNTHETIC, not captured real traffic.

Wire tags follow the pinned third-party schema listed in wsproto.SCHEMA_SOURCES.
These tests establish parser behavior, not current live browser compatibility.
Run: python -B -m unittest discover -s tests -p test_wsproto.py -v
"""
from __future__ import annotations

import gzip
import importlib.util
import json
from pathlib import Path
import random
import sys
import unittest

# Import only the owned module: no plugin/AstrBot/browser dependency or side effect.
_NAME = "_synthetic_test_wsproto"
_SPEC = importlib.util.spec_from_file_location(
    _NAME, Path(__file__).resolve().parents[1] / "douyin" / "wsproto.py")
assert _SPEC is not None and _SPEC.loader is not None
wsproto = importlib.util.module_from_spec(_SPEC)
sys.modules[_NAME] = wsproto
_SPEC.loader.exec_module(wsproto)
decode_frame, Limits = wsproto.decode_frame, wsproto.Limits


def vint(value: int) -> bytes:
    out = bytearray()
    while value > 127:
        out.append((value & 127) | 128)
        value >>= 7
    out.append(value)
    return bytes(out)


def integer(tag: int, value: int) -> bytes:
    return vint(tag << 3) + vint(value)


def blob(tag: int, value: bytes | str) -> bytes:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return vint((tag << 3) | 2) + vint(len(value)) + value


def content_json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def message(*, msg_type: int = 7, content: str | None = None,
            sender: int = 9007199254740993, mid: int = 9007199254740995,
            conv: str = "synthetic-thread", conv_type: int = 1,
            time: int = 1700000000123, extra: bytes = b"") -> bytes:
    if content is None:
        content = content_json({"aweType": 700, "type": 0, "text": "合成测试消息"})
    return (blob(1, conv) + integer(2, conv_type) + integer(3, mid)
            + integer(5, 333) + integer(6, msg_type) + integer(7, sender)
            + blob(8, content) + integer(10, time) + blob(14, "synthetic-sec-uid") + extra)


def response(msg: bytes | None = None, *, cmd: int = 500,
             notify_type: int = 1, status: int = 0, body_tag: int | None = None) -> bytes:
    if msg is None:
        msg = message()
    if cmd == 1099:
        notification = blob(2, msg)
        tag = 503
    else:
        notification = (blob(2, "synthetic-thread") + integer(3, 1)
                        + integer(4, notify_type) + blob(5, msg))
        tag = 500
    return integer(1, cmd) + integer(3, status) + blob(6, blob(body_tag or tag, notification))


def frame(body: bytes | None = None, *, encoding: str = "", payload_type: str = "pb",
          seq: int = 1, extra: bytes = b"") -> bytes:
    if body is None:
        body = response()
    return (integer(1, seq) + integer(2, 9007199254740999) + integer(3, 100)
            + integer(4, 1) + blob(6, encoding) + blob(7, payload_type) + blob(8, body) + extra)


def literal_lz4(data: bytes) -> bytes:
    """SYNTHETIC valid single literal-only raw LZ4 block, not a frame container."""
    if len(data) < 15:
        return bytes([len(data) << 4]) + data
    size = len(data) - 15
    extension = bytes([255]) * (size // 255) + bytes([size % 255])
    return b"\xf0" + extension + data


def json_response() -> dict:
    return {"cmd": 500, "statusCode": 0, "body": {"hasNewMessageNotify": {
        "notifyType": 2, "conversationId": "synthetic-thread", "message": {
            "conversationId": "synthetic-thread", "conversationType": 1,
            "serverMessageId": "9007199254740995", "sender": "9007199254740993",
            "messageType": 7, "createTime": "1700000000123",
            "content": content_json({"text": "合成JSON"}),
        }}}}


class DecoderTests(unittest.TestCase):
    def one(self, raw: bytes | str, **kwargs) -> dict:
        result = decode_frame(raw, **kwargs)
        self.assertEqual(len(result.events), 1, result.diagnostics)
        self.assertTrue(result.dirty)
        self.assertTrue(result.raw_frame_id.startswith("sha256:"))
        event = result.events[0]
        self.assertEqual(set(event), {"event_id", "raw_frame_id", "type", "from_uid",
                                     "from_nick", "content", "timestamp", "aweme_id",
                                     "comment_id", "extra"})
        self.assertEqual(event["extra"]["channel"], "im")
        self.assertEqual(event["extra"]["route"], "dm")
        return event

    def unknown(self, raw, code: str | None = None, **kwargs):
        result = decode_frame(raw, **kwargs)
        self.assertEqual(result.events, [])
        self.assertTrue(result.dirty)
        self.assertTrue(result.diagnostics)
        if code:
            self.assertIn(code, result.diagnostics)
        return result

    def test_text_and_lossless_int64(self):
        event = self.one(frame())
        self.assertEqual(event["from_uid"], "9007199254740993")
        self.assertEqual(event["event_id"], "im:synthetic-thread:9007199254740995")
        self.assertEqual(event["content"], "合成测试消息")
        self.assertEqual(event["type"], "dm")
        self.assertEqual(event["timestamp"], 1700000000123)
        self.assertEqual(event["extra"]["timestamp_unit"], "unknown")
        self.assertEqual(event["from_nick"], "")

    def test_retransmission_identity_is_not_transport_sequence(self):
        first, second = self.one(frame(seq=1)), self.one(frame(seq=2))
        self.assertEqual(first["event_id"], second["event_id"])
        self.assertNotEqual(first["raw_frame_id"], second["raw_frame_id"])

    def test_plain_text_and_small_valid_frame(self):
        event = self.one(frame(response(message(content="hi"))))
        self.assertEqual(event["content"], "hi")
        tiny = frame(response(message(content="x", sender=1, mid=2)))
        self.one(tiny)

    def test_notify_types_and_stranger(self):
        for kind in (1, 2):
            with self.subTest(kind=kind):
                self.assertEqual(self.one(frame(response(notify_type=kind)))["extra"]["notify_type"], kind)
        self.assertEqual(self.one(frame(response(cmd=1099)))["extra"]["response_cmd"], 1099)
        self.unknown(frame(response(notify_type=3)), "unknown_notify_type")

    def test_share_cards_do_not_use_author_as_sender(self):
        for msg_type in (8, 77):
            raw = message(msg_type=msg_type, content=content_json({
                "itemId": "123456", "content_title": "作品", "uid": "111",
                "content_name": "作品作者，不是发信人"}))
            event = self.one(frame(response(raw)))
            self.assertEqual(event["type"], "share")
            self.assertEqual(event["aweme_id"], "123456")
            self.assertEqual(event["from_uid"], "9007199254740993")
            self.assertEqual(event["from_nick"], "")
            self.assertEqual(event["extra"]["work_author_uid"], "111")

    def test_comment_cards_are_im_not_public_activity(self):
        for msg_type in (16, 75, 105):
            raw = message(msg_type=msg_type, content=content_json({
                "comment": "合成评论卡片", "aweme_id": "123", "comment_id": "456",
                "comment_user_name": "卡片中的作者"}))
            event = self.one(frame(response(raw)))
            self.assertEqual(event["type"], "comment")
            self.assertEqual(event["comment_id"], "456")
            self.assertEqual(event["extra"]["semantic"], "im_comment_card")
            self.assertEqual(event["from_nick"], "")

    def test_mentions_use_utf16_offsets_and_keep_group_type(self):
        raw = message(conv_type=2, content=content_json({"text": "😀@小明 你好", "richTextInfos": [
            {"infoType": 1, "location": 2, "length": 3, "info": {"uid": "555"}},
        ]}))
        event = self.one(frame(response(raw)))
        self.assertEqual(event["type"], "mention")
        self.assertEqual(event["extra"]["conversation_type"], 2)
        self.assertEqual(event["extra"]["mentions"][0]["text"], "@小明")
        self.assertEqual(event["extra"]["semantic"], "im_rich_text_mention")

    def test_invalid_mentions_do_not_invent_mentions(self):
        for start, length in ((-1, 1), (0, 100), (0, 1), (True, 1)):
            raw = message(content=content_json({"text": "😀hi", "richTextInfos": [
                {"infoType": 1, "location": start, "length": length, "info": {"uid": "55"}},
            ]}))
            result = decode_frame(frame(response(raw)))
            self.assertEqual(result.events[0]["type"], "dm")
            self.assertIn("invalid_mention_span", result.diagnostics)

    def test_gzip_outer_and_inner(self):
        self.one(gzip.compress(frame(), mtime=0))
        self.one(frame(gzip.compress(response(), mtime=0), encoding="gzip"))
        self.one(frame(gzip.compress(response(), mtime=0)))
        self.unknown(gzip.compress(frame(), mtime=0)[:-2], "gzip_truncated_or_trailing")
        self.unknown(gzip.compress(frame(), mtime=0) + b"tail", "gzip_truncated_or_trailing")

    def test_raw_lz4_and_overlap(self):
        self.one(frame(literal_lz4(response()), encoding="__lz4"))
        self.assertEqual(wsproto._lz4(b"\x32abc\x03\x00", 20), b"abcabcabc")
        self.assertEqual(wsproto._lz4(b"\x1fa\x01\x00\x00", 20), b"a" * 20)
        self.assertEqual(wsproto._lz4(literal_lz4(b"a" * 300), 300), b"a" * 300)

    def test_lz4_invalid_and_expansion_limit(self):
        for raw in (b"\xf0", b"\x30ab", b"\x10a\x00\x00", b"\x10a\x02\x00", b"\x10a\x01"):
            with self.subTest(raw=raw):
                self.unknown(frame(raw, encoding="__lz4"))
        self.unknown(frame(b"\x1fa\x01\x00\xff\x00", encoding="__lz4"),
                     "unpacked_size_limit", limits=Limits(max_unpacked_bytes=32))

    def test_json_same_schema_adapter(self):
        encoded = content_json(json_response())
        self.assertEqual(self.one(encoded)["content"], "合成JSON")
        self.one(frame(encoded.encode(), payload_type="json"))
        self.one(gzip.compress(encoded.encode(), mtime=0))
        snake = encoded.replace("hasNewMessageNotify", "has_new_message_notify").replace("notifyType", "notify_type")
        self.one(snake)

    def test_unrelated_json_not_classified_by_keywords(self):
        self.unknown('{"type":"comment","text":"hi","user_id":"123","comment_id":"456"}')
        self.unknown('[{"cmd":500}]', "unknown_json_schema")
        self.unknown('{"message":{"sender":123}}')

    def test_unknown_commands_and_wire_types(self):
        for cmd in (501, 502, 504, 507, 9999):
            self.unknown(frame(response(cmd=cmd)), f"unsupported_response_cmd:{cmd}")
        self.unknown(frame(response(message(msg_type=999, content='{"text":"do not guess"}'))),
                     "unsupported_message_type:999")
        self.unknown(frame(response(body_tag=503)), "missing_notify_body")
        self.unknown(frame(response(status=9)), "response_nonzero_status")

    def test_missing_identity_does_not_make_actionable_event(self):
        for kwargs in ({"sender": 0}, {"mid": 0}, {"conv": ""}):
            self.unknown(frame(response(message(**kwargs))), "incomplete_message_identity")
        self.unknown(frame(response(message(conv="wrong"))), "conversation_identity_mismatch")
        self.unknown(frame(response(message(msg_type=8, content='{"text":"no item"}'))), "incomplete_share_card")

    def test_heartbeat_and_ack_only(self):
        for raw in ("hi", b"hi", "ok", b"ok"):
            result = decode_frame(raw)
            self.assertFalse(result.dirty)
            self.assertEqual(result.diagnostics, ["heartbeat"])
        ack = blob(5, blob(1, "is_ack") + blob(2, "1"))
        result = decode_frame(frame(b"", extra=ack))
        self.assertFalse(result.dirty)
        self.assertEqual(result.diagnostics, ["ack"])
        self.one(frame(extra=ack))  # ACK flag must not discard a real body.
        self.unknown(b"")
        self.unknown(b"ping")

    def test_unknown_encoding_and_payload_type(self):
        self.unknown(frame(encoding="brotli"), "unsupported_payload_encoding")
        self.unknown(frame(payload_type="unknown"), "unsupported_payload_type")

    def test_wire_bounds(self):
        cases = (b"\x00", b"\x80", b"\xff" * 10 + b"\x00", b"\x0b",
                 blob(1, b"x")[:-1], vint(1 << 3 | 1) + b"short", vint(1 << 3 | 5) + b"x")
        for raw in cases:
            with self.subTest(raw=raw):
                self.unknown(raw)
        self.unknown(frame()[:-1], "wire_truncated_field")
        self.unknown(frame(extra=integer(1, 55)), "wire_duplicate_singular")
        self.unknown(frame(extra=blob(7, "pb")), "wire_duplicate_singular")
        self.unknown(frame(), "wire_field_limit", limits=Limits(max_fields=4))
        self.unknown(integer(90, 5), "unknown_frame_schema")
        self.unknown(integer(1, 1))  # 只有 seqid、没有 payload 类型：仍然不算可用帧

    def test_unknown_fields_are_skipped_not_guessed(self):
        extra = integer(100, 8) + blob(101, b"arbitrary not a protobuf")
        extra += vint(102 << 3 | 1) + b"12345678" + vint(103 << 3 | 5) + b"1234"
        self.one(frame(extra=extra))
        self.one(frame(response(message(extra=extra))))

    def test_size_limits_and_invalid_inputs(self):
        self.unknown(frame(), "frame_size_limit", limits=Limits(max_frame_bytes=16))
        self.unknown("你好" * 20, "frame_size_limit", limits=Limits(max_frame_bytes=40))
        self.unknown(frame(response(message(content="longer"))), "content_size_limit",
                     limits=Limits(max_content_bytes=4))
        self.unknown(gzip.compress(b"A" * 10000), "unpacked_size_limit",
                     limits=Limits(max_unpacked_bytes=100))
        self.unknown(None, "unsupported_input_type")
        self.unknown(frame(), "invalid_limits", limits=Limits(max_fields=0))
        self.one(bytearray(frame()))
        self.one(memoryview(frame()))

    def test_json_bounds_and_ambiguity(self):
        self.unknown("[" * 40 + "]" * 40, "json_depth_limit")
        self.unknown('[1,2,3,4]', "json_node_limit", limits=Limits(max_json_nodes=4))
        self.unknown('{"x":' + "9" * 200 + '}', "json_integer_limit")
        self.unknown('{"x":NaN}', "json_nonfinite_number")
        self.unknown('{"cmd":500,"cmd":0}', "json_duplicate_key")
        self.unknown('{"x":', "invalid_json")
        data = json_response()
        data["status_code"] = 0
        self.unknown(content_json(data), "json_ambiguous_alias")
        data = json_response()
        data["body"]["hasNewMessageNotify"]["message"]["sender"] = 1.5
        self.unknown(content_json(data), "schema_invalid_integer")

    def test_utf8_error_and_no_private_text_in_diagnostic(self):
        self.unknown(frame(response(message(extra=blob(8, b"\xff")))))
        result = self.unknown('{"PRIVATE_SYNTHETIC_MARKER":')
        self.assertNotIn("PRIVATE_SYNTHETIC_MARKER", repr(result.diagnostics))
        self.unknown("\ud800", "malformed_text_or_number")

    def test_truncation_sweep_is_total(self):
        raw = frame()
        for end in range(len(raw)):
            result = decode_frame(raw[:end])
            self.assertIsInstance(result, wsproto.DecodeResult)
            self.assertTrue(result.dirty)
            self.assertEqual(result.events, [])

    def test_seeded_random_bytes_are_total(self):
        rng = random.Random(20261105)
        for _ in range(1000):
            raw = rng.randbytes(rng.randrange(0, 512))
            result = decode_frame(raw)
            self.assertIsInstance(result, wsproto.DecodeResult)
            self.assertTrue(result.dirty)
            self.assertEqual(result.events, [])

    def test_seeded_structured_mutations_are_total(self):
        rng = random.Random(59)
        base = frame()
        for _ in range(1000):
            value = bytearray(base)
            for _ in range(rng.randrange(1, 5)):
                value[rng.randrange(len(value))] = rng.randrange(256)
            result = decode_frame(value)
            self.assertIsInstance(result, wsproto.DecodeResult)
            self.assertIsInstance(result.dirty, bool)

    def test_schema_reference_fields(self):
        schema = wsproto.SCHEMA_FIELDS
        self.assertEqual(schema["Frame"]["payload"], 8)
        self.assertEqual(schema["Response"]["body"], 6)
        self.assertEqual(schema["ResponseBody"]["has_new_message_notify"], 500)
        self.assertEqual(schema["NewMessageNotify"]["message"], 5)
        self.assertEqual(schema["MessageBody"]["sender"], 7)
        self.assertEqual(schema["MessageBody"]["content"], 8)
        self.assertEqual(wsproto.NOTIFY_TYPES, {1: "PER_USER", 2: "PER_CONVERSATION"})

    def test_evidence_is_pinned(self):
        self.assertEqual(len(wsproto.SCHEMA_COMMIT), 40)
        for source in wsproto.SCHEMA_SOURCES:
            self.assertIn(wsproto.SCHEMA_COMMIT, source)


_REAL = Path(__file__).resolve().parents[1] / "tests" / "_real_frames.txt"


@unittest.skipUnless(_REAL.exists(), "抓包 fixture 只在本地存在（含本人 uid，不进仓库）")
class RealFrameTest(unittest.TestCase):
    """用真机抓到的 frontier-im 帧跑一遍 —— schema 对不对，看这个。"""

    def setUp(self):
        import base64
        self.rows = [base64.b64decode(x.strip())
                     for x in _REAL.read_text(encoding="utf-8").splitlines() if x.strip()]

    def test_real_control_frame_is_not_a_signal(self):
        for raw in self.rows:
            result = decode_frame(raw)
            self.assertEqual(result.events, [])
            self.assertFalse(result.dirty, result.diagnostics)
            self.assertTrue(any(d.startswith("control_message:") for d in result.diagnostics),
                            result.diagnostics)

    def test_real_frame_envelope_decodes(self):
        import importlib
        wire = getattr(wsproto, "_wire")
        budget = wsproto._Budget(wsproto.Limits())
        frame = wire(self.rows[0], budget)
        # 真实帧：1=seqid 2=logid 3=service 4=method，5=headers，7='pb'，8=payload
        self.assertIsNotNone(wsproto._one(frame, 1, 0))
        self.assertEqual(wsproto._text(wsproto._one(frame, 7, 2)), "pb")
        payload = wsproto._one(frame, 8, 2)
        self.assertIsNotNone(payload)
        body = wire(payload, budget)
        self.assertEqual(wsproto._one(body, 1, 0), 500)  # cmd
        self.assertIsNotNone(wsproto._one(body, 6, 2))   # body


if __name__ == "__main__":
    unittest.main()
