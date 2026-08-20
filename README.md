# Vobiz + Pipecat — AI Voice Agent

Place an outbound PSTN call from a Vobiz number and hand the live audio to a
[Pipecat](https://pipecat.ai) voice agent over a bidirectional WebSocket stream —
speech in, LLM reasoning, speech out, with the whole conversation recorded.

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](./LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![Pipecat 1.x](https://img.shields.io/badge/Pipecat-1.x-6f42c1.svg)](https://docs.pipecat.ai)
[![Docs](https://img.shields.io/badge/Docs-docs.vobiz.ai-0b7285.svg)](https://docs.vobiz.ai)

## Overview

Pipecat gives you a real-time frame pipeline for voice agents — transport in,
STT, context, LLM, TTS, transport out — but it needs a transport that speaks
telephony. This repository is that transport, wired end to end: a FastAPI server
that starts a Vobiz outbound call, answers Vobiz's webhook with Voice XML, and
bridges the resulting media WebSocket into a Pipecat `Pipeline` using the
`VobizFrameSerializer` from the [`pipecat-vobiz`](https://pypi.org/project/pipecat-vobiz/)
package.

The problem it solves is the awkward seam between a carrier and an agent
framework. A telephony leg is 8 kHz μ-law (or 16-bit linear PCM) arriving as
base64 inside JSON events, with its own start/stop/DTMF/barge-in semantics.
A Pipecat pipeline wants typed audio frames at a pipeline sample rate. The
serializer sits between the two and does the conversion, resampling, byte-order
handling, interruption signalling, and call teardown, so your agent code never
touches the wire format.

This is aimed at developers who already know Pipecat and want to put an agent on
a real phone number, and at Vobiz users who want an LLM on the call instead of a
fixed IVR tree. It is a single-process example: `server.py` runs the HTTP and
WebSocket surface, `bot.py` builds and runs the pipeline in the same process, and
`download_recording.py` is a small helper for pulling recordings afterwards.

By the end of the setup below you will have dialled your own phone from a Vobiz
number, held a spoken conversation with an OpenAI-backed agent, and found the MP3
of that call in `recordings/`.

## What you can build with it

- **Outbound appointment reminders and confirmations** — trigger a call from your
  own backend with `POST /start`, let the agent confirm, reschedule, or cancel,
  and read the outcome back from the recording or your own logging.
- **Automated follow-up and survey calls** — the system prompt in `bot.py`
  defines the script; the agent handles free-form answers instead of DTMF menus.
- **Voice front door with human escalation** — `POST /initiate-transfer` marks a
  live call for handover, and the next `/answer` fetch returns `<Dial>` XML that
  bridges the caller to `TRANSFER_AGENT_NUMBER`.
- **Lead qualification and callback bots** — pass per-call context through the
  `body` field on `/start`; it travels as a query parameter to `/answer` and then
  base64-encoded onto the WebSocket URL, so the bot can be primed per call.
- **Notification calls that need a conversation** — delivery attempts, payment
  reminders, or verification flows where the callee may want to ask a question
  rather than press a key.
- **A test bench for voice-agent latency and quality** — `enable_metrics` and
  `enable_usage_metrics` are on in the pipeline task, so you can measure a real
  PSTN turn rather than a browser microphone.

## How it works

A call runs in two phases: an HTTP phase that Vobiz drives with webhooks, and a
WebSocket phase where audio flows in both directions.

1. You (or `POST /start`) ask the Vobiz REST API to place a call. Vobiz answers
   with a `request_uuid`, and the server pre-registers it in `active_calls`.
2. When the callee picks up, Vobiz fetches your `answer_url` — `POST /answer` on
   this server.
3. `/answer` returns Voice XML containing a `<Record>` element and a `<Stream>`
   element. The `<Stream>` body is the `wss://` URL of this same server, and its
   `contentType` advertises the wire format (`audio/x-mulaw;rate=8000` by default).
4. Vobiz opens the WebSocket and sends a `start` event carrying `streamId`,
   `callId`, and the `mediaFormat` it actually negotiated.
5. `bot.py` reads that `start` event with `parse_vobiz_start()`, builds a
   `VobizFrameSerializer` from it, wraps it in a `FastAPIWebsocketTransport`, and
   runs the Pipecat pipeline until either side hangs up.
6. When the recording is ready, Vobiz posts to `/recording-ready` and the server
   downloads the MP3 into `recordings/` using your account credentials.

```
┌─────────────┐
│  You (curl) │
└──────┬──────┘
       │
       ↓ POST /Call/
┌─────────────────┐
│  Vobiz API      │
└──────┬──────────┘
       │
       ↓ Call initiated
┌─────────────────┐
│  Phone rings    │
└──────┬──────────┘
       │
       ↓ Call answered
┌─────────────────┐
│  Vobiz → POST   │
│  /answer        │
└──────┬──────────┘
       │
       ↓ Returns XML
┌─────────────────┐
│  <Record>       │
│  <Stream>       │
│  wss://...      │
└──────┬──────────┘
       │
       ↓ WebSocket connect + `start` event
┌─────────────────┐
│  Pipecat Bot    │
│  STT → LLM → TTS│
└──────┬──────────┘
       │
       ↓
┌─────────────────┐
│  AI Conversation│
│  + Recording    │
└─────────────────┘
```

### The Pipecat pipeline

`run_bot()` in `bot.py` assembles seven processors in this order:

```python
Pipeline([
    transport.input(),          # Vobiz WebSocket → InputAudioRawFrame
    stt,                        # OpenAISTTService
    context_aggregator.user(),  # LLMContextAggregatorPair.user() + Silero VAD
    llm,                        # OpenAILLMService
    tts,                        # OpenAITTSService (voice="ballad")
    transport.output(),         # OutputAudioRawFrame → Vobiz WebSocket
    context_aggregator.assistant(),
])
```

- **Transport** — `FastAPIWebsocketTransport` with
  `FastAPIWebsocketParams(audio_in_enabled=True, audio_out_enabled=True,
  add_wav_header=False, serializer=VobizFrameSerializer(...))`. `add_wav_header`
  must stay `False`: telephony frames are raw payloads, not WAV files.
- **VAD** — `SileroVADAnalyzer` is passed on `LLMUserAggregatorParams`, not on the
  transport. Under Pipecat 1.x the transport-side `vad_analyzer` argument is a
  silent no-op, which is why `requirements.txt` pins `pipecat-ai>=1.2.0,<2`.
- **Services** — STT, LLM, and TTS are all OpenAI and all read the same
  `OPENAI_API_KEY`. The LLM and STT models are provider defaults; the TTS voice is
  set to `ballad` in `bot.py`.
- **Task parameters** — `PipelineParams(audio_in_sample_rate=8000,
  audio_out_sample_rate=24000, enable_metrics=True, enable_usage_metrics=True)`.

### Audio format at each hop

| Direction | Hop | Format |
|---|---|---|
| Inbound | Vobiz `media` event | base64 payload, `audio/x-mulaw` at 8000 Hz (as negotiated in `start`) |
| Inbound | Serializer `deserialize()` | `ulaw_to_pcm()` → 16-bit linear PCM at the pipeline input rate |
| Inbound | `InputAudioRawFrame` → STT | mono PCM at `audio_in_sample_rate` (8000) |
| Outbound | TTS → `OutputAudioRawFrame` | mono PCM at `audio_out_sample_rate` (24000) |
| Outbound | Serializer `serialize()` | `pcm_to_ulaw()` resamples 24000 → 8000 and compands to μ-law |
| Outbound | Vobiz `playAudio` event | base64 μ-law with `contentType` and `sampleRate` echoed back |

If you set `VOBIZ_ENCODING=audio/x-l16` instead, the serializer skips companding
and resamples linear PCM directly, byte-swapping between Pipecat's native
little-endian PCM and the wire order given by `VOBIZ_L16_ENDIAN`.

The wire format declared in `<Stream contentType>` is only a hint. The
serializer treats the `mediaFormat` in Vobiz's `start` event as authoritative and
adopts it — logging a warning — if the two disagree.

## Architecture

| File | Responsibility |
|---|---|
| `server.py` | FastAPI app. Places calls via the Vobiz REST API, serves the `/answer` Voice XML, hosts the media WebSocket endpoints, handles recording callbacks and human transfer, and tracks `active_calls` in memory. Runs on port `7860`. |
| `bot.py` | Pipecat entry point. Parses the Vobiz `start` event, constructs `VobizFrameSerializer` and `FastAPIWebsocketTransport`, builds the STT → LLM → TTS pipeline, and runs it with `PipelineRunner`. |
| `download_recording.py` | Standalone helper that downloads a recording URL with `X-Auth-ID` / `X-Auth-Token` headers into `manual_record/`. |
| `requirements.txt` | Pins `pipecat-ai[websocket,openai,silero]>=1.2.0,<2` and `pipecat-vobiz>=0.0.3,<0.1`, plus FastAPI, uvicorn, aiohttp, requests, python-dotenv, and loguru. |
| `env.example` | Annotated template for `.env`. Copy it, do not edit it in place. |
| `pipecat.serializers.vobiz` | Supplied by the `pipecat-vobiz` package, not vendored here. Provides `VobizFrameSerializer` and `parse_vobiz_start()`. |

The server exposes the same WebSocket handler on four paths — `/ws`, `/`,
`/voice/ws`, and `/stream` — so an XML document that points at any of them will
still reach the bot.

## Prerequisites

| Requirement | Notes |
|---|---|
| Vobiz account | Sign up at [vobiz.ai](https://vobiz.ai) and take your Auth ID and Auth Token from the console. |
| A Vobiz phone number | Used as the caller ID on outbound calls. Required for `POST /start` unless you pass `from_number` in the request body. |
| OpenAI API key | Funded key from [platform.openai.com](https://platform.openai.com). It is used for STT, the LLM, and TTS. |
| Python 3.10 or newer | Pipecat 1.x requires 3.10+. |
| A public HTTPS tunnel | [ngrok](https://ngrok.com) or equivalent, so Vobiz can reach `/answer` and the `wss://` endpoint from the internet. |
| A phone to answer | The destination handset for your first test call. |

## Setup

1. **Clone the repository**

   ```bash
   git clone https://github.com/vobiz-ai/Vobiz-X-Pipecat.git
   cd Vobiz-X-Pipecat
   ```

2. **Create a virtual environment**

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate      # Windows: .venv\Scripts\activate
   ```

3. **Install dependencies**

   ```bash
   pip install -r requirements.txt
   ```

4. **Create your `.env`**

   ```bash
   cp env.example .env
   ```

   Fill in at minimum:

   ```env
   OPENAI_API_KEY=your-openai-key
   VOBIZ_AUTH_ID=your-auth-id
   VOBIZ_AUTH_TOKEN=your-auth-token
   VOBIZ_PHONE_NUMBER=+15550001111
   ```

5. **Start the tunnel and record its URL**

   ```bash
   ngrok http 7860
   ```

   Copy the `https://` forwarding URL and set it in `.env`:

   ```env
   PUBLIC_URL=https://abc123.ngrok-free.app
   ```

   `PUBLIC_URL` is what the server uses to build both the `answer_url` it sends to
   Vobiz and the `wss://` URL it puts inside `<Stream>`. Without it the server
   falls back to the request `Host` header and warns loudly if that is localhost.

6. **Restart the server** whenever `PUBLIC_URL` changes. `.env` is read once at
   import time.

## Configuration

Every variable below is read by the code. Copy `env.example` to `.env` and edit
the copy — `.env` is already in `.gitignore`.

| Variable | Required | Default | Description |
|---|---|---|---|
| `OPENAI_API_KEY` | Yes | — | Key used by `OpenAISTTService`, `OpenAILLMService`, and `OpenAITTSService` in `bot.py`. |
| `VOBIZ_AUTH_ID` | Yes | — | Vobiz Auth ID. Used for the REST call, the recording download, and the serializer's REST hang-up. `make_vobiz_call()` raises if it is missing. |
| `VOBIZ_AUTH_TOKEN` | Yes | — | Vobiz Auth Token, sent as `X-Auth-Token`. `make_vobiz_call()` raises if it is missing. |
| `PUBLIC_URL` | Yes in practice | Request `Host` header | Public `https://` base URL for this server. Also required by `POST /initiate-transfer`, which returns HTTP 500 without it. |
| `VOBIZ_PHONE_NUMBER` | Conditional | — | Caller ID for `POST /start`. Only optional if the request body supplies `from_number`; otherwise `/start` returns HTTP 400. |
| `VOBIZ_ENCODING` | No | `audio/x-mulaw` | Wire encoding. `audio/x-mulaw` or `audio/x-l16`. Used in `<Stream contentType>` and as the serializer hint. |
| `VOBIZ_SAMPLE_RATE` | No | `8000` | Wire sample rate in Hz. The serializer accepts 8000, 16000, or 24000; `env.example` notes that 24000 is not reliably available in every region. |
| `VOBIZ_L16_ENDIAN` | No | `be` in code, `le` in `env.example` | Byte order for `audio/x-l16` only. `be` swaps between Pipecat's native little-endian PCM and a big-endian wire; `le` is a passthrough. Ignored for μ-law. |
| `ENV` | No | `local` | `local` or `production`. `production` switches the WebSocket URL source and appends a `serviceHost` query parameter. |
| `VOBIZ_PROD_WS_URL` | When `ENV=production` | — | Public `wss://` URL where the bot is hosted. `get_websocket_url()` raises a `ValueError` if `ENV=production` and this is unset. |
| `AGENT_NAME` | When `ENV=production` | — | First half of the `serviceHost=<AGENT_NAME>.<ORGANIZATION_NAME>` query parameter. |
| `ORGANIZATION_NAME` | When `ENV=production` | — | Second half of the `serviceHost` query parameter. |
| `TRANSFER_AGENT_NUMBER` | For transfers | — | E.164 number dialled by the `<Dial>` element. `/answer` and `/transfer-to-human` return HTTP 500 if a transfer is attempted while this is unset. There is deliberately no hardcoded fallback. |
| `ENABLE_RECORDING` | No | `true` | Set to `false` to omit the `<Record>` element from the answer XML. |
| `MAX_RECORDING_LENGTH` | No | `3600` | Value of `maxLength` on `<Record>`, in seconds. |
| `DEEPGRAM_API_KEY` | No | — | Present in `env.example` only. `bot.py` as shipped uses OpenAI STT; set this if you swap in `DeepgramSTTService`. |

### Customising the bot

Edit `bot.py`. The system prompt sets the personality and the opening line:

```python
messages = [
    {
        "role": "system",
        "content": "You are a friendly customer service agent...",
    },
]
```

Change the TTS voice:

```python
tts = OpenAITTSService(
    api_key=os.getenv("OPENAI_API_KEY"),
    voice="nova",  # alloy, echo, fable, onyx, nova, shimmer, ballad
)
```

Pin a specific LLM instead of the provider default:

```python
llm = OpenAILLMService(
    api_key=os.getenv("OPENAI_API_KEY"),
    model="gpt-4o",
)
```

## Running it

**Terminal 1 — the server:**

```bash
python server.py
```

It binds `0.0.0.0:7860`. You should see uvicorn's startup banner and nothing else
until a call arrives.

**Terminal 2 — the tunnel:**

```bash
ngrok http 7860
```

**Terminal 3 — place the call.** Either go through this server:

```bash
curl -X POST https://abc123.ngrok-free.app/start \
  -H "Content-Type: application/json" \
  -d '{
    "phone_number": "+15550003333",
    "from_number": "+15550001111"
  }'
```

Or call the Vobiz REST API directly, pointing `answer_url` at this server:

```bash
curl -X POST https://api.vobiz.ai/api/v1/Account/YOUR_AUTH_ID/Call/ \
  -H "X-Auth-ID: YOUR_AUTH_ID" \
  -H "X-Auth-Token: YOUR_AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "from": "+15550001111",
    "to": "+15550003333",
    "answer_url": "https://abc123.ngrok-free.app/answer",
    "answer_method": "POST"
  }'
```

Replace `YOUR_AUTH_ID` and `YOUR_AUTH_TOKEN` with your credentials, the `from`
number with your Vobiz number, the `to` number with the handset you are testing
against, and the ngrok host with your own.

### What you should observe

- `/start` responds with `{"call_uuid": "...", "status": "call_initiated", ...}`
  and the Vobiz API returns HTTP 201.
- The server logs `========== ANSWER XML REQUEST ==========` followed by the
  generated XML, including the `wss://` URL and the negotiated `contentType`.
- The handset rings. On answer, the log shows
  `[SUCCESS] WebSocket connection accepted for outbound call`, then
  `Vobiz start: callId=..., streamId=..., mediaFormat=('audio/x-mulaw', 8000)`.
- `Starting outbound call conversation` appears, and the agent speaks its opening
  line. Speak back and it replies.
- On hang-up: `Outbound call ended`, and the pipeline task is cancelled.
- Shortly after, `[RECORDING CALLBACK] Downloaded to recordings/<id>.mp3`.

You can inspect live state at any time:

```bash
curl https://abc123.ngrok-free.app/active-calls
```

### Recordings

Recordings arrive automatically. Vobiz posts to `/recording-ready`, and the server
downloads the file with authentication into `recordings/`, which is gitignored.

If the automatic download fails, fetch it yourself:

```bash
curl -X GET "https://media.vobiz.ai/v1/Account/YOUR_AUTH_ID/Recording/RECORDING_ID.mp3" \
  -H "X-Auth-ID: YOUR_AUTH_ID" \
  -H "X-Auth-Token: YOUR_AUTH_TOKEN" \
  -o recording.mp3
```

Or edit the URL at the bottom of `download_recording.py` and run it; it writes
into `manual_record/`.

### Escalating to a human

While a call is live, mark it for transfer:

```bash
curl -X POST https://abc123.ngrok-free.app/initiate-transfer \
  -H "Content-Type: application/json" \
  -d '{"call_uuid": "CALL_UUID_FROM_START"}'
```

The server calls the Vobiz transfer API with `legs: "aleg"` and an `aleg_url`
pointing at `/transfer-to-human`. Vobiz closes the stream, fetches that URL, and
gets back `<Speak>` plus `<Dial>TRANSFER_AGENT_NUMBER</Dial>`. The call record is
kept in `active_calls` across the transfer rather than being deleted.

### Beyond local development

For a persistent deployment, replace the tunnel rather than the code:

1. Deploy `server.py` behind a TLS-terminating proxy on your own infrastructure.
2. Point a domain at it, for example `voice.example.com`.
3. Set `PUBLIC_URL=https://voice.example.com`.
4. If you are hosting the bot separately, set `ENV=production`,
   `VOBIZ_PROD_WS_URL`, `AGENT_NAME`, and `ORGANIZATION_NAME`.
5. Place calls against the production `answer_url`.

## API and protocol reference

### HTTP and WebSocket endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/start` | Initiates an outbound call. Body: `{"phone_number": "...", "from_number": "...", "body": {...}}`. `phone_number` is required; `from_number` falls back to `VOBIZ_PHONE_NUMBER`; `body` is JSON that is forwarded to `/answer` as a query parameter. |
| `GET` `POST` | `/answer` | Vobiz answer webhook. Returns `<Stream>` XML normally, or `<Speak>` + `<Dial>` XML when the call's `transfer_requested` flag is set. Accepts `CallUUID` and `body_data` query parameters. |
| `GET` `POST` | `/recording-finished` | Optional recording-stopped webhook. Logs `RecordUrl`, `RecordingDuration`, `RecordingID`, `CallUUID`, and `RecordingEndReason` from the form body, and stores the recording ID against the call. |
| `GET` `POST` | `/recording-ready` | Recording-available callback wired into `<Record callbackUrl>`. Downloads the MP3 into `recordings/` using account credentials. |
| `POST` | `/transfer-to-human` | Returns the `<Speak>` + `<Dial>` transfer XML. Fetched by Vobiz, not called directly. |
| `POST` | `/initiate-transfer` | Body: `{"call_uuid": "..."}`. Marks the call as transferring and calls the Vobiz transfer API. Expects HTTP 202 back from Vobiz. |
| `GET` | `/active-calls` | Returns the in-memory call registry: status, start time, WebSocket path, and recording details. |
| `WS` | `/ws` `/` `/voice/ws` `/stream` | Media stream endpoints. All four route to the same handler, which accepts the socket, decodes the optional base64 `body` query parameter, and hands the socket to `bot()`. |

### Voice XML elements used

`<Speak>` — text-to-speech prompt, used on the transfer path:

```xml
<Speak voice="WOMAN" language="en-US">
    Please hold while I transfer you to a human agent.
</Speak>
```

`<Stream>` — opens the bidirectional media WebSocket to the bot:

```xml
<Stream bidirectional="true" audioTrack="inbound"
        contentType="audio/x-mulaw;rate=8000" keepCallAlive="true">
    wss://abc123.ngrok-free.app/ws
</Stream>
```

`<Record>` — records the whole session and calls back when the file is ready:

```xml
<Record fileFormat="wav" maxLength="3600" recordSession="true"
        callbackUrl="https://abc123.ngrok-free.app/recording-ready"
        callbackMethod="POST">
</Record>
```

`<Dial>` — bridges the caller to a human agent on the transfer path:

```xml
<Dial>+15550003333</Dial>
```

### Stream events on the wire

Handled by `VobizFrameSerializer`. Inbound, from Vobiz:

| Event | Effect |
|---|---|
| `start` | Carries `streamId`, `callId`, and `mediaFormat`. Negotiates encoding and sample rate; produces no Pipecat frame. |
| `media` | Base64 audio payload, converted to an `InputAudioRawFrame`. |
| `dtmf` | Converted to an `InputDTMFFrame`; invalid digits are dropped with a warning. |
| `playedStream` | Debug-logged only. |
| `clearedAudio` | Debug-logged only. |

Outbound, to Vobiz:

| Event | Sent when |
|---|---|
| `playAudio` | An `AudioRawFrame` leaves the pipeline. Carries `contentType`, `sampleRate`, and the base64 payload alongside `streamId`. |
| `clearAudio` | An interruption frame fires, so queued audio is dropped for barge-in. |
| `stop` | `EndFrame` or `CancelFrame` arrives and `auto_hang_up` is enabled. Vobiz then moves past `<Stream>` and hangs up with `HangupCause="End Of XML Instructions"`. |

With the default `hangup_method="both"`, the serializer also fires a REST
`DELETE /api/v1/Account/{auth_id}/Call/{call_id}/` in the background as a safety
net, which is why `VOBIZ_AUTH_ID` and `VOBIZ_AUTH_TOKEN` are passed into it.

### Further reading

- [Vobiz documentation](https://docs.vobiz.ai) and [API reference](https://docs.vobiz.ai/api-reference)
- [Vobiz Stream XML and stream events](https://docs.vobiz.ai/xml/stream/stream-events)
- [Pipecat documentation](https://docs.pipecat.ai)
- [OpenAI API documentation](https://platform.openai.com/docs)
- [ngrok documentation](https://ngrok.com/docs)

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `/start` returns HTTP 400 "Either set VOBIZ_PHONE_NUMBER in .env or provide 'from_number'" | No caller ID available | Set `VOBIZ_PHONE_NUMBER` in `.env` or pass `from_number` in the request body. |
| `ValueError: Missing Vobiz Auth ID` or `Missing Vobiz Auth Token` | `.env` not loaded or keys blank | Confirm `.env` sits next to `server.py` and both values are filled in, then restart. |
| Server logs `[WARNING] Using localhost for URL!` and the call never reaches the bot | `PUBLIC_URL` unset, so the `Host` header was used | Set `PUBLIC_URL` to the tunnel's `https://` URL and restart the server. |
| Phone rings, call connects, but no WebSocket appears in the logs | The `wss://` URL in `<Stream>` is not publicly reachable | `curl https://<your-tunnel>/answer` and check the URL in the returned XML resolves from outside your network. |
| `Vobiz first WS message was <something> , expected 'start'` | Something consumed the handshake before `parse_vobiz_start()` | Do not call `parse_telephony_websocket()` on this socket; the handler in `server.py` deliberately skips it. |
| WebSocket connects, media frames arrive, but STT produces nothing, and `VOBIZ_ENCODING=audio/x-l16` | Wire byte order does not match the configured one | Flip `VOBIZ_L16_ENDIAN` between `be` and `le` and restart. |
| Audio is present but garbled or chipmunk-pitched | Wire sample rate disagrees with what the pipeline was told | Check the `Vobiz start:` log line for the negotiated `mediaFormat` and align `VOBIZ_SAMPLE_RATE`; the serializer logs a warning when it has to adopt a different value. |
| Bot connects but never speaks first | The opening turn is driven by the system prompt only | Confirm the system prompt still contains the "Begin by saying" instruction, and check the OpenAI key has credit. |
| WebSocket upgrade is silently refused at 24000 Hz | That rate is not available in every region | Set `VOBIZ_SAMPLE_RATE=8000` or `16000`. |
| HTTP 500 "TRANSFER_AGENT_NUMBER not configured in .env" | Transfer attempted with no destination | Set `TRANSFER_AGENT_NUMBER` to an E.164 number and restart. |
| `POST /initiate-transfer` returns HTTP 404 "not found in active calls" | The `call_uuid` is not in the in-memory registry | Use the `call_uuid` returned by `/start`, and check `GET /active-calls`. The registry is lost on restart. |
| Recording download logs `Download failed: HTTP 401` | Credentials not sent or incorrect | Recording URLs require `X-Auth-ID` and `X-Auth-Token`; retry with `download_recording.py` or the curl command above. |
| Everything worked yesterday, nothing works today | A free ngrok URL changed on restart | Update `PUBLIC_URL`, restart `server.py`, and update any `answer_url` you hardcoded. A reserved domain avoids this. |

## Security notes

- `.env` holds your Vobiz and OpenAI credentials and is excluded by `.gitignore`.
  Commit `env.example` only.
- `make_vobiz_call()` logs the Auth ID in full and only the last four characters
  of the Auth Token. Drop those lines entirely before running anywhere with shared
  log access.
- The webhook endpoints (`/answer`, `/recording-finished`, `/recording-ready`) are
  unauthenticated. Anyone who learns your public URL can invoke them. Put them
  behind a shared-secret path segment, a signature check, or an IP allowlist
  before exposing them long-term.
- `CORSMiddleware` is configured with `allow_origins=["*"]` for local testing.
  Narrow it to the origins you actually serve.
- Call recordings contain whatever the caller said, which frequently includes
  personal data. `recordings/` and `manual_record/` are gitignored; treat their
  contents according to your own retention and consent obligations, and make sure
  your call script discloses recording where that is required.
- `active_calls` is an in-memory dictionary. It contains phone metadata and
  recording IDs, and it disappears on restart — which is a privacy property but
  also means transfers cannot survive a redeploy.
- Transcripts and audio leave your infrastructure for OpenAI's STT, LLM, and TTS
  endpoints. Review that against your data-handling policy before going live.

## Roadmap

> Planned improvements to this example. Ideas and pull requests are welcome —
> open an issue to discuss anything here.

- [ ] Add a test suite: unit tests for the answer-XML builder and the transfer
      state machine, plus a WebSocket fixture that replays a recorded Vobiz
      `start` + `media` sequence against the pipeline.
- [ ] Handle reconnection. A dropped media WebSocket currently ends the call;
      buffer briefly and resume the pipeline where possible.
- [ ] Emit turn-latency metrics. `enable_metrics` is already on, so surface
      time-to-first-token and time-to-first-audio through a Prometheus endpoint or
      structured logs instead of leaving them in the frame stream.
- [ ] Move model IDs, TTS voice, and the system prompt out of `bot.py` and into
      configuration, so the agent can be re-scripted without a code change.
- [ ] Replace the in-memory `active_calls` dictionary with Redis or a database, so
      call state and in-flight transfers survive a restart or a second worker.
- [ ] Add a deployment path beyond local plus tunnel: a Dockerfile, a health
      endpoint, and a worked example of running the bot separately from the
      webhook server using `ENV=production`.
- [ ] Add an inbound-call example, so the same pipeline can answer calls to a
      Vobiz number rather than only placing them.

## Contributing

Issues and pull requests are welcome. Before opening a pull request:

```bash
python -m compileall server.py bot.py download_recording.py
```

Then run an end-to-end call against your own Vobiz account and tunnel, and say in
the pull request which wire format you tested with (`VOBIZ_ENCODING` and
`VOBIZ_SAMPLE_RATE`) — μ-law and L16 exercise different paths through the
serializer. Keep changes to `env.example` in step with the configuration table
above, and never commit a populated `.env`.

## License

Released under the [MIT License](./LICENSE) © Vobiz.

MIT is permissive: you may use, modify, and redistribute this code, including in
closed-source commercial products, provided the copyright notice and licence text
are retained. There is no warranty. If your organisation needs a different
licensing arrangement, contact [piyush@vobiz.ai](mailto:piyush@vobiz.ai).

## Built by Team Vobiz

[Vobiz](https://vobiz.ai) is a programmable voice and SIP-trunking platform for
voice APIs, SIP trunking, and AI voice agents. This repository is built and
maintained by the Vobiz team.

**Maintainer:** Piyush Sahoo — [piyush@vobiz.ai](mailto:piyush@vobiz.ai) · [LinkedIn](https://www.linkedin.com/in/piyush-s713/)

Questions, or want to talk through an integration? Open an issue on this repo,
or reach out directly at [piyush@vobiz.ai](mailto:piyush@vobiz.ai).

**Useful links:** [Docs](https://docs.vobiz.ai) · [API reference](https://docs.vobiz.ai/api-reference) · [Sign up](https://vobiz.ai)
