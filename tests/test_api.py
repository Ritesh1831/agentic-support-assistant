from fastapi.testclient import TestClient

import json
from types import SimpleNamespace

import httpx
import pytest
from openai import InternalServerError, RateLimitError

from backend.app.agent import TrendlyAgent
from backend.app.config import ORDERS_PATH, POLICY_PATH
from backend.app.guardrails import inspect_message
from backend.app.main import app, sessions
from backend.app.state import SessionState
from backend.app.tools import SupportTools


def _fake_response(status_code: int) -> httpx.Response:
    request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    return httpx.Response(status_code=status_code, request=request, json={"error": {"message": "synthetic"}})


def test_health_endpoint() -> None:
    client = TestClient(app)
    assert client.get("/health").json() == {"status": "ok"}


def test_frontend_is_served_from_root() -> None:
    client = TestClient(app)
    response = client.get("/")
    assert response.status_code == 200
    assert "Trendly Assist" in response.text
    assert "/static/app.js" in response.text


def test_sensitive_payment_input_never_reaches_llm() -> None:
    sessions.clear()
    client = TestClient(app)
    response = client.post("/chat", json={"session_id": "sensitive-test", "message": "My card is 4111 1111 1111 1111; CVV 123"})
    assert response.status_code == 200
    assert "do not send bank, card, or CVV details" in response.json()["reply"]
    messages = sessions["sensitive-test"].messages
    assert all("4111" not in str(entry) and "CVV 123" not in str(entry) for entry in messages)


def test_missing_api_key_fails_gracefully(monkeypatch) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    client = TestClient(app)
    response = client.post("/chat", json={"session_id": "no-key-test", "message": "What is the return window?"})
    assert response.status_code == 200
    assert "temporarily unavailable" in response.json()["reply"]


def test_completed_action_uses_tool_message_not_model_embellishment(monkeypatch) -> None:
    tools = SupportTools(ORDERS_PATH, POLICY_PATH)
    state = SessionState()
    assert tools.lookup_order(state, "TR-4530", email="marcus.bell@example.com")["ok"]

    class ToolCall:
        id = "call-1"
        function = SimpleNamespace(name="initiate_return_or_exchange", arguments=json.dumps({
            "order_id": "TR-4530", "sku": "TR-KRT-033", "mode": "refund", "reason": "did not fit",
        }))

        def model_dump(self):
            return {"id": self.id, "type": "function", "function": {"name": self.function.name, "arguments": self.function.arguments}}

    replies = [
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=None, tool_calls=[ToolCall()]))]),
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="A confirmation email is on its way.", tool_calls=[]))]),
    ]

    class FakeCompletions:
        def create(self, **_kwargs):
            return replies.pop(0)

    agent = TrendlyAgent(tools)
    monkeypatch.setattr(agent, "_client", lambda: SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions())))
    reply = agent.respond(state, "Yes, refund it because it did not fit.", "Yes, refund it because it did not fit.", inspect_message("Yes, refund it because it did not fit."))
    assert reply == "Refund request initiated for TR-KRT-033."
    assert "confirmation email" not in state.messages[-1]["content"]


def test_transient_rate_limit_is_retried_then_succeeds(monkeypatch) -> None:
    # Simulates the exact pattern seen in the recorded live-eval run: a request
    # fails once on a 429, and a short retry recovers it instead of the customer
    # seeing "temporarily unavailable" for what was really a rate-limit blip.
    monkeypatch.setattr("backend.app.agent.time.sleep", lambda _seconds: None)
    tools = SupportTools(ORDERS_PATH, POLICY_PATH)
    state = SessionState()

    attempts: list[int] = []

    class FlakyCompletions:
        def create(self, **_kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                raise RateLimitError("rate limited", response=_fake_response(429), body=None)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content="Trendly offers free standard shipping on orders of ₹1,499 or more.", tool_calls=[]))])

    agent = TrendlyAgent(tools)
    monkeypatch.setattr(agent, "_client", lambda: SimpleNamespace(chat=SimpleNamespace(completions=FlakyCompletions())))
    reply = agent.respond(state, "what are your shipping charges", "what are your shipping charges", inspect_message("what are your shipping charges"))
    assert len(attempts) == 2
    assert "temporarily unavailable" not in reply
    assert "₹1,499" in reply


def test_retries_exhausted_falls_back_to_temporarily_unavailable(monkeypatch) -> None:
    monkeypatch.setattr("backend.app.agent.time.sleep", lambda _seconds: None)
    tools = SupportTools(ORDERS_PATH, POLICY_PATH)
    state = SessionState()

    class AlwaysDownCompletions:
        def create(self, **_kwargs):
            raise InternalServerError("provider down", response=_fake_response(503), body=None)

    agent = TrendlyAgent(tools)
    monkeypatch.setattr(agent, "_client", lambda: SimpleNamespace(chat=SimpleNamespace(completions=AlwaysDownCompletions())))
    reply = agent.respond(state, "hello", "hello", inspect_message("hello"))
    assert "temporarily unavailable" in reply