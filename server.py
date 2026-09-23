

"""server.py

Webhook server to handle outbound call requests, initiate calls via Vobiz API,
and handle subsequent WebSocket connections for Media Streams.
"""

import base64
import json
import os
import urllib.parse
from contextlib import asynccontextmanager
from html import escape
from datetime import datetime

import aiohttp
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

import events
from dashboard import DASHBOARD_HTML
from ws_tap import WebSocketTap

load_dotenv(override=True)


# ----------------- ACTIVE CALLS TRACKING ----------------- #

# Dictionary to store active call information
# In production, use Redis or a database instead of in-memory dict
active_calls = {}


# ----------------- HELPERS ----------------- #


async def _webhook_payload(request: Request) -> dict:
    """Read a Vobiz webhook body regardless of how it was encoded.

    Vobiz posts form-encoded data; query params carry the rest. Reading a body
    must never raise here, or a malformed webhook would break call handling.
    """
    data = {}
    try:
        form = await request.form()
        data.update({k: str(v) for k, v in form.items()})
    except Exception:
        pass
    if not data:
        try:
            body = await request.json()
            if isinstance(body, dict):
                data.update(body)
        except Exception:
            pass
    data.update({k: v for k, v in request.query_params.items() if k not in data})
    return data


def validate_sip_headers(raw: str):
    """Check a `Key=value,Key2=value2` sipHeaders string against Vobiz's rules.

    Returns (cleaned_string, [warnings]). Never raises and never drops pairs —
    the caller decides what to do.

    Keys must START with the `X-VH-` prefix: `X-VH-Ref=abc123`. A key without it
    is rejected. Key stem and value must both be alphanumeric, so free text
    cannot be carried — send an opaque id and look it up on the receiving side.

    NOTE: VOBIZ_DOCS_CORRECTIONS.md 3.2 claims the reverse (that the key must
    END with `X-VH` and `X-VH-` is only the arrival form). That document is
    wrong on this point; the prefix form here is what Vobiz accepts.
    """
    warnings = []
    pairs = []
    for chunk in (raw or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            warnings.append(f"'{chunk}' is not key=value — ignored")
            continue
        key, value = (p.strip() for p in chunk.split("=", 1))
        if not key.upper().startswith("X-VH-"):
            warnings.append(
                f"key '{key}' does not start with 'X-VH-' — Vobiz rejects it"
            )
        stem = key[5:] if key.upper().startswith("X-VH-") else key
        if not stem.isalnum():
            warnings.append(f"key stem '{stem}' is not alphanumeric — punctuation is rejected")
        if not value.isalnum():
            warnings.append(
                f"value '{value}' is not alphanumeric — free text cannot ride in a SIP "
                "header; send an opaque id and look it up on the receiving side"
            )
        pairs.append(f"{key}={value}")
    return ",".join(pairs), warnings


def build_transfer_xml(dest_type: str, destination: str, host: str, protocol: str,
                       sip_headers: str = None) -> str:
    """Build the <Dial> document that bridges a live call to a human.

    Two destination types, and the element inside <Dial> is what differs:

      pstn -> <Number>+91...</Number>      a phone number
      sip  -> <User>sip:x@domain</User>    a registered SIP endpoint

    callbackUrl is not optional for the demo: per Vobiz's webhook sequence it is
    the ONLY callback that reports the B-leg's identity and outcome. hangup_url
    fires for the A-leg only, so without this the transferred leg is invisible.
    Elements after </Dial> run only when the bridge does not happen, which makes
    them the natural place for no-answer handling.
    """
    # sipHeaders is set on <Dial> (confirmed) and, for a SIP destination, also on
    # <User> so it reaches the endpoint itself.
    hdr_attr = f' sipHeaders="{escape(sip_headers, quote=True)}"' if sip_headers else ""

    if dest_type == "sip":
        target = destination if destination.startswith("sip:") else f"sip:{destination}"
        inner = f"<User{hdr_attr}>{escape(target)}</User>"
    else:
        inner = f"<Number>{escape(destination)}</Number>"

    caller_id = os.getenv("VOBIZ_PHONE_NUMBER", "")
    caller_attr = f' callerId="{caller_id}"' if caller_id else ""

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Speak voice="WOMAN" language="en-US">Please hold while I transfer you.</Speak>
    <Dial action="{protocol}://{host}/dial-complete" method="POST"
          callbackUrl="{protocol}://{host}/dial-events" callbackMethod="POST"
          timeout="30" timeLimit="3600"{caller_attr}{hdr_attr}>
        {inner}
    </Dial>
    <Speak voice="WOMAN" language="en-US">The transfer could not be completed. Goodbye.</Speak>
    <Hangup/>
</Response>"""


async def make_vobiz_call(
    session: aiohttp.ClientSession, to_number: str, from_number: str, answer_url: str,
    hangup_url: str = None
):
    """Make an outbound call using Vobiz's REST API."""
    print("\n[DEBUG] ========== VOBIZ API CALL START ==========")

    auth_id = os.getenv("VOBIZ_AUTH_ID")
    auth_token = os.getenv("VOBIZ_AUTH_TOKEN")

    if not auth_id:
        raise ValueError("Missing Vobiz Auth ID (VOBIZ_AUTH_ID)")

    if not auth_token:
        raise ValueError("Missing Vobiz Auth Token (VOBIZ_AUTH_TOKEN)")

    print(f"[DEBUG] Auth ID: {auth_id}")
    # Log only the last 4 chars so we can tell tokens apart in logs without
    # leaking ~50% of the secret. Drop the whole line if even that is too much.
    print(f"[DEBUG] Auth Token: …{auth_token[-4:]}")

    headers = {
        "Content-Type": "application/json",
        "X-Auth-ID": auth_id,
        "X-Auth-Token": auth_token,
    }

    data = {
        "to": to_number,
        "from": from_number,
        "answer_url": answer_url,
        "answer_method": "POST",
    }
    # Without hangup_url Vobiz never tells us the call ended, so the dashboard
    # would show a call that starts and never finishes.
    if hangup_url:
        data["hangup_url"] = hangup_url
        data["hangup_method"] = "POST"

    url = f"https://api.vobiz.ai/api/v1/Account/{auth_id}/Call/"

    events.record("out", "POST /Call/ (make call)", {**data, "endpoint": url},
                  kind="rest", note=f"dial {to_number} from {from_number}")

    print(f"[DEBUG] API URL: {url}")
    print(f"[DEBUG] Request Headers: {headers}")
    print(f"[DEBUG] Request Body: {json.dumps(data, indent=2)}")
    print(f"[DEBUG] Answer URL being sent: {answer_url}")

    try:
        async with session.post(url, headers=headers, json=data) as response:
            response_text = await response.text()
            print(f"[DEBUG] Response Status: {response.status}")
            print(f"[DEBUG] Response Body: {response_text}")

            if response.status != 201:
                print(f"[ERROR] Vobiz API call failed!")
                print(f"[ERROR] Status: {response.status}")
                print(f"[ERROR] Response: {response_text}")
                raise Exception(f"Vobiz API error ({response.status}): {response_text}")

            result = json.loads(response_text)
            # The Call API returns the id as `request_uuid`, which is the same
            # value Vobiz later sends back as CallUUID. Without mapping it here
            # the dashboard's UUID picker never learns the real call, so the
            # transfer controls have nothing usable to target.
            events.record("in", f"Vobiz REST response {response.status}", result,
                          call_uuid=result.get("request_uuid"),
                          kind="rest", note="call accepted")
            print(f"[SUCCESS] Vobiz API call successful!")
            print(f"[SUCCESS] Call UUID: {result.get('call_uuid', 'N/A')}")
            print("[DEBUG] ========== VOBIZ API CALL END ==========\n")
            return result

    except Exception as e:
        print(f"[ERROR] Exception during Vobiz API call: {e}")
        print(f"[ERROR] Exception type: {type(e).__name__}")
        import traceback
        print(f"[ERROR] Traceback:\n{traceback.format_exc()}")
        print("[DEBUG] ========== VOBIZ API CALL END (WITH ERROR) ==========\n")
        raise


def get_host_and_protocol(request: Request = None):
    """Get host and protocol, prioritizing PUBLIC_URL environment variable.

    Returns:
        tuple: (host, protocol)
    """
    public_url = os.getenv("PUBLIC_URL")

    if public_url:
        # Use configured public URL
        print(f"[INFO] Using PUBLIC_URL from environment: {public_url}")
        # Extract host and protocol from PUBLIC_URL
        if public_url.startswith("https://"):
            protocol = "https"
            host = public_url.replace("https://", "").rstrip("/")
        elif public_url.startswith("http://"):
            protocol = "http"
            host = public_url.replace("http://", "").rstrip("/")
        else:
            # No protocol specified, assume https
            protocol = "https"
            host = public_url.rstrip("/")
        print(f"[INFO] Extracted - Host: {host}, Protocol: {protocol}")
        return host, protocol
    else:
        # Fall back to request headers
        if request is None:
            raise ValueError("Request object required when PUBLIC_URL is not set")

        host = request.headers.get("host")
        if not host:
            raise ValueError("Cannot determine server host from request headers")

        print(f"[DEBUG] Host from request headers: {host}")

        # Detect protocol
        # Check X-Forwarded-Proto header (set by ngrok/proxies) or scheme
        forwarded_proto = request.headers.get("x-forwarded-proto", "")
        if forwarded_proto:
            protocol = forwarded_proto
        else:
            # Fall back to checking if host looks like localhost
            protocol = (
                "http"
                if host.startswith("localhost") or host.startswith("127.0.0.1")
                else "https"
            )

        # Warn if using localhost without PUBLIC_URL set
        if host.startswith("localhost") or host.startswith("127.0.0.1"):
            print("[WARNING] ⚠️  Using localhost for URL!")
            print("[WARNING] ⚠️  Vobiz will NOT be able to reach this URL!")
            print("[WARNING] ⚠️  Solution: Set PUBLIC_URL in .env")

        print(f"[DEBUG] Detected protocol: {protocol}")
        return host, protocol


def get_websocket_url(host: str):
    """Construct WebSocket URL for Vobiz Stream XML.

    """
    env = os.getenv("ENV", "local").lower()

    if env == "production":
        # Production WebSocket endpoint, configured via env var. Set this
        # to the public wss:// URL where your bot is reachable (e.g. a
        # Pipecat Cloud agent endpoint or your own deployment).
        prod_ws_url = os.getenv("VOBIZ_PROD_WS_URL")
        if not prod_ws_url:
            raise ValueError(
                "ENV=production but VOBIZ_PROD_WS_URL is not set. "
                "Set it to the wss:// URL where your bot is hosted."
            )
        return prod_ws_url
    else:
        # Return WebSocket URL for local/ngrok deployment
        return f"wss://{host}/ws"


# ----------------- API ----------------- #


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Replay any events captured before the last restart, so the dashboard is
    # not blank after the process is restarted or killed.
    restored = events.load_from_log()
    if restored:
        print(f"[EVENTS] restored {restored} events from {events.EVENT_LOG}")

    # Create aiohttp session for Vobiz API calls
    app.state.session = aiohttp.ClientSession()
    yield
    # Close session when shutting down
    await app.state.session.close()


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow all origins for testing
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post("/start")
async def initiate_outbound_call(request: Request) -> JSONResponse:
    """Handle outbound call request and initiate call via Vobiz."""
    print("Received outbound call request")

    try:
        data = await request.json()

        # Validate request data
        if not data.get("phone_number"):
            raise HTTPException(
                status_code=400, detail="Missing 'phone_number' in the request body"
            )

        # Extract the phone number to dial
        phone_number = str(data["phone_number"])

        # Extract body data if provided
        body_data = data.get("body", {})
        print(f"\n[INFO] Processing outbound call to {phone_number}")
        print(f"[DEBUG] Body data: {body_data}")

        # Get server URL for answer URL using helper function
        host, protocol = get_host_and_protocol(request)

        # Add body data as query parameters to answer URL
        answer_url = f"{protocol}://{host}/answer"
        if body_data:
            body_json = json.dumps(body_data)
            body_encoded = urllib.parse.quote(body_json)
            answer_url = f"{answer_url}?body_data={body_encoded}"

        print(f"[INFO] Answer URL that will be sent to Vobiz: {answer_url}")

        # Get the from number (optional - can be provided in request body)
        from_number = data.get("from_number") or os.getenv("VOBIZ_PHONE_NUMBER")
        print(f"[DEBUG] From number: {from_number}")

        if not from_number:
            print("[ERROR] VOBIZ_PHONE_NUMBER not set in environment and 'from_number' not provided in request")
            raise HTTPException(
                status_code=400,
                detail="Either set VOBIZ_PHONE_NUMBER in .env or provide 'from_number' in request body"
            )

        # Initiate outbound call via Vobiz
        try:
            print(f"[INFO] Initiating Vobiz API call...")
            call_result = await make_vobiz_call(
                session=request.app.state.session,
                to_number=phone_number,
                from_number=from_number,
                answer_url=answer_url,
                hangup_url=f"{protocol}://{host}/hangup",
            )

            # Extract call UUID from Vobiz response
            call_uuid = call_result.get("request_uuid") or call_result.get("call_uuid") or "unknown"
            print(f"[SUCCESS] Call initiated successfully! Call UUID: {call_uuid}")

            # Pre-create entry in active_calls for transfer tracking
            # This allows /answer to check transfer state using CallUUID from Vobiz
            if call_uuid and call_uuid != "unknown":
                active_calls[call_uuid] = {
                    "status": "initiated",
                    "started_at": datetime.now().isoformat(),
                    "transfer_requested": False,
                    "websocket": None
                }
                print(f"[CALL] Pre-registered call {call_uuid} in active_calls")

        except Exception as e:
            print(f"[ERROR] Failed to initiate Vobiz call: {e}")
            import traceback
            print(f"[ERROR] Full traceback:\n{traceback.format_exc()}")
            raise HTTPException(status_code=500, detail=f"Failed to initiate call: {str(e)}")

    except HTTPException:
        raise
    except Exception as e:
        print(f"Unexpected error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

    return JSONResponse(
        {
            "call_uuid": call_uuid,
            "status": "call_initiated",
            "phone_number": phone_number,
        }
    )


@app.api_route("/answer", methods=["GET", "POST"])
async def get_answer_xml(
    request: Request,
    CallUUID: str = Query(None, description="Vobiz call UUID"),
    body_data: str = Query(None, description="JSON encoded body data"),
) -> HTMLResponse:
    """Return XML instructions for connecting call to WebSocket or transferring to human."""
    print("\n[ANSWER] ========== ANSWER XML REQUEST ==========")
    print(f"[ANSWER] Call UUID: {CallUUID}")

    # Vobiz POSTs the call envelope as form data. The endpoint only needs
    # CallUUID to work, but the full payload is what makes the dashboard useful.
    inbound = await _webhook_payload(request)
    CallUUID = CallUUID or inbound.get("CallUUID")
    events.record("in", "answer_url (StartApp)", inbound, call_uuid=CallUUID)

    # Parse body data from query parameter
    parsed_body_data = {}
    if body_data:
        try:
            parsed_body_data = json.loads(body_data)
        except json.JSONDecodeError:
            print(f"[ANSWER] Failed to parse body data: {body_data}")

    # Check if this call is marked for transfer
    if CallUUID and CallUUID in active_calls:
        call_info = active_calls[CallUUID]
        if call_info.get("transfer_requested"):
            print(f"[ANSWER] 🔄 Call {CallUUID} is marked for transfer - returning Dial XML")

            # Destination is whatever /initiate-transfer recorded for this call;
            # env vars are only the fallback default.
            dest_type = call_info.get("transfer_type") or os.getenv("TRANSFER_TYPE", "pstn")
            destination = call_info.get("transfer_destination") or (
                os.getenv("TRANSFER_SIP_ENDPOINT") if dest_type == "sip"
                else os.getenv("TRANSFER_AGENT_NUMBER")
            )
            if not destination:
                raise HTTPException(
                    status_code=500,
                    detail=f"No transfer destination for type '{dest_type}'. Set it in the "
                           "request, or TRANSFER_AGENT_NUMBER / TRANSFER_SIP_ENDPOINT in .env",
                )

            host, protocol = get_host_and_protocol(request)
            xml_content = build_transfer_xml(
                dest_type, destination, host, protocol,
                call_info.get("transfer_sip_headers", ""))
            events.record_xml(f"XML -> Vobiz: <Dial> transfer ({dest_type})",
                              xml_content, call_uuid=CallUUID)

            print(f"[ANSWER] Transferring to: {destination} ({dest_type})")
            print(f"[ANSWER] Returning Dial XML")
            print("[ANSWER] ========== ANSWER XML END (TRANSFER) ==========\n")

            # Clean up transfer flag
            call_info["transfer_requested"] = False
            call_info["status"] = "transferred"

            return HTMLResponse(content=xml_content, media_type="application/xml")

    # Normal flow: Return Stream XML for bot conversation
    print(f"[ANSWER] Normal call flow - returning Stream XML")

    # Log call details
    if CallUUID:
        if parsed_body_data:
            print(f"[ANSWER] Body data: {parsed_body_data}")

    try:
        # Get the server host and protocol using helper function
        # This ensures we use PUBLIC_URL if configured
        host, protocol = get_host_and_protocol(request)

        # Get base WebSocket URL (Vobiz uses wss:// protocol)
        base_ws_url = get_websocket_url(host)

        # Add query parameters to WebSocket URL
        query_params = []

        # Add serviceHost for production
        env = os.getenv("ENV", "local").lower()
        if env == "production":
            agent_name = os.getenv("AGENT_NAME")
            org_name = os.getenv("ORGANIZATION_NAME")
            service_host = f"{agent_name}.{org_name}"
            query_params.append(f"serviceHost={service_host}")

        # Add body data if available
        if parsed_body_data:
            body_json = json.dumps(parsed_body_data)
            body_encoded = base64.b64encode(body_json.encode("utf-8")).decode("utf-8")
            query_params.append(f"body={body_encoded}")

        # Construct final WebSocket URL with query parameters
        if query_params:
            ws_url = f"{base_ws_url}?{'&amp;'.join(query_params)}"
        else:
            ws_url = base_ws_url

        # Log the WebSocket URL for debugging
        print(f"[INFO] WebSocket URL being sent to Vobiz: {ws_url}")
        print(f"[INFO] Host: {host}, Environment: {env}")

        # Generate XML response for Vobiz

        # Check if recording is enabled
        enable_recording = os.getenv("ENABLE_RECORDING", "true").lower() == "true"
        max_recording_length = os.getenv("MAX_RECORDING_LENGTH", "3600")  # Default: 1 hour

        # Build Record element if recording is enabled
        record_element = ""
        if enable_recording:
            record_element = f"""
        <Record fileFormat="wav" maxLength="{max_recording_length}" recordSession="true" callbackUrl="{protocol}://{host}/recording-ready" callbackMethod="POST">
        </Record>"""
            print(f"[INFO] Recording enabled (maxLength={max_recording_length}s)")
        else:
            print(f"[INFO] Recording disabled (ENABLE_RECORDING=false)")

        # ws_url was built above from get_websocket_url(host), which honours
        # VOBIZ_PROD_WS_URL when ENV=production, and XML-escapes the query
        # separator as &amp; so the <Stream> body stays well-formed XML.
        final_ws_url = ws_url

        vobiz_encoding = os.getenv("VOBIZ_ENCODING", "audio/x-mulaw")
        vobiz_rate = int(os.getenv("VOBIZ_SAMPLE_RATE", "8000"))
        vobiz_content_type = f"{vobiz_encoding};rate={vobiz_rate}"
        print(f"[INFO] Vobiz wire format: {vobiz_content_type}")

        xml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
{record_element}
        <Stream bidirectional="true" audioTrack="inbound" contentType="{vobiz_content_type}" keepCallAlive="true">
            {final_ws_url}
        </Stream>
</Response>"""

        events.record_xml("XML -> Vobiz: <Stream> + <Record>", xml_content,
                          call_uuid=CallUUID)
        print(f"[DEBUG] XML Response:\n{xml_content}")
        print("[ANSWER] ========== ANSWER XML END (STREAM) ==========\n")

        return HTMLResponse(content=xml_content, media_type="application/xml")

    except Exception as e:
        print(f"Error generating answer XML: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to generate XML: {str(e)}")


@app.api_route("/recording-finished", methods=["GET", "POST"])
async def recording_finished(request: Request) -> HTMLResponse:
    """Called by Vobiz when recording stops"""
    print("\n[RECORDING] ========== RECORDING FINISHED ==========")

    # Vobiz sends form data, not JSON
    data = await request.form()

    recording_url = data.get("RecordUrl")
    duration = data.get("RecordingDuration")
    duration_ms = data.get("RecordingDurationMs")
    recording_id = data.get("RecordingID")
    call_uuid = data.get("CallUUID")
    recording_start_ms = data.get("RecordingStartMs")
    recording_end_ms = data.get("RecordingEndMs")
    recording_end_reason = data.get("RecordingEndReason")

    print(f"[RECORDING] Recording URL: {recording_url}")
    print(f"[RECORDING] Duration: {duration} seconds ({duration_ms} ms)")
    print(f"[RECORDING] Recording ID: {recording_id}")
    print(f"[RECORDING] Call UUID: {call_uuid}")
    print(f"[RECORDING] End Reason: {recording_end_reason}")
    print(f"[RECORDING] Start Time: {recording_start_ms}")
    print(f"[RECORDING] End Time: {recording_end_ms}")

    # Store recording ID in active_calls for easy lookup
    if call_uuid and call_uuid in active_calls:
        active_calls[call_uuid]["recording_id"] = recording_id
        active_calls[call_uuid]["recording_url"] = recording_url
        print(f"[RECORDING] ✅ Stored recording ID {recording_id} for call {call_uuid}")
    else:
        print(f"[RECORDING] ⚠️  Call {call_uuid} not found in active_calls (may have ended)")

    # Optional: Download the recording
    # if recording_url:
    #     async with aiohttp.ClientSession() as session:
    #         async with session.get(recording_url) as resp:
    #             audio_data = await resp.read()
    #             with open(f"recordings/{recording_id}.mp3", "wb") as f:
    #                 f.write(audio_data)
    #     print(f"[RECORDING] Downloaded to recordings/{recording_id}.mp3")

    print("[RECORDING] ========== RECORDING FINISHED END ==========\n")

    # Return empty XML response
    return HTMLResponse(content="<Response></Response>", media_type="application/xml")


@app.api_route("/recording-ready", methods=["GET", "POST"])
async def recording_ready(request: Request) -> HTMLResponse:
    """Called by Vobiz when recording file is ready to download (via callbackUrl)"""
    print("\n[RECORDING CALLBACK] ========== RECORDING FILE READY ==========")

    # Vobiz sends form data
    data = await request.form()

    recording_url = data.get("RecordUrl")
    recording_id = data.get("RecordingID")
    call_uuid = data.get("CallUUID")

    events.record("in", "Record callbackUrl (RecordStop)",
                  {k: str(v) for k, v in data.items()}, call_uuid=call_uuid,
                  note="recording ready")

    print(f"[RECORDING CALLBACK] Recording file is ready for download!")
    print(f"[RECORDING CALLBACK] URL: {recording_url}")
    print(f"[RECORDING CALLBACK] Recording ID: {recording_id}")
    print(f"[RECORDING CALLBACK] Call UUID: {call_uuid}")

    # Auto-download the recording file with authentication
    if recording_url and recording_id:
        try:
            # Create recordings directory if it doesn't exist
            os.makedirs("recordings", exist_ok=True)

            # Get Vobiz credentials for authenticated download
            auth_id = os.getenv("VOBIZ_AUTH_ID")
            auth_token = os.getenv("VOBIZ_AUTH_TOKEN")

            headers = {
                "X-Auth-ID": auth_id,
                "X-Auth-Token": auth_token,
            }

            print(f"[RECORDING CALLBACK] Downloading recording...")

            async with aiohttp.ClientSession() as session:
                async with session.get(recording_url, headers=headers) as resp:
                    if resp.status == 200:
                        audio_data = await resp.read()
                        # Use the extension Vobiz actually served. The
                        # <Record> element picks the format (fileFormat="wav"
                        # here), so hardcoding .mp3 produced RIFF/WAVE bytes
                        # in a file named .mp3 that players reject.
                        ext = os.path.splitext(urllib.parse.urlparse(recording_url).path)[1] or ".wav"
                        filename = f"recordings/{recording_id}{ext}"
                        with open(filename, "wb") as f:
                            f.write(audio_data)
                        print(f"[RECORDING CALLBACK] ✅ Downloaded to {filename}")
                        print(f"[RECORDING CALLBACK] File size: {len(audio_data)} bytes")
                    else:
                        print(f"[RECORDING CALLBACK] ❌ Download failed: HTTP {resp.status}")
                        error_text = await resp.text()
                        print(f"[RECORDING CALLBACK] Error: {error_text}")
        except Exception as e:
            print(f"[RECORDING CALLBACK] ❌ Error downloading recording: {e}")
            import traceback
            print(f"[RECORDING CALLBACK] Traceback:\n{traceback.format_exc()}")

    print("[RECORDING CALLBACK] ========== RECORDING FILE READY END ==========\n")

    # Return empty XML response
    return HTMLResponse(content="<Response></Response>", media_type="application/xml")


@app.post("/transfer-to-human")
async def transfer_to_human(request: Request) -> HTMLResponse:
    """Return XML to transfer call to a human agent"""
    print("\n[TRANSFER] ========== TRANSFER TO HUMAN ==========")

    payload = await _webhook_payload(request)
    call_uuid = payload.get("CallUUID")
    events.record("in", "aleg_url (transfer redirect)", payload, call_uuid=call_uuid)

    # Prefer what /initiate-transfer stored for this call; fall back to env.
    call_info = active_calls.get(call_uuid, {})
    dest_type = (request.query_params.get("type") or call_info.get("transfer_type")
                 or os.getenv("TRANSFER_TYPE", "pstn"))
    destination = (request.query_params.get("destination")
                   or call_info.get("transfer_destination")
                   or (os.getenv("TRANSFER_SIP_ENDPOINT") if dest_type == "sip"
                       else os.getenv("TRANSFER_AGENT_NUMBER")))
    if not destination:
        raise HTTPException(
            status_code=500,
            detail=f"No transfer destination for type '{dest_type}'. Set it in the "
                   "request, or TRANSFER_AGENT_NUMBER / TRANSFER_SIP_ENDPOINT in .env",
        )

    sip_headers = (request.query_params.get("sip_headers")
                   or call_info.get("transfer_sip_headers") or "")

    host, protocol = get_host_and_protocol(request)
    xml_content = build_transfer_xml(dest_type, destination, host, protocol, sip_headers)
    events.record_xml(f"XML -> Vobiz: <Dial> transfer ({dest_type})",
                      xml_content, call_uuid=call_uuid)

    print(f"[TRANSFER] Transferring to {destination} ({dest_type})")
    print("[TRANSFER] ========== TRANSFER TO HUMAN END ==========\n")

    return HTMLResponse(content=xml_content, media_type="application/xml")


@app.post("/initiate-transfer")
async def initiate_transfer(request: Request) -> JSONResponse:
    """Trigger call transfer via Vobiz API"""
    print("\n[TRANSFER] ========== INITIATE TRANSFER ==========")

    data = await request.json()
    call_uuid = data.get("call_uuid")

    if not call_uuid:
        raise HTTPException(status_code=400, detail="Missing 'call_uuid' in request body")

    # Check if call exists in active_calls
    if call_uuid not in active_calls:
        raise HTTPException(status_code=404, detail=f"Call {call_uuid} not found in active calls")

    # Destination type: "pstn" (a phone number) or "sip" (a registered endpoint).
    dest_type = (data.get("type") or os.getenv("TRANSFER_TYPE", "pstn")).lower()
    if dest_type not in ("pstn", "sip"):
        raise HTTPException(status_code=400, detail="'type' must be 'pstn' or 'sip'")
    destination = data.get("destination") or (
        os.getenv("TRANSFER_SIP_ENDPOINT") if dest_type == "sip"
        else os.getenv("TRANSFER_AGENT_NUMBER")
    )
    if not destination:
        raise HTTPException(
            status_code=400,
            detail=f"No destination for type '{dest_type}'. Pass 'destination', or set "
                   "TRANSFER_AGENT_NUMBER / TRANSFER_SIP_ENDPOINT in .env",
        )

    # Which leg to redirect. aleg = the caller, bleg = the callee. Transferring
    # one leg leaves the other running its current flow.
    legs = (data.get("legs") or "aleg").lower()
    if legs not in ("aleg", "bleg"):
        raise HTTPException(status_code=400, detail="'legs' must be 'aleg' or 'bleg'")

    sip_headers, header_warnings = validate_sip_headers(data.get("sip_headers", ""))
    if header_warnings:
        for w in header_warnings:
            print(f"[TRANSFER] ⚠️  sipHeaders: {w}")

    call_info = active_calls[call_uuid]

    # Mark call as transferring. /answer and /transfer-to-human both read these
    # back when Vobiz re-fetches the document.
    call_info["status"] = "transferring"
    call_info["transfer_requested"] = True
    call_info["transfer_type"] = dest_type
    call_info["transfer_destination"] = destination
    call_info["transfer_sip_headers"] = sip_headers
    print(f"[TRANSFER] Destination: {destination} ({dest_type}), legs={legs}")
    if sip_headers:
        print(f"[TRANSFER] sipHeaders: {sip_headers}")
    print(f"[TRANSFER] Marked call {call_uuid} as transferring")

    # Get Vobiz credentials
    auth_id = os.getenv("VOBIZ_AUTH_ID")
    auth_token = os.getenv("VOBIZ_AUTH_TOKEN")

    # Get PUBLIC_URL for transfer endpoint
    public_url = os.getenv("PUBLIC_URL")
    if not public_url:
        raise HTTPException(status_code=500, detail="PUBLIC_URL not configured in .env")

    # Construct transfer URL. The destination rides along as query params so the
    # redirect is self-describing even if active_calls has been lost.
    base = public_url if public_url.startswith("http") else f"https://{public_url}"
    params = {"type": dest_type, "destination": destination}
    if sip_headers:
        params["sip_headers"] = sip_headers
    transfer_url = f"{base}/transfer-to-human?{urllib.parse.urlencode(params)}"

    print(f"[TRANSFER] Call UUID: {call_uuid}")
    print(f"[TRANSFER] Transfer URL: {transfer_url}")

    # Vobiz Transfer API endpoint
    vobiz_url = f"https://api.vobiz.ai/api/v1/Account/{auth_id}/Call/{call_uuid}/"

    headers = {
        "X-Auth-ID": auth_id,
        "X-Auth-Token": auth_token,
        "Content-Type": "application/json"
    }

    # Vobiz reads the url matching `legs`, so send only that one.
    transfer_data = {"legs": legs, f"{legs}_url": transfer_url, f"{legs}_method": "POST"}

    events.record("out", "POST /Call/{uuid}/ (transfer)",
                  {**transfer_data, "endpoint": vobiz_url}, call_uuid=call_uuid,
                  kind="rest", note=f"redirect A-leg -> {destination} ({dest_type})")

    print(f"[TRANSFER] Calling Vobiz Transfer API...")
    print(f"[TRANSFER] URL: {vobiz_url}")
    print(f"[TRANSFER] Data: {json.dumps(transfer_data, indent=2)}")
    print(f"[TRANSFER] NOTE: Transfer API should close Stream and fetch new XML from {transfer_url}")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(vobiz_url, headers=headers, json=transfer_data) as resp:
                response_text = await resp.text()
                print(f"[TRANSFER] Vobiz Response Status: {resp.status}")
                print(f"[TRANSFER] Vobiz Response Body: {response_text}")

                if resp.status == 202:  # 202 Accepted
                    result = json.loads(response_text)
                    events.record("in", f"Vobiz REST response {resp.status}", result,
                                  call_uuid=call_uuid, kind="rest",
                                  note="transfer executed")
                    print(f"[TRANSFER] ✅ Transfer API call successful!")
                    print(f"[TRANSFER] Vobiz should now fetch XML from {transfer_url}")
                    print("[TRANSFER] ========== INITIATE TRANSFER END ==========\n")

                    return JSONResponse({
                        "status": "transfer_initiated",
                        "call_uuid": call_uuid,
                        "legs": legs,
                        "type": dest_type,
                        "destination": destination,
                        "sip_headers": sip_headers,
                        "sip_header_warnings": header_warnings,
                        "transfer_url": transfer_url,
                        "vobiz_response": result
                    })
                else:
                    print(f"[TRANSFER] ❌ Transfer failed!")
                    print("[TRANSFER] ========== INITIATE TRANSFER END (FAILED) ==========\n")
                    raise HTTPException(
                        status_code=resp.status,
                        detail=f"Vobiz transfer failed: {response_text}"
                    )

    except Exception as e:
        print(f"[TRANSFER] ❌ Error during transfer: {e}")
        import traceback
        print(f"[TRANSFER] Traceback:\n{traceback.format_exc()}")
        print("[TRANSFER] ========== INITIATE TRANSFER END (ERROR) ==========\n")
        raise HTTPException(status_code=500, detail=f"Transfer error: {str(e)}")


@app.api_route("/hangup", methods=["GET", "POST"])
async def hangup_webhook(request: Request) -> HTMLResponse:
    """Vobiz hangup_url. Fires once per call, for the A-leg only."""
    payload = await _webhook_payload(request)
    call_uuid = payload.get("CallUUID")
    events.record("in", "hangup_url (Hangup)", payload, call_uuid=call_uuid,
                  note=payload.get("HangupCauseName"))
    if call_uuid in active_calls:
        active_calls[call_uuid]["status"] = "completed"
    print(f"[HANGUP] {call_uuid} — {payload.get('HangupCauseName')}")
    return HTMLResponse(content="", media_type="application/xml")


@app.api_route("/dial-events", methods=["GET", "POST"])
async def dial_events(request: Request) -> HTMLResponse:
    """Dial callbackUrl — DialAnswer, DialConnected, DialHangup.

    The only webhook that reports the transferred leg's identity and outcome:
    hangup_url covers the A-leg only, so this is where the B-leg becomes visible.
    """
    payload = await _webhook_payload(request)
    event_name = payload.get("Event") or payload.get("DialAction") or "DialEvent"
    events.record("in", f"Dial callback ({event_name})", payload,
                  call_uuid=payload.get("CallUUID"),
                  note=f"B-leg {payload.get('DialBLegTo', '')} {payload.get('DialBLegStatus', '')}".strip())
    print(f"[DIAL] {event_name} — B-leg {payload.get('DialBLegUUID')} "
          f"{payload.get('DialBLegStatus')}")
    return HTMLResponse(content="", media_type="application/xml")


@app.api_route("/dial-complete", methods=["GET", "POST"])
async def dial_complete(request: Request) -> HTMLResponse:
    """Dial action — final result once the bridge ends."""
    payload = await _webhook_payload(request)
    events.record("in", "Dial action (final result)", payload,
                  call_uuid=payload.get("CallUUID"),
                  note=payload.get("DialStatus"))
    print(f"[DIAL] complete — {payload.get('DialStatus')}")
    return HTMLResponse(content="", media_type="application/xml")


@app.get("/dashboard")
async def dashboard() -> HTMLResponse:
    """Live webhook inspector for the demo."""
    return HTMLResponse(content=DASHBOARD_HTML)


@app.get("/events")
async def events_stream(request: Request) -> StreamingResponse:
    """Server-sent events: history, then live."""
    return StreamingResponse(
        events.sse_stream(request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/events/clear")
async def events_clear(request: Request) -> JSONResponse:
    """Clear the live feed. Pass {"wipe_log": true} to also delete the stored
    record — otherwise clearing the screen keeps the history on disk."""
    wipe = False
    try:
        wipe = bool((await request.json()).get("wipe_log"))
    except Exception:
        pass
    events.clear(wipe_log=wipe)
    return JSONResponse({"status": "cleared", "log_wiped": wipe})


@app.get("/events/info")
async def events_info() -> JSONResponse:
    return JSONResponse({
        "persisted": events.log_size(),
        "in_memory": len(events.history()),
        "file": events.EVENT_LOG,
    })


@app.get("/active-calls")
async def get_active_calls() -> JSONResponse:
    """List all currently active calls"""
    print("[ACTIVE CALLS] Fetching active calls list")

    # Create a serializable version of active_calls (excluding websocket objects)
    calls_info = {}
    for call_uuid, call_data in active_calls.items():
        calls_info[call_uuid] = {
            "status": call_data.get("status"),
            "started_at": call_data.get("started_at"),
            "path": call_data.get("path"),
            "recording_id": call_data.get("recording_id"),  # Include recording ID if available
            "recording_url": call_data.get("recording_url")  # Include recording URL if available
            # Exclude 'websocket' as it's not JSON serializable
        }

    return JSONResponse({
        "active_calls": list(active_calls.keys()),
        "count": len(active_calls),
        "calls": calls_info
    })


async def handle_vobiz_websocket(
    websocket: WebSocket,
    path: str,
    body: str = None,
    serviceHost: str = None,
):
    """Common handler for Vobiz WebSocket connections on any path."""
    print("[DEBUG] ========================================")
    print(f"[DEBUG] WebSocket connection attempt on path: {path}")
    print(f"[DEBUG] Client: {websocket.client}")
    print(f"[DEBUG] Headers: {dict(websocket.headers)}")
    print(f"[DEBUG] Query params - body: {body}, serviceHost: {serviceHost}")
    print("[DEBUG] ========================================")

    try:
        await websocket.accept()
        print("[SUCCESS] WebSocket connection accepted for outbound call")
    except Exception as e:
        print(f"[ERROR] Failed to accept WebSocket connection: {e}")
        raise

    # Decode body parameter if provided
    body_data = {}
    if body:
        try:
            # Base64 decode the JSON (it was base64-encoded in the answer endpoint)
            decoded_json = base64.b64decode(body).decode("utf-8")
            body_data = json.loads(decoded_json)
            print(f"Decoded body data: {body_data}")
        except Exception as e:
            print(f"Error decoding body parameter: {e}")
    else:
        print("No body parameter received")

    call_uuid = None

    try:
        # Import the bot function from the bot module
        from bot import bot
        from pipecat.runner.types import WebSocketRunnerArguments

        print("[DEBUG] Starting bot initialization...")

        # Do NOT call parse_telephony_websocket(websocket) here — it consumes
        # the initial handshake messages and leaves the socket "empty" for
        # the Pipecat transport. bot.py uses parse_vobiz_start() instead,
        # which captures the negotiated mediaFormat AND the stream/call IDs.
        call_uuid = (
            websocket.query_params.get("call_uuid")
            or websocket.query_params.get("call_id")
        )
        stream_id = None

        if call_uuid:
            # Update or create entry in active_calls with WebSocket reference
            if call_uuid in active_calls:
                # Update existing entry (from /start pre-registration)
                active_calls[call_uuid]["status"] = "active"
                active_calls[call_uuid]["websocket"] = websocket
                active_calls[call_uuid]["path"] = path
                print(f"[CALL] ✅ Updated existing call {call_uuid} with WebSocket")
            else:
                # Create new entry
                active_calls[call_uuid] = {
                    "status": "active",
                    "started_at": datetime.now().isoformat(),
                    "path": path,
                    "websocket": websocket,
                    "transfer_requested": False
                }
                print(f"[CALL] ✅ Created new call entry for {call_uuid}")

            print(f"[CALL] Active calls count: {len(active_calls)}")
        else:
            print("[CALL] ⚠️  No call UUID found in URL query params")

        # Wrap the socket so the dashboard can see the stream protocol. The tap
        # only observes: every method delegates to the real WebSocket, and the
        # transport and serializer behave exactly as before.
        tapped = WebSocketTap(websocket, call_uuid=call_uuid)

        # Create runner arguments and run the bot
        runner_args = WebSocketRunnerArguments(websocket=tapped)
        runner_args.handle_sigint = False

        print("[DEBUG] Calling bot function...")
        # We pass call_id if we have it, but we let stream_id be None so bot/transport can find it from the stream
        try:
            await bot(runner_args, call_id=call_uuid, stream_id=stream_id)
        finally:
            # Emit whatever audio was counted but not yet rolled up, so the
            # feed does not end mid-window.
            tapped.flush()

        print("[DEBUG] Bot function completed")

    except Exception as e:
        print(f"[ERROR] Error in WebSocket endpoint: {e}")
        import traceback
        print(f"[ERROR] Traceback:\n{traceback.format_exc()}")
        try:
            await websocket.close()
        except:
            pass
    finally:
        # Remove call from active_calls when WebSocket closes
        # BUT: Don't remove if call is being transferred (status == "transferring")
        if call_uuid and call_uuid in active_calls:
            call_status = active_calls[call_uuid].get("status", "active")
            if call_status == "transferring":
                print(f"[CALL] 🔄 Call {call_uuid} is being transferred - keeping in active_calls")
                # Remove websocket reference but keep call record for transfer
                active_calls[call_uuid]["websocket"] = None
            else:
                # Normal call end - remove completely
                del active_calls[call_uuid]
                print(f"[CALL] 🔴 Removed call UUID: {call_uuid}")
                print(f"[CALL] Active calls count: {len(active_calls)}")


# Register WebSocket endpoints for common paths Vobiz might use
@app.websocket("/ws")
async def websocket_ws(
    websocket: WebSocket,
    body: str = Query(None),
    serviceHost: str = Query(None),
):
    """Handle WebSocket connection at /ws path."""
    await handle_vobiz_websocket(websocket, "/ws", body, serviceHost)


@app.websocket("/")
async def websocket_root(
    websocket: WebSocket,
    body: str = Query(None),
    serviceHost: str = Query(None),
):
    """Handle WebSocket connection at root path."""
    await handle_vobiz_websocket(websocket, "/", body, serviceHost)


@app.websocket("/voice/ws")
async def websocket_voice_ws(
    websocket: WebSocket,
    body: str = Query(None),
    serviceHost: str = Query(None),
):
    """Handle WebSocket connection at /voice/ws path to match user XML."""
    await handle_vobiz_websocket(websocket, "/voice/ws", body, serviceHost)


@app.websocket("/stream")
async def websocket_stream(
    websocket: WebSocket,
    body: str = Query(None),
    serviceHost: str = Query(None),
):
    """Handle WebSocket connection at /stream path."""
    await handle_vobiz_websocket(websocket, "/stream", body, serviceHost)


# ----------------- Main ----------------- #


if __name__ == "__main__":
    # Cloud Run and most PaaS hosts inject PORT; 7860 is the local default.
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "7860")))
