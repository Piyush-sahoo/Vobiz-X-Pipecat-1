"""mock_customer_agent.py — stand in for a customer's own voice agent.

Run this to rehearse the "customer's agent" path without needing the customer.
It is the minimum a third-party server must implement to receive a Vobiz
<Stream>, and it prints what it sees so you can confirm the format matches
what you advertised.

    python mock_customer_agent.py            # ws://localhost:7870/ws
    ngrok http 7870                          # to reach it from Vobiz

Then in the dashboard choose "Customer's agent" and paste the wss:// URL.

What a real customer's server must do, all demonstrated below:

  1. Accept the WebSocket and read the `start` event for streamId and callId.
  2. Echo that streamId on EVERY outbound message. This is the step people
     miss; without it Vobiz ignores the audio and there is no error.
  3. Speak the negotiated encoding and sample rate from `start.mediaFormat`,
     which is authoritative over whatever <Stream contentType> advertised.
  4. Have its own Vobiz credentials if it wants to hang up over REST.

This mock echoes the caller's audio back so you hear yourself — proof the
round trip works end to end, with no AI involved.
"""

import argparse
import asyncio
import json

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
import uvicorn

app = FastAPI()


@app.websocket("/ws")
async def agent(ws: WebSocket):
    await ws.accept()
    print("\n[MOCK] WebSocket accepted")

    stream_id = None
    frames_in = 0
    frames_out = 0

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                print(f"[MOCK] non-JSON frame, {len(raw)} bytes")
                continue

            event = msg.get("event")

            if event == "start":
                start = msg.get("start") or {}
                fmt = start.get("mediaFormat") or {}
                stream_id = start.get("streamId")
                print(f"[MOCK] start  streamId={stream_id}")
                print(f"[MOCK]        callId={start.get('callId')}")
                print(f"[MOCK]        encoding={fmt.get('encoding')} "
                      f"rate={fmt.get('sampleRate')}   <-- the authoritative format")

            elif event == "media":
                frames_in += 1
                # Echo it straight back. Same encoding and rate as received, and
                # crucially the streamId from `start`.
                payload = (msg.get("media") or {}).get("payload")
                if payload and stream_id:
                    await ws.send_text(json.dumps({
                        "event": "playAudio",
                        "media": {
                            "contentType": (msg.get("media") or {}).get("contentType",
                                                                        "audio/x-mulaw"),
                            "sampleRate": (msg.get("media") or {}).get("sampleRate", 8000),
                            "payload": payload,
                        },
                        "streamId": stream_id,
                    }))
                    frames_out += 1
                if frames_in % 100 == 0:
                    print(f"[MOCK] {frames_in} in / {frames_out} out")

            elif event == "dtmf":
                print(f"[MOCK] dtmf  {(msg.get('dtmf') or {}).get('digit')}")

            elif event in ("playedStream", "clearedAudio"):
                print(f"[MOCK] {event}  {msg.get('name') or msg.get('streamId')}")

            else:
                print(f"[MOCK] {event}")

    except WebSocketDisconnect:
        print(f"[MOCK] closed — {frames_in} frames in, {frames_out} echoed back\n")
    except Exception as e:
        print(f"[MOCK] error: {e}")


@app.get("/")
async def health():
    return {"status": "mock customer agent", "ws": "/ws"}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=7870)
    args = ap.parse_args()
    print(f"Mock customer agent on ws://localhost:{args.port}/ws")
    print("Expose it with:  ngrok http", args.port)
    uvicorn.run(app, host="0.0.0.0", port=args.port)
