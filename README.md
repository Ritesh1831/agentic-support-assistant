# Trendly Agentic Support Assistant

Support agent for Trendly (fashion D2C brand). It answers order status, handles returns and size
exchanges, answers shipping/policy questions, and hands off to a human when it should.

## Base URL / start command

Local: `http://127.0.0.1:8000/` (chat UI at `/`, API docs at `/docs`, health check at `/health`)

```powershell
uv venv .venv
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
.venv\Scripts\python.exe -m uvicorn backend.app.main:app --reload --port 8000
```

If you don't use `uv`, plain venv + pip works the same way:

```bash
python -m venv .venv
source .venv/bin/activate        # .venv\Scripts\activate on Windows
pip install -r requirements.txt
uvicorn backend.app.main:app --reload --port 8000
```

Add your key to `.env` in the repo root before starting:

```env
GROQ_API_KEY=your_groq_key_here
TRENDLY_MODEL=openai/gpt-oss-120b
TRENDLY_NOW=2026-08-06
```

`TRENDLY_NOW` is optional — it pins "today" so the 30-day window and delay math give the same
result every time you test. Leave it out and it just uses the real date.

Run tests with:

```bash
python -m pytest -q
```

Live deployment: Render, using the included `render.yaml`. Build command
`pip install -r requirements.txt`, start command
`uvicorn backend.app.main:app --host 0.0.0.0 --port $PORT`, health path `/health`. Set
`GROQ_API_KEY` as a secret in the Render dashboard, don't commit `.env`.

## What it does

Seven tools, real function calling (the model picks which tool to call, not a keyword router):
`find_orders_by_email`, `lookup_order`, `search_policy`, `check_return_eligibility`,
`initiate_return_or_exchange`, `issue_delay_store_credit`, `escalate_to_human`.

The important part: eligibility, identity verification, delay math, and the one-exchange limit
are all worked out in plain Python code, not left for the model to guess from the policy text. The
model can only call the tool and pass on what it says. This is what stops it from making up a
policy or approving something it shouldn't.

Order IDs alone don't unlock anything — every order needs a matching email or phone before any
detail is shown. Two wrong tries and it stops asking and hands off to a human on its own, even if
the model itself doesn't think to do that. Bank/card/CVV numbers never reach the model at all —
they get caught and blocked before the request is even sent.