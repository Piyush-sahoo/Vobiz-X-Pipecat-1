# 4. Streaming protocol

The media plane: one WebSocket per call, JSON messages carrying base64 audio.

## Who does what

```
Vobiz ──wss──► server.py (hosts the socket, 4 routes → one handler)
                   │
                   ├─► ws_tap.py            observe only, never modifies
                   │
                   └─► bot.py               builds serializer + transport
                          │
                          └─► pipecat-vobiz  VobizFrameSerializer
                                 │           μ-law ⇄ PCM, resample, JSON
                                 │
                                 └─► Gemini Live
```

`server.py` answers the door. `bot.py` does the talking. The installed
`pipecat-vobiz` package handles the wire format — it is not in this repo.

## Messages

### Vobiz → server

| Event | Carries | Meaning |
|---|---|---|
| `start` | `streamId`, `callId`, `mediaFormat` | First message. **Authoritative** over `<Stream contentType>` |
| `media` | base64 `payload` | Inbound audio, ~50/sec |
| `dtmf` | `digit` | Keypad press |
| `playedStream` | `name` | **Checkpoint** — queued audio finished playing |
| `clearedAudio` | `streamId` | Confirms a `clearAudio` took effect |

### Server → Vobiz

| Event | Carries | Meaning |
|---|---|---|
| `playAudio` | `media.payload`, `contentType`, `sampleRate`, `streamId` | Outbound audio |
| `clearAudio` | `streamId` | Barge-in — discard queued audio |
| `stop` | `streamId` | End the stream; Vobiz then proceeds past `<Stream>` |

**Every outbound message must echo the `streamId` from `start`.** This is the
most common mistake when writing a server against this protocol — without it
Vobiz ignores the audio, with no error.

### The `start` event

```json
{"event": "start",
 "start": {"streamId": "26361aa4-…", "callId": "3bd0d027-…",
           "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000}}}
```

`parse_vobiz_start()` reads exactly one message and leaves the rest queued for
the transport. `bot.py` treats `mediaFormat` as authoritative and adopts it —
logging a warning — if it disagrees with the env vars.

## The audio path

| Direction | Hop | Format |
|---|---|---|
| In | Vobiz `media` | base64 μ-law @ 8000 Hz |
| In | `deserialize()` | `ulaw_to_pcm()` → 16-bit PCM at pipeline rate |
| In | → Gemini | mono PCM @ 8000 |
| Out | Gemini → frame | mono PCM @ 8000 |
| Out | `serialize()` | `pcm_to_ulaw()` → μ-law @ 8000 |
| Out | Vobiz `playAudio` | base64 μ-law |

### Keep the whole pipeline at 8 kHz

`PipelineParams(audio_in_sample_rate=8000, audio_out_sample_rate=8000)`.

Pipecat's output transport resamples **before** the serializer sees the frame, so
the serializer only does an 8k→8k no-op. Leaving `audio_out_sample_rate` at the
service-native 24 kHz pushes resampling into the serializer, whose stream
resampler **returns empty on its first calls and silently drops frames** —
measured at 3 of the first 10 frames at 24 kHz, 1 of 10 at 16 kHz. The symptom is
a clipped first word.

### L16 instead of μ-law

Set `VOBIZ_ENCODING=audio/x-l16`. The serializer skips companding and resamples
linear PCM directly, byte-swapping per `VOBIZ_L16_ENDIAN`. RFC 2586 says
big-endian; some accounts transport little-endian. **If L16 frames arrive at the
right size but produce no transcripts, flip it to `le`.**

## Barge-in

```
caller interrupts
  → Pipecat emits InterruptionFrame
  → serializer sends  {"event":"clearAudio","streamId":…}
  → Vobiz discards queued audio
  → Vobiz replies     {"event":"clearedAudio","streamId":…}
```

Both appear in the dashboard's stream pane as a visible pair — the clearest
on-screen evidence that interruption handling works.

## Hangup

`VobizFrameSerializer` supports three strategies, configured via `auto_hang_up`:

| Method | Does |
|---|---|
| `ws_stop` | Sends `{"event":"stop"}`. Vobiz proceeds past `<Stream>` and hangs up with `HangupCause="End Of XML Instructions"` |
| `rest` | Fires a REST `DELETE` with `auth_id`/`auth_token` — works even if the socket is already dead |
| `both` | Both, REST as a safety net |

This is why a **remotely hosted bot needs its own Vobiz credentials**: the REST
half of hangup cannot be done over the WebSocket.

## Observed traffic shape

From a real call, as the dashboard rolls it up:

```
APP ← VOBIZ   media (audio in)    101 frames · 44,642 bytes · 2.0s
APP → VOBIZ   playAudio (out)      59 frames · 30,480 bytes · 2.0s
APP → VOBIZ   clearAudio           barge-in — discard queued audio
APP ← VOBIZ   clearedAudio         Vobiz confirmed audio cleared
```

Inbound is constant — Vobiz streams whether or not anyone speaks. Outbound is
bursty, only while the bot talks. **That asymmetry is normal**; equal rates in
both directions would suggest the bot is emitting silence.

## Pointing the stream at another server

The `<Stream>` body is just a string your server generates, so the media plane
can live anywhere:

```
ENV=production
VOBIZ_PROD_WS_URL=wss://other-host.example.com/ws
```

`get_websocket_url()` then returns that verbatim. `/answer`, transfers and
webhooks stay here; only audio moves.

The other server must speak this protocol: accept `start`, echo `streamId` on
everything outbound, match the negotiated format, and be publicly reachable over
TLS. Any server running `VobizFrameSerializer` works; a plain echo server does not.

Two consequences: the dashboard's stream pane goes empty (the tap only sees this
process), and `ENV=production` also appends
`serviceHost=<AGENT_NAME>.<ORGANIZATION_NAME>` — set both vars or it arrives as
`serviceHost=None.None`.

> Not yet verified on this account. The split is read from the code, not tested.
