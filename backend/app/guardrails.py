from __future__ import annotations

import re
from dataclasses import dataclass

HUMAN_REQUEST_RE = re.compile(r"\b(?:human|live agent|real person|representative|customer service agent)\b", re.I)
AUTHORITY_RE = re.compile(r"\b(?:discount|coupon|promo(?:tional)? code|goodwill|waive|waiver)\b", re.I)
INJECTION_RE = re.compile(r"\b(?:ignore (?:all |previous |your )?instructions|reveal (?:your )?(?:system )?prompt|roleplay (?:as|override)|developer message)\b", re.I)
CARD_RE = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
CVV_RE = re.compile(r"\b(?:cvv|cvc|security code)\s*(?:is|:)?\s*\d{3,4}\b", re.I)
# Deliberately conservative: a labelled long account number, not every ordinary number.
BANK_RE = re.compile(r"\b(?:bank(?: account)?|account number|a/c)\s*(?:is|:|#)?\s*\d{8,18}\b", re.I)
ORDER_ID_RE = re.compile(r"\b(TR-\d{4,})\b", re.I)
EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
PHONE_RE = re.compile(r"\+\d[\d -]{7,18}\d")


@dataclass(frozen=True)
class GuardrailSignals:
    asks_for_human: bool
    authority_overreach: bool
    prompt_injection: bool
    payment_details: bool


@dataclass(frozen=True)
class IdentityCredentials:
    order_id: str
    email: str | None = None
    phone: str | None = None


def inspect_message(message: str) -> GuardrailSignals:
    return GuardrailSignals(
        asks_for_human=bool(HUMAN_REQUEST_RE.search(message)),
        authority_overreach=bool(AUTHORITY_RE.search(message)),
        prompt_injection=bool(INJECTION_RE.search(message)),
        payment_details=bool(CARD_RE.search(message) or CVV_RE.search(message) or BANK_RE.search(message)),
    )


def redact_payment_details(message: str) -> str:
    result = CARD_RE.sub("[REDACTED PAYMENT NUMBER]", message)
    result = CVV_RE.sub("[REDACTED CVV]", result)
    return BANK_RE.sub("[REDACTED BANK DETAIL]", result)


def extract_identity_credentials(message: str) -> IdentityCredentials | None:
    """Detect an explicit order-ID plus credential pair for secure pre-verification."""
    order = ORDER_ID_RE.search(message)
    email = EMAIL_RE.search(message)
    phone = PHONE_RE.search(message)
    if not order or not (email or phone):
        return None
    return IdentityCredentials(
        order_id=order.group(1).upper(),
        email=email.group(0) if email else None,
        phone=phone.group(0).replace(" ", "") if phone else None,
    )
