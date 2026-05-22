def build_rpc_instructions() -> str:
    return """
    CURRENT STATE: Introduction

    YOUR JOB - Right Party Confirmation:
    1. Wait for the user to confirm their identity.
    2. If they confirm -> call confirm_right_party tool.
    3. If wrong number -> call fail_right_party tool.
    4. Do NOT reveal policy details yet.
    Answer any question the user asks.
    """
