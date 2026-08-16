from __future__ import annotations

import logging
import os
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parents[2]
ASSIGNMENT_MATERIAL_DIR = ROOT_DIR / "material"
FRONTEND_DIR = ROOT_DIR / "frontend"
# Supports local `uvicorn backend.app.main:app`; deployment-supplied environment values
# remain authoritative because load_dotenv does not override them by default.
load_dotenv(ROOT_DIR / ".env")

ORDERS_PATH = ASSIGNMENT_MATERIAL_DIR / "orders.json"
POLICY_PATH = ASSIGNMENT_MATERIAL_DIR / "trendly_policy.md"
MODEL = os.getenv("TRENDLY_MODEL", "openai/gpt-oss-120b")
MAX_TOOL_ITERATIONS = 6
MAX_TURNS_BEFORE_ESCALATION = 16
# Groq's free tier rate-limits per minute; a turn that needs several tool round
# trips (or a burst of concurrent chats) can trip a transient 429/5xx that has
# nothing to do with the actual request. Retry a few times before giving up.
MAX_LLM_RETRIES = 3
LLM_RETRY_BASE_DELAY_SECONDS = 0.6


def business_today() -> date:
    """Return an overridable UTC date so policy tests and demos are reproducible."""
    configured = os.getenv("TRENDLY_NOW")
    return date.fromisoformat(configured) if configured else date.today()


def get_holidays() -> frozenset[date]:
    """Parse TRENDLY_HOLIDAYS ("2026-01-26,2026-08-15,2026-10-02") into a date
    set. No holiday list was given in the original assignment brief, so the
    business-day math has always skipped weekends only; this makes a real
    calendar pluggable via configuration once Trendly ops supplies one,
    without requiring a code change.
    """
    raw = os.getenv("TRENDLY_HOLIDAYS")
    if not raw:
        return frozenset()
    holidays: set[date] = set()
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            holidays.add(date.fromisoformat(token))
        except ValueError:
            logger.warning("Skipping malformed TRENDLY_HOLIDAYS entry: %r", token)
    return frozenset(holidays)


HOLIDAYS: frozenset[date] = get_holidays()
