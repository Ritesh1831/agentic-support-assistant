from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SessionState:
    """In-memory session state. Production should replace this with a durable store."""

    messages: list[dict[str, Any]] = field(default_factory=list)
    raw_transcript: list[dict[str, str]] = field(default_factory=list)
    verified_customer_id: str | None = None
    verified_order_ids: set[str] = field(default_factory=set)
    active_order_id: str | None = None
    failed_verification_attempts: int = 0
    exchanges_this_session: dict[str, dict[str, int]] = field(default_factory=dict)
    delay_credit_issued: set[str] = field(default_factory=set)
    escalations: list[dict[str, Any]] = field(default_factory=list)
    turn_count: int = 0
