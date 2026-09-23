# 2. Call lifecycle

One outbound call, answered, streamed to the AI, transferred to a human, ended.
Every step names the file and line that runs it.

## Phase 1 — placing the call

**1. `POST /start`** — `server.py:337`

```bash
curl -X POST http://localhost:7860/start \
  -H "Content-Type: application/json" \
  -d '{"phone_number": "+91XXXXXXXXXX"}'
```

The handler resolves the public host, builds `answer_url` and `hangup_url` from
it, then calls `make_vobiz_call()`.

**2. `make_vobiz_call()`** — `server.py:145` — sends:

```json
POST https://api.vobiz.ai/api/v1/Account/{auth_id}/Call/
{ "to": "+91…", "from": "+91…",
  "answer_url": "https://…/answer",  "answer_method": "POST",
  "hangup_url": "https://…/hangup",  "hangup_method": "POST" }
```

Vobiz replies `201` with a `request_uuid`. **That value is the A-leg identifier**
and arrives as `CallUUID` in every later webhook. The handler pre-registers it in
`active_calls` so a transfer can be requested before any webhook has arrived.

> Without `hangup_url`, Vobiz never reports the call ending. It is optional to
> the API and essential in practice.

## Phase 2 — the answer webhook

**3. Callee answers → Vobiz POSTs `answer_url`** — `server.py:429`

`/answer` does three things:

1. Reads the form payload (`_webhook_payload()` `:41`) and records it
2. Checks `active_calls[CallUUID]["transfer_requested"]` — if set, returns
   `<Dial>` instead (see Phase 5)
3. Otherwise returns the streaming document

```xml
<Response>
    <Record fileFormat="wav" maxLength="3600" recordSession="true"
            callbackUrl="https://…/recording-ready" callbackMethod="POST"/>
    <Stream bidirectional="true" audioTrack="inbound"
            contentType="audio/x-mulaw;rate=8000" keepCallAlive="true">
        wss://…/ws
    </Stream>
</Response>
```

| Attribute | Why |
|---|---|
| `bidirectional="true"` | Without it the bot can hear but not speak |
| `keepCallAlive="true"` | Stops Vobiz hanging up when the XML document ends |
| `contentType` | A *hint*; the `start` event is authoritative |

## Phase 3 — the media stream

**4. Vobiz opens the WebSocket** — `server.py:1095`, handler at `:974`

Four paths (`/ws`, `/`, `/voice/ws`, `/stream`) all reach `handle_vobiz_websocket()`,
so XML pointing at any of them works. It accepts the socket, decodes the optional
base64 `body` parameter, and wraps the socket in `WebSocketTap`.

**5. `bot.py:208` — `bot()`** calls `parse_vobiz_start()`, which reads exactly one
message:

```json
{"event": "start",
 "start": {"streamId": "…", "callId": "…",
           "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000}}}
```

It builds `VobizFrameSerializer` from those values and hands it to
`FastAPIWebsocketTransport`.

**6. `bot.py:145` — `run_bot()`** assembles the pipeline and runs it. On
`on_client_connected` it queues an `LLMRunFrame` so the agent speaks first —
without it the callee hears silence and usually hangs up.

Audio then flows continuously until either side disconnects.
See [Streaming protocol](04-streaming-protocol.md).

## Phase 4 — the conversation

```
Vobiz ──"media" (μ-law 8k, base64)──► serializer ──PCM──► Gemini Live
Vobiz ◄──"playAudio" (μ-law 8k)────── serializer ◄─PCM─── Gemini Live
```

Barge-in: when the caller interrupts, Pipecat emits an `InterruptionFrame`, the
serializer sends `clearAudio`, and Vobiz replies `clearedAudio`. Both appear in
the dashboard's stream pane.

## Phase 5 — transfer to a human

**7. `POST /initiate-transfer`** — `server.py:732`

```json
{"call_uuid": "…", "legs": "aleg", "type": "sip",
 "destination": "agent@registrar.vobiz.ai",
 "sip_headers": "X-VH-Ref=abc123"}
```

Validates the leg and destination, stores them on `active_calls`, then POSTs to
Vobiz:

```json
POST /api/v1/Account/{auth_id}/Call/{call_uuid}/
{"legs": "aleg", "aleg_url": "https://…/transfer-to-human?…", "aleg_method": "POST"}
```

Vobiz returns `202 Accepted`.

**8. Vobiz fetches the transfer URL** — `server.py:695`

A transfer is a **redirect, not a bridge**. The leg abandons its current document
— the `<Stream>` ends here — and executes whatever comes back:

```xml
<Response>
    <Speak>Please hold while I transfer you.</Speak>
    <Dial action="…/dial-complete" callbackUrl="…/dial-events"
          timeout="30" timeLimit="3600" callerId="+91…">
        <Number>+91…</Number>          <!-- or <User>sip:…</User> -->
    </Dial>
    <Speak>The transfer could not be completed. Goodbye.</Speak>
    <Hangup/>
</Response>
```

Elements **after** `</Dial>` run only if the bridge never happens — the natural
place for no-answer handling.

## Phase 6 — the end

Webhook order for a transferred call:

```
1.  answer_url          Event = StartApp
2.  Record action       Event = Record
3.  ── Transfer API called ──
4.  aleg_url            (standard call parameters)
5.  Dial callbackUrl    Event = DialAnswer      ← first sight of the B-leg UUID
6.  Dial callbackUrl    Event = DialConnected
7.  Dial callbackUrl    Event = DialHangup
8.  hangup_url          Event = Hangup
9.  Dial action         Event = Redirect
10. Record callbackUrl  Event = RecordStop      ← recording downloadable now
```

Two properties that surprise people:

- **`hangup_url` fires once, for the A-leg only.** The transferred leg produces no
  hangup webhook of its own. An integration listening only on `hangup_url` never
  sees the B-leg — which is why `<Dial callbackUrl>` is not optional.
- **The recording is not available until after hangup.** `/recording-ready`
  (`server.py:628`) downloads it with `X-Auth-ID` / `X-Auth-Token` into
  `recordings/`.

## Verifying a call actually worked

A clean handshake does not prove the agent spoke. The recording is stereo, one
leg per channel — check both:

```
ch0 (caller)  .######...######..#     6.5s
ch1 (agent)   ......###########..     5.5s
```

Audio on ch1 is the proof the bot was heard.
