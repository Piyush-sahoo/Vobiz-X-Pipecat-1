# 7. Deployment

## Live

| | |
|---|---|
| Service | https://vobiz-pipecat.onrender.com |
| Dashboard | https://vobiz-pipecat.onrender.com/dashboard |
| Answer URL | `https://vobiz-pipecat.onrender.com/answer` |
| Repo | https://github.com/Piyush-sahoo/Vobiz-X-Pipecat-1 |
| Region | Singapore — closest Render region to an Indian DID |

## The pipeline

```
git push origin master
        │
        ├──► GitHub Actions (.github/workflows/ci.yml)
        │       · install dependencies
        │       · import server, bot, events, dashboard, ws_tap
        │       · run 11 transfer / SIP-header tests
        │       · fail if .env or a literal token was committed
        │
        └──► Render (autoDeploy)
                · pip install -r requirements.txt
                · python server.py
```

Render redeploys on push by itself. Actions is the **gate in front of it** —
it catches a broken import before a bad commit reaches the service that answers
real phone calls.

## Configuration

Secrets live in two places, and neither is in the repo.

**GitHub Actions secrets** — `VOBIZ_AUTH_ID`, `VOBIZ_AUTH_TOKEN`,
`VOBIZ_PHONE_NUMBER`, `GOOGLE_API_KEY`, `RENDER_API_KEY`.

**Render environment** — the same four, plus:

| Variable | Value | Why |
|---|---|---|
| `PUBLIC_URL` | the service URL | The answer XML and `wss://` URL are built from it |
| `ENV` | `local` | **Not `production`** — see below |
| `BOT_MODE` | `realtime` | Gemini Live |
| `GEMINI_LIVE_MODEL` | `models/gemini-3.8-live` | Confirmed available on this key |
| `VOBIZ_ENCODING` / `VOBIZ_SAMPLE_RATE` | `audio/x-mulaw` / `8000` | Wire format |

### Why `ENV=local` in production

Counter-intuitive but correct. `ENV=production` makes `get_websocket_url()`
return `VOBIZ_PROD_WS_URL` — meant for a **separately hosted** bot. Here `bot.py`
runs in the same process, so the stream URL must be this service's own host,
which is what the `local` branch builds from `PUBLIC_URL`.

Set `ENV=production` only when deliberately moving the media plane elsewhere.

## Constraints

### Single instance, always

`active_calls` is an in-process dict shared by `/answer`, `/start`, the WebSocket
and `/initiate-transfer`. A second instance would serve `/answer` for a call
whose WebSocket lands on the other one. Session affinity does not help: it is
cookie-based, and Vobiz's WebSocket client sends no cookie.

Raising instance count requires moving that state to Redis or Firestore first.

### The free plan will drop calls

Render's free tier **spins down after ~15 minutes idle**, and a cold start takes
30–60 seconds. Vobiz times out fetching `/answer` and drops the call — the phone
rings, the caller hears it answer, then nothing. The service finishes waking
afterwards: warm, correct, too late.

Observed during setup: a request timed out at 20s, the next returned in 0.11s.

**Upgrade to `starter` before any demo.** It needs a card on the Render account;
`starter` returned `402 Payment information is required` during setup.

### Long timeouts for the WebSocket

A media stream can run for the whole call. Any platform default of 30–60 seconds
will sever it mid-conversation. Render handles this; on Cloud Run it is
`--timeout=3600`.

## Alternative: Cloud Run

`deploy.sh` and `Dockerfile` support it, parameterised by environment:

```bash
export GCP_PROJECT=your-project
export VOBIZ_AUTH_ID=MA_XXXXXXXX
export VOBIZ_PHONE_NUMBER=+10000000000
./deploy.sh
```

Non-default flags, and why:

| Flag | Reason |
|---|---|
| `--max-instances=1` | The `active_calls` constraint above |
| `--min-instances=1` | A cold start on `/answer` makes Vobiz time out |
| `--no-cpu-throttling` | Cloud Run throttles CPU between requests, starving the audio pipeline |
| `--timeout=3600` | The 5-minute default would sever the WebSocket |

> **Known blocker on `vobiz-dashboard-prod`.** The org policy
> `constraints/iam.allowedPolicyMemberDomains` restricts IAM members to the
> vobiz.ai customer ID, so `allUsers` cannot be granted `run.invoker`.
> `--allow-unauthenticated` **fails silently**: the deploy exits 0 and prints a
> URL, but the service returns 403 to everyone, including Vobiz. An external load
> balancer does not help — a serverless NEG still passes through Cloud Run IAM.
>
> Always curl the URL anonymously after deploying. The existing `vobiz-dg-agent`
> service has the same problem.

## Local development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp env.example .env          # fill in credentials
ngrok http 7860              # public tunnel
# set PUBLIC_URL to the ngrok URL, then:
python server.py
```

`.env` is read once at import, so **restart after changing `PUBLIC_URL`**.

**Check the tunnel before every test.** A dead tunnel gives a 404 on `answer_url`
and the call drops with nothing in your server log — indistinguishable from an
application bug:

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://<your-tunnel>/dashboard
```

## Verification checklist

After any deploy:

```bash
B=https://vobiz-pipecat.onrender.com
curl -s -o /dev/null -w "%{http_code}\n" $B/dashboard        # 200, and NOT 403
curl -s -X POST $B/answer -d "CallUUID=preflight"            # <Stream> with the right host
```

Confirm the `wss://` URL inside the XML points at the deployed host. Then place a
real call — HTTP health does not prove the media path.
