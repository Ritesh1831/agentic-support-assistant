from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any

from . import config
from .policy import PolicySearch
from .state import SessionState

logger = logging.getLogger(__name__)

NON_RETURNABLE_CATEGORIES = {
    "innerwear", "socks", "jewellery", "beauty", "fragrance", "face_masks", "gift_cards",
}

# Policy §4.1 backstop: exchanges are size-only, never colour/style. Matched on whole
# words via regex rather than substring containment, because "red" is a substring of
# ordinary words like "delivered" or "preferred" and a naive `in` check would falsely
# block plain size-exchange reasons that merely happen to contain those words.
COLOUR_STYLE_DENYLIST = re.compile(
    r"\b(colour|color|style|design|pattern|print|printed|shade|"
    r"red|blue|green|black|white|pink|yellow|purple|orange|grey|gray|"
    r"navy|maroon|beige|brown|cream|gold|silver|teal|olive|mustard|"
    r"peach|lavender|coral|burgundy|khaki|turquoise|charcoal|ivory)\b",
    re.IGNORECASE,
)


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).date()


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def business_days_after(start: date, end: date, holidays: frozenset[date] = config.HOLIDAYS) -> int:
    """Weekdays strictly after start through end, excluding `holidays` (empty by
    default — no holiday calendar was supplied in the original brief; set
    TRENDLY_HOLIDAYS to populate config.HOLIDAYS once Trendly ops has one)."""
    if end <= start:
        return 0
    count = 0
    cursor = start
    while cursor < end:
        cursor = date.fromordinal(cursor.toordinal() + 1)
        if cursor.weekday() < 5 and cursor not in holidays:
            count += 1
    return count


class SupportTools:
    def __init__(self, orders_path: Path, policy_path: Path) -> None:
        data = json.loads(orders_path.read_text(encoding="utf-8"))
        self.orders = {order["order_id"]: order for order in data["orders"]}
        self.customers = {customer["customer_id"]: customer for customer in data["customers"]}
        self.policy = PolicySearch(policy_path)
        # Optional cross-session collaborators (session_store.check_exchange /
        # .record_exchange / .publish_escalation), assigned onto the instance
        # once from main.py after construction, rather than threaded through as
        # extra parameters on initiate_return_or_exchange/escalate_to_human.
        # dispatch() calls every tool as `handler(state, **arguments)`, where
        # `arguments` comes straight from the model's tool-call JSON — routing
        # these callables through that path would mean either special-casing
        # dispatch() per tool name to inject them, or exposing them on the TOOLS
        # schema where the model could try to "supply" them itself. Setting them
        # once here needs no change to dispatch() or the TOOLS schema at all,
        # which is the smallest change that gets the same result. None means "no
        # cross-session backing store available" — the existing session-scoped
        # behavior (exchange cap, in-memory-only escalations) still applies.
        self._check_cross_session_exchange: Callable[[str, str, str], bool] | None = None
        self._record_cross_session_exchange: Callable[[str, str, str], None] | None = None
        self._publish_escalation: Callable[[dict[str, Any]], None] | None = None

    def _order_for_verified_session(self, state: SessionState, order_id: str) -> dict[str, Any] | None:
        if order_id not in state.verified_order_ids:
            return None
        return self.orders.get(order_id)

    @staticmethod
    def _existing_escalation(state: SessionState, reason: str, order_id: str | None) -> dict[str, Any] | None:
        return next((ticket for ticket in state.escalations if ticket["reason"] == reason and ticket.get("order_id") == order_id), None)

    def _create_escalation_once(
        self, state: SessionState, *, reason: str, summary: str, priority: str, order_id: str | None = None
    ) -> dict[str, Any]:
        existing = self._existing_escalation(state, reason, order_id)
        if existing:
            return existing
        return self.escalate_to_human(state, reason, summary, priority, order_id)["escalation"]

    def _damage_window_context(self, state: SessionState, order_id: str | None) -> str:
        """Policy §6 context for a human ticket; it never decides a damage claim."""
        if not order_id:
            return "48-hour reporting window could not be evaluated because no order ID was supplied."
        order = self._order_for_verified_session(state, order_id)
        if not order:
            return "48-hour reporting window could not be evaluated because the order is not verified."
        delivered_at = _parse_datetime(order.get("delivered_at"))
        if not delivered_at:
            return "48-hour reporting window could not be evaluated because this order has no delivery timestamp."
        # The assignment only provides a date override, so its start-of-day UTC is
        # used for reproducible evaluation. In production this should use a real clock.
        reference = datetime.combine(config.business_today(), time.min, tzinfo=UTC)
        elapsed_hours = max(0, int((reference - delivered_at).total_seconds() // 3600))
        status = "within" if elapsed_hours <= 48 else "outside"
        return f"Reported {elapsed_hours} hours after delivery: {status} the 48-hour policy §6.1 window."

    def is_delayed(self, order: dict[str, Any]) -> tuple[bool, int]:
        # Policy §1.5: more than three business days after expected delivery, not
        # status-based. A late-arriving order stays delayed even after delivery — use
        # the delivery date as the end point once delivered, business_today() while
        # still in transit, rather than only ever checking in-transit orders.
        expected = _parse_date(order.get("expected_delivery"))
        if not expected:
            return False, 0
        delivered = _parse_date(order.get("delivered_at"))
        end = delivered if delivered else config.business_today()
        late_days = business_days_after(expected, end, config.HOLIDAYS)
        return late_days > 3, late_days

    def _delay_credit_context(self, state: SessionState, order: dict[str, Any]) -> dict[str, Any] | None:
        """Policy §1.5 entitlement returned alongside verified order facts."""
        delayed, business_days_late = self.is_delayed(order)
        final_sale_only = bool(order["items"]) and all(item.get("final_sale", False) for item in order["items"])
        if not delayed or final_sale_only or order["order_id"] in state.delay_credit_issued:
            return None
        return {"order_id": order["order_id"], "amount_inr": 250, "business_days_late": business_days_late}

    def eligibility(self, state: SessionState, order_id: str, sku: str | None = None) -> dict[str, Any]:
        order = self._order_for_verified_session(state, order_id)
        if not order:
            return {"ok": False, "error": "Order details are unavailable until this exact order is verified."}
        selected = [item for item in order["items"] if sku is None or item["sku"] == sku]
        if sku and not selected:
            return {"ok": False, "error": "That SKU is not part of this verified order."}
        today = config.business_today()
        items: list[dict[str, Any]] = []
        for item in selected:
            result: dict[str, Any] = {
                "sku": item["sku"], "item": item["name"], "category": item["category"],
                "refund_eligible": False, "exchange_eligible": False, "reasons": [],
            }
            # Policy §2.6 short-circuits all other return gates.
            if order["status"] == "cancelled":
                result["reasons"].append({"code": "ORDER_CANCELLED", "detail": "This order was cancelled; no return can be raised."})
            elif not order.get("delivered_at"):
                result["reasons"].append({"code": "NOT_DELIVERED", "detail": "The item has not been delivered, so it cannot be returned or exchanged yet."})
            else:
                delivered = _parse_date(order["delivered_at"])
                assert delivered is not None
                days_since_delivery = (today - delivered).days
                result["days_since_delivery"] = days_since_delivery
                if today > date.fromordinal(delivered.toordinal() + 30):
                    result["reasons"].append({"code": "OUTSIDE_30_DAY_WINDOW", "detail": f"It was delivered {days_since_delivery} calendar days ago, outside the 30-day return window."})
                elif item["category"] in NON_RETURNABLE_CATEGORIES:
                    result["reasons"].append({"code": "NON_RETURNABLE_CATEGORY", "detail": f"{item['category'].replace('_', ' ').title()} items cannot be returned or exchanged under policy §2.3."})
                elif item.get("final_sale"):
                    result["exchange_eligible"] = True
                    result["reasons"].append({"code": "FINAL_SALE_EXCHANGE_ONLY", "detail": "Final-sale items are eligible for a size exchange only; refunds and store credit are not available."})
                else:
                    result["refund_eligible"] = True
                    result["exchange_eligible"] = True
                    result["reasons"].append({"code": "ELIGIBLE", "detail": "The item is within the return window and eligible for a refund or size exchange."})
            if result["refund_eligible"] or result["exchange_eligible"]:
                # Policy §2.2 cannot be validated from the dataset, but must be disclosed.
                result["return_condition_notice"] = "Items must be unworn and unwashed, with original tags attached and original packaging where provided (policy §2.2)."
            if item["category"] == "footwear" and (result["refund_eligible"] or result["exchange_eligible"]):
                result["footwear_notice"] = "Return footwear in its original shoe box; without it, ₹300 is deducted from the refund (policy §2.5)."
            items.append(result)
        return {"ok": True, "order_id": order_id, "items": items, "as_of": today.isoformat()}

    def find_orders_by_email(self, state: SessionState, email: str) -> dict[str, Any]:
        normalized = email.strip().lower()
        customer_ids = {c_id for c_id, c in self.customers.items() if c["email"].lower() == normalized}
        matches = [
            {key: order[key] for key in ("order_id", "status", "placed_at")}
            for order in self.orders.values() if order["customer_id"] in customer_ids
        ]
        return {"ok": True, "orders": matches}

    def lookup_order(self, state: SessionState, order_id: str, email: str | None = None, phone: str | None = None) -> dict[str, Any]:
        order = self.orders.get(order_id)
        if not order:
            return {"ok": False, "error": "No order was found with that ID."}
        if order_id in state.verified_order_ids:
            state.active_order_id = order_id
            response: dict[str, Any] = {"ok": True, "verified": True, "order": order}
            if delay_context := self._delay_credit_context(state, order):
                response["delay_credit_available"] = delay_context
            if order["status"] == "lost_in_transit":
                response["requires_escalation"] = True
                response["escalation"] = self._create_escalation_once(
                    state, reason="lost_parcel", order_id=order_id, priority="high",
                    summary="Carrier status is lost_in_transit. Policy §1.6 requires human handling as a lost-parcel claim, not a return.",
                )
            return response
        customer = self.customers[order["customer_id"]]
        email_matches = email is not None and email.strip().lower() == customer["email"].lower()
        phone_matches = phone is not None and phone.strip() == customer["phone"]
        if not (email_matches or phone_matches):
            if email is not None or phone is not None:
                state.failed_verification_attempts += 1
                return {"ok": False, "verified": False, "error": "The order was found, but those details do not verify ownership."}
            return {"ok": False, "verified": False, "error": "The order was found, but ownership is not verified. Ask for the email address or phone number used for this order."}
        state.verified_order_ids.add(order_id)
        state.verified_customer_id = order["customer_id"]
        state.active_order_id = order_id
        state.failed_verification_attempts = 0
        response = {"ok": True, "verified": True, "order": order}
        if delay_context := self._delay_credit_context(state, order):
            response["delay_credit_available"] = delay_context
        if order["status"] == "lost_in_transit":
            response["requires_escalation"] = True
            response["escalation"] = self._create_escalation_once(
                state, reason="lost_parcel", order_id=order_id, priority="high",
                summary="Carrier status is lost_in_transit. Policy §1.6 requires human handling as a lost-parcel claim, not a return.",
            )
        return response

    def search_policy(self, state: SessionState, query: str) -> dict[str, Any]:
        results = self.policy.search(query)
        return {"ok": True, "results": results, "message": None if results else "No relevant policy section was found."}

    def check_return_eligibility(self, state: SessionState, order_id: str, sku: str | None = None) -> dict[str, Any]:
        return self.eligibility(state, order_id, sku)

    def initiate_return_or_exchange(self, state: SessionState, order_id: str, sku: str, mode: str, reason: str) -> dict[str, Any]:
        if mode not in {"refund", "exchange"}:
            return {"ok": False, "error": "Mode must be refund or exchange."}
        eligibility = self.eligibility(state, order_id, sku)
        if not eligibility.get("ok"):
            return eligibility
        item = eligibility["items"][0]
        if mode == "refund" and not item["refund_eligible"]:
            return {"ok": False, "error": "Authoritative eligibility result does not allow a refund.", "eligibility": item}
        order = self._order_for_verified_session(state, order_id)
        assert order is not None
        # Policy §3.3: the assistant cannot collect COD bank details or complete
        # this handoff in chat, so a human ticket is created deterministically.
        if mode == "refund" and order["payment_method"] == "cash_on_delivery":
            ticket = self._create_escalation_once(
                state, reason="cod_refund_secure_details", order_id=order_id, priority="normal",
                summary=f"COD refund requested for verified order {order_id}, SKU {sku}. Bank details must be collected by a human via secure link under policy §3.3.",
            )
            return {"ok": False, "requires_escalation": True, "error": "Cash-on-delivery refunds require a human agent to collect bank details through a secure link.", "escalation": ticket}
        if mode == "exchange":
            if not item["exchange_eligible"]:
                return {"ok": False, "error": "Authoritative eligibility result does not allow an exchange.", "eligibility": item}
            if COLOUR_STYLE_DENYLIST.search(reason):
                return {"ok": False, "error": "Trendly offers size exchanges only, not colour or style exchanges (policy §4.1)."}
            exchanges = state.exchanges_this_session.setdefault(order_id, {})
            # state.exchanges_this_session only ever knows about *this* chat
            # session; a customer who opens a fresh tab bypasses the §4.4 one-
            # exchange-per-item cap entirely otherwise. self._check_cross_session_
            # exchange (bound to session_store.check_exchange from main.py) closes
            # that gap by checking a customer-scoped record that outlives any one
            # session, without requiring a verified customer_id it can't run at all.
            used_cross_session = (
                exchanges.get(sku, 0) >= 1
                or (
                    self._check_cross_session_exchange is not None
                    and state.verified_customer_id is not None
                    and self._check_cross_session_exchange(state.verified_customer_id, order_id, sku)
                )
            )
            if used_cross_session:
                ticket = self._create_escalation_once(
                    state, reason="second_exchange", order_id=order_id, priority="normal",
                    summary=f"Second size exchange requested for verified order {order_id}, SKU {sku}. Policy §4.4 requires human approval.",
                )
                return {"ok": False, "requires_escalation": True, "error": "A second exchange for this item requires human approval (policy §4.4).", "escalation": ticket}
            exchanges[sku] = exchanges.get(sku, 0) + 1
            if self._record_cross_session_exchange is not None and state.verified_customer_id is not None:
                self._record_cross_session_exchange(state.verified_customer_id, order_id, sku)
        return {
            "ok": True, "action": mode, "order_id": order_id, "sku": sku, "reason": reason,
            "message": f"{mode.title()} request initiated for {sku}.",
        }

    def issue_delay_store_credit(self, state: SessionState, order_id: str) -> dict[str, Any]:
        order = self._order_for_verified_session(state, order_id)
        if not order:
            return {"ok": False, "error": "This order must be verified before issuing store credit."}
        delayed, business_days_late = self.is_delayed(order)
        if not delayed:
            return {"ok": False, "error": "This order does not meet the policy definition of a delayed order."}
        # Policy §2.4: a final-sale-only order has no store-credit entitlement.
        if order["items"] and all(item.get("final_sale", False) for item in order["items"]):
            return {"ok": False, "error": "Final-sale items are not eligible for store credit under policy §2.4."}
        if order_id in state.delay_credit_issued:
            return {"ok": False, "error": "The ₹250 delay store credit was already issued in this session for this order."}
        state.delay_credit_issued.add(order_id)
        return {"ok": True, "order_id": order_id, "amount_inr": 250, "business_days_late": business_days_late, "message": "₹250 store credit issued."}

    def escalate_to_human(self, state: SessionState, reason: str, summary: str, priority: str, order_id: str | None = None) -> dict[str, Any]:
        existing = self._existing_escalation(state, reason, order_id)
        if existing:
            return {"ok": True, "escalation": existing, "already_escalated": True}
        ticket = {
            "ticket_id": f"ESC-{len(state.escalations) + 1:04d}", "reason": reason, "summary": summary,
            "priority": priority, "order_id": order_id, "created_at": datetime.now(UTC).isoformat(),
        }
        if any(term in reason.lower() for term in ("damaged", "wrong", "defective")):
            ticket["damage_window_context"] = self._damage_window_context(state, order_id)
        state.escalations.append(ticket)
        # This is the single place a *new* ticket is ever appended — reached both
        # by the internal deterministic escalation paths (via _create_escalation_once)
        # and by the model calling escalate_to_human directly as a tool — so hooking
        # the outbox publish here, rather than only inside _create_escalation_once,
        # covers every escalation the app can raise with one call site.
        if self._publish_escalation is not None:
            self._publish_escalation(ticket)
        return {"ok": True, "escalation": ticket}

    def dispatch(self, state: SessionState, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        handlers: dict[str, Callable[..., dict[str, Any]]] = {
            "find_orders_by_email": self.find_orders_by_email,
            "lookup_order": self.lookup_order,
            "search_policy": self.search_policy,
            "check_return_eligibility": self.check_return_eligibility,
            "initiate_return_or_exchange": self.initiate_return_or_exchange,
            "issue_delay_store_credit": self.issue_delay_store_credit,
            "escalate_to_human": self.escalate_to_human,
        }
        handler = handlers.get(name)
        if not handler:
            return {"ok": False, "error": f"Unknown tool: {name}"}
        try:
            return handler(state, **arguments)
        except (TypeError, ValueError) as error:
            # The model supplied arguments that don't match the tool's signature
            # or fail a value check — this is a bad call, not an internal bug.
            return {"ok": False, "error": f"Invalid tool arguments: {error}"}
        except Exception as error:
            # Anything else is an unexpected internal failure inside the tool
            # itself; without this, it would propagate uncaught out of the
            # agent loop and surface as an unhandled 500 instead of letting the
            # model recover (or the outer fallback reply take over) gracefully.
            logger.error(
                "Tool %s raised %s: %s", name, type(error).__name__, error,
                exc_info=True, extra={"tool_name": name},
            )
            return {"ok": False, "error": f"Tool {name} encountered an internal error. Please try again."}
