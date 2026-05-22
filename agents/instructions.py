from livekit.agents.llm.chat_context import Instructions

from .guardrails import build_guardrails
from .rpc import build_rpc_instructions
from .policy_narration import build_customer_block, build_narration_instructions
from .intent import build_intent_instructions
from .concern import (
    build_concern_instructions,
    build_partial_payment_instructions,
    build_call_back_instructions,
    build_escalation_instructions,
    build_closing_instructions,
)
from .compliance import build_compliance_instructions
from .orchestrator import ConversationState


def build_common_blocks(metadata: dict) -> str:
    base = build_guardrails()
    base += build_customer_block(metadata)
    base += build_compliance_instructions()
    base += _sentiment_instructions()
    base += _language_instructions()
    base += _dtmf_instructions()
    return base


def _sentiment_instructions() -> str:
    return """
    SENTIMENT AWARENESS:
    - Continuously assess the user's sentiment from their tone and words.
    - If they sound angry, frustrated, or aggressive, call request_escalation tool immediately.
    - If neutral or happy, continue normally.
    - Call detect_sentiment tool at state transitions to record the sentiment.
    """


def _language_instructions() -> str:
    return """
    LANGUAGE DETECTION:
    - Detect the user's primary language (Hindi / English / Hinglish) from their speech.
    - Respond in the same language they use.
    - Call detect_language tool once confident.
    """


def _dtmf_instructions() -> str:
    return """
    DTMF / KEYPAD HANDLING:
    - If the user says they pressed a key (e.g., "main ne 1 daba diya") or you hear tones:
      Treat 1 = Yes / Confirm, 2 = No / Decline, 3 = Call me back, 0 = Talk to agent.
    - Ask "Press 1 to confirm" when you need clear confirmation.
    """


STATE_BUILDERS = {
    ConversationState.INTRO: lambda: build_rpc_instructions(),
    ConversationState.NARRATION: lambda: build_narration_instructions() + build_concern_instructions(),
    ConversationState.FEASIBILITY: lambda: build_intent_instructions() + build_concern_instructions(),
    ConversationState.PARTIAL_PAYMENT: lambda: build_partial_payment_instructions(),
    ConversationState.CALL_BACK: lambda: build_call_back_instructions(),
    ConversationState.ESCALATION: lambda: build_escalation_instructions(),
    ConversationState.CLOSING: lambda: build_closing_instructions(),
}


def compose_instructions(state: str, metadata: dict) -> str:
    base = build_common_blocks(metadata)
    state_builder = STATE_BUILDERS.get(state, lambda: "")
    return base + state_builder()


def compose_instructions_obj(state: str, metadata: dict) -> Instructions:
    text = compose_instructions(state, metadata)
    return Instructions(audio=text)
