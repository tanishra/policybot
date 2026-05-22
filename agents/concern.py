def build_concern_instructions() -> str:
    return """
    CONCERN HANDLING:
    - Listen to the user's concern.
    - Categorize using the categorize_concern tool.
    - Show empathy.
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
