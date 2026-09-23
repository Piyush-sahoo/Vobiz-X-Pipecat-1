"""events.py — in-memory webhook event bus for the demo dashboard.

Every exchange with Vobiz is recorded here in one of two directions:

    OUT  this server -> Vobiz   (REST calls we make, XML documents we return)
    IN   Vobiz -> this server   (answer_url, hangup_url, Dial and Record callbacks)

The bus is deliberately in-process and bounded: it is a demo surface, not an
audit log. Recording an event must never be able to break a call, so every
public function here swallows its own errors.
"""

import asyncio
import json
import os
from collections import deque
from datetime import datetime

MAX_EVENTS = 1000                       # kept in memory and replayed to the UI
EVENT_LOG = os.getenv("EVENT_LOG", "events.jsonl")   # append-only, survives restarts

_events: deque = deque(maxlen=MAX_EVENTS)
_subscribers: list[asyncio.Queue] = []
_seq = 0

# Vobiz sends the whole call envelope on every webhook. These are the fields
# worth putting in front of a demo audience; everything else stays in `raw`.
HIGHLIGHT_FIELDS = [
    "Event", "CallUUID", "CallStatus", "Direction", "From", "To",
    "DialAction", "DialBLegUUID", "DialBLegTo", "DialBLegStatus",
    "DialBLegDuration", "DialBLegHangupCauseName",
    "HangupCause", "HangupCauseName", "HangupCauseCode", "Duration",
    "RecordUrl", "RecordingID", "RecordingDuration",
]


def _highlights(payload: dict) -> dict:
    return {k: payload[k] for k in HIGHLIGHT_FIELDS if payload.get(k) not in (None, "")}


def _append_to_log(event: dict) -> None:
    """Append one event as a JSON line. Best effort: a disk problem must never
    break a call, so this swallows its own errors."""
    try:
        with open(EVENT_LOG, "a") as f:
            f.write(json.dumps(event) + "\n")
    except Exception as e:
        print(f"[EVENTS] could not write {EVENT_LOG}: {e}")


def load_from_log() -> int:
    """Repopulate memory from the log at startup, so a restart — or the OS
    reclaiming the process — does not lose the events already captured."""
    global _seq
    if not os.path.exists(EVENT_LOG):
        return 0
    loaded = 0
    try:
        with open(EVENT_LOG) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue      # a torn final line from a killed process
                _events.append(event)
                _seq = max(_seq, event.get("seq", 0))
                loaded += 1
    except Exception as e:
        print(f"[EVENTS] could not read {EVENT_LOG}: {e}")
    return loaded


def log_size() -> int:
    """Total events on disk, which outlives the in-memory window."""
    try:
        with open(EVENT_LOG) as f:
            return sum(1 for line in f if line.strip())
    except Exception:
        return 0


def record(direction: str, label: str, payload=None, call_uuid: str = None,
           kind: str = "webhook", note: str = None) -> dict:
    """Append an event and fan it out to live dashboard subscribers.

    direction: "in" (Vobiz -> us) or "out" (us -> Vobiz)
    kind:      "webhook" | "rest" | "xml"
    """
    global _seq
    try:
        payload = payload if isinstance(payload, dict) else ({} if payload is None else {"body": str(payload)})
        _seq += 1
        event = {
            "seq": _seq,
            "ts": datetime.now().strftime("%H:%M:%S.%f")[:-3],
            "direction": direction,
            "kind": kind,
            "label": label,
            "call_uuid": call_uuid or payload.get("CallUUID") or payload.get("call_uuid"),
            "note": note,
            "highlights": _highlights(payload),
            "raw": payload,
        }
        _events.append(event)
        _append_to_log(event)
        for q in list(_subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass  # a slow dashboard tab must not stall a call
        return event
    except Exception as e:  # never propagate into call handling
        print(f"[EVENTS] failed to record {label}: {e}")
        return {}


def record_xml(label: str, xml: str, call_uuid: str = None) -> None:
    """Record an XML document we are returning to Vobiz."""
    record("out", label, {"xml": xml}, call_uuid=call_uuid, kind="xml")


def history() -> list:
    return list(_events)


def clear(wipe_log: bool = False) -> None:
    """Clear the live feed. The log is kept unless explicitly wiped, so clearing
    the screen mid-demo does not destroy the record of what happened."""
    _events.clear()
    if wipe_log:
        try:
            open(EVENT_LOG, "w").close()
        except Exception as e:
            print(f"[EVENTS] could not truncate {EVENT_LOG}: {e}")


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=100)
    _subscribers.append(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    if q in _subscribers:
        _subscribers.remove(q)


async def sse_stream(request):
    """Server-sent events: replay history, then stream live."""
    q = subscribe()
    try:
        for event in history():
            yield f"data: {json.dumps(event)}\n\n"
        while True:
            if await request.is_disconnected():
                break
            try:
                event = await asyncio.wait_for(q.get(), timeout=15.0)
                yield f"data: {json.dumps(event)}\n\n"
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
    finally:
        unsubscribe(q)
