from __future__ import annotations

import os
import uuid

import pytest

# Importing this triggers config.py's load_dotenv() call, so a REDIS_URL set in
# the repo .env is visible to os.getenv() in the skipif markers below.
from backend.app import config  # noqa: F401
from backend.app.session_store import SessionStore, _json_to_state, _state_to_json
from backend.app.state import SessionState


def test_memory_fallback_when_no_redis_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REDIS_URL", raising=False)
    store = SessionStore()
    assert store.health() == {"backend": "memory"}
    state = store.get("session-a")
    assert isinstance(state, SessionState)
    state.turn_count = 3
    store.save("session-a", state)
    assert store.get("session-a").turn_count == 3


def test_get_creates_fresh_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REDIS_URL", raising=False)
    store = SessionStore()
    state = store.get("brand-new-session")
    assert state.messages == []
    assert state.turn_count == 0
    assert state.verified_order_ids == set()
    assert state.escalations == []


def test_save_and_get_roundtrip() -> None:
    # Exercises the real serialize/deserialize functions directly, independent
    # of whether a given SessionStore instance happens to be using Redis or
    # the in-memory fallback.
    state = SessionState()
    state.turn_count = 7
    state.active_order_id = "TR-4530"
    restored = _json_to_state(_state_to_json(state))
    assert restored.turn_count == 7
    assert restored.active_order_id == "TR-4530"


def test_set_serialization() -> None:
    state = SessionState()
    state.verified_order_ids = {"TR-4521", "TR-4530"}
    restored = _json_to_state(_state_to_json(state))
    assert isinstance(restored.verified_order_ids, set)
    assert restored.verified_order_ids == {"TR-4521", "TR-4530"}


def test_delete_removes_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REDIS_URL", raising=False)
    store = SessionStore()
    state = store.get("session-b")
    state.turn_count = 9
    store.save("session-b", state)
    assert store.get("session-b").turn_count == 9
    store.delete("session-b")
    assert store.get("session-b").turn_count == 0


def test_nested_dict_survives_roundtrip() -> None:
    state = SessionState()
    state.exchanges_this_session = {"TR-4530": {"TR-KRT-033": 1}}
    restored = _json_to_state(_state_to_json(state))
    assert restored.exchanges_this_session == {"TR-4530": {"TR-KRT-033": 1}}


def test_redis_unavailable_falls_back_to_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    # A well-formed but unreachable URL must degrade to memory, not crash the
    # process. This does not need a real Redis instance, so it always runs.
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    store = SessionStore()
    assert store.health() == {"backend": "memory"}


@pytest.mark.skipif(not os.getenv("REDIS_URL"), reason="REDIS_URL not set")
def test_redis_backend_roundtrip_when_available() -> None:
    store = SessionStore()
    health = store.health()
    if health["backend"] != "redis":
        # REDIS_URL is set but the connection itself failed (wrong scheme,
        # host unreachable, credentials rejected, etc.) — SessionStore already
        # logged a warning and fell back to memory by design. Skip rather than
        # fail: this test's purpose is to verify a *working* Redis round trip,
        # not to re-diagnose a broken connection string.
        pytest.skip("REDIS_URL is set but not connectable; see warning log")
    assert health["connected"] is True

    session_id = f"test-{uuid.uuid4()}"
    state = store.get(session_id)
    state.turn_count = 4
    state.verified_order_ids = {"TR-4530"}
    store.save(session_id, state)

    # A second, independent SessionStore instance forces a real round trip
    # through Redis rather than the first instance's in-process memory dict,
    # proving persistence actually happened server-side.
    other_store = SessionStore()
    reloaded = other_store.get(session_id)
    assert reloaded.turn_count == 4
    assert reloaded.verified_order_ids == {"TR-4530"}

    store.delete(session_id)
    assert store.get(session_id).turn_count == 0
