from typing import Literal

CONCERN_CATEGORY_VALUES = [
    "Financial Problem",
    "Mis-selling",
    "Product Misunderstanding",
    "Better Investment Available",
    "Lower Returns",
    "Wants to Surrender",
    "Deferred Payment Request",
    "Refusal to Pay",
    "Long Investment Period",
    "Reduced Paid-Up Preference",
    "Other",
]

ConcernCategory = Literal[
    "Financial Problem",
    "Mis-selling",
    "Product Misunderstanding",
    "Better Investment Available",
    "Lower Returns",
    "Wants to Surrender",
    "Deferred Payment Request",
    "Refusal to Pay",
    "Long Investment Period",
    "Reduced Paid-Up Preference",
    "Other",
]

CONFIDENCE_THRESHOLD = 0.7


def coerce_concern_category(
    category: str,
    confidence: float = 1.0,
    user_quote: str = "",
) -> tuple[str, float]:
    if confidence < CONFIDENCE_THRESHOLD:
        return "Other", confidence
    if category in CONCERN_CATEGORY_VALUES:
        return category, confidence
    return "Other", confidence


def build_concern_instructions() -> str:
    taxonomy_bullets = "\n".join(f"    - {c}" for c in CONCERN_CATEGORY_VALUES)
    return f"""
    CONCERN HANDLING:
    - Listen to the user's concern about their policy/renewal.
    - Categorize using the categorize_concern tool with the EXACT category from the taxonomy below.
    - Always output a confidence score (0.0 to 1.0) indicating how sure you are of the category.
    - If confidence is below {CONFIDENCE_THRESHOLD}, the system auto-classifies as "Other".
    - Show empathy before calling the tool.

    CONCERN TAXONOMY (use EXACT match):
{taxonomy_bullets}

    CATEGORY GUIDE:
    - Financial Problem: "no money", "tight budget", "can't afford", "expenses"
    - Mis-selling: "agent lied", "wasn't told about this", "misled", "wrong information"
    - Product Misunderstanding: "thought it was different", "didn't know about charges"
    - Better Investment Available: "found better plan", "switching to another company"
    - Lower Returns: "returns are too low", "not getting enough", "poor returns"
    - Wants to Surrender: "want to close", "cancel my policy", "surrender"
    - Deferred Payment Request: "pay later", "extension", "delay", "next month"
    - Refusal to Pay: "won't pay", "not paying", "refuse to renew", "no"
    - Long Investment Period: "too long", "many years", "can't wait that long"
    - Reduced Paid-Up Preference: "make paid-up", "stop paying but keep policy"
    - Other: anything that doesn't clearly fit above categories
    """


def build_consent_instructions() -> str:
    return """
    CURRENT STATE: Recording Consent

    YOUR JOB:
    1. The user has confirmed their identity.
    2. Ask: "This call is being recorded for quality and training purposes. Is that okay?"
    3. Wait for their response.
    4. If they say YES (or equivalent) -> call grant_recording_consent tool.
    5. If they say NO (or equivalent) -> call deny_recording_consent tool.
    6. Do NOT discuss policy details until consent is granted.
    """


def build_partial_payment_instructions() -> str:
    return """
    CURRENT STATE: Partial Payment / EMI

    YOUR JOB:
    1. Ask how much they can pay now.
    2. Offer EMI: they can pay the rest in 2-3 installments.
    3. Call capture_partial_payment tool with amount and EMI preference.
    """


def build_call_back_instructions() -> str:
    return """
    CURRENT STATE: Call Back Scheduling

    YOUR JOB:
    1. Ask when is a good time to call back.
    2. Capture preferred date and time.
    3. Call schedule_call_back tool with the preferred time.
    """


def build_escalation_instructions() -> str:
    return """
    CURRENT STATE: Escalation

    YOUR JOB:
    1. Show empathy. Apologize for any inconvenience.
    2. Say a senior team member will call them back.
    3. Call request_escalation tool with the reason.
    """


def build_closing_instructions() -> str:
    return """
    CURRENT STATE: Closing

    YOUR JOB:
    Politely say goodbye and end the call.
    """
