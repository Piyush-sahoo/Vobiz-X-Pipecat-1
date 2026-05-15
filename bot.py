#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import os

from dotenv import load_dotenv
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
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
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.openai.stt import OpenAISTTService
from pipecat.services.openai.tts import OpenAITTSService
from pipecat.transports.base_transport import BaseTransport
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

load_dotenv(override=True)


async def run_bot(transport: BaseTransport, handle_sigint: bool):
    llm = OpenAILLMService(api_key=os.getenv("OPENAI_API_KEY"))

    stt = OpenAISTTService(api_key=os.getenv("OPENAI_API_KEY"))

    tts = OpenAITTSService(
        api_key=os.getenv("OPENAI_API_KEY"),
        voice="ballad",
    )

    messages = [
        {
            "role": "system",
            "content": (
                "You are a friendly assistant. "
                "Your responses will be read aloud, so keep them concise and conversational. "
                "Avoid special characters or formatting. "
                "Begin by saying: 'Hello! This is an automated call from our Vobiz AI assistant.'"
            ),
        },
    ]

    context = LLMContext(messages)
    # pipecat 1.x: vad_analyzer lives on LLMUserAggregatorParams now,
    # not on the transport (transport-side vad_analyzer is silently a no-op).
    context_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()),
    )

    pipeline = Pipeline(
        [
            transport.input(),  # Websocket input from client
            stt,  # Speech-To-Text
            context_aggregator.user(),
            llm,  # LLM
            tts,  # Text-To-Speech
            transport.output(),  # Websocket output to client
            context_aggregator.assistant(),
        ]
    )

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            audio_in_sample_rate=8000,   # Vobiz MULAW input (8kHz telephony)
            audio_out_sample_rate=24000, # OpenAI TTS native (auto-resampled to 8kHz for Vobiz)
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
    )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        # Wait for the user to speak first 
        logger.info("Starting outbound call conversation")

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Outbound call ended")
        # task.cancel() is correct when the *caller* hangs up first — the
        # WS is already dead so there is no in-flight TTS to drain. If your
        # bot ends the call itself (e.g. graceful EndFrame from a flow),
        # prefer `await task.stop_when_done()` so queued TTS frames finish
        # playing before the pipeline tears down.
        await task.cancel()

    runner = PipelineRunner(handle_sigint=handle_sigint)

    await runner.run(task)


async def bot(runner_args: RunnerArguments, call_id: str = None, stream_id: str = None):
    """Main bot entry point compatible with Pipecat Cloud."""

    # Read Vobiz's `start` event off the WebSocket to learn the negotiated
    # wire format (encoding + sample rate + IDs). Env vars are fallback hints.
    env_encoding = os.getenv("VOBIZ_ENCODING", "audio/x-mulaw")
    env_sample_rate = int(os.getenv("VOBIZ_SAMPLE_RATE", "8000"))

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
        auth_id=os.getenv("VOBIZ_AUTH_ID", ""),
        auth_token=os.getenv("VOBIZ_AUTH_TOKEN", ""),
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
            add_wav_header=False,  # CRITICAL: Must be False for telephony
            serializer=serializer,
            # NOTE: vad_analyzer is deprecated on FastAPIWebsocketParams in
            # pipecat 1.x. VAD is now wired on LLMUserAggregatorParams above.
        ),
    )

    handle_sigint = runner_args.handle_sigint

    await run_bot(transport, handle_sigint)
