# 3. Files and functions

Every file in the repo, and every function that matters.

## Map

```
Vobiz-X-Pipecat/
├── server.py          1140   HTTP control plane + WebSocket hosting
├── bot.py              249   Pipecat pipeline (the AI)
├── dashboard.py        400   Inspector UI (one HTML string)
├── events.py           166   Event bus + persistence
├── ws_tap.py           143   WebSocket observer
├── download_recording.py 82  Standalone recording fetcher
│
├── tests/test_transfer.py 80 Transfer XML + SIP header rules
├── docs/                     This documentation
│
├── render.yaml               Render blueprint (live deploy target)
├── Dockerfile                Container build (Cloud Run)
├── deploy.sh                 Cloud Run deploy (optional path)
├── .github/workflows/ci.yml  CI gate
├── requirements.txt          Pinned dependencies
└── env.example               Annotated config template
```

---

## `server.py` — the control plane

Holds `active_calls = {}` (`:35`), the in-process dict tracking every live call.
Keys are the A-leg UUID; values carry status, transfer intent and destination.
This dict is why the service must run single-instance.

### Helpers

| Function | Line | Does |
|---|---|---|
| `_webhook_payload(request)` | 41 | Reads a Vobiz webhook body whatever the encoding — form, JSON, query params. Never raises; a malformed webhook must not break call handling. |
| `validate_sip_headers(raw)` | 64 | Checks `Key=value` pairs. Keys must start `X-VH-`; stem and value alphanumeric. Returns `(cleaned, warnings)` — **warns, never blocks**. |
| `build_transfer_xml(...)` | 104 | Builds the `<Dial>` document. `pstn` → `<Number>`, `sip` → `<User>`. Attaches `sipHeaders` to both `<Dial>` and `<User>`. |
| `make_vobiz_call(...)` | 145 | POSTs to the Vobiz Call API. Registers `answer_url` and `hangup_url`. |
| `get_host_and_protocol(request)` | 228 | Resolves the public host — `PUBLIC_URL` if set, else the `Host` header. Warns loudly on localhost. |
| `get_websocket_url(host)` | 286 | Builds the `<Stream>` URL. **`ENV=production` returns `VOBIZ_PROD_WS_URL`** — the hook for hosting the bot elsewhere. Otherwise `wss://{host}/ws`. |
| `lifespan(app)` | 312 | Startup: replays `events.jsonl`, opens the shared aiohttp session. |

### Routes

| Route | Line | Direction | Purpose |
|---|---|---|---|
| `POST /start` | 337 | you → server | Place an outbound call |
| `/answer` | 429 | Vobiz → server | **Returns the XML that drives the call.** Streaming document, or `<Dial>` if a transfer is pending |
| `/recording-finished` | 579 | Vobiz → server | Early recording notification |
| `/recording-ready` | 627 | Vobiz → server | Recording complete; downloads the file into `recordings/` |
| `POST /transfer-to-human` | 694 | Vobiz → server | The redirect target; returns `<Dial>` |
| `POST /initiate-transfer` | 732 | you → server | Asks Vobiz to redirect a leg |
| `/hangup` | 871 | Vobiz → server | A-leg ended (A-leg only, always) |
| `/dial-events` | 884 | Vobiz → server | `DialAnswer` / `DialConnected` / `DialHangup` — **the only B-leg visibility** |
| `/dial-complete` | 901 | Vobiz → server | Final dial result |
| `GET /dashboard` | 912 | browser | The inspector UI |
| `GET /events` | 918 | browser | SSE stream — history, then live |
| `POST /events/clear` | 928 | browser | Clear feed; `{"wipe_log":true}` also deletes the log |
| `GET /events/info` | 941 | browser | Stored / in-memory counts |
| `GET /active-calls` | 950 | you | Live state |
| `ws /ws`, `/`, `/voice/ws`, `/stream` | 1095-1130 | Vobiz → server | All four call `handle_vobiz_websocket()` `:974` |

### `handle_vobiz_websocket()` — `:974`

Accepts the socket, decodes the base64 `body` query parameter, wraps the socket
in `WebSocketTap`, builds `WebSocketRunnerArguments`, and calls `bot()`. On exit
it flushes the tap so no counted audio is lost.

It deliberately does **not** call `parse_telephony_websocket()` — that would
consume the handshake and leave the socket empty for the transport.

---

## `bot.py` — the AI pipeline

| Symbol | Line | Does |
|---|---|---|
| `BOT_MODE` | 56 | `cascaded` (default) or `realtime` |
| `TELEPHONY_RATE` | 59 | `8000` — the whole pipeline stays at 8 kHz |
| `SYSTEM_PROMPT` | 61 | Personality; instructs short spoken answers |
| `GREETING_SEED` | 68 | What realtime mode infers its opening line from |
| `_build_cascaded()` | 74 | Deepgram STT → Gemini LLM → Deepgram TTS |
| `_build_realtime()` | 110 | Gemini Live, speech-to-speech, single service |
| `run_bot(transport, …)` | 145 | Assembles the pipeline, wires connect/disconnect handlers, runs it |
| `bot(runner_args, …)` | 208 | Entry point. Parses `start`, builds serializer + transport |

### The two modes

| | `cascaded` | `realtime` (deployed) |
|---|---|---|
| Services | Deepgram STT + Gemini + Deepgram TTS | Gemini Live only |
| Transcript | Yes, both sides | No local transcript |
| Latency | Higher | ~1.5s lower |
| Turn-taking | Local VAD | Decided server-side |

### Three constraints that are easy to break

1. **`audio_out_sample_rate` is 8000 in both modes.** Pipecat resamples before the
   serializer sees the frame. Leaving it at the service-native 24 kHz pushes
   resampling into the serializer, whose stream resampler returns empty on its
   first calls and silently drops frames — measured at 3 of the first 10.
2. **VAD belongs on `LLMUserAggregatorParams`, not the transport.** Under Pipecat
   1.x the transport's `vad_analyzer` argument is silently dropped by Pydantic:
   no error, no turn detection.
3. **`add_wav_header=False`.** Telephony frames are raw payloads, not WAV files.

---

## `events.py` — the event bus

Records every exchange with Vobiz in one of two directions, fans them out to the
dashboard over SSE, and persists them.

| Function | Line | Does |
|---|---|---|
| `record(direction, label, payload, …)` | 85 | Append + fan out. `direction` is `in`/`out`; `kind` is `webhook`/`rest`/`xml`/`stream`. **Swallows its own errors** — recording must never break a call. |
| `record_xml(label, xml, …)` | 120 | Records an XML document being returned to Vobiz |
| `_append_to_log(event)` | 41 | Appends one JSON line to `events.jsonl` |
| `load_from_log()` | 51 | Replays the log at startup; tolerates a torn final line from a killed process |
| `log_size()` | 76 | Total events on disk |
| `history()` / `clear()` | 125 / 129 | In-memory window; `clear(wipe_log=True)` also truncates the file |
| `subscribe()` / `unsubscribe()` | 140 / 146 | SSE subscriber queues |
| `sse_stream(request)` | 151 | Replays history, then streams live with a 15s keepalive |

`MAX_EVENTS = 1000` in memory; `HIGHLIGHT_FIELDS` picks which Vobiz fields get
surfaced as chips rather than buried in the raw payload.

---

## `ws_tap.py` — the observer

`WebSocketTap` is a transparent proxy around the FastAPI WebSocket. The transport
and serializer own the socket; this only watches.

| Method | Line | Does |
|---|---|---|
| `__getattr__` | 40 | Delegates everything not intercepted — `close`, `client_state`, `query_params` |
| `_rollup(direction, force)` | 47 | Emits an aggregated audio event every `ROLLUP_SECONDS` (2.0) |
| `_observe(direction, raw)` | 65 | Classifies a frame: control events individually, audio counted |
| `flush()` | 112 | Emits pending counts at stream end |
| `receive` / `receive_text` / `receive_bytes` | 121-135 | Observe inbound, return the original payload unchanged |
| `send_text` / `send_bytes` | 137-142 | Observe outbound, forward exactly |

**Why roll up:** `media` and `playAudio` arrive ~50× per second in each direction.
Listing them individually buries every checkpoint within seconds.

`CONTROL_EVENTS` = `start`, `dtmf`, `playedStream`, `clearedAudio`, `clearAudio`,
`stop`, `mark`.

---

## `dashboard.py` — the UI

A single `DASHBOARD_HTML` string: no CDN, no build step, no external fonts.

- Full-height app shell — the page never scrolls; each feed scrolls on its own
- Two live panes: **HTTP webhooks** and **Stream events**, fed by one SSE stream
- Direction badges always read `APP → VOBIZ` / `APP ← VOBIZ`, so only the arrow moves
- Controls to place a call and fire either transfer type
- Each row is a `<details>`: heading collapsed, full payload on click

---

## `pipecat-vobiz` — not in this repo

Installed from PyPI. Provides:

| Symbol | Does |
|---|---|
| `parse_vobiz_start(websocket)` | Reads exactly one message and returns `stream_id`, `call_id`, `encoding`, `sample_rate` |
| `VobizFrameSerializer.serialize()` | Pipecat frame → Vobiz JSON (`playAudio`, `clearAudio`, `stop`) |
| `VobizFrameSerializer.deserialize()` | Vobiz JSON → Pipecat frame |

This is where μ-law companding, resampling, byte-order handling and REST hangup
live. See [Streaming protocol](04-streaming-protocol.md).

---

## Supporting files

| File | Purpose |
|---|---|
| `download_recording.py` | Standalone fetcher into `manual_record/`. From the original repo; not wired in. |
| `tests/test_transfer.py` | 11 tests pinning transfer XML and `X-VH-` rules |
| `render.yaml` | Render blueprint. **Documents the config; the live service was created via API** |
| `Dockerfile` | python:3.11-slim + `libgomp1` (onnxruntime needs it for Silero VAD) |
| `deploy.sh` | Cloud Run path, parameterised — no identifiers committed |
| `.github/workflows/ci.yml` | Imports, tests, and a committed-secret check |
| `env.example` | Annotated template. Copy to `.env`, which is gitignored |
