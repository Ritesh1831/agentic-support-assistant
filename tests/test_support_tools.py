from __future__ import annotations

import pytest

from backend.app.config import ORDERS_PATH, POLICY_PATH
from backend.app.guardrails import (
    extract_identity_credentials,
    inspect_message,
    redact_payment_details,
)
from backend.app.state import SessionState
from backend.app.tools import SupportTools


@pytest.fixture
def support(monkeypatch: pytest.MonkeyPatch) -> SupportTools:
    monkeypatch.setenv("TRENDLY_NOW", "2026-08-06")
    return SupportTools(ORDERS_PATH, POLICY_PATH)


def verified(support: SupportTools, order_id: str) -> SessionState:
    state = SessionState()
    customer = support.customers[support.orders[order_id]["customer_id"]]
    assert support.lookup_order(state, order_id, email=customer["email"])["ok"]
    return state


def item(support: SupportTools, order_id: str, sku: str | None = None) -> dict:
    result = support.eligibility(verified(support, order_id), order_id, sku)
    assert result["ok"]
    return result["items"][0]


def test_lookup_fails_closed_and_is_per_order(support: SupportTools) -> None:
    state = SessionState()
    assert not support.lookup_order(state, "TR-4521")["ok"]
    assert not support.lookup_order(state, "TR-4521", email="wrong@example.com")["ok"]
    assert state.failed_verification_attempts == 1
    assert support.lookup_order(state, "TR-4521", email="ananya.rao@example.com")["ok"]
    # Verifying a customer's first order does not disclose their different order automatically.
    assert not support.lookup_order(state, "TR-4524")["ok"]


@pytest.mark.parametrize(
    ("order_id", "sku", "refund", "exchange", "reason"),
    [
        ("TR-4521", None, False, False, "NOT_DELIVERED"),
        ("TR-4522", "TR-TSH-002", True, True, "ELIGIBLE"),
        ("TR-4522", "TR-SOK-031", False, False, "NON_RETURNABLE_CATEGORY"),
        ("TR-4523", None, False, False, "OUTSIDE_30_DAY_WINDOW"),
        ("TR-4524", None, False, False, "NOT_DELIVERED"),
        ("TR-4525", None, False, False, "NOT_DELIVERED"),
        ("TR-4526", None, False, False, "NOT_DELIVERED"),
        ("TR-4527", None, False, False, "NON_RETURNABLE_CATEGORY"),
        ("TR-4528", None, False, True, "FINAL_SALE_EXCHANGE_ONLY"),
        ("TR-4529", None, False, False, "ORDER_CANCELLED"),
        ("TR-4530", None, True, True, "ELIGIBLE"),
    ],
)
def test_all_dataset_orders_follow_real_policy(support: SupportTools, order_id: str, sku: str | None, refund: bool, exchange: bool, reason: str) -> None:
    result = item(support, order_id, sku)
    assert result["refund_eligible"] is refund
    assert result["exchange_eligible"] is exchange
    assert result["reasons"][0]["code"] == reason


def test_delay_is_date_based_not_status_based(support: SupportTools) -> None:
    for order_id, days in (("TR-4521", 4), ("TR-4524", 4), ("TR-4525", 16)):
        delayed, late_days = support.is_delayed(support.orders[order_id])
        assert delayed and late_days == days


def test_delivered_on_time_order_is_never_flagged_delayed(support: SupportTools) -> None:
    # TR-4530 was delivered a day before its expected date; delivery alone must not
    # make an on-time order look delayed.
    assert support.is_delayed(support.orders["TR-4530"]) == (False, 0)


def test_delivered_order_can_still_be_flagged_delayed(support: SupportTools) -> None:
    # A delivered order that arrived well past its expected date is still delayed
    # under policy §1.5, which is not conditioned on the order still being in transit.
    support.orders["TEST-LATE-DELIVERED"] = {
        "order_id": "TEST-LATE-DELIVERED", "customer_id": "C-100", "status": "delivered",
        "delivered_at": "2026-08-01T11:00:00Z", "expected_delivery": "2026-07-20",
        "items": [{"sku": "TEST-LATE-1", "name": "Test Item", "category": "apparel", "final_sale": False}],
    }
    delayed, late_days = support.is_delayed(support.orders["TEST-LATE-DELIVERED"])
    assert delayed and late_days > 3


def test_delay_credit_is_verified_and_once_per_session(support: SupportTools) -> None:
    state = verified(support, "TR-4521")
    first = support.issue_delay_store_credit(state, "TR-4521")
    assert first["ok"] and first["amount_inr"] == 250
    assert not support.issue_delay_store_credit(state, "TR-4521")["ok"]


def test_footwear_disclosure_and_size_only_exchange(support: SupportTools) -> None:
    state = verified(support, "TR-4530")
    assert not support.initiate_return_or_exchange(state, "TR-4530", "TR-KRT-033", "exchange", "change colour to blue")["ok"]
    assert support.initiate_return_or_exchange(state, "TR-4530", "TR-KRT-033", "exchange", "size XL is needed")["ok"]
    second = support.initiate_return_or_exchange(state, "TR-4530", "TR-KRT-033", "exchange", "size L is needed")
    assert second["requires_escalation"]


def test_size_exchange_accepts_bare_numeric_size_without_the_word_size(support: SupportTools) -> None:
    # Numeric sizing (jeans, footwear) is real in the dataset; a customer does not
    # have to say the literal word "size" for this to be a legitimate size exchange.
    support.orders["TEST-NUM-SIZE"] = {
        "order_id": "TEST-NUM-SIZE", "customer_id": "C-100", "status": "delivered",
        "delivered_at": "2026-07-30T11:00:00Z", "expected_delivery": "2026-07-30",
        "payment_method": "credit_card",
        "items": [{"sku": "TEST-NUM-SIZE-1", "name": "Jeans", "category": "apparel", "final_sale": False}],
    }
    state = verified(support, "TEST-NUM-SIZE")
    result = support.initiate_return_or_exchange(
        state, "TEST-NUM-SIZE", "TEST-NUM-SIZE-1", "exchange", "need it in 30 instead, current one does not fit",
    )
    assert result["ok"]


def test_size_exchange_rejects_colour_word_without_the_word_colour(support: SupportTools) -> None:
    state = verified(support, "TR-4530")
    result = support.initiate_return_or_exchange(state, "TR-4530", "TR-KRT-033", "exchange", "I want it in blue instead")
    assert not result["ok"]


def test_size_exchange_reason_mentioning_delivered_is_not_false_flagged_as_colour(support: SupportTools) -> None:
    # "delivered" contains "red" as a substring; the denylist must match whole words
    # only, or this legitimate size-exchange reason would be wrongly rejected.
    state = verified(support, "TR-4530")
    result = support.initiate_return_or_exchange(
        state, "TR-4530", "TR-KRT-033", "exchange", "it was delivered late, please exchange for a smaller size",
    )
    assert result["ok"]


def test_eligible_footwear_includes_box_notice(support: SupportTools) -> None:
    # Synthetic in-memory record only: the immutable supplied JSON is never changed.
    support.orders["TEST-SHOE"] = {
        "order_id": "TEST-SHOE", "customer_id": "C-100", "status": "delivered",
        "delivered_at": "2026-07-30T11:00:00Z", "expected_delivery": "2026-07-30",
        "items": [{"sku": "TEST-SHOE-1", "name": "Test Sneakers", "category": "footwear", "final_sale": False}],
    }
    footwear = item(support, "TEST-SHOE")
    assert footwear["refund_eligible"]
    assert "original shoe box" in footwear["footwear_notice"]


def test_eligible_item_discloses_required_return_condition(support: SupportTools) -> None:
    eligible = item(support, "TR-4530")
    assert "unworn and unwashed" in eligible["return_condition_notice"]
    assert "original tags" in eligible["return_condition_notice"]


def test_cod_refund_creates_secure_human_handoff(support: SupportTools) -> None:
    # Synthetic in-memory record only: the immutable supplied JSON is never changed.
    support.orders["TEST-COD"] = {
        "order_id": "TEST-COD", "customer_id": "C-101", "status": "delivered",
        "delivered_at": "2026-07-30T11:00:00Z", "expected_delivery": "2026-07-30",
        "payment_method": "cash_on_delivery",
        "items": [{"sku": "TEST-COD-1", "name": "Test Top", "category": "apparel", "final_sale": False}],
    }
    state = verified(support, "TEST-COD")
    result = support.initiate_return_or_exchange(state, "TEST-COD", "TEST-COD-1", "refund", "did not fit")
    assert not result["ok"] and result["requires_escalation"]
    assert result["escalation"]["reason"] == "cod_refund_secure_details"
    assert len(state.escalations) == 1


def test_lost_order_is_escalated_on_verified_lookup(support: SupportTools) -> None:
    state = SessionState()
    lookup = support.lookup_order(state, "TR-4526", email="marcus.bell@example.com")
    assert lookup["ok"] and lookup["requires_escalation"]
    assert lookup["escalation"]["reason"] == "lost_parcel"
    duplicate = support.escalate_to_human(state, "lost_parcel", "Duplicate model request.", "high", "TR-4526")
    assert duplicate["already_escalated"]
    assert len(state.escalations) == 1


def test_damage_handoff_includes_48_hour_context(support: SupportTools) -> None:
    state = verified(support, "TR-4527")
    ticket = support.escalate_to_human(state, "damaged_item", "Customer reports damaged earrings.", "high", "TR-4527")["escalation"]
    assert "outside the 48-hour" in ticket["damage_window_context"]


def test_sensitive_details_are_detected_and_redacted() -> None:
    message = "My card is 4111 1111 1111 1111 and cvv is 123"
    assert inspect_message(message).payment_details
    redacted = redact_payment_details(message)
    assert "4111" not in redacted and "123" not in redacted


def test_explicit_identity_pair_is_extractable_for_preverification() -> None:
    credentials = extract_identity_credentials("Please help with TR-4530; my email is marcus.bell@example.com")
    assert credentials is not None
    assert credentials.order_id == "TR-4530"
    assert credentials.email == "marcus.bell@example.com"


def test_policy_search_avoids_noise_and_returns_complete_section(support: SupportTools) -> None:
    results = support.search_policy(SessionState(), "delayed order store credit")["results"]
    assert results and results[0]["section"].startswith("1. Shipping")
    assert support.search_policy(SessionState(), "quantum banana telescope") ["results"] == []


def test_verified_delayed_lookup_exposes_credit_entitlement(support: SupportTools) -> None:
    state = verified(support, "TR-4521")
    lookup = support.lookup_order(state, "TR-4521")
    assert lookup["delay_credit_available"] == {"order_id": "TR-4521", "amount_inr": 250, "business_days_late": 4}
