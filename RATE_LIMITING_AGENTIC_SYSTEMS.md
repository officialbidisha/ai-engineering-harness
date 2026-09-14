# Rate limiting for agentic systems

Rate limiting is usually taught as "protect an API from too many requests."
That framing undersells what it actually has to do once the thing making
requests is an **agent** rather than a human clicking a button: one
top-level trigger can fan out into dozens of LLM calls, tool calls, and
retries; the cost of a single call isn't known until it's finished
streaming; and a bug in the agent's own control flow can generate more
load in one run than a thousand normal users would in a day. This doc
covers the five core algorithms in full, then the parts that are specific
to agentic systems — multi-layer enforcement, unknown-cost-upfront
requests, runaway-loop protection, and protecting fragile things
downstream — with references to the runnable examples already in this
repo (`send_tavily.py`) where the pattern is actually implemented.

---

## Part 1: why agentic systems break the classic model

A classic API rate limiter assumes: one inbound request ≈ one unit of
work, of roughly known and bounded cost, initiated by something with
human-scale patience for a 429. None of those three assumptions survive
contact with an agent:

1. **One trigger, unbounded fan-out.** A single user request ("summarize
   this account") can become a triage call, three parallel retrieval
   calls, a synthesis call, and — if anything in that chain retries or a
   planning loop decides it needs more evidence — an arbitrary number of
   further calls. Rate limiting the *inbound* request tells you nothing
   about the *actual* load it generates.
2. **Cost isn't known until the call finishes.** An LLM call's true cost
   is a function of how many tokens it actually generates, which you
   don't know until generation completes — especially with streaming.
   A classic limiter that charges a flat `cost=1` per request is
   measuring the wrong thing entirely.
3. **A stuck loop is not "too many users," it's one broken run.** A
   retry-until-success node, or a planner that keeps deciding it needs
   "just one more" piece of evidence, can generate more requests in ten
   seconds than your entire real user base generates in an hour — and no
   per-user or per-IP limiter was designed to catch a single run
   misbehaving against itself.

The rest of this doc is organized around fixing each of these three
assumptions in turn.

---

## Part 2: the five core algorithms, in full

Every rate limiter answers the same question — "should this unit of work
be admitted right now?" — with a different trade-off between exactness,
memory, and how it handles bursts.

### 1. Token bucket

A bucket holds up to `capacity` tokens and refills continuously at `rate`
tokens/second. A request is admitted if enough tokens are available.

```python
def allow(self, cost=1.0):
    now = self.clock.now()
    self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
    self.last = now
    if self.tokens >= cost:
        self.tokens -= cost
        return True
    return False
```

**Property:** permits a burst up to `capacity` (an idle client "saves up"),
then throttles to the steady `rate`. **The property that matters most for
LLM traffic specifically:** `cost` doesn't have to be `1` — charging
`cost = estimated_tokens / 1000` turns this into an actual *token* budget
instead of a request-count budget, which is the correct unit when one
call is 200 tokens and the next is 200,000. This is the standard answer
to "how do you prevent denial-of-wallet."

Verified, `capacity=5, rate=1/s`:
```
8 instant requests: [True, True, True, True, True, False, False, False]
after advancing 3s: [True, True, True, False]     # exactly 3 tokens refilled
```

### 2. Leaky bucket

Requests enter a fixed-size queue that drains ("leaks") at a **constant**
rate, regardless of how they arrived.

```python
def allow(self, cost=1.0):
    now = self.clock.now()
    self.level = max(0.0, self.level - (now - self.last) * self.leak_rate)
    self.last = now
    if self.level + cost <= self.capacity:
        self.level += cost
        return True
    return False
```

**Property:** output is perfectly smoothed — bursts never reach whatever's
downstream. **Where it earns its place over token bucket:** token bucket
protects *you* from a bursty caller; leaky bucket protects **the thing
behind you** — a third-party API with a hard per-second cap that will
429 you (or worse) the instant you burst at it, no matter how briefly.
If your agent fans out five parallel searches against a provider with a
strict per-second ceiling, leaky-bucket-style pacing on the *outbound*
side is what keeps that fan-out from tripping the provider's own limiter.

Verified, same parameters: `[True ×5, False ×3]` — identical admission
pattern to token bucket for a burst from empty, but the *shaping* differs
under sustained load: leaky bucket paces admission at exactly
`leak_rate`; token bucket lets a full-capacity burst through
instantaneously the moment it's refilled.

### 3. Fixed window counter

Count requests per fixed calendar window; reset at the boundary.

**Property:** O(1) memory, trivial to implement. **The flaw, and it's
serious:** the counter has no memory of the previous window, so a client
can send `limit` requests at the tail of one window and `limit` more at
the head of the next — both individually within-limit, but landing within
a fraction of a second of each other in real time.

Verified, `limit=100 per 60s`:
```
100 allowed at t=59s, 100 at t=60s -> 200 in a 1.0s span. 2x the intended rate.
```

### 4. Sliding window log

Store a timestamp for every allowed request; on each new request, evict
timestamps older than the window and count what remains.

**Property: exact.** No boundary burst, no approximation — the window is
always "the last N seconds, ending right now," recalculated fresh on
every call, with no fixed calendar alignment to exploit.

Verified on the same adversarial pattern that broke fixed window:
```
100 at t=59s, 0 at t=60s -> 100 total. Boundary burst eliminated.
```

**The cost:** O(limit) memory *per key*. A limit of 10,000/minute across
1,000 tenants is up to 10 million timestamps resident at once — at scale
this becomes the dominant cost, which is why it's usually not what
actually runs in production.

### 5. Sliding window counter — the production default

The compromise: keep only **two** counters per key — the current window's
count and the previous window's count — and estimate the true rolling
count by weighting the previous window by how much of it still overlaps
the sliding N-second lookback:

```python
elapsed  = (now mod window) / window
estimate = prev_count * (1 - elapsed) + cur_count
```

Walk the intuition: `elapsed` is "how far into the current window are
we, as a fraction" — 0.0 right at the start of the window, approaching
1.0 right before it rolls over. `(1 - elapsed)` is therefore "how much of
the *previous* window's traffic should still count as if it happened
within the last N seconds." Right at the start of a new window
(`elapsed≈0`), almost all of the previous window's count still applies —
which is exactly what should happen, since almost the entire sliding
lookback still overlaps the previous window at that instant. By the time
you're halfway through the current window (`elapsed=0.5`), only half of
the previous window's count is still "in view."

Verified:
```
100 at t=59s, 0 at t=60s -> 100 total     (at t=60, elapsed≈0, so prev counts almost fully)
at t=90s (halfway into window 1): another 50 allowed
                                  — weight on the previous window has decayed to ~0.5
```

**Property:** O(1) memory, closes the boundary-burst hole from fixed
window, and is only an *approximation* — it assumes requests were
uniformly distributed within the previous window, so it can be slightly
wrong if that window was itself bursty. In practice the error is small
and bounded, which is why this — not the exact sliding log — is what most
production rate limiters actually run.

### Comparison

| Algorithm | Memory/key | Bursts | Exact | Boundary burst | Smooths output | Use when |
|---|---|---|---|---|---|---|
| Token bucket | O(1) | Allowed up to capacity | Yes | No | No | Variable-cost LLM token budgets |
| Leaky bucket | O(1) | Absorbed into the queue | Yes | No | **Yes** | Protecting a fragile downstream with a hard cap |
| Fixed window | O(1) | Yes, and at the boundary | Yes within a window | **Yes — 2× limit** | No | Coarse quotas where a burst is tolerable |
| Sliding window log | **O(limit)** | No | **Yes** | No | No | Low-cardinality keys, strict correctness |
| Sliding window counter | O(1) | Bounded | Approximate | No | No | **The production default** at high cardinality |

---

## Part 3: multi-layer enforcement — the core agentic-system difference

A single limiter at the API boundary answers "is this inbound request
allowed?" — it says nothing about what that request then does internally.
An agentic system needs limits at **every layer a fan-out can happen**,
because a request that passes the outer limiter can still generate
unbounded internal load:

```
per-user / per-session request rate     (classic API layer — coarse, cheap, first line of defense)
  → per-thread / per-run token budget    (a token-bucket, variable cost, scoped to one agent run)
    → per-node / per-tool-call ceiling   (structural, not time-based — see Part 5)
      → per-tool-type budget             (a misbehaving single tool shouldn't hide inside aggregate headroom)
        → per-provider rate limit        (leaky-bucket-shaped, protecting what's actually downstream)
```

Each layer catches a different failure mode. The outer layer catches a
human or script hammering your API. The per-run budget catches a single
expensive conversation before it becomes an expensive habit. The
structural fan-out ceiling (next section) catches a planning bug, not a
volume problem at all. The per-tool budget catches one tool's outage or
misbehavior from being masked by the fact that the *other* nine tools
still have headroom. The provider-level limiter protects the one thing
you don't control the failure mode of.

**The design mistake to avoid:** treating "we have a rate limiter" as one
answer. The question "where" is doing as much work as the question "which
algorithm" — a system with only the outer layer looks fine under load
testing with well-behaved clients and then falls over the first time an
internal loop misbehaves, because nothing downstream of the entry point
was ever bounded at all.

---

## Part 4: structural ceilings — bounding a single run, not traffic over time

Everything in Part 2 bounds requests **over time**. A stuck planning loop
or a pathological fan-out isn't a time-based problem — it can happen
entirely within one run, in seconds, and a rate limiter tuned for
sustained abuse won't even notice it happening, because the aggregate
rate might still look normal system-wide.

This is a genuinely different mechanism: a **hard, structural ceiling on
fan-out width or step count within a single run**, independent of
anything time-based. This repo already has one, in `send_tavily.py`:

```python
MAX_QUERIES = 5

def fan_out(state: FanState):
    list_length = len(state["queries"])
    if list_length > MAX_QUERIES:
        raise ValueError(f"The total fan out is {list_length} which exceeds {MAX_QUERIES}")
    return [Send("retrieve", {"query": q}) for q in state["queries"]]
```

This isn't rate limiting in the Part 2 sense at all — there's no clock,
no bucket, no window. It's a guard that says "no single run is allowed to
fan out into more than 5 parallel branches, full stop, regardless of how
much budget is left or how fast the clock is ticking." If an upstream
planning step ever produced 500 queries instead of 5 — a prompt-injection
payload, a model hallucinating a huge list, a bug — this is what catches
it, and none of the time-based algorithms in Part 2 would have, because
500 requests arriving in one burst from one run doesn't necessarily look
anomalous to a limiter that's watching aggregate traffic across all
users.

**The general pattern for any agentic system:** put a hard ceiling on
(a) fan-out width per step, (b) total steps/node-visits per run, and
(c) total recursion depth for any planning/replanning loop. These are
cheap to implement, catch an entire class of bug that temporal rate
limiting structurally cannot see, and should be treated as a *required*
companion to Part 2's limiters, not a redundant extra.

---

## Part 5: the unknown-cost-upfront problem

An LLM call's true cost isn't known until it finishes — `max_tokens` is
an upper bound, not the actual cost, and with streaming output the exact
token count only exists once the stream ends. This breaks the simple
"charge cost, then proceed" model from Part 2's token bucket cleanly.

Two practical answers, and they compose:

- **Charge an estimate upfront, reconcile after.** Admit the call against
  `cost = max_tokens / 1000` (or a cheaper heuristic on prompt length),
  then once the real usage is known, credit the difference back into the
  bucket. This means the bucket is briefly *pessimistic* (it holds less
  available capacity than it truly has, mid-flight) rather than
  optimistic — the safe direction to be wrong in.
- **Charge incrementally while streaming**, for cases where even a
  temporary over-reservation is unacceptable (a very tight per-second
  provider cap) — more accurate, but couples the limiter to the
  generation loop itself rather than just the request boundary, which is
  real added complexity to weigh against the accuracy gain.

**The tool-call version of this problem is different, not the same
problem restated.** A tool call that costs real money per invocation (a
paid third-party lookup) or carries real risk (a destructive action) 
isn't well modeled as "tokens" at all — the right budget there is denominated in
**dollars or in a risk-weighted count**, tracked per tool, independent of
the LLM token budget. Conflating the two — treating a $2-per-call paid
API and a $0.001 LLM call as the same "unit" in one shared budget — hides
the fact that ten calls to the expensive tool can quietly consume what
was meant to be a whole day's spend.

---

## Part 6: protecting fragile things downstream (the leaky-bucket case, concretely)

`send_tavily.py`'s `retrieve` node calls a third-party search API from
inside a `Send`-based fan-out — meaning several branches can call out to
Tavily concurrently, from a single run:

```python
async def retrieve(state: SubState) -> dict:
    try:
        result = await tavily.search(query=state["query"], max_results=2, include_raw_content=False)
        ...
    except (UsageLimitExceededError, BadRequestError, ForbiddenError,
            InvalidAPIKeyError, TavilyTimeoutError, requests.exceptions.RequestException) as e:
        hits = [{"query": state["query"], "error": str(e)}]
    return {"hits": hits}
```

Two things worth naming precisely here:

- **The fan-out ceiling from Part 4 is doing double duty.** `MAX_QUERIES`
  doesn't just bound this run's own cost — it bounds how many concurrent
  requests get thrown at Tavily at once, which is a leaky-bucket-shaped
  concern (don't burst a downstream provider) being partially satisfied
  by a structural ceiling (Part 4) rather than a temporal limiter (Part
  2). In a system with a much wider legitimate fan-out requirement, you'd
  want an actual leaky bucket in front of the Tavily client specifically,
  not just a fixed cap on branch count — the cap protects against
  pathological width; a real leaky bucket would also pace *legitimate*
  width against Tavily's own rate limit.
- **`UsageLimitExceededError` being caught per-branch, not per-run, is a
  deliberate isolation choice.** One branch hitting Tavily's own rate
  limit doesn't fail the whole run — it returns a hit annotated with an
  error, and `compile_answer` (elsewhere in the same file) already
  separates `good_hits` from `failed_queries` and reports both. This is
  the same per-branch failure isolation principle from
  `SYSTEM_DESIGN_PATTERNS.md`'s design 1, applied to the specific case
  where the failure *is* a rate-limit rejection from something
  downstream.

---

## Part 7: rate limiting to protect a human, not just a system

An approval-gated write agent (see `SYSTEM_DESIGN_PATTERNS.md` design 3)
doesn't remove the need for rate limiting — it relocates part of the
problem. The *proposal-generation* step still needs standard LLM rate
limiting. But there's a layer that's easy to miss entirely: **the
approval queue itself has a capacity, and it's a human's.**

If an agent (or a burst of triggering events) generates proposals faster
than a human reviewer can reasonably evaluate them, the queue backs up,
review quality degrades under pressure, and the entire safety property
the approval gate exists for gets quietly eroded — not because the
gate failed, but because it was never paced to the reviewer's actual
throughput. The fix is the same shape as everything else in this doc,
applied to a human instead of a downstream system: a bounded queue with
a defined max depth, and a policy for what happens when it's full (queue,
reject new proposals, or — the leaky-bucket framing — smooth the
*rate* at which new proposals are surfaced to the reviewer, even if the
agent itself generated them in a burst).

---

## Part 8: retries, backoff, and why a retry storm is a self-inflicted denial-of-wallet

A 429 response should trigger **exponential backoff with jitter** on the
caller's side, not an immediate retry — a fleet of callers all retrying
on the same fixed delay after being rejected synchronizes into another
burst, arriving together and getting rejected together, indefinitely.
Jitter (randomizing the delay slightly per caller) breaks that
synchronization.

The sharper point for an agentic system specifically: **retries must be
idempotent**, independent of rate limiting entirely — this is the same
requirement covered in depth in `docs/CAP_THEOREM_AND_AGENTIC_SYSTEMS.md`
for write actions, and it applies here for exactly the same reason. A
naive retry loop that doesn't know whether its previous attempt actually
succeeded or just timed out can turn a single legitimate call into
several actual calls to whatever's downstream — which is, mechanically,
a self-inflicted denial-of-wallet event, generated by your own retry
logic rather than by an attacker or a misbehaving user.

---

## Rapid-fire

| Probe | One-line answer |
|---|---|
| Token bucket vs leaky bucket | Token bucket controls admission and allows bursts (protects you from a bursty caller); leaky bucket controls output pacing and smooths bursts away (protects whatever's downstream of you). |
| Why is `cost=1` per request wrong for LLM traffic | One call can be 200 tokens, another 200,000 — charge `cost = estimated_tokens / 1000` so the budget is a token budget, not a request-count budget. |
| Fixed window's flaw | No memory of the previous window — a client can get 2× the limit by straddling the boundary. |
| Sliding window log vs counter | Log is exact but O(limit) memory per key; counter is O(1) and approximate (assumes uniform distribution in the previous window) — counter is the production default at scale. |
| Why does an agentic system need more than one rate limiter | A single inbound request can fan out into unbounded internal calls — limits are needed at the user, per-run, per-tool-call, per-tool-type, and provider layers, each catching a different failure mode. |
| Why isn't a time-based rate limiter enough for a runaway planning loop | The loop can exhaust budget entirely within one run, in seconds — aggregate traffic can still look normal system-wide. Needs a structural ceiling on fan-out width / step count, independent of the clock. |
| How do you rate-limit a call whose cost isn't known until it finishes | Charge an estimate upfront (e.g. from `max_tokens`), reconcile the difference once real usage is known — stay pessimistic, not optimistic, while it's in flight. |
| Why can't a tool-call budget and an LLM token budget share one bucket | They're denominated in different things (dollars/risk vs. tokens) — combining them hides the fact that a few calls to an expensive tool can consume what was meant to be a whole day's LLM budget. |
| What does rate limiting protect in a HITL system that a human reviews | The reviewer's own throughput — an unbounded proposal queue erodes review quality even though the approval gate itself never technically failed. |
| Why is a naive retry-on-429 dangerous | Synchronized retries (no jitter) re-create the burst that got rejected; non-idempotent retries can turn one logical call into several real ones — a self-inflicted denial-of-wallet. |
