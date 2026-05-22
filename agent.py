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
from livekit import agents, rtc
from livekit.agents import AgentServer, AgentSession, Agent, function_tool, RunContext, JobProcess, JobExecutorType
from livekit.agents.llm import ChatRole
from livekit.agents.llm.chat_context import Instructions
from livekit.plugins import silero, openai, deepgram, sarvam

import logger as db_logger

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("renewal-bot")

os.environ.setdefault("SSL_CERT_FILE", certifi.where())

# macOS: spawned child processes inherit a stale DNS resolver state from the
# parent.  Calling libc's res_init() re-reads /etc/resolv.conf so that the
# Rust livekit-ffi layer can resolve LiveKit Cloud hostnames.
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


class ConversationState:
    INTRO = "intro"
    NARRATION = "narration"
    FEASIBILITY = "feasibility"
    PARTIAL_PAYMENT = "partial_payment"
    CALL_BACK = "call_back"
    ESCALATION = "escalation"
    CLOSING = "closing"


class RenewalAssistant(Agent):
    def __init__(self, metadata: dict, **kwargs) -> None:
        self.metadata = metadata
        self.state = ConversationState.INTRO
        self.outcome = {
            "disposition": "No Response",
            "ptp_date": None,
            "concern_cat": None,
            "concern_notes": None,
            "alt_number": None,
            "partial_amount": None,
            "emi_option": None,
            "call_back_time": None,
            "detected_language": None,
            "sentiment": None,
        }
        super().__init__(
            instructions=Instructions(audio=self.get_instructions_for_state(ConversationState.INTRO)),
            **kwargs
        )

    # ── Instructions Builder ──────────────────────────────────────────────

    def _guardrails(self) -> str:
        return """
        GUARDRAILS:
        - You are Priya, a polite insurance renewal assistant.
        - NEVER give financial advice.
        - NEVER promise guaranteed returns.
        - Respond concisely in a sweet, conversational Indian tone.
        - Match the user's language (Hindi, English, or Hinglish).
        - You can answer any policy questions at any point.
        - The conversation must flow naturally through the states.
        """

    def _customer_block(self) -> str:
        m = self.metadata
        return f"""
        CUSTOMER DETAILS:
          Name: {m.get('name')}
          Policy: {m.get('policy_number')} | Plan: {m.get('plan_name')}
          Premium Due: Rs. {m.get('due_amount')} | Due Date: {m.get('due_date')}
          Policy Started: {m.get('policy_purchase_date')} | Term: {m.get('policy_term_years')} years
          Sum Assured: Rs. {m.get('sum_assured')}
          Premium Frequency: {m.get('premium_frequency')}
          Last Payment: {m.get('last_payment_date')} via {m.get('payment_method')}
          Agent: {m.get('agent_name')} | Branch: {m.get('branch')}
          Email: {m.get('email')}
        """

    def _sentiment_instructions(self) -> str:
        return """
        SENTIMENT AWARENESS:
        - Continuously assess the user's sentiment from their tone and words.
        - If they sound angry, frustrated, or aggressive, call request_escalation tool immediately (do NOT try to sell further).
        - If neutral or happy, continue normally.
        - Call detect_sentiment tool at state transitions to record the sentiment.
        """

    def _language_instructions(self) -> str:
        return """
        LANGUAGE DETECTION:
        - Detect the user's primary language (Hindi / English / Hinglish) from their speech.
        - Respond in the same language they use.
        - Call detect_language tool once confident.
        """

    def _dtmf_instructions(self) -> str:
        return """
        DTMF / KEYPAD HANDLING:
        - If the user says they pressed a key (e.g., "main ne 1 daba diya") or you hear tones:
          Treat 1 = Yes / Confirm, 2 = No / Decline, 3 = Call me back, 0 = Talk to agent.
        - Ask "Press 1 to confirm" when you need clear confirmation.
        """

    def _state_tag(self, state: str, job: str) -> str:
        return f"""
        CURRENT STATE: {state}
        YOUR JOB: {job}
        """

    def get_instructions_for_state(self, state: str) -> str:
        base = self._guardrails() + self._customer_block()
        base += self._sentiment_instructions()
        base += self._language_instructions()
        base += self._dtmf_instructions()

        if state == ConversationState.INTRO:
            return base + self._state_tag("Introduction", """
            NOTE: You have already greeted the user. Do NOT greet again.
            1. Wait for the user to confirm their identity.
            2. If they confirm -> call confirm_right_party tool.
            3. If wrong number -> call fail_right_party tool.
            4. Do NOT reveal policy details yet.
            Answer any question the user asks.
            """)

        elif state == ConversationState.NARRATION:
            return base + self._state_tag("Policy Narration", """
            1. Tell the user about their policy: plan name, policy number, premium, due date.
            2. Ask: Can you make the payment? By when?
            3. Answer ANY questions (policy, coverage, payment history, agent, etc).
            4. If they give a payment date -> capture_promise_to_pay.
            5. If they refuse or have a concern -> categorize_concern.
            6. If they say partial / installment / half / EMI -> transition to partial_payment.
            7. If they say call later / call back / busy -> transition to call_back.
            """)

        elif state == ConversationState.FEASIBILITY:
            return base + self._state_tag("Payment Discussion", """
            1. Listen to the user's payment response.
            2. If they agree on a date -> capture_promise_to_pay.
            3. If they refuse or raise a concern -> categorize_concern.
            4. If they say partial / installment / half -> transition to partial_payment.
            5. If they say call later / call back -> transition to call_back.
            """)

        elif state == ConversationState.PARTIAL_PAYMENT:
            return base + self._state_tag("Partial Payment / EMI", """
            1. Ask how much they can pay now.
            2. Offer EMI: they can pay the rest in 2-3 installments.
            3. Call capture_partial_payment tool with amount and EMI preference.
            """)

        elif state == ConversationState.CALL_BACK:
            return base + self._state_tag("Call Back Scheduling", """
            1. Ask when is a good time to call back.
            2. Capture preferred date and time.
            3. Call schedule_call_back tool with the preferred time.
            """)

        elif state == ConversationState.ESCALATION:
            return base + self._state_tag("Escalation", """
            1. Show empathy. Apologize for any inconvenience.
            2. Say a senior team member will call them back.
            3. Call request_escalation tool with the reason.
            """)

        elif state == ConversationState.CLOSING:
            return base + self._state_tag("Closing", """
            Politely say goodbye and end the call.
            """)

        return base

    # ── State Transitions ─────────────────────────────────────────────────

    async def _transition_state(self, new_state: str):
        self.state = new_state
        await self.update_instructions(Instructions(audio=self.get_instructions_for_state(self.state)))

    # ── Tools: Identity ───────────────────────────────────────────────────

    @function_tool()
    async def confirm_right_party(self, context: RunContext) -> str:
        logger.info(f"confirm_right_party — {self.metadata.get('name')} confirmed")
        await self._transition_state(ConversationState.NARRATION)
        return f"Identity confirmed. Share policy details with {self.metadata.get('name')}."

    @function_tool()
    async def fail_right_party(self, context: RunContext, alternate_number_provided: str = None) -> str:
        logger.info(f"fail_right_party — wrong number for {self.metadata.get('name')}")
        if alternate_number_provided:
            self.outcome["alt_number"] = alternate_number_provided
        self.outcome["disposition"] = "Alternate Number Captured" if alternate_number_provided else "Wrong Number"
        await self._transition_state(ConversationState.CLOSING)
        return "Apologize politely and say goodbye."

    # ── Tools: Payment ────────────────────────────────────────────────────

    @function_tool()
    async def capture_promise_to_pay(self, context: RunContext, expected_date: str) -> str:
        logger.info(f"capture_promise_to_pay — date={expected_date}")
        self.outcome["disposition"] = "Promise to Pay"
        self.outcome["ptp_date"] = expected_date
        await self._transition_state(ConversationState.CLOSING)
        return f"Payment date '{expected_date}' recorded. Say you are sending payment link via WhatsApp and say goodbye."

    @function_tool()
    async def categorize_concern(self, context: RunContext, concern_category: str, user_quote: str) -> str:
        logger.info(f"categorize_concern — category={concern_category}, quote={user_quote}")
        self.outcome["disposition"] = "Concern Captured"
        self.outcome["concern_cat"] = concern_category
        self.outcome["concern_notes"] = user_quote
        await self._transition_state(ConversationState.CLOSING)
        return "Concern noted. Show empathy, say the team will follow up, and say goodbye."

    # ── Tools: Partial Payment / EMI ──────────────────────────────────────

    @function_tool()
    async def capture_partial_payment(self, context: RunContext, partial_amount: str, emi_option: str = None) -> str:
        logger.info(f"capture_partial_payment — amount={partial_amount}, emi={emi_option}")
        self.outcome["disposition"] = "Partial Payment Arranged"
        self.outcome["partial_amount"] = partial_amount
        self.outcome["emi_option"] = emi_option or "None"
        await self._transition_state(ConversationState.CLOSING)
        return "Partial payment arranged. Confirm with the user and say goodbye."

    # ── Tools: Call Back Scheduling ───────────────────────────────────────

    @function_tool()
    async def schedule_call_back(self, context: RunContext, preferred_time: str) -> str:
        logger.info(f"schedule_call_back — time={preferred_time}")
        self.outcome["disposition"] = "Call Back Scheduled"
        self.outcome["call_back_time"] = preferred_time
        await self._transition_state(ConversationState.CLOSING)
        return f"Call back scheduled for {preferred_time}. Confirm with the user and say goodbye."

    # ── Tools: Escalation ─────────────────────────────────────────────────

    @function_tool()
    async def request_escalation(self, context: RunContext, reason: str) -> str:
        logger.info(f"request_escalation — reason={reason}")
        self.outcome["disposition"] = "Escalated"
        self.outcome["concern_notes"] = reason
        await self._transition_state(ConversationState.CLOSING)
        return "Escalation noted. Show empathy, say a senior team member will call back within 24 hours, and say goodbye."

    # ── Tools: Detection ──────────────────────────────────────────────────

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


# ── Server Setup ─────────────────────────────────────────────────────────────
server = AgentServer(
    ws_url=os.getenv("LIVEKIT_URL"),
    api_key=os.getenv("LIVEKIT_API_KEY"),
    api_secret=os.getenv("LIVEKIT_API_SECRET"),
    job_executor_type=JobExecutorType.THREAD,
    setup_fnc=_prewarm_setup,
)

# ── Timing ───────────────────────────────────────────────────────────────────
_roundtrip_times = deque(maxlen=20)


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

    # ── AI Pipeline ───────────────────────────────────────────────────────
    deepgram_stt = deepgram.STT(
        model=os.getenv("PRIMARY_STT_MODEL", "nova-3"),
        language=os.getenv("PRIMARY_STT_LANGUAGE", "multi"),
        api_key=os.getenv("DEEPGRAM_API_KEY"),
    )

    openai_llm = openai.LLM(
        model=os.getenv("PRIMARY_LLM_MODEL", "gpt-4o-mini"),
        api_key=os.getenv("OPENAI_API_KEY"),
    )

    tts_model = sarvam.TTS(
        model=os.getenv("PRIMARY_TTS_MODEL", "bulbul:v3"),
        speaker=os.getenv("PRIMARY_TTS_SPEAKER", "priya"),
        target_language_code=os.getenv("PRIMARY_TTS_LANGUAGE", "en-IN"),
        pace=float(os.getenv("TTS_PACE", "1.2")),
        temperature=0.5,
        min_buffer_size=int(os.getenv("TTS_MIN_BUFFER", "30")),
        max_chunk_length=int(os.getenv("TTS_MAX_CHUNK", "50")),
        api_key=os.getenv("SARVAM_API_KEY"),
    )

    vad_model = silero.VAD.load()
    tts_model.prewarm()
    logger.info("AI pipeline ready (STT + LLM + TTS + VAD, prewarmed)")

    # ── AMD State (Answering Machine Detection) ───────────────────────────
    amd = {"human_detected": False, "should_end": False}

    # ── Session ───────────────────────────────────────────────────────────
    try:
        session = AgentSession(
            stt=deepgram_stt,
            llm=openai_llm,
            tts=tts_model,
            vad=vad_model,
            turn_detection="stt",
            preemptive_generation=True,
            min_endpointing_delay=float(os.getenv("MIN_ENDPOINTING_DELAY", "0.4")),
            max_endpointing_delay=float(os.getenv("MAX_ENDPOINTING_DELAY", "0.8")),
            min_interruption_duration=float(os.getenv("MIN_INTERRUPTION_DURATION", "0.3")),
        )

        assistant = RenewalAssistant(metadata=metadata)
        logger.info(f"Assistant created (state={assistant.state})")

        # ── Timing + AMD Listeners ────────────────────────────────────────
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

    # ── Greeting ──────────────────────────────────────────────────────────
    name = metadata.get("name", "Customer")
    greeting_text = f"नमस्ते, मैं रिन्यूअल टीम से प्रिया बोल रही हूँ। क्या मेरी बात {name} जी से हो रही है?"
    t0 = time.time()
    try:
        await session.say(text=greeting_text, allow_interruptions=True)
        logger.info(f"[TIMING] Greeting sent in {time.time() - t0:.2f}s")
    except Exception as e:
        logger.error(f"say() failed: {e}")

    # ── AMD Monitor ───────────────────────────────────────────────────────
    async def _amd_monitor():
        await asyncio.sleep(6)
        if not amd["human_detected"]:
            logger.info("AMD: No human speech in 6s — voicemail assumed")
            assistant.outcome["disposition"] = "Voicemail"
            amd["should_end"] = True

    amd_task = asyncio.create_task(_amd_monitor())

    # ── Wait for call to end ──────────────────────────────────────────────
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

    # ── Build Transcript ──────────────────────────────────────────────────
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

    safe_transcript = re.sub(r'\+?\b\d{10,12}\b', "[REDACTED]", raw_transcript)
    safe_transcript = re.sub(r'\bPOL-\d+\b', "[REDACTED]", safe_transcript)

    # ── Recording URL (from env if available) ─────────────────────────────
    recording_url = os.getenv("LIVEKIT_RECORDING_URL", "")

    # ── Log Call ──────────────────────────────────────────────────────────
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
            concern_notes=assistant.outcome["concern_notes"],
            alt_number=assistant.outcome["alt_number"],
            detected_language=assistant.outcome["detected_language"],
            sentiment=assistant.outcome["sentiment"],
            partial_amount=assistant.outcome["partial_amount"],
            emi_option=assistant.outcome["emi_option"],
            call_back_time=assistant.outcome["call_back_time"],
            transcript=safe_transcript,
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
