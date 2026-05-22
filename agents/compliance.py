import json
import os
import re

_RULES: dict | None = None

PROHIBITED_REGEX = [
    re.compile(r'\bguarantee[\w]*[\s%0-9,]*return[s]?\b', re.IGNORECASE),
    re.compile(r'\bassured?\s+return[s]?\b', re.IGNORECASE),
    re.compile(r'\brisk[\s-]?free\b', re.IGNORECASE),
    re.compile(r'\bbetter\s+than\b', re.IGNORECASE),
    re.compile(r'\bbest\s+policy\b', re.IGNORECASE),
    re.compile(r'\bnever\s+lose\s+money\b', re.IGNORECASE),
    re.compile(r'\bguarantee[\w]*[\s%0-9,]*profit[s]?\b', re.IGNORECASE),
    re.compile(r'\bfixed\s+income[s]?\b', re.IGNORECASE),
    re.compile(r'\b100%\s*safe\b', re.IGNORECASE),
    re.compile(r'\bhundred\s+percent\s*safe\b', re.IGNORECASE),
    re.compile(r'\bno\s+risk[s]?\b', re.IGNORECASE),
    re.compile(r'\bzero\s+risk[s]?\b', re.IGNORECASE),
    re.compile(r'\bsure\s+shot\b', re.IGNORECASE),
    re.compile(r'\bguarantee[\w]*\b', re.IGNORECASE),
]


def _load_rules() -> dict:
    global _RULES
    if _RULES is not None:
        return _RULES
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "compliance_rules.json")
    try:
        with open(path) as f:
            _RULES = json.load(f)
    except Exception:
        _RULES = {"prohibited_patterns": [], "required_disclosures": []}
    return _RULES


def compliance_check(text: str) -> tuple[bool, str, str]:
    for pattern in PROHIBITED_REGEX:
        match = pattern.search(text)
        if match:
            sanitized = _sanitize(text, match.group())
            return False, match.group(), sanitized
    return True, "", text


def _sanitize(text: str, violation: str) -> str:
    return re.sub(re.escape(violation), "[adjusted]", text, flags=re.IGNORECASE)


def build_compliance_instructions() -> str:
    return """
    COMPLIANCE:
    - Identify yourself as Priya from Fairvalue Insuretech Private Limited.
    - State the purpose of the call clearly (insurance policy renewal).
    - Do NOT provide financial advice or recommendations.
    - Do NOT use any of these prohibited phrases: guaranteed return, assured return,
      risk-free, best policy, never lose money, guaranteed profit, fixed income, 100% safe.
    - Do NOT make misleading statements about policy benefits.
    - If you are unsure, err on the side of saying less.
    """
