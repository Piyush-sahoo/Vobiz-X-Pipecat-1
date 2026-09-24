#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#
"""bot.py — one bot, two pipelines.

BOT_MODE selects the stack at runtime:

  BOT_MODE=cascaded  (default)
      Deepgram STT -> Gemini LLM -> Deepgram TTS
      Three services, each swappable. Cheaper, easier to debug, and you
      get a real transcript of both sides.

  BOT_MODE=realtime
      Gemini Live (speech-to-speech, single service)
      Lower latency, no STT/TTS stages, but no local transcript and the
      turn-taking is decided server-side.

Both share the same Vobiz plumbing: VobizFrameSerializer, 8 kHz mu-law,
and the same /ws transport.

IMPORTANT — audio_out_sample_rate is 8000 in BOTH modes. Pipecat's output
transport resamples to that rate *before* the serializer sees the frame,
so VobizFrameSerializer only ever does an 8k->8k no-op. Leaving it at the
service-native rate (24 kHz for Gemini Live / OpenAI TTS) pushes the
resampling into the serializer, whose stream resampler returns empty on
its first calls and silently drops frames (measured: 3 of the first 10
frames at 24 kHz, 1 of 10 at 16 kHz).
"""

import os

from dotenv import load_dotenv
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.runner.types import RunnerArguments
from pipecat.serializers.vobiz import VobizFrameSerializer, parse_vobiz_start
from pipecat.transports.base_transport import BaseTransport
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

load_dotenv(override=True)

BOT_MODE = os.getenv("BOT_MODE", "cascaded").strip().lower()

# Vobiz is 8 kHz mu-law on the wire. Keep the whole pipeline there.
TELEPHONY_RATE = 8000

# --- ICICI Lombard customer support persona -------------------------------
#
# Tuned for a spoken phone call, not a chat window. Three things matter:
#   - short turns, because the caller cannot skim speech
#   - no markdown or symbols, because TTS reads them aloud
#   - a clear escalation path, because the demo ends in a live transfer
#
# Override any of this with AGENT_* env vars without touching code.

BRAND = os.getenv("AGENT_BRAND", "ICICI Lombard")
AGENT_NAME_SPOKEN = os.getenv("AGENT_NAME_SPOKEN", "Riya")

SYSTEM_PROMPT = os.getenv("AGENT_SYSTEM_PROMPT") or (
    f"You are {AGENT_NAME_SPOKEN}, an AI voice assistant for {BRAND} general "
    "insurance customer support. You are on a live phone call.\n\n"

    "HOW YOU SPEAK\n"
    "Your words are spoken aloud by a text to speech engine. Keep every reply to "
    "one or two short sentences. Never use markdown, bullet points, asterisks, "
    "emoji or special characters. Say rupee amounts and policy numbers as words a "
    "person would say them. Use plain Indian English, warm and efficient. Do not "
    "monologue: ask one question at a time and wait.\n\n"

    "WHAT YOU HELP WITH\n"
    "Motor, health, travel and home insurance. Typical reasons people call: "
    "checking or renewing a policy, asking about premium and due dates, "
    "registering a motor or health claim, checking the status of an existing "
    "claim, cashless hospitalisation and network hospitals, roadside assistance, "
    "and requesting policy documents.\n\n"

    "HOW YOU HANDLE A CALL\n"
    "Identify the reason for the call first. Ask for the policy number when it is "
    "needed, and read it back to confirm. If the caller gives a claim number, "
    "confirm it the same way. Acknowledge the situation before moving to process, "
    "especially on an accident or hospitalisation.\n\n"

    "HONESTY RULES, THESE MATTER\n"
    f"You are an AI assistant, not a human. Say so plainly if asked. You do not "
    "have live access to policy records in this demo, so never invent a premium "
    "amount, a claim status, a due date or a policy detail. If you do not have "
    "something, say you will connect the caller to a specialist who can see their "
    "records. Never promise a settlement, an amount, or a timeline.\n\n"

    "ESCALATION\n"
    "Offer to transfer to a human specialist when the caller asks for a person, "
    "is distressed, is reporting an accident or hospitalisation, disputes "
    "something, or needs a record you cannot see. Say clearly that you are "
    "connecting them and that they should stay on the line."
)

GREETING_SEED = os.getenv("AGENT_GREETING") or (
    f"Greet the caller warmly. Say: Thank you for calling {BRAND}, this is "
    f"{AGENT_NAME_SPOKEN}, an AI assistant. Then ask how you can help them today. "
    "Keep it to two short sentences and then stop and listen."
)


def _build_cascaded():
    """Deepgram STT -> Gemini LLM -> Deepgram TTS."""
    from pipecat.services.deepgram.stt import DeepgramSTTService
    from pipecat.services.deepgram.tts import DeepgramTTSService
    from pipecat.services.google.llm import GoogleLLMService

    dg_key = os.getenv("DEEPGRAM_API_KEY")
    if not dg_key:
        raise RuntimeError("BOT_MODE=cascaded needs DEEPGRAM_API_KEY (STT + TTS)")
    google_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
    if not google_key:
        raise RuntimeError("BOT_MODE=cascaded needs GOOGLE_API_KEY (LLM)")

    stt = DeepgramSTTService(api_key=dg_key)

    llm = GoogleLLMService(
        api_key=google_key,
        settings=GoogleLLMService.Settings(
            model=os.getenv("GEMINI_MODEL", "gemini-3.6-flash"),
        ),
    )

    # Synthesize straight to 8 kHz so nothing downstream has to resample.
    tts = DeepgramTTSService(
        api_key=dg_key,
        settings=DeepgramTTSService.Settings(
            voice=os.getenv("DEEPGRAM_VOICE", "aura-2-thalia-en"),
        ),
        sample_rate=TELEPHONY_RATE,
        encoding="linear16",
    )

    context = LLMContext([{"role": "system", "content": SYSTEM_PROMPT}])
    return [stt, llm, tts], context


def _build_realtime():
    """Gemini Live — speech-to-speech, no STT/TTS stages."""
    from pipecat.services.google.gemini_live.llm import (
        GeminiLiveLLMService,
        GeminiModalities,
    )

    google_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
    if not google_key:
        raise RuntimeError("BOT_MODE=realtime needs GOOGLE_API_KEY")

    # Everything goes in Settings. Mixing top-level kwargs (voice_id=,
    # system_instruction=) with settings= is the trap: settings TAKES
    # PRECEDENCE, so a partially-populated Settings silently discards the
    # kwargs you passed alongside it.
    #
    # Model must carry the "models/" prefix. Live-capable models are the
    # ones advertising bidiGenerateContent; gemini-3.8-live is the newest.
    llm = GeminiLiveLLMService(
        api_key=google_key,
        settings=GeminiLiveLLMService.Settings(
            model=os.getenv("GEMINI_LIVE_MODEL", "models/gemini-3.8-live"),
            voice=os.getenv("GEMINI_VOICE", "Charon"),
            system_instruction=SYSTEM_PROMPT,
            modalities=GeminiModalities.AUDIO,
        ),
    )

    # Realtime services stream audio straight to the provider, so the
    # aggregator runs in "event-only, no context push" mode. The seed
    # message is what the service infers from at context initialization.
    context = LLMContext([{"role": "user", "content": GREETING_SEED}])
    return [llm], context


async def run_bot(transport: BaseTransport, handle_sigint: bool):
    if BOT_MODE == "realtime":
        stages, context = _build_realtime()
    elif BOT_MODE == "cascaded":
        stages, context = _build_cascaded()
    else:
        raise RuntimeError(f"BOT_MODE must be 'cascaded' or 'realtime', got {BOT_MODE!r}")

    logger.info(f"BOT_MODE={BOT_MODE} — pipeline stages: {[type(s).__name__ for s in stages]}")

    # pipecat 1.x: VAD belongs on LLMUserAggregatorParams. Passing
    # vad_analyzer= to the transport params is silently dropped by Pydantic,
    # which gives you no error and no turn detection.
    context_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()),
        # Required for speech-to-speech: decouples context writes from
        # transcripts, since a realtime service decides turns server-side.
        # Pipecat auto-detects this, but the docs say to set it explicitly.
        realtime_service_mode=(BOT_MODE == "realtime"),
    )

    pipeline = Pipeline(
        [
            transport.input(),
            *( [] if BOT_MODE == "realtime" else [stages[0]] ),   # stt
            context_aggregator.user(),
            *( stages if BOT_MODE == "realtime" else stages[1:] ), # llm (+ tts)
            transport.output(),
            context_aggregator.assistant(),
        ]
    )

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            audio_in_sample_rate=TELEPHONY_RATE,
            audio_out_sample_rate=TELEPHONY_RATE,  # see module docstring
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
    )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info(f"Call connected ({BOT_MODE}) — triggering greeting")
        # Nothing runs the LLM until a frame asks it to. Without this the
        # callee hears dead air until they speak first, and on an outbound
        # call they usually just hang up.
        await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Call ended")
        # Correct when the *caller* hangs up first: the WS is already dead,
        # so there is no in-flight TTS to drain. If the bot ends the call
        # itself, prefer `await task.stop_when_done()`.
        await task.cancel()

    runner = PipelineRunner(handle_sigint=handle_sigint)
    await runner.run(task)


async def bot(runner_args: RunnerArguments, call_id: str = None, stream_id: str = None,
              auth_id: str = None, auth_token: str = None):
    """Main bot entry point — identical Vobiz handshake in both modes."""
    env_encoding = os.getenv("VOBIZ_ENCODING", "audio/x-mulaw")
    env_sample_rate = int(os.getenv("VOBIZ_SAMPLE_RATE", str(TELEPHONY_RATE)))

    # Read Vobiz's `start` event to learn the negotiated wire format. Env
    # vars are only fallback hints — this event is authoritative.
    parsed = await parse_vobiz_start(runner_args.websocket)
    logger.info(
        f"Vobiz start: callId={parsed['call_id']!r}, streamId={parsed['stream_id']!r}, "
        f"mediaFormat=({parsed['encoding']!r}, {parsed['sample_rate']})"
    )
    call_id = call_id or parsed["call_id"]
    stream_id = stream_id or parsed["stream_id"]
    vobiz_encoding = parsed["encoding"] or env_encoding
    vobiz_sample_rate = parsed["sample_rate"] or env_sample_rate

    serializer = VobizFrameSerializer(
        stream_id=stream_id,
        call_id=call_id,
        # These are used for the serializer's REST hang-up, so they must be the
        # account that placed the call — not necessarily the server's own.
        auth_id=auth_id or os.getenv("VOBIZ_AUTH_ID", ""),
        auth_token=auth_token or os.getenv("VOBIZ_AUTH_TOKEN", ""),
        params=VobizFrameSerializer.InputParams(
            vobiz_sample_rate=vobiz_sample_rate,
            encoding=vobiz_encoding,
            sample_rate=None,
            l16_byte_order=os.getenv("VOBIZ_L16_ENDIAN", "be"),
            auto_hang_up=True,
        ),
    )

    transport = FastAPIWebsocketTransport(
        websocket=runner_args.websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,  # CRITICAL: must be False for telephony
            serializer=serializer,
        ),
    )

    await run_bot(transport, runner_args.handle_sigint)
