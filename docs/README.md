# Vobiz × Pipecat — Documentation

A Vobiz phone call, bridged into a Pipecat AI voice agent, with every exchange
between this server and Vobiz visible in a live dashboard.

## Read in this order

| # | Document | What it answers |
|---|---|---|
| 1 | [Architecture](01-architecture.md) | The two planes, and why the split matters |
| 2 | [Call lifecycle](02-call-lifecycle.md) | One call, start to recording, step by step |
| 3 | [Files and functions](03-files-and-functions.md) | Every file, every function, what it does |
| 4 | [Streaming protocol](04-streaming-protocol.md) | The WebSocket wire format, and the audio path |
| 5 | [Transfers](05-transfers.md) | PSTN vs SIP, A-leg vs B-leg, SIP headers |
| 6 | [Dashboard](06-dashboard.md) | The inspector UI and the event bus behind it |
| 7 | [Deployment](07-deployment.md) | Render, CI/CD, Cloud Run, and the constraints |
| 8 | [Gotchas](08-gotchas.md) | Failures that cost real debugging time |

## The one-paragraph version

You place a call through the Vobiz REST API. When the callee answers, Vobiz
fetches your `answer_url` and executes whatever XML you return. This server
returns `<Stream>`, which makes Vobiz open a WebSocket back to it and stream the
call audio in both directions. That audio is handed to a Pipecat pipeline
running Gemini Live, so the caller talks to an AI. Mid-call, a REST request can
redirect either leg to a new XML document containing `<Dial>`, which bridges the
call to a human on a phone number or a SIP endpoint.

## Two ideas that explain most of the design

**The control plane and the media plane are separate.** HTTP and XML decide what
happens; a WebSocket carries the audio. They can live on different machines.
See [Architecture](01-architecture.md).

**Vobiz is driven by XML you generate per call.** There is no dashboard-configured
IVR. Every decision is a document your server returns at the moment it is asked,
which is why routing can be a live database lookup rather than a static tree.

## Quick reference

| | |
|---|---|
| Local | `python server.py` → `http://localhost:7860` |
| Dashboard | `/dashboard` |
| Deployed | https://vobiz-pipecat.onrender.com |
| Repo | https://github.com/Piyush-sahoo/Vobiz-X-Pipecat-1 |
| Stack | FastAPI · Pipecat 1.8.1 · pipecat-vobiz 0.0.3 · Gemini Live |
