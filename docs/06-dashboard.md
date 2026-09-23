# 6. Dashboard

A live inspector at `/dashboard` showing every exchange between this server and
Vobiz, in both directions, as it happens.

## Layout

```
┌────────────────────────────────────────────────────────────────────────┐
│  Vobiz × Pipecat — Webhook Inspector   ● live   APP = this server      │
├──────────────┬──────────────────────────┬──────────────────────────────┤
│  Place call  │  HTTP webhooks           │  Stream events               │
│  Transfer    │                          │                              │
│   · leg      │  APP → VOBIZ  REST       │  APP ← VOBIZ  stream start   │
│   · type     │  APP ← VOBIZ  webhook    │  APP ← VOBIZ  media rollup   │
│   · headers  │  APP → VOBIZ  XML        │  APP → VOBIZ  playAudio      │
│  Refresh     │       (scrolls)          │       (scrolls)              │
└──────────────┴──────────────────────────┴──────────────────────────────┘
      fixed              independent scroll        independent scroll
```

The page itself never scrolls. Header and sidebar stay put; each feed scrolls on
its own, and the pane headings stay visible while you scroll back.

## Two panes, deliberately separate

| Pane | Shows | Rhythm |
|---|---|---|
| **HTTP webhooks** | REST out, XML out, webhooks in | Sparse — the *order* is the story |
| **Stream events** | The WebSocket media plane | Continuous — the *shape* is the story |

They are separated because they answer different questions and move at wildly
different speeds. Interleaving them would bury the ten webhooks of a call under
thousands of audio frames.

## Reading a row

```
▶  APP → VOBIZ   20:25:50.974   POST /Call/{uuid}/ (transfer)   redirect A-leg → +91… (pstn)
   └ caret       └ direction    └ timestamp  └ label            └ note
```

- **`APP` is always left, `VOBIZ` always right.** Only the arrow flips, so
  direction is one glyph to track rather than a label that moves.
- **Click any row** to expand: highlighted fields as chips, then the full raw
  payload or the exact XML document.

### Colours

| Colour | Means |
|---|---|
| Blue | `APP → VOBIZ` · REST call |
| Amber | `APP → VOBIZ` · XML reply |
| Green | `APP ← VOBIZ` · webhook |
| Purple | WebSocket media plane |

## Audio rollups

`media` and `playAudio` arrive **~50× per second in each direction**. Listing them
individually buries everything else within seconds.

So control frames are listed individually, and audio is **counted and rolled up
every 2 seconds**:

```
APP ← VOBIZ   media (audio in)     101 frames · 44,642 bytes · 2.0s
APP → VOBIZ   playAudio (out)       59 frames · 30,480 bytes · 2.0s
```

You see audio flowing without it drowning the checkpoints. A control event
force-flushes the pending rollup so ordering stays truthful.

## Controls

**Place a call** — a number, and Call. Uses `POST /start`.

**Transfer live call** —

| Control | |
|---|---|
| Which leg | `A-leg` / `B-leg` |
| Destination type | `PSTN number` / `SIP endpoint` |
| Destination | Number, or SIP URI |
| SIP headers | Appears only for SIP. Warnings shown rather than silently dropped |
| Call UUID | Auto-populates from events; selecting one stops new calls stealing it |

**Refresh / Clear feed** — Refresh reconnects the SSE stream, which replays
history, so there is no second code path to drift. Clear empties the screen but
**keeps the log** unless `wipe_log` is passed.

## How it works

```
server.py ──► events.record() ──► in-memory deque (1000)
                   │                     │
                   │                     └──► events.jsonl  (append-only)
                   │
                   └──► asyncio.Queue per subscriber
                              │
                              └──► GET /events  (SSE)  ──► browser
```

`GET /events` replays stored history, then streams live with a 15-second
keepalive. `record()` swallows its own errors — a dashboard problem must never
break a call.

## Storage

Events append to `events.jsonl` and are replayed at startup, so a restart does
not lose the record. `GET /events/info` reports stored and in-memory counts.

> **On Render the filesystem is ephemeral.** The log resets on every deploy and
> every spin-down. Persistence across restarts works locally; in the cloud it
> needs a Render disk or an external store.

## What the dashboard cannot see

The stream pane only observes traffic through **this** process. If you point
`VOBIZ_PROD_WS_URL` at another host, audio bypasses `ws_tap.py` entirely and the
stream pane stays empty. The HTTP pane keeps working, since `/answer` and the
webhooks still land here.
