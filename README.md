# Trendly Agentic Support Assistant

Support agent for Trendly (fashion D2C brand). It answers order status, handles returns and size
exchanges, answers shipping/policy questions, and hands off to a human when it should. Built for
the Yellow.ai FDE (Intern) screening assignment.

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

Full breakdown of the tools and policy sections they cover is in
[`IMPLEMENTATION_HANDOFF.md`](IMPLEMENTATION_HANDOFF.md). Architecture, trade-offs, and
limitations are in [`SOLUTION.md`](SOLUTION.md).

## AI-usage note

I built this with Codex end to end — the FastAPI backend, the tool definitions, the eligibility
logic, the frontend, the tests, all of it came from Codex. I gave it the requirements and the
policy doc and let it write the code. What I did myself was test the thing properly (I wrote a
script that runs it through all 10 orders plus some edge cases and saves the actual chat replies to
a file), read through what came back, and fix the specific spots that were wrong. I did not
personally design the tool schemas or the BM25 search or anything like that, that part is Codex's
work.

Some things I actually changed myself, or told Codex to change, after testing:

- I ran the order TR-4521 through it and the bot correctly said "not delivered yet, can't return
  it", but it never mentioned that the order is late and the customer can get the ₹250 credit for
  that. The policy says that should be offered, so I told Codex to make the delay check run
  whenever an order is looked up, and add a note in the reply if the model doesn't bring it up on
  its own. Now it never skips this.

- I tested giving two wrong emails in a row for the same order to see if it would ask forever. It
  did, it just kept asking for the email again and did not stop. That's not okay for something
  that's supposed to protect customer data, so I added a rule that after 2 wrong tries, it goes to
  a human no matter what the model wants to do, the code forces it.

- In the first version of the system prompt I did not tell it that Trendly only does size
  exchanges, not colour or style. So when I asked to swap an item to a different colour, it just
  tried to do it. I added a line to the prompt saying exchanges are size only, and also added a
  check in the code as backup in case the model forgets.

- I sent a test message with a fake card number in it just to check what happens. It got sent to
  the model first, which is not something I wanted, even for a fake number. Told Codex to build a
  proper check that catches anything that looks like a card, CVV or bank number and stops it right
  there before it goes to the model at all, and shows a message asking the person not to share
  that stuff in chat.

- When I ran a full test of all 10 orders together in one go, a couple of the replies came back as
  "the support assistant is temporarily unavailable", which was just the Groq API getting rate
  limited because I was sending requests too fast one after another. There was no retry, it just
  gave up right away. I added a small retry step, so if a request fails because of a rate limit or
  a temporary server issue, it waits a bit and tries again 2-3 times before showing the
  "unavailable" message to the customer. This fixed both of those cases when I reran the same test.

I'm fine explaining or changing any part of this live, I went through the whole codebase enough
times while testing that I know how it fits together.