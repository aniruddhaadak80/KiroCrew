"""WeCom long-connection reliability contracts.

Each class here pins one way the channel used to lose a turn, or deliver one it
should not have, on the WebSocket surface:

* a redelivered callback ran the whole turn a second time;
* a group message ran inside the sender's private DM session;
* shutdown returned while turn tasks were still using the session it closed;
* a rejected bot credential left the settings badge green;
* a stream bubble sealed by the platform kept "accepting" frames that went
  nowhere, so the rest of the answer — including the final frame — was lost;
* the anti-kick event never matched, so a replaced connection kept reconnecting.

These are wire-level behaviours of the real ``WeComClient`` /
``WeComTransport``, so the tests drive the real objects against fakes rather
than mocking the behaviour under test.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.testing.channel_fixtures import load_fixture
from kiro_crew.wecom.client import (
    _MSGID_WINDOW_MAX,
    _MSGID_WINDOW_TTL_SECS,
    _SUBSCRIBE_REQ_PREFIX,
    CHAT_TYPE_UNKNOWN,
    WeComClient,
    WeComInbound,
    _build_subscribe_frame,
    new_stream_id,
)
from kiro_crew.wecom.renderer import WeComRenderer
from kiro_crew.wecom.transport import WECOM_CAPABILITIES, WeComTransport

#: Anchored to the repo root, never a relative path: xdist workers may change CWD.
_REPO_ROOT = Path(__file__).resolve().parents[1]
CHANNEL_FIXTURES = _REPO_ROOT / "test" / "fixtures" / "channels"


def _client() -> WeComClient:
    return WeComClient(bot_id="bot", secret="s", ws_url="wss://example.invalid/ws")


class RecordingWS:
    """Fake socket that records frames and never closes itself.

    Bound to a client so it can ACK as the frame is written: ``send_stream`` and
    ``send_proactive`` register their waiter BEFORE the send, so resolving it here
    stands in for the platform's reply without a test waiting out the real timeout.
    ``ack_errcode`` makes it answer with a refusal instead; ``ack=False`` makes it
    answer not at all, which is the timeout case.
    """

    def __init__(
        self,
        client: WeComClient | None = None,
        *,
        ack: bool = True,
        ack_errcode: int = 0,
        ack_errcode_by_cmd: dict[str, int] | None = None,
    ) -> None:
        self.sent: list[dict] = []
        self.closed = False
        self._client = client
        self._ack = ack
        self.ack_errcode = ack_errcode
        #: Per-cmd override, for a test that needs one command refused and another
        #: accepted -- e.g. proving a push refusal is not blamed on a stream bubble.
        self._by_cmd = ack_errcode_by_cmd or {}

    async def send_json(self, frame: dict) -> None:
        self.sent.append(frame)
        if not self._ack or self._client is None:
            return
        # Route the reply through the client's REAL ACK handler rather than
        # reimplementing it here, so the fake cannot drift from what the server
        # actually causes — waiter resolution, dead-stream marking and
        # subscribe-auth detection are all one code path.
        req_id = (frame.get("headers") or {}).get("req_id", "")
        errcode = self._by_cmd.get(frame.get("cmd", ""), self.ack_errcode)
        await self._client._handle_message(
            json.dumps({"headers": {"req_id": req_id}, "errcode": errcode})
        )

    async def close(self) -> None:
        self.closed = True

    def stream_ids(self) -> list[str]:
        return [f["body"]["stream"]["id"] for f in self.sent if "body" in f]

    def contents(self) -> list[str]:
        return [f["body"]["stream"]["content"] for f in self.sent if "body" in f]


@contextlib.contextmanager
def capture(logger_name: str, level: int = logging.DEBUG) -> Iterator[list[str]]:
    """Collect messages from ONE logger, independent of propagation.

    ``caplog`` installs its handler at the root, so a module logger that does not
    propagate is captured locally and silently missed under CI's sharded run — a
    test of mine failed exactly that way on the 3.10 shard. Attaching to the named
    logger removes the dependency entirely.
    """
    messages: list[str] = []

    class _Sink(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(record.getMessage())

    sink = _Sink(level)
    log = logging.getLogger(logger_name)
    previous = log.level
    log.addHandler(sink)
    log.setLevel(level)
    try:
        yield messages
    finally:
        log.removeHandler(sink)
        log.setLevel(previous)


def _ack(req_id: str, errcode: int) -> str:
    """A cmd-less reply/ping ACK frame, as WeCom sends it."""
    return json.dumps({"headers": {"req_id": req_id}, "errcode": errcode, "errmsg": "e"})


# ---------------------------------------------------------------------------
# Inbound dedupe
# ---------------------------------------------------------------------------


class TestRedeliveryIsSuppressed:
    def test_the_same_msgid_is_delivered_once(self) -> None:
        c = _client()
        assert c.already_delivered("m-1") is False
        assert c.already_delivered("m-1") is True

    def test_an_absent_msgid_is_never_suppressed(self) -> None:
        # No id is no EVIDENCE of a duplicate. Suppressing on an empty key would
        # collapse every id-less frame into one and drop real messages.
        c = _client()
        assert c.already_delivered("") is False
        assert c.already_delivered("") is False

    def test_distinct_msgids_all_pass(self) -> None:
        c = _client()
        assert [c.already_delivered(f"m{i}") for i in range(5)] == [False] * 5

    def test_the_window_is_bounded_and_evicts_the_oldest(self) -> None:
        c = _client()
        for i in range(_MSGID_WINDOW_MAX + 10):
            c.already_delivered(f"m{i}")
        assert len(c._seen_msgids) <= _MSGID_WINDOW_MAX
        # The oldest ids fell out; the newest are still remembered.
        assert c.already_delivered(f"m{_MSGID_WINDOW_MAX + 9}") is True
        assert c.already_delivered("m0") is False

    def test_an_entry_older_than_the_ttl_is_readmitted(self, monkeypatch) -> None:
        c = _client()
        clock = {"t": 1000.0}
        monkeypatch.setattr("kiro_crew.wecom.client.time.monotonic", lambda: clock["t"])
        assert c.already_delivered("m-1") is False
        clock["t"] += _MSGID_WINDOW_TTL_SECS + 1
        assert c.already_delivered("m-1") is False, "a stale entry must not suppress forever"

    @pytest.mark.asyncio
    async def test_the_transport_drops_a_redelivered_frame_before_dispatch(self) -> None:
        c = _client()
        seen: list[str] = []

        async def dispatch(inbound: WeComInbound) -> None:
            seen.append(inbound.text)

        t = WeComTransport(c, allowed_users=["Wei"], dispatch=dispatch)
        frame = WeComInbound(userid="Wei", text="hi", req_id="r1", msgid="dup")
        await t.receive(frame)
        await t.receive(frame)
        assert seen == ["hi"], "a redelivered callback must not run the turn twice"

    @pytest.mark.asyncio
    async def test_an_unauthorized_frame_does_not_consume_the_window(self) -> None:
        # Ordering matters: recording before authorization would let an
        # unauthorized sender evict genuine entries, reopening the gap.
        c = _client()
        seen: list[str] = []

        async def dispatch(inbound: WeComInbound) -> None:
            seen.append(inbound.text)

        t = WeComTransport(c, allowed_users=["Wei"], dispatch=dispatch)
        await t.receive(WeComInbound(userid="stranger", text="x", req_id="r", msgid="shared"))
        await t.receive(WeComInbound(userid="Wei", text="mine", req_id="r", msgid="shared"))
        assert seen == ["mine"], "the allowed user's message was suppressed by a denied one"


# ---------------------------------------------------------------------------
# Group chats fail closed
# ---------------------------------------------------------------------------


class TestGroupChatFailsClosed:
    @pytest.mark.asyncio
    async def test_a_group_message_is_refused(self) -> None:
        # Sessions are keyed on userid, so a group turn would run inside the
        # sender's private DM session and publish its history to the room.
        c = _client()
        seen: list[str] = []

        async def dispatch(inbound: WeComInbound) -> None:
            seen.append(inbound.text)

        t = WeComTransport(c, allowed_users=["Wei"], dispatch=dispatch)
        await t.receive(
            WeComInbound(userid="Wei", text="hi", req_id="r1", chatid="room-1", chattype="group")
        )
        assert seen == []

    @pytest.mark.asyncio
    async def test_a_direct_message_still_passes(self) -> None:
        c = _client()
        seen: list[str] = []

        async def dispatch(inbound: WeComInbound) -> None:
            seen.append(inbound.text)

        t = WeComTransport(c, allowed_users=["Wei"], dispatch=dispatch)
        await t.receive(WeComInbound(userid="Wei", text="hi", req_id="r1", chattype="single"))
        assert seen == ["hi"]

    @pytest.mark.asyncio
    async def test_an_absent_chattype_is_treated_as_direct(self) -> None:
        # WeCom sends chattype for group traffic; defaulting the other way would
        # fail closed on every ordinary DM.
        c = _client()
        seen: list[str] = []

        async def dispatch(inbound: WeComInbound) -> None:
            seen.append(inbound.text)

        t = WeComTransport(c, allowed_users=["Wei"], dispatch=dispatch)
        await t.receive(WeComInbound(userid="Wei", text="hi", req_id="r1"))
        assert seen == ["hi"]

    @pytest.mark.asyncio
    async def test_the_refusal_is_audited(self) -> None:
        c = _client()
        t = WeComTransport(c, allowed_users=["Wei"], dispatch=None)
        rows: list[dict[str, Any]] = []

        class FakeSel:
            def log_api_access(self, **kw: Any) -> None:
                rows.append(kw)

        import kiro_crew.wecom.transport as mod

        original = mod.sel
        mod.sel = lambda: FakeSel()  # type: ignore[assignment]
        try:
            await t.receive(WeComInbound(userid="Wei", text="hi", req_id="r", chattype="group"))
        finally:
            mod.sel = original  # type: ignore[assignment]
        assert rows and rows[0]["outcome"] == "denied"
        assert "denied_group_chat" in rows[0]["resources"]

    @pytest.mark.asyncio
    async def test_the_routing_fields_are_parsed_off_the_wire(self) -> None:
        # The gate can only fail closed if chattype actually reaches it, and the
        # dedupe window can only work if msgid does.
        c = _client()
        captured: list[WeComInbound] = []

        async def collect(inbound: WeComInbound) -> None:
            captured.append(inbound)

        c.set_message_handler(collect)
        c._dispatch_callback(
            {
                "headers": {"req_id": "r1"},
                "body": {
                    "msgid": "m1",
                    "chattype": "group",
                    "chatid": "room",
                    "from": {"userid": "Wei"},
                    "text": {"content": "hi"},
                },
            }
        )
        await asyncio.gather(*list(c._handler_tasks))

        assert len(captured) == 1
        assert captured[0].msgid == "m1"
        assert captured[0].chattype == "group"
        assert captured[0].chatid == "room"

    @pytest.mark.asyncio
    async def test_non_string_routing_fields_degrade_to_safe_defaults(self) -> None:
        c = _client()
        captured: list[WeComInbound] = []

        async def collect(inbound: WeComInbound) -> None:
            captured.append(inbound)

        c.set_message_handler(collect)
        c._dispatch_callback(
            {
                "headers": {"req_id": "r1"},
                "body": {
                    "msgid": 12345,
                    "chattype": {"weird": True},
                    "from": {"userid": "Wei"},
                    "text": {"content": "hi"},
                },
            }
        )
        await asyncio.gather(*list(c._handler_tasks))

        assert captured[0].msgid == "", "a non-string msgid must not enter the window"
        assert captured[0].chattype == CHAT_TYPE_UNKNOWN, (
            "a malformed chattype must NOT read as a direct chat -- that is the "
            "fail-open shape, where a malformed group callback passes the "
            "direct-only gate and leaks a private session into the room"
        )

    @pytest.mark.asyncio
    async def test_a_malformed_chattype_is_refused_by_the_gate(self) -> None:
        # The end-to-end half of the case above: the sentinel has to be something
        # the gate actually rejects, not merely something that is not "single".
        c = _client()
        seen: list[str] = []

        async def dispatch(inbound: WeComInbound) -> None:
            seen.append(inbound.text)

        t = WeComTransport(c, allowed_users=["Wei"], dispatch=dispatch)
        await t.receive(
            WeComInbound(userid="Wei", text="hi", req_id="r", chattype=CHAT_TYPE_UNKNOWN)
        )
        assert seen == []

    @pytest.mark.asyncio
    async def test_an_unknown_but_real_chattype_string_is_also_refused(self) -> None:
        # A value WeCom may add later is "not a DM" until someone decides it is.
        c = _client()
        seen: list[str] = []

        async def dispatch(inbound: WeComInbound) -> None:
            seen.append(inbound.text)

        t = WeComTransport(c, allowed_users=["Wei"], dispatch=dispatch)
        await t.receive(WeComInbound(userid="Wei", text="hi", req_id="r", chattype="broadcast"))
        assert seen == []


# ---------------------------------------------------------------------------
# Quiescent shutdown
# ---------------------------------------------------------------------------


class TestShutdownIsQuiescent:
    @pytest.mark.asyncio
    async def test_close_awaits_in_flight_turn_tasks(self) -> None:
        c = _client()
        started = asyncio.Event()
        finished: list[str] = []

        async def slow_turn(_inbound: WeComInbound) -> None:
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                finished.append("cancelled")
                raise

        c.set_message_handler(slow_turn)
        c._dispatch_callback(
            {
                "headers": {"req_id": "r1"},
                "body": {"from": {"userid": "Wei"}, "text": {"content": "hi"}},
            }
        )
        await started.wait()

        await c.close()

        assert finished == ["cancelled"], (
            "close() returned while a turn task was still running -- it would "
            "keep using the session close() is about to shut"
        )
        assert not any(not t.done() for t in c._handler_tasks)

    @pytest.mark.asyncio
    async def test_close_is_safe_with_no_tasks(self) -> None:
        await _client().close()  # must not raise


# ---------------------------------------------------------------------------
# Subscribe ACK
# ---------------------------------------------------------------------------


class TestSubscribeAck:
    def test_the_subscribe_frame_carries_the_recognizable_prefix(self) -> None:
        frame = _build_subscribe_frame("bot", "secret")
        assert frame["cmd"] == "aibot_subscribe"
        assert frame["headers"]["req_id"].startswith(_SUBSCRIBE_REQ_PREFIX), (
            "without the prefix the cmd-less subscribe ACK is indistinguishable "
            "from a pong, and a rejected credential cannot be detected"
        )

    @pytest.mark.asyncio
    async def test_a_rejected_credential_reports_not_healthy_with_a_reason(self) -> None:
        c = _client()
        events: list[tuple[bool, str]] = []
        c.on_status = lambda healthy, reason: events.append((healthy, reason))
        # Start from healthy so the transition is observable (status is deduped).
        c._notify_status(True, "")
        events.clear()

        await c._handle_message(_ack(_SUBSCRIBE_REQ_PREFIX + "abc", 40001))

        assert events and events[0][0] is False
        assert "credentials" in events[0][1]

    @pytest.mark.asyncio
    async def test_no_vendor_errcode_or_errmsg_reaches_the_log_or_the_badge(self) -> None:
        # Same posture the reply ACK keeps: errmsg can echo the rejected payload,
        # so neither value is logged nor surfaced on the settings badge.
        c = _client()
        events: list[tuple[bool, str]] = []
        c.on_status = lambda healthy, reason: events.append((healthy, reason))
        secret = "sk-live-SUBSCRIBESECRET"
        raw = json.dumps(
            {
                "headers": {"req_id": _SUBSCRIBE_REQ_PREFIX + "abc"},
                "errcode": 40001,
                "errmsg": f"invalid credential {secret}",
            }
        )
        with capture("kiro_crew.wecom.client") as messages:
            await c._handle_message(raw)

        blob = "\n".join(messages)
        assert messages, "capture is not working, so the assertions below prove nothing"
        assert "40001" not in blob
        assert secret not in blob
        assert all("40001" not in r and secret not in r for _, r in events)

    @pytest.mark.asyncio
    async def test_an_accepted_subscribe_reports_nothing(self) -> None:
        c = _client()
        events: list[tuple[bool, str]] = []
        c.on_status = lambda healthy, reason: events.append((healthy, reason))
        await c._handle_message(_ack(_SUBSCRIBE_REQ_PREFIX + "abc", 0))
        assert events == []

    @pytest.mark.asyncio
    async def test_a_subscribe_ack_never_marks_a_stream_dead(self) -> None:
        c = _client()
        c._track_stream(_SUBSCRIBE_REQ_PREFIX + "abc", "stream-x")
        await c._handle_message(_ack(_SUBSCRIBE_REQ_PREFIX + "abc", 40001))
        assert c.stream_is_dead("stream-x") is False

    @pytest.mark.asyncio
    async def test_a_credential_error_does_not_stop_reconnecting(self) -> None:
        # A secret can be corrected in the dashboard while the gateway runs.
        c = _client()
        await c._handle_message(_ack(_SUBSCRIBE_REQ_PREFIX + "abc", 40001))
        assert c._kicked is False


# ---------------------------------------------------------------------------
# Stream liveness
# ---------------------------------------------------------------------------


class TestSealedStreamIsDetected:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("errcode", [846605, 846608])
    async def test_a_terminal_errcode_marks_the_streams_bubble_dead(self, errcode: int) -> None:
        c = _client()
        ws = RecordingWS(c)
        c._ws = ws  # type: ignore[assignment]
        await c.send_stream("r1", "s1", "hello", finish=False)

        await c._handle_message(_ack("r1", errcode))

        assert c.stream_is_dead("s1") is True

    @pytest.mark.asyncio
    async def test_the_recorded_expired_ack_envelope_is_understood(self) -> None:
        # The ACK envelope is shared by the subscribe, ping and reply
        # acknowledgements and is distinguishable only by req_id, so this branch
        # is read against the RECORDED vendor shape rather than a hand-written
        # echo of the code.
        c = _client()
        ws = RecordingWS(c)
        c._ws = ws  # type: ignore[assignment]
        fx = load_fixture("wecom", "reply_ack_stream_expired", root=CHANNEL_FIXTURES)
        req_id = fx.payload["headers"]["req_id"]
        await c.send_stream(req_id, "s1", "hello", finish=False)

        await c._handle_message(json.dumps(fx.payload))

        assert c.stream_is_dead("s1") is True

    @pytest.mark.asyncio
    async def test_a_non_terminal_errcode_leaves_the_bubble_usable(self) -> None:
        c = _client()
        ws = RecordingWS(c)
        c._ws = ws  # type: ignore[assignment]
        await c.send_stream("r1", "s1", "hello", finish=False)
        await c._handle_message(_ack("r1", 500))
        assert c.stream_is_dead("s1") is False

    @pytest.mark.asyncio
    async def test_a_successful_ack_leaves_the_bubble_usable(self) -> None:
        c = _client()
        ws = RecordingWS(c)
        c._ws = ws  # type: ignore[assignment]
        await c.send_stream("r1", "s1", "hello", finish=False)
        await c._handle_message(_ack("r1", 0))
        assert c.stream_is_dead("s1") is False

    @pytest.mark.asyncio
    async def test_the_newest_bubble_on_a_req_id_owns_the_ack(self) -> None:
        # One req_id legitimately carries several bubbles (an answer plus an
        # out-of-band notice); sends are serialized, so the newest is the one the
        # ACK belongs to.
        c = _client()
        ws = RecordingWS(c)
        c._ws = ws  # type: ignore[assignment]
        await c.send_stream("r1", "old", "a", finish=False)
        await c.send_stream("r1", "new", "b", finish=False)
        await c._handle_message(_ack("r1", 846608))
        assert c.stream_is_dead("new") is True
        assert c.stream_is_dead("old") is False

    @pytest.mark.asyncio
    async def test_a_ping_ack_is_not_read_as_a_reply_refusal(self) -> None:
        c = _client()
        c._ping_reqs.add("ping-1")
        c._track_stream("ping-1", "s1")
        await c._handle_message(_ack("ping-1", 846608))
        assert c.stream_is_dead("s1") is False

    def test_an_unknown_stream_is_not_dead(self) -> None:
        assert _client().stream_is_dead("never-sent") is False
        assert _client().stream_is_dead("") is False

    @pytest.mark.asyncio
    async def test_the_tracking_table_is_bounded(self) -> None:
        c = _client()
        ws = RecordingWS(c)
        c._ws = ws  # type: ignore[assignment]
        for i in range(400):
            await c.send_stream(f"r{i}", f"s{i}", "x", finish=False)
        assert len(c._stream_of_req) <= 256


# ---------------------------------------------------------------------------
# The renderer rolls off a sealed bubble
# ---------------------------------------------------------------------------


def _renderer(client: WeComClient) -> WeComRenderer:
    return WeComRenderer(client, "r1", "https://fallback", WECOM_CAPABILITIES)


class TestRendererRollsOffASealedBubble:
    @pytest.mark.asyncio
    async def test_a_sealed_bubble_is_replaced_by_a_fresh_one(self) -> None:
        c = _client()
        ws = RecordingWS(c)
        c._ws = ws  # type: ignore[assignment]
        r = _renderer(c)
        await r.on_turn_start()
        await r.on_text_chunk("part one ")
        first = r._stream_id

        c._mark_stream_dead(first)
        await r.on_tool_call("t1", "read")  # force=True, so it pushes

        assert r._stream_id != first, "a sealed bubble must not keep receiving frames"

    @pytest.mark.asyncio
    async def test_the_continuation_does_not_repeat_delivered_text(self) -> None:
        c = _client()
        ws = RecordingWS(c)
        c._ws = ws  # type: ignore[assignment]
        r = _renderer(c)
        await r.on_turn_start()
        # Two accepted frames, so the older length is known-delivered.
        await r.on_text_chunk("AAA")
        await r._push(force=True)
        await r.on_text_chunk("BBB")
        await r._push(force=True)

        c._mark_stream_dead(r._stream_id)
        await r.on_text_chunk("CCC")
        await r._push(force=True)

        tail = ws.contents()[-1]
        assert "CCC" in tail
        assert not tail.startswith("AAA"), (
            "the continuation restarted from the beginning; the reader sees the "
            "whole answer twice"
        )

    @pytest.mark.asyncio
    async def test_a_SECOND_refusal_does_not_skip_the_span_the_first_lost(self) -> None:
        """Consecutive refusals must not open a hole in the answer.

        846605 means the inbound req_id is unroutable, so it does not refuse one
        bubble — it refuses every replacement too. The roll offsets belong to the
        bubble being abandoned, and carrying them forward made the SECOND roll
        resume from a position recorded against the FIRST bubble, past a span the
        replacement never managed to deliver. Nothing reports it: `send_stream`
        returns True either way and the reader simply never sees that text.
        """
        c = _client()
        ws = RecordingWS(c)
        c._ws = ws  # type: ignore[assignment]
        r = _renderer(c)
        await r.on_turn_start()
        await r.on_text_chunk("AAA")
        await r._push(force=True)
        await r.on_text_chunk("BBB")
        await r._push(force=True)

        # Bubble one is sealed: BBB is the frame that may never have landed, so the
        # replacement must replay from it.
        c._mark_stream_dead(r._stream_id)
        await r.on_text_chunk("CCC")
        await r._push(force=True)
        second = r._stream_id

        # The replacement is refused as well, before anything it sent could land.
        c._mark_stream_dead(second)
        await r.on_text_chunk("DDD")
        await r._push(force=True)

        third = ws.contents()[-1]
        assert r._stream_id not in (second,), "the sealed replacement kept receiving frames"
        assert "BBB" in third, (
            "the third bubble skipped BBB -- it was only ever written to two bubbles "
            "the platform refused, so the reader has a silent hole in the answer"
        )
        assert "CCC" in third and "DDD" in third

    @pytest.mark.asyncio
    async def test_the_final_frame_lands_in_a_live_bubble(self) -> None:
        c = _client()
        ws = RecordingWS(c)
        c._ws = ws  # type: ignore[assignment]
        r = _renderer(c)
        await r.on_turn_start()
        await r.on_text_chunk("hello")
        await r._push(force=True)
        sealed = r._stream_id
        c._mark_stream_dead(sealed)
        await r.on_text_chunk(" world")

        await r.on_done()

        final = [f for f in ws.sent if f["body"]["stream"]["finish"] is True]
        assert final, "the turn never sealed"
        assert final[-1]["body"]["stream"]["id"] != sealed

    @pytest.mark.asyncio
    async def test_no_empty_bubble_is_posted_when_nothing_remains(self) -> None:
        # If the earlier bubble already carries the whole answer there is nothing
        # left to say, and a fresh bubble would post a blank message. The seal is
        # cosmetic, so an unsealed bubble holding the full answer is the better
        # outcome. What matters is that no empty FRAME goes out.
        c = _client()
        ws = RecordingWS(c)
        c._ws = ws  # type: ignore[assignment]
        r = _renderer(c)
        await r.on_turn_start()
        await r.on_text_chunk("all of it")
        await r._push(force=True)
        await r._push(force=True)  # second accepted frame -> prev_sent_abs is current
        c._mark_stream_dead(r._stream_id)
        before = len(ws.sent)

        await r.on_done()

        posted = [f["body"]["stream"]["content"] for f in ws.sent[before:]]
        assert all(p.strip() for p in posted), f"an empty bubble was posted: {posted!r}"

    @pytest.mark.asyncio
    async def test_the_final_frame_leaves_an_aged_bubble_even_before_a_refusal(
        self, monkeypatch
    ) -> None:
        # The platform expires a bubble ~10 minutes in and only REFUSES a later
        # write. A turn that spent that long in a tool call therefore has an
        # expired-but-not-yet-refused bubble, and writing the final answer there
        # loses it. Rotation must key on age, not only on a refusal.
        monkeypatch.setattr("kiro_crew.wecom.renderer._STREAM_MAX_AGE_S", 0.0)
        c = _client()
        ws = RecordingWS(c)
        c._ws = ws  # type: ignore[assignment]
        r = _renderer(c)
        await r.on_turn_start()
        first = r._stream_id
        await r.on_text_chunk("the answer")

        await r.on_done()

        final = [f for f in ws.sent if f["body"]["stream"]["finish"] is True]
        assert final, "the turn never sealed"
        assert (
            final[-1]["body"]["stream"]["id"] != first
        ), "the final answer went into a bubble the platform had already expired"

    @pytest.mark.asyncio
    async def test_a_capped_frame_does_not_record_the_uncapped_length(self) -> None:
        # Recording the full accumulated length while sending a CAPPED frame let a
        # later rotation resume past text that was never delivered.
        from kiro_crew.messaging.transport import TransportCapabilities

        c = _client()
        ws = RecordingWS(c)
        c._ws = ws  # type: ignore[assignment]
        r = WeComRenderer(c, "r1", "https://fallback", TransportCapabilities(max_message_chars=10))
        await r.on_turn_start()
        await r.on_text_chunk("x" * 50)
        await r._push(force=True)

        assert r._sent_abs <= 10, "only the characters actually sent count as sent"
        assert len(ws.contents()[-1]) <= 10

    @pytest.mark.asyncio
    async def test_the_answers_tail_is_delivered_as_CONFIRMED_pushes(self, monkeypatch) -> None:
        # A stream frame's acceptance cannot be confirmed: every frame of a turn
        # replays the one inbound req_id, so a waiter for one can be resolved by
        # another's ACK. A proactive push mints its OWN req_id, so its verdict is
        # exact -- which is what makes an over-cap answer's tail either delivered or
        # reported, never silently lost.
        slept: list[float] = []

        async def fake_sleep(secs: float) -> None:
            slept.append(secs)

        monkeypatch.setattr("kiro_crew.wecom.renderer.asyncio.sleep", fake_sleep)
        c = _client()
        ws = RecordingWS(c)
        c._ws = ws  # type: ignore[assignment]
        r = WeComRenderer(c, "r1", "https://fallback", WECOM_CAPABILITIES, chat_id="Wei")
        await r.on_turn_start()
        await r.on_text_chunk("word " * 4000)

        await r.on_done()

        sealing = [f for f in ws.sent if f.get("body", {}).get("stream", {}).get("finish")]
        pushes = [f for f in ws.sent if f.get("cmd") == "aibot_send_msg"]
        assert len(sealing) == 1, "the head seals the bubble the user is watching"
        assert pushes, "the tail must go out as confirmed pushes"
        assert len(slept) == len(pushes), "every pushed chunk is paced"

    @pytest.mark.asyncio
    async def test_a_refused_tail_chunk_stops_and_is_reported(self, monkeypatch) -> None:
        # Nothing can recover a refusal here; the alternative is pretending the
        # tail landed.
        async def fake_sleep(secs: float) -> None:
            return None

        monkeypatch.setattr("kiro_crew.wecom.renderer.asyncio.sleep", fake_sleep)
        c = _client()
        ws = RecordingWS(c, ack_errcode=45009)  # the platform refuses every push
        c._ws = ws  # type: ignore[assignment]
        r = WeComRenderer(c, "r1", "https://fallback", WECOM_CAPABILITIES, chat_id="Wei")
        await r.on_turn_start()
        await r.on_text_chunk("word " * 4000)

        with capture("kiro_crew.wecom.renderer") as messages:
            await r.on_done()

        assert any("refused by the platform" in m for m in messages)
        pushes = [f for f in ws.sent if f.get("cmd") == "aibot_send_msg"]
        assert len(pushes) == 1, "a refusal stops the run rather than hammering the quota"

    @pytest.mark.asyncio
    async def test_a_tail_with_no_conversation_id_is_reported_not_dropped_silently(
        self, monkeypatch
    ) -> None:
        async def fake_sleep(secs: float) -> None:
            return None

        monkeypatch.setattr("kiro_crew.wecom.renderer.asyncio.sleep", fake_sleep)
        c = _client()
        c._ws = RecordingWS(c)  # type: ignore[assignment]
        r = WeComRenderer(c, "r1", "https://fallback", WECOM_CAPABILITIES)  # no chat_id
        await r.on_turn_start()
        await r.on_text_chunk("word " * 4000)

        with capture("kiro_crew.wecom.renderer") as messages:
            await r.on_done()

        assert any("no conversation id" in m for m in messages)

    @pytest.mark.asyncio
    async def test_an_OBSERVED_seal_refusal_is_re_delivered_as_a_push(self) -> None:
        # A stream frame's ACK cannot be waited on, but a terminal one that has
        # already landed IS observable -- and that case is now recovered instead of
        # assumed successful.
        c = _client()
        ws = RecordingWS(c, ack_errcode=846608)  # the seal is refused as expired
        c._ws = ws  # type: ignore[assignment]
        r = WeComRenderer(c, "r1", "https://fallback", WECOM_CAPABILITIES, chat_id="Wei")
        await r.on_turn_start()
        await r.on_text_chunk("the whole answer")

        await r.on_done()

        pushes = [f for f in ws.sent if f.get("cmd") == "aibot_send_msg"]
        assert pushes, "an observed refusal must be re-delivered, not assumed delivered"
        assert "the whole answer" in pushes[-1]["body"]["markdown"]["content"]

    @pytest.mark.asyncio
    async def test_a_live_bubble_is_never_rolled(self) -> None:
        c = _client()
        ws = RecordingWS(c)
        c._ws = ws  # type: ignore[assignment]
        r = _renderer(c)
        await r.on_turn_start()
        first = r._stream_id
        await r.on_text_chunk("hi")
        await r._push(force=True)
        await r.on_done()
        assert r._stream_id == first
        assert set(ws.stream_ids()) == {first}


# ---------------------------------------------------------------------------
# The anti-kick event
# ---------------------------------------------------------------------------


class TestAntiKick:
    @pytest.mark.asyncio
    async def test_the_kick_arrives_wrapped_in_an_event_callback(self) -> None:
        # Driven from the RECORDED vendor shape, not a hand-written echo of the
        # code: matching only a top-level cmd meant this never fired, so a
        # replaced connection kept reconnecting and the two instances took turns
        # evicting each other. If the recorded shape is wrong, this test is the
        # thing that has to change -- which is the point of recording it.
        c = _client()
        fx = load_fixture("wecom", "event_callback_disconnected", root=CHANNEL_FIXTURES)
        await c._handle_message(json.dumps(fx.payload))
        assert c._kicked is True

    @pytest.mark.asyncio
    async def test_the_legacy_top_level_spelling_still_kicks(self) -> None:
        c = _client()
        await c._handle_message(json.dumps({"cmd": "disconnected_event", "body": {}}))
        assert c._kicked is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "eventtype", ["enter_chat", "template_card_event", "feedback_event", "who_knows"]
    )
    async def test_another_event_does_not_stop_the_channel(self, eventtype: str) -> None:
        c = _client()
        raw = json.dumps(
            {
                "cmd": "aibot_event_callback",
                "body": {"msgtype": "event", "event": {"eventtype": eventtype}},
            }
        )
        await c._handle_message(raw)
        assert c._kicked is False

    @pytest.mark.asyncio
    async def test_a_malformed_event_envelope_is_survivable(self) -> None:
        c = _client()
        for body in (
            '{"cmd":"aibot_event_callback","body":[]}',
            '{"cmd":"aibot_event_callback","body":{"event":"nope"}}',
            '{"cmd":"aibot_event_callback"}',
        ):
            await c._handle_message(body)
        assert c._kicked is False


# ---------------------------------------------------------------------------
# An unexpected fault reconnects instead of killing the channel
# ---------------------------------------------------------------------------


class TestUnexpectedFaultReconnects:
    @pytest.mark.asyncio
    async def test_an_unexpected_error_backs_off_and_reports(self, monkeypatch) -> None:
        c = _client()
        events: list[tuple[bool, str]] = []
        c.on_status = lambda healthy, reason: events.append((healthy, reason))
        c._notify_status(True, "")
        events.clear()

        calls = {"n": 0}

        async def boom() -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise ValueError("something nobody predicted")
            c._closed = True

        monkeypatch.setattr(c, "_connect_and_serve", boom)
        monkeypatch.setattr("kiro_crew.wecom.client.asyncio.sleep", _immediate_sleep)

        await c._run_loop()

        assert calls["n"] >= 2, (
            "an unexpected error escaped the loop -- the task dies while _closed "
            "is False, so the channel is silently dead with a stale badge"
        )
        assert events and events[0][0] is False
        assert "ValueError" in events[0][1]

    @pytest.mark.asyncio
    async def test_cancellation_is_not_treated_as_a_fault(self, monkeypatch) -> None:
        c = _client()

        async def cancelled() -> None:
            raise asyncio.CancelledError

        monkeypatch.setattr(c, "_connect_and_serve", cancelled)
        await c._run_loop()  # returns rather than retrying


async def _immediate_sleep(_delay: float) -> None:
    """Skip the backoff wait without yielding control to a real timer."""
    return None


class TestProactivePushIsAckConfirmed:
    """A push the platform refused must not be reported as delivered.

    Unlike a stream frame -- recoverable by rolling to a new bubble -- there is no
    later frame to notice a refusal on, and the dashboard mirror records delivery
    from the return value. So the ACK is awaited.
    """

    @pytest.mark.asyncio
    async def test_an_accepted_push_reports_success(self) -> None:
        c = _client()
        ws = RecordingWS(c)  # acks with errcode 0
        c._ws = ws  # type: ignore[assignment]

        assert await c.send_proactive("Wei", "hello") is True

        assert ws.sent[0]["cmd"] == "aibot_send_msg"
        assert ws.sent[0]["body"]["chat_type"] == 1, "an absent chat_type prefers GROUP"

    @pytest.mark.asyncio
    async def test_a_refused_push_reports_failure(self) -> None:
        c = _client()
        c._ws = RecordingWS(c, ack_errcode=45009)  # type: ignore[assignment]
        assert await c.send_proactive("Wei", "hello") is False

    @pytest.mark.asyncio
    async def test_a_push_refusal_is_not_attributed_to_a_stream_bubble(self) -> None:
        # A push carries its own req_id, so a terminal code on it must not seal an
        # unrelated answer bubble.
        c = _client()
        # Only the PUSH is refused; the stream frame is accepted.
        ws = RecordingWS(c, ack_errcode_by_cmd={"aibot_send_msg": 846608})
        c._ws = ws  # type: ignore[assignment]
        await c.send_stream("r1", "s1", "hi", finish=False)

        assert await c.send_proactive("Wei", "hello") is False
        assert c.stream_is_dead("s1") is False

    @pytest.mark.asyncio
    async def test_an_unacknowledged_push_reports_failure(self, monkeypatch) -> None:
        # Unacknowledged is NOT delivered; reporting success would let the mirror
        # record a message the platform dropped.
        monkeypatch.setattr("kiro_crew.wecom.client._PUSH_ACK_TIMEOUT_SECS", 0.05)
        c = _client()
        c._ws = RecordingWS(c, ack=False)  # type: ignore[assignment]
        assert await c.send_proactive("Wei", "hello") is False

    @pytest.mark.asyncio
    async def test_no_live_socket_reports_failure(self) -> None:
        c = _client()
        c._ws = None
        assert await c.send_proactive("Wei", "hello") is False

    @pytest.mark.asyncio
    async def test_close_fails_a_push_still_waiting(self) -> None:
        # Otherwise the caller hangs until its timeout for no reason.
        c = _client()
        c._ws = RecordingWS(c, ack=False)  # type: ignore[assignment]
        pushing = asyncio.create_task(c.send_proactive("Wei", "hello"))
        await asyncio.sleep(0)
        await c.close()
        assert await pushing is False


class TestQuotaArithmetic:
    """The pace has to leave room for the frames that ignore it.

    Pacing to the FULL 30/minute and then adding the unthrottled frames — the
    placeholder, the first tool footer of each bubble, the final seal, an overflow
    push — lands above the quota, and a refusal on the FINAL frame leaves a partial
    answer delivered. So the throttle is derived from what is left, and this pins
    the arithmetic rather than the tuned number.
    """

    def test_a_full_minute_of_frames_fits_inside_the_quota(self) -> None:
        import math

        from kiro_crew.wecom.renderer import (
            _STREAM_THROTTLE_S,
            _UNTHROTTLED_FRAME_BUDGET,
            WECOM_QUOTA_PER_MIN,
        )

        throttled_per_min = math.floor(60.0 / _STREAM_THROTTLE_S)
        assert throttled_per_min + _UNTHROTTLED_FRAME_BUDGET <= WECOM_QUOTA_PER_MIN, (
            f"{throttled_per_min} paced frames + {_UNTHROTTLED_FRAME_BUDGET} unthrottled "
            f"exceeds WeCom's {WECOM_QUOTA_PER_MIN}/minute ceiling"
        )

    def test_the_reserve_covers_every_frame_that_skips_the_throttle(self) -> None:
        # placeholder + first tool footer + final seal = 3 is the floor; the rest is
        # headroom for an overflow push and a second bubble's first footer.
        from kiro_crew.wecom.renderer import _UNTHROTTLED_FRAME_BUDGET

        assert _UNTHROTTLED_FRAME_BUDGET >= 3

    def test_the_pace_still_reads_as_live_typing(self) -> None:
        # A guard on the other side: a throttle so slow the stream stops feeling
        # live would "fit the quota" trivially.
        from kiro_crew.wecom.renderer import _STREAM_THROTTLE_S

        assert _STREAM_THROTTLE_S <= 3.0


def test_new_stream_id_is_unique() -> None:
    assert new_stream_id() != new_stream_id()
