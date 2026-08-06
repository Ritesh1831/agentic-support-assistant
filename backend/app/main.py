from __future__ import annotations

from threading import Lock

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agent import TrendlyAgent
from .config import FRONTEND_DIR, ORDERS_PATH, POLICY_PATH
from .guardrails import extract_identity_credentials, inspect_message, redact_payment_details
from .state import SessionState
from .tools import SupportTools


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=5000)


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    escalations: list[dict]


app = FastAPI(title="Trendly Agentic Support Assistant", version="0.1.0")
support_tools = SupportTools(ORDERS_PATH, POLICY_PATH)
agent = TrendlyAgent(support_tools)
sessions: dict[str, SessionState] = {}
sessions_lock = Lock()

app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.get("/", include_in_schema=False)
def frontend() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest) -> ChatResponse:
    if not request.message.strip():
        raise HTTPException(status_code=422, detail="message must not be blank")
    with sessions_lock:
        state = sessions.setdefault(request.session_id, SessionState())
        signals = inspect_message(request.message)
        identity_note: str | None = None
        delay_precheck: dict | None = None
        # An explicit ID + credential pair is a verification attempt even if the
        # model would otherwise merely ask again. This closes a data-safety gap
        # around the two-failed-verification forced-handoff rule.
        credentials = extract_identity_credentials(request.message) if not signals.payment_details else None
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
        reply = agent.respond(
            state, request.message, redact_payment_details(request.message), signals,
            identity_note, delay_precheck,
        )
        return ChatResponse(session_id=request.session_id, reply=reply, escalations=state.escalations)
