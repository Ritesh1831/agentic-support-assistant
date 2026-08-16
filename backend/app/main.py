from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Iterator
from threading import Lock
from typing import Any, cast

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from .agent import TrendlyAgent
from .config import FRONTEND_DIR, ORDERS_PATH, POLICY_PATH
from .guardrails import (
    GuardrailSignals,
    extract_identity_credentials,
    inspect_message,
    redact_payment_details,
)
from .logging_config import configure_logging
from .session_store import ESCALATION_QUEUE_KEY, SessionStore
from .state import SessionState
from .tools import SupportTools

configure_logging()
logger = logging.getLogger(__name__)


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=5000)


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    escalations: list[dict]


app = FastAPI(title="Trendly Agentic Support Assistant", version="0.1.0")
# Rate limit by session_id (sent as the X-Session-ID header) when the frontend
# supplies one, so a shared network (office wifi, campus NAT) doesn't get one
# customer's abuse throttling for everyone on it; falls back to client IP for
# any caller that doesn't send the header (e.g. a raw API client).
limiter = Limiter(key_func=lambda request: request.headers.get("X-Session-ID", request.client.host if request.client else "unknown"))
app.state.limiter = limiter
# slowapi's handler is typed specifically for RateLimitExceeded; FastAPI's
# add_exception_handler stub expects a handler generic over Exception. This is
# a known stub-level mismatch (not a real bug), and whether mypy actually
# flags it varies by exact fastapi/starlette/slowapi/mypy version — a
# `# type: ignore` comment would be "unused" (and thus itself an error under
# warn_unused_ignores) in whichever environment doesn't hit the mismatch. A
# cast sidesteps that: it satisfies the type checker unconditionally, in
# every environment, without ever becoming a stale/unused suppression.
app.add_exception_handler(
    RateLimitExceeded, cast(Callable[[Request, Exception], Any], _rate_limit_exceeded_handler),
)
support_tools = SupportTools(ORDERS_PATH, POLICY_PATH)
agent = TrendlyAgent(support_tools)
# Session content itself now lives in session_store (Redis-backed, with an
# automatic in-memory fallback) instead of a plain module-level dict, so a
# conversation survives a process restart/redeploy when Redis is configured.
session_store = SessionStore()
# Bind the cross-session exchange cap (Bug 5) and escalation outbox (Enhancement 1)
# onto the tools instance now that both objects exist. See the comment in
# SupportTools.__init__ for why this is done as a post-construction attribute
# assignment instead of threading these through dispatch()/the tool schema.
support_tools._check_cross_session_exchange = session_store.check_exchange
support_tools._record_cross_session_exchange = session_store.record_exchange
support_tools._publish_escalation = session_store.publish_escalation
# One lock per session_id, not one global lock: a chat turn makes several sequential
# outbound Groq HTTP calls (with retry backoff), so a single process-wide lock would
# serialize every customer's conversation behind whichever one is currently talking to
# the model. The registry lock below only ever guards the lock dict itself, never I/O
# (including the session_store read/write), so it's held for microseconds and never
# becomes a bottleneck itself.
_session_locks: dict[str, Lock] = {}
_session_locks_guard = Lock()


def _get_session(session_id: str) -> tuple[SessionState, Lock]:
    with _session_locks_guard:
        lock = _session_locks.setdefault(session_id, Lock())
    return session_store.get(session_id), lock


def _run_precheck(state: SessionState, payload: ChatRequest) -> tuple[GuardrailSignals, str | None, dict[str, Any] | None]:
    """Shared by /chat and /chat/stream. Must be called with the session's
    lock already held, since it can mutate `state` (a failed/succeeded
    identity precheck updates verification counters and cached order facts).
    """
    signals = inspect_message(payload.message)
    identity_note: str | None = None
    delay_precheck: dict[str, Any] | None = None
    # An explicit ID + credential pair is a verification attempt even if the
    # model would otherwise merely ask again. This closes a data-safety gap
    # around the two-failed-verification forced-handoff rule.
    credentials = extract_identity_credentials(payload.message) if not signals.payment_details else None
    if credentials:
        verification = support_tools.lookup_order(state, credentials.order_id, credentials.email, credentials.phone)
        if verification.get("verified"):
            delay_precheck = verification.get("delay_credit_available")
            identity_note = (
                f"Deterministic identity precheck succeeded for {credentials.order_id}. "
                "Call lookup_order with that order_id before discussing its facts."
            )
        elif verification.get("error"):
            identity_note = (
                "A deterministic ownership check for the exact credentials in this message failed and has already counted. "
                "Do not retry those same credentials; ask for the correct email/phone or follow the mandatory handoff instruction."
            )
    return signals, identity_note, delay_precheck


app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # Only catches plain Exception. FastAPI/Starlette dispatch matches the most
    # specific registered handler first, and FastAPI already registers its own
    # handler for HTTPException (and RequestValidationError) ahead of this one,
    # so a 422 from ChatRequest validation or a raised HTTPException (403, 404,
    # ...) elsewhere in this module is untouched by this catch-all — it only
    # fires for a genuinely unhandled bug.
    logger.error(
        "Unhandled exception [%s %s]: %s: %s",
        request.method, request.url.path, type(exc).__name__, exc, exc_info=True,
    )
    return JSONResponse(status_code=500, content={
        "reply": "The support assistant is temporarily unavailable. Please try again shortly.",
        "session_id": "",
        "escalations": [],
    })


@app.get("/", include_in_schema=False)
def frontend() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "session_store": session_store.health()}


@app.get("/admin", include_in_schema=False)
def admin_dashboard() -> FileResponse:
    # No auth on the page itself — it's a static shell with no data baked in.
    # Every API call it makes (/admin/escalations, /admin/transcript/...) is
    # independently gated by the X-Admin-Key check, which is where the real
    # authorization boundary is enforced.
    return FileResponse(FRONTEND_DIR / "admin.html")


@app.post("/chat", response_model=ChatResponse)
@limiter.limit("30/minute")
def chat(request: Request, payload: ChatRequest) -> ChatResponse:
    # `request` here is deliberately the real Starlette/FastAPI Request, not the
    # ChatRequest body — slowapi's @limiter.limit locates the request object by
    # inspecting the endpoint's signature for a parameter literally named
    # `request` typed as starlette.requests.Request, so the body model (what
    # used to be named `request`) is renamed to `payload` to make room for it.
    if not payload.message.strip():
        raise HTTPException(status_code=422, detail="message must not be blank")
    state, session_lock = _get_session(payload.session_id)
    with session_lock:
        signals, identity_note, delay_precheck = _run_precheck(state, payload)
        reply = agent.respond(
            state, payload.message, redact_payment_details(payload.message), signals,
            identity_note, delay_precheck,
        )
        session_store.save(payload.session_id, state)
        logger.info("Chat turn processed", extra={"session_id": payload.session_id})
        return ChatResponse(session_id=payload.session_id, reply=reply, escalations=state.escalations)


@app.post("/chat/stream")
def chat_stream(payload: ChatRequest) -> StreamingResponse:
    # Kept as a separate, unlimited-by-slowapi endpoint on purpose: the
    # frontend always tries /chat/stream first and falls back to plain /chat
    # (see app.js), so rate limiting only needs to live on the one endpoint
    # that's guaranteed to be hit for every turn either way.
    if not payload.message.strip():
        raise HTTPException(status_code=422, detail="message must not be blank")
    state, session_lock = _get_session(payload.session_id)

    def event_stream() -> Iterator[str]:
        with session_lock:
            try:
                signals, identity_note, delay_precheck = _run_precheck(state, payload)
                # agent.respond_stream already catches Groq/OpenAI failures
                # internally and yields its own "temporarily unavailable"
                # fallback text (mirroring respond()), so the broad except
                # below is only reached by something outside that — a bug in
                # the precheck, serialization, etc. — not an ordinary LLM
                # outage.
                for token in agent.respond_stream(
                    state, payload.message, redact_payment_details(payload.message), signals,
                    identity_note, delay_precheck,
                ):
                    yield f"data: {json.dumps({'token': token})}\n\n"
            except Exception as error:
                logger.error(
                    "Streaming chat turn failed: %s: %s", type(error).__name__, error,
                    exc_info=True, extra={"session_id": payload.session_id},
                )
                yield f"data: {json.dumps({'error': 'temporarily unavailable'})}\n\n"
            finally:
                session_store.save(payload.session_id, state)
                yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.delete("/chat/{session_id}")
def clear_session(session_id: str) -> dict[str, Any]:
    with _session_locks_guard:
        # A process-local lock is the existence signal here: in the single-process
        # deployment this app targets, any session that was ever chatted with has
        # one. After a restart the lock registry is empty even though Redis may
        # still hold the session — sending a new message would recreate the lock
        # (and the session) again regardless, so this is a soft "not found," not a
        # guarantee the underlying Redis key is absent.
        existed = _session_locks.pop(session_id, None) is not None
    if not existed:
        raise HTTPException(status_code=404, detail="Session not found")
    session_store.delete(session_id)
    return {"cleared": True, "session_id": session_id}


@app.get("/admin/transcript/{session_id}")
def admin_transcript(session_id: str, x_admin_key: str | None = Header(default=None)) -> dict[str, Any]:
    admin_key = os.getenv("ADMIN_KEY", "")
    if not admin_key or x_admin_key != admin_key:
        raise HTTPException(status_code=403, detail="Forbidden")
    state = session_store.get(session_id)
    return {
        "session_id": session_id,
        "transcript": state.raw_transcript,
        "turn_count": state.turn_count,
        "escalations": state.escalations,
    }


@app.get("/admin/escalations")
def admin_escalations(x_admin_key: str | None = Header(default=None)) -> dict[str, Any]:
    admin_key = os.getenv("ADMIN_KEY", "")
    if not admin_key or x_admin_key != admin_key:
        raise HTTPException(status_code=403, detail="Forbidden")
    # Reaches into session_store's backend directly rather than adding new
    # SessionStore methods for this one admin-only read, since the shape of
    # the result (redis queue vs. every in-memory session's tickets) is
    # specific to this endpoint, not a general session_store concern.
    if session_store._redis is not None:
        try:
            raw_tickets = session_store._redis.lrange(ESCALATION_QUEUE_KEY, 0, -1)
            escalations = [json.loads(raw) for raw in raw_tickets]
            return {"escalations": escalations, "total": len(escalations), "source": "redis"}
        except Exception as error:
            logger.warning("Failed to read escalation queue from Redis: %s", error)
    escalations = [ticket for state in session_store._memory.values() for ticket in state.escalations]
    return {"escalations": escalations, "total": len(escalations), "source": "memory"}
