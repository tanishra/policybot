OUTCOME_DEFAULTS = {
    "disposition": "No Response",
    "ptp_date": None,
    "concern_cat": None,
    "concern_confidence": None,
    "concern_notes": None,
    "alt_number": None,
    "detected_language": None,
    "sentiment": None,
    "partial_amount": None,
    "emi_option": None,
    "call_back_time": None,
}


def create_outcome() -> dict:
    return dict(OUTCOME_DEFAULTS)


def log_disposition(outcome: dict) -> None:
    print(f"[DISPATCHER] Disposition: {outcome.get('disposition')} — "
          f"PTP={outcome.get('ptp_date')}, "
          f"Concern={outcome.get('concern_cat')} "
          f"(confidence={outcome.get('concern_confidence')})")
