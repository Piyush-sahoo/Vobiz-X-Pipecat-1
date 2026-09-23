# 5. Transfers

Moving a live call to a human — on a phone number or a registered SIP endpoint.

## The mental model

**A transfer is a redirect, not a bridge.**

The Transfer API does not connect anyone to anyone. It tells one leg to abandon
the XML document it is executing and fetch a new one. Bridging happens only
because the new document contains `<Dial>`.

```
1. POST /Call/{call_uuid}/  {"legs":"aleg","aleg_url":"…"}   → 202 Accepted
2. That leg discards its current document — the <Stream> ends here
3. Vobiz POSTs aleg_url with the standard call parameters
4. The XML you return becomes the leg's new document, immediately
```

## Legs

| `legs` | Redirects | The other leg |
|---|---|---|
| `aleg` | The caller | Keeps running its current flow |
| `bleg` | The callee / agent | Keeps running its current flow |

Vobiz also accepts `both`; this project deliberately does not expose it.

Pass the **A-leg UUID** in the path either way — it is the `request_uuid` the
Call API returned, and the `CallUUID` in every webhook. `legs` selects which side
of that call gets redirected.

## Two destination types

Set by `type` on `/initiate-transfer`; the difference is one element inside `<Dial>`.

### PSTN — a phone number

```xml
<Dial callerId="+91…" callbackUrl="…/dial-events" action="…/dial-complete"
      timeout="30" timeLimit="3600">
    <Number>+919XXXXXXXXX</Number>
</Dial>
```

### SIP — a registered endpoint

```xml
<Dial callerId="+91…" callbackUrl="…/dial-events" action="…/dial-complete"
      timeout="30" timeLimit="3600" sipHeaders="X-VH-Ref=abc123">
    <User sipHeaders="X-VH-Ref=abc123">sip:agent@registrar.vobiz.ai</User>
</Dial>
```

`build_transfer_xml()` (`server.py:104`) adds the `sip:` scheme if missing.

> **Calling a SIP endpoint is different from transferring to one.** To *place* a
> call to a SIP URI, put it in the `to` field of the Call API — no `<Dial>` needed.
> `<Dial><User>` is only for bridging an already-running call.

## Why `callbackUrl` is not optional

`hangup_url` fires **once per call, for the A-leg only**. The transferred B-leg
produces no hangup webhook of its own.

`<Dial callbackUrl>` is therefore the **only** webhook that reports the B-leg's
identity and outcome:

| Event | `DialAction` | First carries |
|---|---|---|
| `DialAnswer` | `answer` | `DialBLegUUID` — earliest sight of the B-leg |
| `DialConnected` | `connected` | The session envelope, `From`/`To`/`CallStatus` |
| `DialHangup` | `hangup` | Duration, cost, hangup cause, timings |

Omit it and a transferred call looks like it vanished.

## Elements after `</Dial>`

They run **only if the dial does not result in a bridge** — the natural place for
no-answer handling:

```xml
    </Dial>
    <Speak>The transfer could not be completed. Goodbye.</Speak>
    <Hangup/>
```

## SIP headers

Custom metadata carried to the SIP endpoint and echoed in the Dial callbacks.
Set on both `<Dial>` and `<User>`.

### The format rule

**Keys must start with `X-VH-`.** Key stem and value must both be alphanumeric.

```
X-VH-Ref=abc123,X-VH-Clinic=alpha     accepted
Ref=abc123                            rejected
X-VH-Note=needs help                  rejected — space in the value
```

Because stem and value must be alphanumeric, **free text cannot be carried**. No
customer name, no AI summary. The supported pattern is an opaque reference id
plus a server-side lookup by the receiving client.

`validate_sip_headers()` (`server.py:64`) **warns rather than blocks**, and the
dashboard surfaces the warnings, so a rejected set does not read as success.

> **Conflict in the source docs.** `VOBIZ_DOCS_CORRECTIONS.md` §3.2 states the
> opposite — that keys must *end* with `X-VH` and `X-VH-` is only the arrival
> form. That document is wrong on this point; the prefix form above is what the
> platform accepts. Worth fixing before that doc reaches the docs team.

## API

```bash
curl -X POST http://localhost:7860/initiate-transfer \
  -H "Content-Type: application/json" \
  -d '{
    "call_uuid":   "3bd0d027-…",
    "legs":        "aleg",
    "type":        "sip",
    "destination": "agent@registrar.vobiz.ai",
    "sip_headers": "X-VH-Ref=abc123"
  }'
```

| Field | Required | Values |
|---|---|---|
| `call_uuid` | yes | The A-leg UUID |
| `legs` | no | `aleg` (default), `bleg` |
| `type` | no | `pstn` (default), `sip` |
| `destination` | yes* | Number or SIP URI. *Falls back to `TRANSFER_AGENT_NUMBER` / `TRANSFER_SIP_ENDPOINT` |
| `sip_headers` | no | `X-VH-Key=value,…` |

The destination rides along as query parameters on the transfer URL, so the
redirect is self-describing even if `active_calls` has been lost.

## Status

| Path | State |
|---|---|
| A-leg → PSTN | **Verified on a live call** — full webhook sequence observed |
| A-leg → SIP | Not yet exercised |
| B-leg → either | Not yet exercised |

Before demoing SIP, read the JsSIP User-Agent trap in [Gotchas](08-gotchas.md) —
it fails silently and looks like anything but what it is.
