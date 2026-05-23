class ConversationState:
    INTRO = "intro"
    CONSENT = "consent"
    NARRATION = "narration"
    FEASIBILITY = "feasibility"
    AMBIGUOUS = "ambiguous"
    PARTIAL_PAYMENT = "partial_payment"
    CALL_BACK = "call_back"
    ESCALATION = "escalation"
    CLOSING = "closing"


VALID_TRANSITIONS = {
    ConversationState.INTRO: [
        ConversationState.CONSENT,
        ConversationState.CLOSING,
    ],
    ConversationState.CONSENT: [
        ConversationState.NARRATION,
        ConversationState.CLOSING,
    ],
    ConversationState.NARRATION: [
        ConversationState.AMBIGUOUS,
        ConversationState.FEASIBILITY,
        ConversationState.PARTIAL_PAYMENT,
        ConversationState.CALL_BACK,
        ConversationState.ESCALATION,
        ConversationState.CLOSING,
    ],
    ConversationState.FEASIBILITY: [
        ConversationState.AMBIGUOUS,
        ConversationState.PARTIAL_PAYMENT,
        ConversationState.CALL_BACK,
        ConversationState.ESCALATION,
        ConversationState.CLOSING,
    ],
    ConversationState.AMBIGUOUS: [
        ConversationState.NARRATION,
        ConversationState.FEASIBILITY,
        ConversationState.PARTIAL_PAYMENT,
        ConversationState.CALL_BACK,
        ConversationState.ESCALATION,
        ConversationState.CLOSING,
    ],
    ConversationState.PARTIAL_PAYMENT: [ConversationState.CLOSING],
    ConversationState.CALL_BACK: [ConversationState.CLOSING],
    ConversationState.ESCALATION: [ConversationState.CLOSING],
    ConversationState.CLOSING: [],
}


def is_valid_transition(from_state: str, to_state: str) -> bool:
    allowed = VALID_TRANSITIONS.get(from_state, [])
    return to_state in allowed
