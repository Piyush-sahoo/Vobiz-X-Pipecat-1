# 1. Architecture

## The two planes

Everything in this system belongs to one of two planes. Almost every design
decision follows from keeping them apart.

```
                        ┌─────────────────────────────┐
                        │           VOBIZ             │
                        │  (carrier + media platform) │
                        └──────┬───────────────┬──────┘
                               │               │
              CONTROL PLANE    │               │    MEDIA PLANE
              HTTP + XML       │               │    WebSocket + audio
              sparse, ordered  │               │    ~50 frames/sec each way
                               │               │
        ┌──────────────────────▼───────┐   ┌───▼──────────────────────┐
        │  server.py                   │   │  server.py  (ws routes)  │
        │                              │   │      ↓                   │
        │  POST /Call/     → place     │   │  ws_tap.py  (observe)    │
        │  POST /answer    ← webhook   │   │      ↓                   │
        │       returns XML            │   │  bot.py     (pipeline)   │
        │  POST /Call/{id}/ → transfer │   │      ↓                   │
        │  /hangup /dial-events ←      │   │  pipecat-vobiz           │
        │  /recording-ready     ←      │   │  (μ-law ⇄ PCM)           │
        └──────────────────────────────┘   │      ↓                   │
                                           │  Gemini Live             │
                                           └──────────────────────────┘
```

**Control plane** — request/response over HTTPS. Your server tells Vobiz to place
or redirect a call; Vobiz asks your server what to do and reports what happened.
Low volume, and the *order* of events is the meaning.

**Media plane** — one long-lived WebSocket per call carrying base64 audio frames.
High volume, and no individual frame matters; the aggregate does.

## Why the split matters

Because the `<Stream>` URL is just a string in a document your server generates,
the media plane can point anywhere:

```xml
<Stream ...>wss://some-other-host/ws</Stream>
```

Set `ENV=production` and `VOBIZ_PROD_WS_URL`, and audio goes to a different
machine while this server still handles `/answer`, transfers and webhooks.
See [`get_websocket_url()`](03-files-and-functions.md#get_websocket_url) and
[Gotchas](08-gotchas.md#pointing-the-stream-at-another-server).

The cost: this server's dashboard only sees traffic through its own process, so
moving the stream empties the stream pane.

## Request flow in one picture

```
   YOU                    THIS SERVER                 VOBIZ              PHONE
    │                          │                        │                  │
    ├── POST /start ──────────►│                        │                  │
    │                          ├── POST /Call/ ────────►│                  │
    │                          │◄─ 201 request_uuid ────┤                  │
    │                          │                        ├─── rings ───────►│
    │                          │                        │                  │
    │                          │◄─ POST /answer ────────┤◄── answered ─────┤
    │                          ├─ <Stream> + <Record> ─►│                  │
    │                          │                        │                  │
    │                          │◄══ WebSocket open ═════┤                  │
    │                          │◄══════ audio ═════════►│◄════ audio ═════►│
    │                          │                        │                  │
    ├── POST /initiate-transfer│                        │                  │
    │                          ├── POST /Call/{id}/ ───►│                  │
    │                          │◄─ POST /transfer-… ────┤                  │
    │                          ├───── <Dial> XML ──────►│                  │
    │                          │◄─ /dial-events ────────┤─── rings human ─►│
    │                          │◄─ /hangup ─────────────┤                  │
    │                          │◄─ /recording-ready ────┤                  │
```

## Process model, and why it is single-instance

`server.py` and `bot.py` run in **one process**. The Pipecat pipeline is started
from inside the WebSocket handler, not as a separate service.

State lives in `active_calls`, a plain dict at `server.py:35`, shared by
`/start`, `/answer`, the WebSocket handler and `/initiate-transfer`.

That dict is the reason the service must run as a single instance. With two
instances behind a load balancer, `/answer` could be served by one while the
WebSocket lands on the other, and the transfer state written by `/initiate-transfer`
would be invisible to the instance that Vobiz later asks for XML. Cloud Run and
Render both offer session affinity, but it is cookie-based and Vobiz's WebSocket
client sends no cookie.

**To scale past one instance,** `active_calls` has to move to Redis or Firestore.
Nothing else in the design blocks it.

## Where each concern lives

| Concern | File |
|---|---|
| Placing and transferring calls (REST → Vobiz) | `server.py` |
| Generating Vobiz XML | `server.py` only — `build_transfer_xml()` and `/answer` |
| Receiving webhooks | `server.py` |
| Hosting the WebSocket | `server.py` — four routes, one handler |
| Observing stream traffic | `ws_tap.py` |
| Running the AI pipeline | `bot.py` |
| Audio wire format | `pipecat-vobiz` (installed package, not this repo) |
| Event capture and persistence | `events.py` |
| The inspector UI | `dashboard.py` |
