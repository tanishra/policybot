import certifi
import ctypes
import ctypes.util
from dotenv import load_dotenv
import logging
import json
import time
import re
import os
import socket
import asyncio
from collections import deque

# Patch heartbeat BEFORE any livekit import to prevent LB idle timeout
import livekit.agents.worker as _lk_worker
_lk_worker.HEARTBEAT_INTERVAL = 15

from livekit import agents, rtc
from livekit.agents import AgentServer, AgentSession, Agent, function_tool, RunContext, JobProcess, JobExecutorType
from livekit.plugins import silero, openai, deepgram, sarvam, elevenlabs

import logger as db_logger
from agents.orchestrator import ConversationState
from agents.instructions import compose_instructions_obj
from agents.dispatcher import create_outcome
from agents.concern import ConcernCategory, coerce_concern_category
from agents.compliance import compliance_check

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("renewal-bot")

os.environ.setdefault("SSL_CERT_FILE", certifi.where())

def _reinit_resolver() -> None:
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"))
        libc.res_init()
    except Exception:
        pass

def _prewarm_setup(proc: JobProcess) -> None:
    _reinit_resolver()
    host = (os.getenv("LIVEKIT_URL") or "").replace("wss://", "").replace("https://", "")
    if not host:
        return
    try:
        socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
        socket.getaddrinfo(host, 443, socket.AF_INET6, socket.SOCK_STREAM)
        logger.info(f"DNS prewarmed: {host}")
    except Exception as e:
        logger.warning(f"DNS prewarm failed: {e}")

REQUIRED_ENV_VARS = [
    "LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET",
    "OPENAI_API_KEY", "DEEPGRAM_API_KEY", "SARVAM_API_KEY"
]
missing_vars = [var for var in REQUIRED_ENV_VARS if not os.getenv(var)]
if missing_vars:
    raise RuntimeError(f"Missing required environment variables: {', '.join(missing_vars)}")


class RenewalAssistant(Agent):
    def __init__(self, metadata: dict, **kwargs) -> None:
        self.metadata = metadata
        self.state = ConversationState.INTRO
        self.outcome = create_outcome()
        super().__init__(
            instructions=compose_instructions_obj(ConversationState.INTRO, metadata),
            **kwargs
        )

    async def _transition_state(self, new_state: str):
        self.state = new_state
        await self.update_instructions(compose_instructions_obj(self.state, self.metadata))

    @function_tool()
    async def confirm_right_party(self, context: RunContext) -> str:
        logger.info(f"confirm_right_party — {self.metadata.get('name')} confirmed")
        await self._transition_state(ConversationState.CONSENT)
        return "Identity confirmed. Ask for recording consent before sharing policy details."

    @function_tool()
    async def grant_recording_consent(self, context: RunContext) -> str:
        logger.info("grant_recording_consent — consent given")
        self.outcome["recording_consent"] = "Yes"
        await self._transition_state(ConversationState.NARRATION)
        return f"Recording consent granted. Share policy details with {self.metadata.get('name')}."

    @function_tool()
    async def deny_recording_consent(self, context: RunContext) -> str:
        logger.info("deny_recording_consent — consent denied")
        self.outcome["recording_consent"] = "No"
        self.outcome["disposition"] = "Consent Denied"
        await self._transition_state(ConversationState.CLOSING)
        return "Recording consent denied. Politely say goodbye and end the call."

    @function_tool()
    async def fail_right_party(self, context: RunContext, alternate_number_provided: str = None) -> str:
        logger.info(f"fail_right_party — wrong number for {self.metadata.get('name')}")
        if alternate_number_provided:
            self.outcome["alt_number"] = alternate_number_provided
        self.outcome["disposition"] = "Alternate Number Captured" if alternate_number_provided else "Wrong Number"
        await self._transition_state(ConversationState.CLOSING)
        return "Apologize politely and say goodbye."

    @function_tool()
    async def capture_promise_to_pay(self, context: RunContext, expected_date: str) -> str:
        logger.info(f"capture_promise_to_pay — date={expected_date}")
        self.outcome["disposition"] = "Promise to Pay"
        self.outcome["ptp_date"] = expected_date
        await self._transition_state(ConversationState.CLOSING)
        return f"Payment date '{expected_date}' recorded. Say you are sending payment link via WhatsApp and say goodbye."

    @function_tool()
    async def categorize_concern(self, context: RunContext, concern_category: ConcernCategory, user_quote: str, confidence: float = 1.0) -> str:
        coerced_cat, coerced_conf = coerce_concern_category(concern_category, confidence, user_quote)
        logger.info(f"categorize_concern — category={concern_category} confidence={confidence} -> coerced={coerced_cat}")
        self.outcome["disposition"] = "Concern Captured"
        self.outcome["concern_cat"] = coerced_cat
        self.outcome["concern_confidence"] = coerced_conf
        self.outcome["concern_notes"] = user_quote
        await self._transition_state(ConversationState.CLOSING)
        return "Concern noted. Show empathy, say the team will follow up, and say goodbye."

    @function_tool()
    async def capture_partial_payment(self, context: RunContext, partial_amount: str, emi_option: str = None) -> str:
        logger.info(f"capture_partial_payment — amount={partial_amount}, emi={emi_option}")
        self.outcome["disposition"] = "Partial Payment Arranged"
        self.outcome["partial_amount"] = partial_amount
        self.outcome["emi_option"] = emi_option or "None"
        await self._transition_state(ConversationState.CLOSING)
        return "Partial payment arranged. Confirm with the user and say goodbye."

    @function_tool()
    async def schedule_call_back(self, context: RunContext, preferred_time: str) -> str:
        logger.info(f"schedule_call_back — time={preferred_time}")
        self.outcome["disposition"] = "Call Back Scheduled"
        self.outcome["call_back_time"] = preferred_time
        await self._transition_state(ConversationState.CLOSING)
        return f"Call back scheduled for {preferred_time}. Confirm with the user and say goodbye."

    @function_tool()
    async def request_escalation(self, context: RunContext, reason: str) -> str:
        logger.info(f"request_escalation — reason={reason}")
        self.outcome["disposition"] = "Escalated"
        self.outcome["concern_notes"] = reason
        await self._transition_state(ConversationState.CLOSING)
        return "Escalation noted. Show empathy, say a senior team member will call back within 24 hours, and say goodbye."

    @function_tool()
    async def detect_sentiment(self, context: RunContext, sentiment: str) -> str:
        logger.info(f"detect_sentiment — {sentiment}")
        self.outcome["sentiment"] = sentiment
        return f"Customer sentiment recorded as {sentiment}."

    @function_tool()
    async def detect_language(self, context: RunContext, language: str) -> str:
        logger.info(f"detect_language — {language}")
        self.outcome["detected_language"] = language
        return f"Customer language recorded as {language}."


server = AgentServer(
    ws_url=os.getenv("LIVEKIT_URL"),
    api_key=os.getenv("LIVEKIT_API_KEY"),
    api_secret=os.getenv("LIVEKIT_API_SECRET"),
    job_executor_type=JobExecutorType.THREAD,
    setup_fnc=_prewarm_setup,
    max_retry=64,
)

_roundtrip_times = deque(maxlen=20)


def _init_tts():
    legacy = os.getenv("TTS_PROVIDER")
    fallback = os.getenv("TTS_FALLBACK")
    if legacy and not fallback:
        order = [legacy]
    else:
        order = (fallback or "sarvam,elevenlabs,deepgram").split(",")
    errors = []

    for name in order:
        name = name.strip().lower()
        try:
            if name == "sarvam":
                tts = sarvam.TTS(
                    model=os.getenv("PRIMARY_TTS_MODEL", "bulbul:v3"),
                    speaker=os.getenv("PRIMARY_TTS_SPEAKER", "priya"),
                    target_language_code=os.getenv("PRIMARY_TTS_LANGUAGE", "en-IN"),
                    pace=float(os.getenv("TTS_PACE", "1.2")),
                    temperature=0.5,
                    min_buffer_size=int(os.getenv("TTS_MIN_BUFFER", "30")),
                    max_chunk_length=int(os.getenv("TTS_MAX_CHUNK", "50")),
                    api_key=os.getenv("SARVAM_API_KEY"),
                )
                logger.info("TTS: Sarvam Bulbul v3")
                return tts

            elif name == "elevenlabs":
                key = os.getenv("ELEVENLABS_API_KEY", "")
                if not key:
                    raise RuntimeError("ELEVENLABS_API_KEY not set")
                tts = elevenlabs.TTS(
                    model=os.getenv("ELEVENLABS_MODEL", "eleven_turbo_v2_5"),
                    voice_id=os.getenv("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM"),
                    api_key=key,
                )
                logger.info(f"TTS: ElevenLabs (voice_id={os.getenv('ELEVENLABS_VOICE_ID', '21m00Tcm4TlvDq8ikWAM')})")
                return tts

            elif name == "deepgram":
                tts = deepgram.TTS(
                    model=os.getenv("DEEPGRAM_TTS_MODEL", "aura-2-andromeda-en"),
                    api_key=os.getenv("DEEPGRAM_API_KEY"),
                )
                logger.info("TTS: Deepgram Aura")
                return tts

        except Exception as e:
            logger.warning(f"TTS {name} failed: {e}")
            errors.append(f"{name}: {e}")

    raise RuntimeError(f"No working TTS provider. Tried: {', '.join(order)}. Errors: {', '.join(errors)}")


@server.rtc_session(agent_name="priya")
async def my_agent(ctx: agents.JobContext):
    start_time = time.time()
    room_name = ctx.room.name
    logger.info(f"AGENT DISPATCHED to room: {room_name}")

    try:
        participant = await ctx.wait_for_participant()
        logger.info(f"Participant joined: identity={participant.identity}")
    except Exception as e:
        logger.error(f"Failed waiting for participant: {e}")
        return

    metadata = {}
    if participant.metadata:
        try:
            metadata = json.loads(participant.metadata)
            logger.info(f"Metadata: {json.dumps(metadata)}")
        except json.JSONDecodeError:
            logger.error("Failed to parse metadata")

    if not metadata:
        logger.error("No metadata, skipping call.")
        return

    logger.info(f"Customer: {metadata.get('name')}, mobile={metadata.get('mobile_number')}")

    deepgram_stt = deepgram.STT(
        model=os.getenv("PRIMARY_STT_MODEL", "nova-3"),
        language=os.getenv("PRIMARY_STT_LANGUAGE", "multi"),
        api_key=os.getenv("DEEPGRAM_API_KEY"),
    )

    openai_llm = openai.LLM(
        model=os.getenv("PRIMARY_LLM_MODEL", "gpt-4o-mini"),
        api_key=os.getenv("OPENAI_API_KEY"),
    )

    tts_model = _init_tts()
    vad_model = silero.VAD.load()
    tts_model.prewarm()
    logger.info("AI pipeline ready (STT + LLM + TTS + VAD, prewarmed)")

    amd = {"human_detected": False, "should_end": False}
    compliance_violations = 0

    try:
        session = AgentSession(
            stt=deepgram_stt,
            llm=openai_llm,
            tts=tts_model,
            vad=vad_model,
            turn_handling={
                "turn_detection": "stt",
                "endpointing": {
                    "min_delay": float(os.getenv("MIN_ENDPOINTING_DELAY", "0.4")),
                    "max_delay": float(os.getenv("MAX_ENDPOINTING_DELAY", "0.8")),
                },
                "interruption": {
                    "mode": "vad",
                    "min_duration": float(os.getenv("MIN_INTERRUPTION_DURATION", "0.3")),
                },
                "preemptive_generation": {
                    "enabled": True,
                },
            },
        )

        assistant = RenewalAssistant(metadata=metadata)
        logger.info(f"Assistant created (state={assistant.state})")

        _timing = {"user_stopped": 0, "agent_thinking": 0, "agent_speaking": 0}

        def on_user_state(ev):
            if ev.new_state == "speaking":
                amd["human_detected"] = True
            if ev.new_state == "listening" and ev.old_state == "speaking":
                _timing["user_stopped"] = time.time()
                logger.info(f"[TIMING] user_stopped_speaking t={_timing['user_stopped']:.3f}")

        def on_agent_state(ev):
            t = time.time()
            key = None
            if ev.new_state == "thinking":
                _timing["agent_thinking"] = t
                key = "AGENT_STARTED_THINKING"
            elif ev.new_state == "speaking":
                _timing["agent_speaking"] = t
                key = "AGENT_STARTED_SPEAKING"
            elif ev.new_state == "listening" and ev.old_state == "speaking":
                key = "AGENT_FINISHED_SPEAKING"

            if key:
                logger.info(f"[TIMING] {key} t={t:.3f}")

            if _timing["agent_speaking"] and _timing["user_stopped"] and _timing["agent_thinking"]:
                if _timing["agent_speaking"] > _timing["agent_thinking"] > _timing["user_stopped"]:
                    endpoint = _timing["agent_thinking"] - _timing["user_stopped"]
                    process = _timing["agent_speaking"] - _timing["agent_thinking"]
                    total = _timing["agent_speaking"] - _timing["user_stopped"]
                    logger.info(f"[TIMING] ROUND-TRIP: endpointing={endpoint:.2f}s processing={process:.2f}s total={total:.2f}s")
                    _roundtrip_times.append(total)
                _timing["user_stopped"] = 0
                _timing["agent_thinking"] = 0
                _timing["agent_speaking"] = 0

        session.on("user_state_changed", on_user_state)
        session.on("agent_state_changed", on_agent_state)

        await session.start(room=ctx.room, agent=assistant)
        logger.info("Session started")

    except Exception as e:
        logger.error(f"Session init failed: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return

    name = metadata.get("name", "Customer")
    greeting_text = (
        f"नमस्ते, मैं Fairvalue Insuretech प्राइवेट लिमिटेड की तरफ से "
        f"आपकी इंश्योरेंस पॉलिसी रिन्यूअल के बारे में बात कर रही हूँ। "
        f"क्या मेरी बात {name} जी से हो रही है?"
    )
    t0 = time.time()
    passed, violation, safe_text = compliance_check(greeting_text)
    if not passed:
        compliance_violations += 1
        logger.warning(f"[COMPLIANCE] Greeting violation: {violation}")
    try:
        await session.say(text=safe_text, allow_interruptions=True)
        logger.info(f"[TIMING] Greeting sent in {time.time() - t0:.2f}s")
    except Exception as e:
        logger.error(f"say() failed: {e}")

    async def _amd_monitor():
        await asyncio.sleep(6)
        if not amd["human_detected"]:
            logger.info("AMD: No human speech in 6s — voicemail assumed")
            assistant.outcome["disposition"] = "Voicemail"
            amd["should_end"] = True

    amd_task = asyncio.create_task(_amd_monitor())

    logger.info("Waiting for call to end...")
    try:
        while ctx.room.connection_state == rtc.ConnectionState.CONN_CONNECTED:
            if amd["should_end"]:
                logger.info("AMD triggered — ending call")
                break
            await asyncio.sleep(0.5)
    except Exception as e:
        logger.warning(f"Room wait error: {e}")
    finally:
        amd_task.cancel()

    duration = int(time.time() - start_time)
    logger.info(f"Call ended. Duration: {duration}s")

    avg_rt = sum(_roundtrip_times) / len(_roundtrip_times) if _roundtrip_times else 0
    logger.info(f"[TIMING] AVG round-trip: {avg_rt:.2f}s over {len(_roundtrip_times)} turns")
    logger.info(f"[COMPLIANCE] Violations: {compliance_violations}")

    raw_transcript = ""
    try:
        msgs = list(assistant.chat_ctx.messages()) if assistant.chat_ctx else []
        for msg in msgs:
            try:
                r = str(getattr(msg, "role", "")).upper()
                c = str(getattr(msg, "content", ""))
                if r == "USER":
                    raw_transcript += f"Customer: {c}\n"
                elif r == "ASSISTANT":
                    raw_transcript += f"Priya: {c}\n"
            except Exception:
                pass
    except Exception as e:
        logger.error(f"Transcript build error: {e}")

    PII_PATTERNS = [
        (r'\+?\b\d{10,12}\b', "[REDACTED]"),              # phone numbers
        (r'\bPOL-\d+\b', "[REDACTED]"),                    # policy numbers
        (r'\b[A-Z]{5}[0-9]{4}[A-Z]\b', "[REDACTED]"),     # PAN
        (r'\b\d{4}\s?\d{4}\s?\d{4}\b', "[REDACTED]"),     # Aadhaar
        (r'\b\d{9,18}\b', "[REDACTED]"),                   # bank accounts
    ]

    def redact_pii(text: str) -> str:
        for pattern, replacement in PII_PATTERNS:
            text = re.sub(pattern, replacement, text)
        return text

    safe_transcript = redact_pii(raw_transcript)
    safe_concern_notes = redact_pii(assistant.outcome.get("concern_notes") or "")

    recording_url = os.getenv("LIVEKIT_RECORDING_URL", "")

    try:
        await db_logger.log_call(
            customer_name=metadata.get("name"),
            mobile_number=metadata.get("mobile_number"),
            policy_number=metadata.get("policy_number"),
            call_status="Completed",
            duration=duration,
            disposition=assistant.outcome["disposition"],
            promise_to_pay_date=assistant.outcome["ptp_date"],
            concern_category=assistant.outcome["concern_cat"],
            concern_confidence=assistant.outcome["concern_confidence"],
            concern_notes=safe_concern_notes,
            alt_number=assistant.outcome["alt_number"],
            detected_language=assistant.outcome["detected_language"],
            sentiment=assistant.outcome["sentiment"],
            partial_amount=assistant.outcome["partial_amount"],
            emi_option=assistant.outcome["emi_option"],
            call_back_time=assistant.outcome["call_back_time"],
            transcript=safe_transcript,
            recording_consent=assistant.outcome.get("recording_consent"),
            recording_url=recording_url,
        )
        logger.info("Call record saved")
    except Exception as e:
        logger.error(f"Failed to save call record: {e}")

    logger.info(f"SESSION ENDED for {room_name} ({duration}s)")

    try:
        await ctx.shutdown()
    except Exception:
        pass


if __name__ == "__main__":
    agents.cli.run_app(server)
