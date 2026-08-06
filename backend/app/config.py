from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[2]
ASSIGNMENT_MATERIAL_DIR = ROOT_DIR / "assignment material"
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