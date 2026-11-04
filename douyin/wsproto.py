"""Read-only, bounded Frontier IM decoder (stdlib only).

Evidence, pinned rather than inferred from arbitrary field numbers:
  https://github.com/zhinjs/douyin-im/tree/f6eee669c39094224e00c6b877e1964d9da4e393
  src/services/im/proto/im.proto: Frame, Response, ResponseBody, MessageBody,
    NewMessageNotify, NewMessageNotifyType, StrangerNewMessageNotify.
  src/services/im/ws-client.ts: cmd 500 -> body500, cmd1099 -> body503;
    cmd504 is a DIFFERENT P2P structure, deliberately unsupported here.
  src/services/im/content.ts: text7, share8/77, comment-card16/75/105,
    richTextInfos/infoType1 mentions (UTF-16 offsets).
  src/services/im/frame.ts and lz4.ts: __lz4 is a raw LZ4 block, not gzip.

This is a third-party reconstruction: its schema header says "Generated from
Douyin Chat 1.2.1 native descriptors and Frontier renderer codec." It is NOT an
assertion that every current browser endpoint uses this schema. No captured
private traffic is bundled. JSON Response support is a protobufjs-style schema
adapter, not evidence that Frontier sends JSON; gzip support is a bounded
transport convenience, not a claim that the SDK uses gzip. Unknown packets,
read receipts, P2P, commands, and frontier-pc activity notices stay dirty for DOM.

Every emitted event has extra.channel='im', extra.route='dm'. A 'comment' is an
IM comment CARD, a 'mention' an IM rich-text mention, NEVER a public activity
notification or permission to post a public reply. Group conversation_type is
preserved. Nickname mapping and create_time units are not evidenced: nickname
stays empty; timestamp is the raw integer with timestamp_unit='unknown'.

API: decode_frame(bytes | bytearray | memoryview | str, *, limits=Limits())
returns DecodeResult(events, dirty, diagnostics, raw_frame_id). Business frames
remain dirty even after successful decoding so DOM can supplement missing data.
No network, ACK transmission, credentials, browser access, or protobuf runtime.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import zlib
from typing import Any

SCHEMA_COMMIT = "f6eee669c39094224e00c6b877e1964d9da4e393"
_SOURCE = f"https://github.com/zhinjs/douyin-im/blob/{SCHEMA_COMMIT}/src/services/im/"
SCHEMA_SOURCES = tuple(_SOURCE + name for name in (
    "proto/im.proto", "ws-client.ts", "content.ts", "frame.ts", "lz4.ts", "codec.ts",
))
# Auditable subset, exact field numbers from proto/im.proto (not heuristic tags).
# Unlisted fields are intentionally not interpreted. int64 IDs remain integers
# during wire parsing and decimal strings in the public event contract.
SCHEMA_FIELDS = {
    "Frame": {"seqid": 1, "logid": 2, "service": 3, "method": 4,
              "headers": 5, "payload_encoding": 6, "payload_type": 7,
              "payload": 8, "log_id_new": 9, "server_timing": 10,
              "msg_id": 11, "frame_type": 12},
    "Response": {"cmd": 1, "sequence_id": 2, "status_code": 3,
                 "error_desc": 4, "inbox_type": 5, "body": 6, "log_id": 7},
    "ResponseBody": {"has_new_message_notify": 500,
                     "mark_conversation_read_notify": 501,
                     "conversation_info_updated_notify": 502,
                     "stranger_has_new_message_notify": 503,
                     "has_new_p2p_message_notify": 504,
                     "new_friend_message_notify": 507},
    "NewMessageNotify": {"conversation_id": 2, "conversation_type": 3,
                         "notify_type": 4, "message": 5, "need_pull": 21},
    "StrangerNewMessageNotify": {"message": 2},
    "MessageBody": {"conversation_id": 1, "conversation_type": 2,
                    "server_message_id": 3, "index_in_conversation": 4,
                    "conversation_short_id": 5, "message_type": 6, "sender": 7,
                    "content": 8, "ext": 9, "create_time": 10, "version": 11,
                    "status": 12, "order_in_conversation": 13, "sec_sender": 14,
                    "property_list": 15, "user_profile": 16},
}
NOTIFY_TYPES = {1: "PER_USER", 2: "PER_CONVERSATION"}


@dataclass(frozen=True)
class Limits:
    max_frame_bytes: int = 1024 * 1024
    max_unpacked_bytes: int = 4 * 1024 * 1024
    max_fields: int = 8192
    max_depth: int = 32
    max_json_nodes: int = 16384
    max_content_bytes: int = 256 * 1024


@dataclass
class DecodeResult:
    events: list[dict[str, Any]] = field(default_factory=list)
    dirty: bool = True
    diagnostics: list[str] = field(default_factory=list)
    raw_frame_id: str = ""


class DecodeError(ValueError):
    """A bounded, non-payload-bearing diagnostic."""


@dataclass
class _Budget:
    limits: Limits
    fields: int = 0


def _varint(data: bytes, pos: int) -> tuple[int, int]:
    value = 0
    for index in range(10):
        if pos >= len(data):
            raise DecodeError("wire_truncated_varint")
        octet = data[pos]
        pos += 1
        if index == 9 and octet > 1:
            raise DecodeError("wire_varint_overflow")
        value |= (octet & 127) << (7 * index)
        if not octet & 128:
            return value, pos
    raise DecodeError("wire_varint_overflow")


def _wire(data: bytes, budget: _Budget) -> dict[int, list[tuple[int, Any]]]:
    """Parse one level, skipping unknown fixed fields without recursively guessing."""
    fields: dict[int, list[tuple[int, Any]]] = {}
    pos = 0
    while pos < len(data):
        budget.fields += 1
        if budget.fields > budget.limits.max_fields:
            raise DecodeError("wire_field_limit")
        key, pos = _varint(data, pos)
        number, kind = key >> 3, key & 7
        if not 1 <= number <= 536870911:
            raise DecodeError("wire_invalid_field_number")
        if kind == 0:
            value, pos = _varint(data, pos)
        elif kind in (1, 2, 5):
            if kind == 2:
                length, pos = _varint(data, pos)
            else:
                length = 8 if kind == 1 else 4
            if length > len(data) - pos:
                raise DecodeError("wire_truncated_field")
            value = data[pos:pos + length]
            pos += length
        else:
            raise DecodeError("wire_unsupported_type")
        fields.setdefault(number, []).append((kind, value))
    return fields


def _one(fields: dict, number: int, kind: int, default: Any = None) -> Any:
    values = fields.get(number, [])
    if not values:
        return default
    if len(values) != 1:
        raise DecodeError("wire_duplicate_singular")
    if values[0][0] != kind:
        raise DecodeError("wire_schema_type_mismatch")
    return values[0][1]


def _text(data: bytes | None) -> str:
    if data is None:
        return ""
    try:
        return data.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise DecodeError("invalid_utf8") from exc


def _gzip(data: bytes, limit: int) -> bytes:
    try:
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        output = decoder.decompress(data, limit + 1)
        if len(output) > limit or decoder.unconsumed_tail:
            raise DecodeError("unpacked_size_limit")
        if not decoder.eof or decoder.unused_data:
            raise DecodeError("gzip_truncated_or_trailing")
        return output
    except zlib.error as exc:
        raise DecodeError("invalid_gzip") from exc


def _lz4(data: bytes, limit: int) -> bytes:
    """Raw block per pinned lz4.ts; bounds checked before every read/expansion."""
    pos = 0
    output = bytearray()

    def length(base: int) -> int:
        nonlocal pos
        total = base
        if base == 15:
            while True:
                if pos >= len(data):
                    raise DecodeError("lz4_truncated_length")
                extra = data[pos]
                pos += 1
                total += extra
                if total > limit:
                    raise DecodeError("unpacked_size_limit")
                if extra != 255:
                    break
        return total

    while pos < len(data):
        token = data[pos]
        pos += 1
        literals = length(token >> 4)
        if pos + literals > len(data):
            raise DecodeError("lz4_truncated_literals")
        if len(output) + literals > limit:
            raise DecodeError("unpacked_size_limit")
        output.extend(data[pos:pos + literals])
        pos += literals
        if pos == len(data):
            return bytes(output)
        if pos + 2 > len(data):
            raise DecodeError("lz4_truncated_offset")
        offset = data[pos] | data[pos + 1] << 8
        pos += 2
        if offset == 0 or offset > len(output):
            raise DecodeError("lz4_invalid_offset")
        matches = length(token & 15) + 4
        if len(output) + matches > limit:
            raise DecodeError("unpacked_size_limit")
        for _ in range(matches):
            output.append(output[-offset])
    return bytes(output)


def _json(text: str, limits: Limits) -> Any:
    # Preflight nesting before json.loads (which otherwise recurses in C).
    depth = 0
    quoted = escaped = False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > limits.max_depth:
                raise DecodeError("json_depth_limit")
        elif char in "]}":
            depth -= 1

    def integer(value: str) -> int:
        if len(value) > 128:
            raise DecodeError("json_integer_limit")
        return int(value)

    def constant(_: str) -> Any:
        raise DecodeError("json_nonfinite_number")

    def pairs(items: list) -> dict:
        result = {}
        for key, value in items:
            if key in result:
                raise DecodeError("json_duplicate_key")
            result[key] = value
        return result

    try:
        value = json.loads(text, parse_int=integer, parse_constant=constant,
                           object_pairs_hook=pairs)
    except (ValueError, RecursionError) as exc:
        if isinstance(exc, DecodeError):
            raise
        raise DecodeError("invalid_json") from exc
    pending = [value]
    nodes = 0
    while pending:
        item = pending.pop()
        nodes += 1
        if nodes > limits.max_json_nodes:
            raise DecodeError("json_node_limit")
        if isinstance(item, dict):
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return value


def _pick(obj: dict, snake: str, camel: str, default: Any = None) -> Any:
    if snake != camel and snake in obj and camel in obj:
        raise DecodeError("json_ambiguous_alias")
    return obj.get(snake, obj.get(camel, default))


def _number(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise DecodeError("schema_invalid_integer")
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 20:
        value = int(value)
    if not isinstance(value, int) or not 0 <= value <= (1 << 63) - 1:
        raise DecodeError("schema_invalid_integer")
    return value


def _string(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise DecodeError("schema_invalid_string")
    return value


def _id(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return str(value)
    return ""


def _message_wire(data: bytes, budget: _Budget) -> dict:
    f = _wire(data, budget)
    result = {}
    for tag, name in ((1, "conversation_id"), (8, "content"), (14, "sec_sender")):
        if tag in f:
            result[name] = _text(_one(f, tag, 2))
    for tag, name in ((2, "conversation_type"), (3, "server_message_id"),
                      (5, "conversation_short_id"), (6, "message_type"),
                      (7, "sender"), (10, "create_time"), (12, "status")):
        if tag in f:
            result[name] = _one(f, tag, 0)
    # ext/profile are not used to invent nicknames or sender identities.
    return result


def _response_wire(data: bytes, budget: _Budget) -> dict:
    f = _wire(data, budget)
    result = {"cmd": _one(f, 1, 0), "status_code": _one(f, 3, 0, 0)}
    cmd = result["cmd"]
    if cmd not in (500, 1099):
        return result
    body = _wire(_one(f, 6, 2, b""), budget)
    tag = 500 if cmd == 500 else 503
    notify_data = _one(body, tag, 2)
    if notify_data is None:
        raise DecodeError("missing_notify_body")
    notify = _wire(notify_data, budget)
    message_data = _one(notify, 5 if cmd == 500 else 2, 2)
    if message_data is None:
        raise DecodeError("missing_message_body")
    result["message"] = _message_wire(message_data, budget)
    if cmd == 500:
        result["notify_type"] = _one(notify, 4, 0)
        result["conversation_id"] = _text(_one(notify, 2, 2))
    return result


def _response_json(value: Any) -> dict:
    if not isinstance(value, dict):
        raise DecodeError("unknown_json_schema")
    cmd = _number(value.get("cmd"))
    result = {"cmd": cmd, "status_code": _pick(value, "status_code", "statusCode", 0)}
    if cmd not in (500, 1099):
        return result
    body = value.get("body")
    if not isinstance(body, dict):
        raise DecodeError("missing_notify_body")
    if cmd == 500:
        notify = _pick(body, "has_new_message_notify", "hasNewMessageNotify")
    else:
        notify = _pick(body, "stranger_has_new_message_notify", "strangerHasNewMessageNotify")
    if not isinstance(notify, dict) or not isinstance(notify.get("message"), dict):
        raise DecodeError("missing_message_body")
    result["message"] = notify["message"]
    if cmd == 500:
        result["notify_type"] = _pick(notify, "notify_type", "notifyType")
        result["conversation_id"] = _pick(notify, "conversation_id", "conversationId", "")
    return result


def _normalize(response: dict, result: DecodeResult, limits: Limits) -> None:
    cmd = _number(response.get("cmd"))
    if _number(response.get("status_code")):
        raise DecodeError("response_nonzero_status")
    if cmd not in (500, 1099):
        result.diagnostics.append(f"unsupported_response_cmd:{cmd}")
        return
    notify_type = response.get("notify_type")
    if notify_type is not None and _number(notify_type) not in (1, 2):
        raise DecodeError("unknown_notify_type")
    message = response["message"]
    get = lambda snake, camel, default=None: _pick(message, snake, camel, default)
    sender = _number(message.get("sender"))
    mid = _number(get("server_message_id", "serverMessageId"))
    conv = _string(get("conversation_id", "conversationId"))
    if not sender or not mid or not conv:
        raise DecodeError("incomplete_message_identity")
    outer_conv = response.get("conversation_id")
    if outer_conv and outer_conv != conv:
        raise DecodeError("conversation_identity_mismatch")
    msg_type = _number(get("message_type", "messageType"))
    content = _string(message.get("content"))
    if len(content.encode("utf-8")) > limits.max_content_bytes:
        raise DecodeError("content_size_limit")
    parsed: Any = None
    if content.lstrip().startswith(("{", "[")):
        parsed = _json(content, limits)
        if not isinstance(parsed, dict):
            raise DecodeError("unknown_content_schema")
    data = parsed or {}
    extra: dict[str, Any] = {
        "channel": "im", "route": "dm", "schema_commit": SCHEMA_COMMIT,
        "conversation_id": conv,
        "conversation_type": _number(get("conversation_type", "conversationType")),
        "conversation_short_id": str(_number(get("conversation_short_id", "conversationShortId"))),
        "message_type": msg_type, "notify_type": notify_type, "response_cmd": cmd,
        "sec_sender": _string(get("sec_sender", "secSender")),
        "timestamp_unit": "unknown", "sender_nickname_missing": True,
    }
    kind, aweme_id, comment_id = "dm", "", ""
    if msg_type == 7:
        text = _string(data.get("text")) if parsed is not None else content
        if parsed is not None and "text" not in data:
            raise DecodeError("unknown_content_schema")
        mentions = []
        infos = data.get("richTextInfos", [])
        if not isinstance(infos, list):
            raise DecodeError("invalid_mention_list")
        utf16 = text.encode("utf-16-le")
        for info in infos:
            if not isinstance(info, dict) or info.get("infoType") != 1:
                continue
            details = info.get("info")
            uid = _id(details.get("uid")) if isinstance(details, dict) else ""
            start, length = info.get("location"), info.get("length")
            if (not uid or type(start) is not int or type(length) is not int
                    or start < 0 or length <= 0 or (start + length) * 2 > len(utf16)):
                result.diagnostics.append("invalid_mention_span")
                continue
            try:
                label = utf16[start * 2:(start + length) * 2].decode("utf-16-le")
            except UnicodeError:
                result.diagnostics.append("invalid_mention_span")
                continue
            mentions.append({"uid": uid, "text": label, "location": start, "length": length})
        if mentions:
            kind = "mention"
            extra["mentions"] = mentions
            extra["semantic"] = "im_rich_text_mention"
    elif msg_type in (8, 77):
        aweme_id = _id(data.get("itemId"))
        if not aweme_id:
            raise DecodeError("incomplete_share_card")
        kind = "share"
        text = _string(data.get("text")) or _string(data.get("content_title"))
        extra["work_author_uid"] = _id(data.get("uid"))
        extra["semantic"] = "im_work_card"
    elif msg_type in (16, 75, 105):
        if parsed is None or not any(key in data for key in ("comment", "text")):
            raise DecodeError("incomplete_comment_card")
        kind = "comment"
        text = _string(data.get("comment", data.get("text")))
        aweme_id = _id(data.get("itemId", data.get("aweme_id")))
        comment_id = _id(data.get("comment_id"))
        extra["comment_author_name"] = _string(data.get("comment_user_name"))
        extra["semantic"] = "im_comment_card"
    else:
        result.diagnostics.append(f"unsupported_message_type:{msg_type}")
        return
    result.events.append({
        "event_id": f"im:{conv}:{mid}", "raw_frame_id": result.raw_frame_id,
        "type": kind, "from_uid": str(sender), "from_nick": "", "content": text,
        "timestamp": _number(get("create_time", "createTime")),
        "aweme_id": aweme_id, "comment_id": comment_id, "extra": extra,
    })


def decode_frame(payload: bytes | bytearray | memoryview | str, *,
                 limits: Limits | None = None) -> DecodeResult:
    """Decode one received frame; malformed/unknown inputs return dirty diagnostics.

    raw_frame_id is a content hash, never a transport sequence used as an event
    identity. Retransmissions with changed envelopes deduplicate by server ID.
    diagnostics contain only codes/numeric schema types, never payload text.
    """
    result = DecodeResult()
    limits = limits or Limits()
    try:
        if any(type(value) is not int or value <= 0 for value in vars(limits).values()):
            raise DecodeError("invalid_limits")
        if isinstance(payload, str):
            if len(payload) > limits.max_frame_bytes:
                raise DecodeError("frame_size_limit")
            raw = payload.encode("utf-8")
        elif isinstance(payload, (bytes, bytearray, memoryview)):
            size = payload.nbytes if isinstance(payload, memoryview) else len(payload)
            if size > limits.max_frame_bytes:
                raise DecodeError("frame_size_limit")
            raw = bytes(payload)
        else:
            raise DecodeError("unsupported_input_type")
        if len(raw) > limits.max_frame_bytes:
            raise DecodeError("frame_size_limit")
        result.raw_frame_id = "sha256:" + hashlib.sha256(raw).hexdigest()
        if raw in (b"hi", b"ok"):
            result.dirty = False
            result.diagnostics.append("heartbeat")
            return result
        if not raw:
            raise DecodeError("empty_frame")
        if raw.startswith(b"\x1f\x8b"):
            raw = _gzip(raw, limits.max_unpacked_bytes)
        if raw.lstrip().startswith((b"{", b"[")):
            response = _response_json(_json(_text(raw), limits))
        else:
            budget = _Budget(limits)
            frame = _wire(raw, budget)
            for tag in (1, 2, 3, 4):
                if _one(frame, tag, 0) is None:
                    raise DecodeError("unknown_frame_schema")
            body = _one(frame, 8, 2, b"")
            headers = {}
            for kind, entry in frame.get(5, []):
                if kind != 2:
                    raise DecodeError("wire_schema_type_mismatch")
                item = _wire(entry, budget)
                key, value = _text(_one(item, 1, 2)), _text(_one(item, 2, 2))
                if key in headers:
                    raise DecodeError("duplicate_frame_header")
                headers[key] = value
            if headers.get("is_ack") == "1" and not body:
                result.dirty = False
                result.diagnostics.append("ack")
                return result
            encoding = _text(_one(frame, 6, 2))
            payload_type = _text(_one(frame, 7, 2))
            if encoding == "__lz4":
                body = _lz4(body, limits.max_unpacked_bytes)
            elif encoding == "gzip":
                body = _gzip(body, limits.max_unpacked_bytes)
            elif encoding:
                raise DecodeError("unsupported_payload_encoding")
            elif body.startswith(b"\x1f\x8b"):
                body = _gzip(body, limits.max_unpacked_bytes)
            if len(body) > limits.max_unpacked_bytes:
                raise DecodeError("unpacked_size_limit")
            if payload_type == "pb":
                response = _response_wire(body, budget)
            elif payload_type == "json":
                response = _response_json(_json(_text(body), limits))
            else:
                raise DecodeError("unsupported_payload_type")
        _normalize(response, result, limits)
    except DecodeError as exc:
        result.events.clear()
        result.diagnostics.append(str(exc))
    except (UnicodeError, RecursionError, OverflowError):
        result.events.clear()
        result.diagnostics.append("malformed_text_or_number")
    return result
