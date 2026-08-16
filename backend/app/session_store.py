"""Redis-backed session store with an automatic in-memory fallback.

`SessionState` holds two Python `set` fields (`verified_order_ids`,
`delay_credit_issued`) and a nested dict field (`exchanges_this_session`) that
are not directly JSON-serializable as-is; this module handles that
conversion. It also transparently falls back to the same in-memory dict the
application used before Redis was introduced whenever `REDIS_URL` is unset,
or whenever Redis is unreachable for any reason at all — a broken or absent
Redis must never take the chat endpoint down, only degrade session
persistence back to single-process memory.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from dataclasses import asdict
from typing import Any

from .state import SessionState

logger = logging.getLogger(__name__)

SESSION_TTL_SECONDS = 86_400  # 24 hours, refreshed (sliding) on every read
KEY_PREFIX = "trendly:session:"
EXCHANGE_KEY_PREFIX = "trendly:exchange:"
EXCHANGE_TTL_SECONDS = 7_776_000  # 90 days
ESCALATION_QUEUE_KEY = "trendly:escalations:queue"
ESCALATION_QUEUE_TTL_SECONDS = 604_800  # 7 days


def _state_to_json(state: SessionState) -> str:
    data: dict[str, Any] = asdict(state)
    data["verified_order_ids"] = sorted(data["verified_order_ids"])
    data["delay_credit_issued"] = sorted(data["delay_credit_issued"])
    # exchanges_this_session (dict[str, dict[str, int]]), messages, and
    # escalations are already plain dicts/lists — JSON-safe as-is.
    return json.dumps(data)


def _json_to_state(raw: str) -> SessionState:
    data: dict[str, Any] = json.loads(raw)
    data["verified_order_ids"] = set(data["verified_order_ids"])
    data["delay_credit_issued"] = set(data["delay_credit_issued"])
    return SessionState(**data)


class SessionStore:
    """Reads/writes SessionState by session_id, Redis-first with memory fallback."""

    def __init__(self) -> None:
        self._memory: dict[str, SessionState] = {}
        self._redis: Any = None
        redis_url = os.getenv("REDIS_URL")
        if redis_url:
            try:
                import redis as redis_lib

                client = redis_lib.from_url(
                    redis_url,
                    decode_responses=True,
                    socket_connect_timeout=2,
                    socket_timeout=2,
                    retry_on_timeout=True,
                )
                client.ping()
                self._redis = client
            except Exception as error:
                logger.warning("Redis unavailable, falling back to in-memory: %s", error)
                self._redis = None

    def get(self, session_id: str) -> SessionState:
        if self._redis is not None:
            try:
                raw = self._redis.get(KEY_PREFIX + session_id)
                if raw is not None:
                    state = _json_to_state(raw)
                    self._redis.expire(KEY_PREFIX + session_id, SESSION_TTL_SECONDS)
                    self._memory[session_id] = state
                    return state
                state = SessionState()
                self.save(session_id, state)
                return state
            except Exception as error:
                logger.warning("Redis get() failed for session %s, using memory: %s", session_id, error)
        return self._memory.setdefault(session_id, SessionState())

    def save(self, session_id: str, state: SessionState) -> None:
        if self._redis is not None:
            try:
                self._redis.setex(KEY_PREFIX + session_id, SESSION_TTL_SECONDS, _state_to_json(state))
            except Exception as error:
                logger.warning("Redis save() failed for session %s: %s", session_id, error)
        # Always keep memory current too, so a mid-conversation Redis outage
        # degrades gracefully to the last-known state instead of a blank one.
        self._memory[session_id] = state

    def delete(self, session_id: str) -> None:
        if self._redis is not None:
            try:
                self._redis.delete(KEY_PREFIX + session_id)
            except Exception as error:
                logger.warning("Redis delete() failed for session %s: %s", session_id, error)
        self._memory.pop(session_id, None)

    def health(self) -> dict[str, Any]:
        if self._redis is not None:
            try:
                self._redis.ping()
                return {"backend": "redis", "connected": True}
            except Exception:
                return {"backend": "redis", "connected": False}
        return {"backend": "memory"}

    def check_exchange(self, customer_id: str, order_id: str, sku: str) -> bool:
        """True if this customer already used their one exchange for this item,
        in *any* session — not just the current one. This is a non-critical cap
        (policy §4.4 already escalates a second exchange to a human either way),
        so it fails open (returns False, i.e. "allow") whenever Redis isn't
        available rather than blocking exchanges just because persistence is
        down.
        """
        if self._redis is None:
            return False
        try:
            return bool(self._redis.exists(f"{EXCHANGE_KEY_PREFIX}{customer_id}:{order_id}:{sku}"))
        except Exception as error:
            logger.warning("Redis check_exchange() failed for %s/%s/%s: %s", customer_id, order_id, sku, error)
            return False

    def record_exchange(self, customer_id: str, order_id: str, sku: str) -> None:
        if self._redis is None:
            return
        try:
            self._redis.setex(f"{EXCHANGE_KEY_PREFIX}{customer_id}:{order_id}:{sku}", EXCHANGE_TTL_SECONDS, "1")
        except Exception as error:
            logger.warning("Redis record_exchange() failed for %s/%s/%s: %s", customer_id, order_id, sku, error)

    def publish_escalation(self, ticket: dict[str, Any]) -> None:
        """Push a new escalation ticket to a durable outbox and, optionally, a
        webhook — so a human handoff is discoverable outside this one process's
        memory instead of only living in ChatResponse.escalations.
        """
        if self._redis is not None:
            try:
                self._redis.lpush(ESCALATION_QUEUE_KEY, json.dumps(ticket, ensure_ascii=False))
                self._redis.expire(ESCALATION_QUEUE_KEY, ESCALATION_QUEUE_TTL_SECONDS)
            except Exception as error:
                logger.warning("Redis publish_escalation() failed: %s", error)

        webhook_url = os.getenv("ESCALATION_WEBHOOK_URL")
        if webhook_url:
            try:
                body = json.dumps(ticket, ensure_ascii=False).encode("utf-8")
                request = urllib.request.Request(
                    webhook_url, data=body, method="POST",
                    headers={"Content-Type": "application/json"},
                )
                # stdlib-only on purpose: this is the one integration point meant
                # to let a Slack/Zapier/Make.com webhook be wired up later with
                # zero new dependencies and zero further code changes.
                urllib.request.urlopen(request, timeout=3)
            except Exception as error:
                logger.warning("Escalation webhook POST failed: %s", error)
