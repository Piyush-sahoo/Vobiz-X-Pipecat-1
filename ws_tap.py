"""ws_tap.py — observe the Vobiz media WebSocket without changing its behaviour.

The transport and the serializer own this socket; this wrapper only watches.
Every method delegates, and a failure to record must never surface to the
caller, or the tap could drop a call.

The Vobiz stream protocol, as implemented by pipecat.serializers.vobiz:

    Vobiz -> server     start, media, dtmf, playedStream, clearedAudio
    server -> Vobiz     playAudio, clearAudio, stop

`media` and `playAudio` carry the audio itself and arrive ~50x a second in each
direction. Listing them individually would bury every other event, so they are
counted and rolled up on an interval; the checkpoint and control events
(start / dtmf / playedStream / clearAudio / clearedAudio / stop) are emitted as
they happen.
"""

import json
import time

import events

ROLLUP_SECONDS = 2.0

# Events worth seeing individually. Everything else is audio and gets counted.
CONTROL_EVENTS = {"start", "dtmf", "playedStream", "clearedAudio", "clearAudio", "stop", "mark"}


class WebSocketTap:
    """Transparent proxy around a FastAPI WebSocket that records stream events."""

    def __init__(self, websocket, call_uuid: str = None):
        self._ws = websocket
        self._call_uuid = call_uuid
        self._stream_id = None
        # direction -> [frame count, byte count, window start]
        self._acc = {"in": [0, 0, time.monotonic()], "out": [0, 0, time.monotonic()]}

    def __getattr__(self, name):
        # Anything not intercepted (close, client_state, query_params, accept…)
        # goes straight through to the real socket.
        return getattr(self._ws, name)

    # ---------- recording ----------

    def _rollup(self, direction: str, force: bool = False):
        acc = self._acc[direction]
        frames, nbytes, started = acc
        if frames == 0:
            return
        elapsed = time.monotonic() - started
        if not force and elapsed < ROLLUP_SECONDS:
            return
        label = "media (audio in)" if direction == "in" else "playAudio (audio out)"
        events.record(
            direction, label,
            {"frames": frames, "bytes": nbytes, "seconds": round(elapsed, 1),
             "streamId": self._stream_id},
            call_uuid=self._call_uuid, kind="stream",
            note=f"{frames} frames · {nbytes:,} bytes · {elapsed:.1f}s",
        )
        self._acc[direction] = [0, 0, time.monotonic()]

    def _observe(self, direction: str, raw):
        try:
            if not isinstance(raw, str):
                # Binary frames carry audio only; count without parsing.
                self._acc[direction][0] += 1
                self._acc[direction][1] += len(raw or b"")
                self._rollup(direction)
                return
            msg = json.loads(raw)
            event = msg.get("event")

            if event == "start":
                start = msg.get("start") or {}
                self._stream_id = start.get("streamId")
                self._call_uuid = self._call_uuid or start.get("callId")
                fmt = start.get("mediaFormat") or {}
                events.record(
                    direction, "stream start", msg, call_uuid=self._call_uuid, kind="stream",
                    note=f"{fmt.get('encoding')} @ {fmt.get('sampleRate')} Hz",
                )
                return

            if event in CONTROL_EVENTS:
                # Flush any pending audio so the control event lands in order.
                self._rollup(direction, force=True)
                note = None
                if event == "dtmf":
                    note = f"digit {(msg.get('dtmf') or {}).get('digit')}"
                elif event == "playedStream":
                    note = f"checkpoint {msg.get('name')}"
                elif event == "clearAudio":
                    note = "barge-in — discard queued audio"
                elif event == "clearedAudio":
                    note = "Vobiz confirmed audio cleared"
                elif event == "stop":
                    note = "bot ended the stream"
                events.record(direction, f"{event}", msg, call_uuid=self._call_uuid,
                              kind="stream", note=note)
                return

            # media / playAudio: count, do not list.
            self._acc[direction][0] += 1
            self._acc[direction][1] += len(raw)
            self._rollup(direction)
        except Exception as e:
            print(f"[WS TAP] ignored: {e}")

    def flush(self):
        for d in ("in", "out"):
            try:
                self._rollup(d, force=True)
            except Exception:
                pass

    # ---------- intercepted socket methods ----------

    async def receive(self):
        message = await self._ws.receive()
        if isinstance(message, dict):
            self._observe("in", message.get("text") or message.get("bytes"))
        return message

    async def receive_text(self):
        text = await self._ws.receive_text()
        self._observe("in", text)
        return text

    async def receive_bytes(self):
        data = await self._ws.receive_bytes()
        self._observe("in", data)
        return data

    async def send_text(self, data):
        self._observe("out", data)
        return await self._ws.send_text(data)

    async def send_bytes(self, data):
        self._observe("out", data)
        return await self._ws.send_bytes(data)
