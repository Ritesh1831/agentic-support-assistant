# Solution Note

## What this is

A support agent for Trendly that handles order status, returns, size exchanges, and shipping
policy questions in chat, and hands off to a human when it needs to. Built for the Yellow.ai FDE
assignment. Stack is FastAPI on the backend, a plain HTML/CSS/JS chat page on the frontend, and
Groq (free tier, model `openai/gpt-oss-120b`) for the LLM.

## Architecture

The backend has one main route, `POST /chat`. Each browser tab gets a `session_id`, and the
backend keeps a small state object in memory for that session: what's been verified, which order
is active, how many times verification has failed, how many exchanges have been used, and a log of
any escalations raised. This is how the app remembers things across turns without the customer
having to repeat themselves.

When a message comes in, it goes through a few checks before it ever reaches the model:

1. A quick regex scan for things like card numbers, CVV, or an explicit request for a human. If
   there's a card number in there, it gets stripped out before the message goes anywhere near the
   LLM, and the customer gets a fixed reply telling them not to share that in chat.
2. A check for whether this message, combined with an order ID it mentions, matches a valid
   verification attempt. If someone gives the wrong email twice in a row for the same order, the
   system forces an escalation on its own, it doesn't wait for the model to notice.
3. If none of that fires, the message goes to the model along with the system prompt, the chat
   history, and the list of tools it can call.

The model then decides what to do. It has seven tools: look up orders by email, look up one order
in full, search the policy doc, check return eligibility, start a return or exchange, issue a
₹250 delay credit, and escalate to a human. It can call more than one tool in a row before giving
a final answer, up to a limit of six calls per turn, so it can chain steps like "look up the order,
then check eligibility, then explain the result" without needing a separate planning step.

The important design decision here is that the model is only allowed to relay answers, not
calculate them. Whether an order is eligible for a return, whether it's late enough to earn the
₹250 credit, whether a customer is who they say they are, all of that is worked out in plain
Python functions, not by the model reading policy text and reasoning about it. The model calls a
tool, gets back a yes/no with a reason code, and repeats that reason back to the customer. This
matters because a language model can be talked into bending on things like this if a customer
pushes hard enough, but a Python if-statement can't.

## Key trade-offs

**Policy search is a small home-made BM25-style index, not embeddings.** The policy doc is short
(a handful of sections), so a proper vector search felt like overkill for something this size, and
it also avoids needing another paid or free-tier API just for search. The downside is it's matching
on words, not meaning, so a customer asking something in a roundabout way might not pull up the
right section as easily as an embedding search would. It works fine for direct questions like
"what's your return window" but would probably need an upgrade if the policy doc grew a lot bigger.

**Session state lives in memory, not a database.** This was the fastest way to get multi-turn
memory working for a one-day build, and it's fine for a demo. But it means every session is gone
the moment the server restarts, and if this app runs on more than one server process at once (which
you'd want for real traffic), each process would have its own separate memory and a customer could
get inconsistent answers depending which one they hit. For the assignment this is an accepted
shortcut, not an oversight.

**Guardrails are regex-based, not a second model call.** Detecting things like "customer wants a
discount" or "this looks like a card number" is done with pattern matching, not by asking the LLM
a second time to classify the message. This keeps things fast and free (no extra API call), but
regex only catches what it's written to catch. A customer phrasing a request for money back in an
unusual way might slip past the discount check and reach the model directly, relying on the system
prompt to say no instead of a guaranteed code-level block.

**The math for "delayed" excludes weekends only, not holidays.** No public holiday list was given
anywhere in the assignment material, so the business-day counting only skips Saturday and Sunday.
This is called out here on purpose because it's a real gap, not something quietly assumed to be
fine.

## Known limitations

- No inventory or stock data exists anywhere in the given files, so when a customer asks for a
  size exchange, the app cannot actually check if that size is in stock. It's honest about this
  with the customer and tells them what the policy says happens if the size is out of stock
  (converts to a refund), rather than guessing or pretending to check.
- The one-exchange-per-item rule is only tracked for the current chat session. There's no order
  history database, so if a customer starts a new session, the app has no memory that they already
  used their one exchange earlier. A real version of this would need to check actual order/exchange
  history, not session memory.
- The "no tracking movement for 10 days" trigger for a lost parcel (mentioned in the policy) can't
  be checked, because the order data given doesn't include a tracking event timeline, just a
  status field. Only the "carrier marked it lost" trigger can actually be detected from the data.
- Everything runs in memory on a single process. A server restart wipes all active sessions,
  escalation tickets, and delay-credit records. This is fine for a demo or a screening assignment,
  not fine for real customer traffic.
- The item condition rule (unworn, unwashed, tags attached) is something the app can tell the
  customer about, but obviously can't verify from a chat message. That part still needs an actual
  human or warehouse check once the item is physically returned.
- Guardrail detection (discount requests, injection attempts, payment details) is pattern-based and
  was tuned against the specific phrasings tried during testing. It is not guaranteed to catch
  every possible way someone could phrase the same request.
- Test suite covers all 10 given orders plus the deterministic logic (31 tests total, no live model
  call needed for those), but full conversational behaviour (does the model actually call the right
  tool, in the right order, and phrase things the way the prompt asks) can only be checked by
  actually running it against the live Groq API, which is a slower and less repeatable kind of test.

## Five discovery questions for Trendly's ops team

1. **What system actually holds order history and exchange history in production?** Right now the
   one-exchange-per-item rule only lasts as long as one chat session. Before this goes live, it
   needs to check a real record of what's already happened to that order, not just what happened
   in the current conversation.

2. **Is there a public holiday calendar we should be using for the delay math?** The 3-business-day
   delay rule only excludes weekends right now, since no holiday list was given. If Trendly has
   fixed shipping/business holidays, the delay calculation should account for those too, or a
   customer could get told they're not eligible for the ₹250 credit on a day that was actually a
   holiday, which would be wrong in their favour or against it depending which way the mistake
   goes.

3. **Where should escalation tickets actually land?** Right now they're just stored as records
   inside the app's own memory. For this to be useful to a real support team, it needs to go
   somewhere they actually check, like a helpdesk tool, a Slack channel, or an email queue, and
   that needs deciding before this can really replace any human workflow.

4. **How should size/stock availability actually be checked?** This is the biggest functional gap.
   The app currently just tells the customer it can't check stock and asks them to confirm if their
   size is unavailable. If Trendly has an inventory system with an API, connecting to it would let
   the exchange flow work properly instead of relying on the customer's word.

5. **What's an acceptable rate of sending something to a human that the bot could have handled
   itself?** The current guardrails lean toward escalating when unsure, since getting this wrong in
   the other direction (the bot deciding something it shouldn't have) is worse. But if that means
   too many easy questions get sent to a human anyway, that defeats the point of automating this.
   Ops would know better than anyone what's an acceptable trade-off here between safe and useful.