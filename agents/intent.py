def build_intent_instructions() -> str:
    return """
    CURRENT STATE: Payment Discussion

    YOUR JOB:
    1. Listen to the user's payment response.
    2. If they agree on a date -> capture_promise_to_pay.
    3. If they refuse or raise a concern -> categorize_concern.
    4. If they say partial / installment / half -> transition to partial_payment.
    5. If they say call later / call back -> transition to call_back.
    6. If their response is UNCLEAR, VAGUE, or AMBIGUOUS (e.g., "I'll think about it",
       "Let me check", "Maybe", "Hmm", "Not sure", silence, mumbling) ->
       call mark_ambiguous(attempt=1) to reprompt once.
    """
