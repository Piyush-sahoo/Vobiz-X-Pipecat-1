# 8. Gotchas

Failures that cost real debugging time. Every one of these is silent — the
symptom never points at the cause.

---

## A space in a SIP User-Agent kills `<Dial><User>`

**Symptom:** registration succeeds, the caller's phone rings, they answer, they
hear ringback, the call ends. `DialStatus=completed`, `DialRingStatus=true`,
`DialBLegUUID` **empty**, hangup cause `End Of XML Instructions` (4010).

**Cause:** Vobiz stores the User-Agent from the SIP REGISTER and later
interpolates it, unescaped, into a semicolon-delimited gateway URI. A space makes
the URI unparseable and Kamailio drops the INVITE:

```
ERROR: pv [pv_trans.c:1547]: tr_eval_uri(): invalid uri
  [...;user_agent=JsSIP 3.10.1;registrarip=...]
INVITE|blocking gw: ...
```

JsSIP's default User-Agent is `JsSIP 3.10.1` — the space is the whole problem.

**Fix:** set a space-free `user_agent` on the JsSIP UA, e.g.
`user_agent: "VobizDemo/1.0"`, and **re-register** — the registrar keeps the old
entry until the client REGISTERs over it.

Nothing anywhere says the INVITE was blocked except the platform's own logs.
Search `service:vobiz-outboundsip` for the SIP username and look for
`blocking gw` / `tr_eval_uri(): invalid uri` before suspecting anything else.

---

## A dead answer URL looks exactly like a broken app

**Symptom:** the Call API returns 201, the phone rings, the call drops
immediately. **No `/answer` request in your server log.**

**Cause:** Vobiz could not reach `answer_url`. A stopped ngrok tunnel returns
ngrok's own 404 page; the call has no XML to execute and ends.

**Fix:** check the tunnel before blaming code:

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://<tunnel>/dashboard
```

`200` ready · `404` tunnel down.

The tell is the absence of a log line, not the presence of an error.

---

## `--allow-unauthenticated` fails silently on Cloud Run

**Symptom:** deploy exits 0, prints a Service URL, and the service 403s everyone.

**Cause:** the org policy `constraints/iam.allowedPolicyMemberDomains` restricts
IAM members to the organisation's customer ID, so `allUsers` cannot be granted
`run.invoker`. The only sign is a `Setting IAM policy failed` line at the end of
an otherwise successful deploy.

**Fix:** a project-scoped exception to that constraint, which needs
`roles/orgpolicy.policyAdmin` — `roles/owner` and `organizationAdmin` are not
enough. An external load balancer is **not** a workaround: with a serverless NEG
the request still passes through Cloud Run IAM.

**Always verify anonymously after deploying.** The existing `vobiz-dg-agent`
service has the same problem and has likely never received a webhook.

---

## `hangup_url` never covers the B-leg

**Symptom:** a transferred call appears to vanish. No duration, no outcome for
the leg you transferred to.

**Cause:** `hangup_url` fires **once per call, for the A-leg only**.

**Fix:** set `callbackUrl` on `<Dial>`. It is the only webhook reporting the
B-leg's identity and outcome, and `DialAnswer` is the earliest point the B-leg
UUID exists.

---

## A resampling serializer drops the first frames

**Symptom:** the agent's first word is clipped.

**Cause:** leaving `audio_out_sample_rate` at the service-native rate (24 kHz for
Gemini Live) pushes resampling into the serializer, whose stream resampler
returns empty on its first calls and silently drops frames — measured at 3 of the
first 10 at 24 kHz, 1 of 10 at 16 kHz.

**Fix:** set `audio_out_sample_rate=8000`. Pipecat resamples before the serializer
sees the frame, leaving it an 8k→8k no-op.

---

## VAD on the transport is silently ignored

**Symptom:** no turn detection. The bot talks over the caller or never yields.

**Cause:** under Pipecat 1.x, `vad_analyzer=` on the transport params is dropped
by Pydantic. No error, no warning.

**Fix:** pass it on `LLMUserAggregatorParams` instead.

---

## Nothing runs the LLM until a frame asks it to

**Symptom:** dead air after answer. On an outbound call the callee usually hangs
up before speaking.

**Fix:** queue an `LLMRunFrame()` on `on_client_connected` so the agent greets
first.

---

## A successful handshake does not mean the agent spoke

**Symptom:** logs show `start`, `Call connected`, a clean hangup — and the caller
heard nothing.

**Fix:** the recording is stereo, one leg per channel. Check **both**:

```
ch0 (caller)  .######...######..#     6.5s
ch1 (agent)   ......###########..     5.5s
```

Audio on ch1 is the proof. Inbound-only traffic means the pipeline ran but never
produced speech.

---

## Render's free tier drops calls

**Symptom:** the phone rings, answers, then dies — intermittently, usually after
a quiet period.

**Cause:** free instances spin down after ~15 minutes. A cold start takes 30–60s;
Vobiz times out fetching `/answer` first.

**Fix:** upgrade to `starter`. Requires a card on the account.

---

## SIP header keys must start with `X-VH-`

**Symptom:** headers never arrive. No error.

**Fix:** `X-VH-Ref=abc123`, not `Ref=abc123`. Key stem and value must both be
alphanumeric, so **free text cannot be carried** — send an opaque id and look it
up on the receiving side.

> `VOBIZ_DOCS_CORRECTIONS.md` §3.2 asserts the opposite (keys must *end* with
> `X-VH`). That document is wrong on this point.

---

## Account state masquerades as code bugs

Before debugging a failed outbound call, probe the Call endpoint with a
deliberately invalid 17-digit `to`:

| Response | Means |
|---|---|
| `401` | Bad credentials |
| `402` | Insufficient balance |
| `400 failed getting country code` | **Auth and DID ownership are both fine** — the problem is elsewhere |

Accounts are not interchangeable: an account rejects a `from` number it does not
own.

---

## `sip_registered` is cosmetic

The field never flips true. It is not evidence of anything. A dead answer URL is
the usual real cause of failed WebRTC calls.
