# Agentic system design patterns

Four recurring shapes that come up when you move the primitives in this repo
(`Command`, `interrupt()`, `Send`, reducers, checkpointing) from a single
script into a real multi-agent system operating on business data. Each one
follows the same skeleton: clarify the requirement → define the trust
boundary → sketch the architecture → deep-dive the hard part → design the
eval/rollout plan → name the trade-offs → phase the MVP.

---

## 1. A read-only briefing agent (retrieval, no write path)

**The ask:** an agent that helps someone prepare for a meeting by pulling
relevant context from a CRM-like record store and a team chat tool, and
compiling it into a short brief.

### Clarify

- **Who:** the account/deal owner, not an admin — this bounds the access
  surface to begin with.
- **Risk tier:** reads sensitive business data but writes nothing. That
  single fact determines almost everything else about the design.
- **Latency:** interactive, pre-meeting — seconds, not minutes.
- **Success metric:** adoption + time saved + accuracy of what's surfaced
  (measured against a golden set, not vibes).

### Trust boundary

Access is scoped to records the requesting user is already authorized to
see — territory, deal ownership, whatever the source system's ACL model is.
**This has to be enforced as a pre-filter on the retrieval query itself**,
not as a post-hoc filter on the result list: post-filtering a ranked result
set leaks in three distinct ways — restricted content still enters whatever
reads the candidate set downstream (a reranker, a summarizer) before the
filter runs; a caller who normally gets 5 results and suddenly gets 2 has
just learned that 3 restricted things exist; and it depends on application
code remembering to filter correctly every single time instead of being
structurally impossible. Pre-filtering — folding the caller's authorization
into the query itself, or better, into the access token the retrieval
service was issued — makes leakage impossible by construction rather than
prevented by a conditional.

### Architecture

```
request → triage (what kind of prep is this?)
        → parallel retrieve:
            - structured record lookup (CRM fields, deterministic)
            - hybrid search over chat history (BM25 + dense, RRF-fused)
            - knowledge-base retrieval (product/competitive docs)
        → compile: synthesize a brief with inline citations
        → respond
```

The three retrieval branches are independent — no branch's output depends
on another's — so they fan out via `Send`, one branch per source, merging
into a shared `hits` channel via an additive reducer. Each branch fails in
isolation: if chat search times out, the brief still compiles from the
other two sources with a note about what's missing, rather than the whole
request failing because one of three sources was slow.

**Explicitly note what this design does *not* need: a human-approval gate.**
Every action downstream of this agent is a read. The temptation on a
security-conscious team is to bolt HITL onto everything reflexively — the
actual judgment call is recognizing that approval gates exist to catch
consequential, hard-to-reverse actions, and a read that only a human ever
sees isn't one. Saving the gate for scenario 3 (where it's load-bearing) is
the more precise answer than applying it uniformly.

### Deep-dive: retrieval quality

- **Chunking:** a recursive splitter (paragraph → line → word boundaries),
  not fixed-size — a chunk that's cut mid-table or mid-heading embeds
  worse and reads worse in the compiled brief. Chunk size is a legitimate
  measured choice (recall@k on a golden query set), not a fixed default.
- **Hybrid retrieval:** dense embeddings catch "what's the renewal risk"
  finding a passage about "SOC2 concerns"; they're bad at exact tokens —
  a deal ID, a ticket number. Sparse (BM25) is the mirror image. Fuse by
  **rank**, not raw score (Reciprocal Rank Fusion) — the two retrievers'
  scores live on incomparable scales, and RRF rewards a document both
  retrievers agree on over one retriever's confident outlier.
- **Reranking:** cheap bi-encoder search gets a candidate set into scope
  (optimizes recall — don't lose the right passage); an optional
  cross-encoder rerank on the shortlist gets precision on the ones that
  matter — worth the extra latency when the source pool is large and
  heterogeneous, skippable when it's already small and ACL-narrowed to a
  handful of records.
- **Grounding discipline:** every claim in the compiled brief carries a
  citation back to a specific retrieved chunk. If the model's structured
  output cites something that wasn't actually in the retrieved set, that's
  a fabricated citation — detect it by checking claimed sources against
  the retrieval set and strip or flag anything that doesn't match, rather
  than trusting the model's self-reported grounding.

### Eval / rollout

Retrieval and generation are evaluated **separately** — recall@k tells you
whether the right context was even found (a ceiling nothing downstream can
fix); precision@k and a groundedness/citation-accuracy check on the
compiled brief tell you whether it was used well. Track a **leakage rate**
(fraction of requests where retrieval touched something the caller
shouldn't see) as a release-blocking metric with a target of zero, not a
quality metric to trend over time.

### Trade-offs

Caching retrieval results trades freshness for latency and cost — short
TTLs (minutes) are the right default here specifically because deal state
changes and a stale field in a brief is worse than an extra cache miss.
Eviction policy matters more than it looks: plain LRU means one person
running a broad scan (e.g. pulling every record in a territory for a
review) evicts everyone else's warm working set; a scan-resistant policy
(LFU with decay, or an admission filter) avoids that failure mode.

---

## 2. A model-routing and governance layer

**The ask:** a layer that decides which model handles which task across a
portfolio of agents, with cost and audit control.

### Clarify

Not one model for everything — different tasks carry different risk and
cost profiles, and treating them uniformly either overpays for low-stakes
work or under-invests in high-stakes work.

### Architecture

```
request → classify task risk tier (public-facing copy vs. deal-sensitive analysis vs. write-triggering)
        → route to the model tier mapped to that risk tier
        → execute, log to an audit trail (task, tier, model, tokens, latency, cost)
        → on provider failure/rate-limit: fall back to the next tier down, logged as a degradation, not silently
```

**Risk-tier → model-tier mapping** is the actual design artifact, not the
routing code — e.g. public marketing copy can go to a smaller/cheaper
model with lenient review; anything touching customer-specific sensitive
data or feeding a write-capable downstream agent goes to the strongest
available model with tighter output constraints (structured output,
stricter groundedness checks).

### Deep-dive: cost control (denial-of-wallet)

A per-request rate limiter isn't the right unit for LLM traffic — one
request can be 200 tokens, another 200,000. **Token bucket with a
variable cost** is the correct primitive: `cost = estimated_tokens / 1000`
instead of `cost = 1`, so the budget is actually a token budget, not a
request-count budget that a single expensive call can blow through
unnoticed. Layer a **hard per-tenant/per-task ceiling** on top of the
bucket so one workflow can't exhaust the shared budget for everyone else.

**Fail open or fail closed, decided deliberately per tier:** if the budget
service itself is unavailable, does a request proceed anyway (risking an
unbounded bill) or get rejected (degrading availability to protect spend)?
The defensible answer is tier-dependent — fail closed on the expensive
tier, fail open on the cheap one, because the cost of an outage there is
lower than the cost of an unbounded bill.

### Eval / rollout

Track cost and latency **per tier**, not in aggregate — an aggregate
number hides whether the expensive tier is being over-used for tasks that
didn't need it. Alert on tier-migration drift (tasks classified into a
higher tier than their historical baseline) as an early signal of either a
classifier regression or a genuine shift in what's being asked of the
system.

### Trade-offs

A stricter tiering policy costs latency and money on the low end (routing
conservatively) or risk on the high end (routing aggressively to save
cost). State this explicitly rather than presenting tiering as a free
win — it's a deliberate, revisitable policy choice, not a fixed property
of the system.

---

## 3. A write agent, gated (the hard one)

**The ask:** an agent that updates a record in a system of record — e.g. a
deal's status field — based on a conversation in a team chat tool.

### Clarify

The moment the agent **writes**, the entire risk posture changes. This is
the scenario where every mechanism from the read-only case (1) still
applies, plus a whole additional layer that only exists because the
action is consequential and not trivially reversible.

### Architecture

```
trigger (chat message) → reasoning agent proposes a change (field, new value, justification, citation to the source message)
                       → interrupt(): pause, surface a preview of the exact diff to a human
                       → on approval: deterministic executor performs the write (not the reasoning agent itself)
                       → read-back verification: re-fetch the record, confirm the write landed as intended
                       → append to an immutable audit trail (who approved, what changed, when, source citation)
```

**Reasoning and execution are architecturally separate nodes.** The LLM
that proposed the change is never the thing holding write credentials —
a second, deterministic (non-LLM) executor performs the actual API call,
with the exact previously-approved payload and nothing else. This means a
prompt-injection payload hidden in the source chat message can at most
influence what gets *proposed* for review; it cannot reach the write path
on its own, because the executor only ever executes what a human
explicitly approved, verbatim.

### Deep-dive: the pause/resume and idempotency mechanics

`interrupt()` pauses execution and checkpoints exactly what's pending — the
proposed diff, the source citation — so the approval can happen
asynchronously (a human might not respond for hours) without holding a
process open. **The gotcha to design around:** on resume, the node that
called `interrupt()` re-runs from its own top, not from the middle — so
any side effect placed *before* the `interrupt()` call in that same node
executes twice (once leading into the pause, once again on replay).
Structure the node so `interrupt()` sits as early as possible, before any
side-effecting code.

**The write itself must be idempotent**, independent of the interrupt
mechanics — a human double-clicking approve, a network retry, or a second
independent trigger for the same underlying request should not double-
apply the change. Derive an idempotency key from the *intent* of the
write (who, what field, what value, what source event) rather than from
the delivery mechanism, and make the check-and-claim step a single atomic
operation (an `INSERT ... ON CONFLICT DO NOTHING` or equivalent) — a
naive check-then-write is itself a race between two concurrent attempts.

**The audit trail has to be the one component in this design that's
strongly consistent (no forking between replicas).** Everything upstream
of it (retrieval, drafting) can tolerate staleness; the record of "did
this write happen, and was it approved" cannot — two different answers to
that question depending on which replica you ask defeats the entire point
of having an audit trail. In practice this means backing it with a single
linearizable store (not an eventually-consistent one), even while the
rest of the system is built for availability over strict consistency.

### Eval / rollout

Approval-gated systems get a trust signal read-only agents don't: the
**human-override rate** — how often the human rejects or edits the
proposed change. A high override rate is a leading indicator the
underlying model's proposals aren't trustworthy yet, well before it shows
up in any downstream metric.

### Trade-offs

Requiring approval on every write is safe but doesn't scale past a small
pilot — the honest next step (not the starting point) is a graduated
model: full approval on every write during pilot, moving to
sampled/spot-check approval or auto-approval below some risk/confidence
threshold once the override rate has been low and stable for a defined
period. State the threshold and the observation window explicitly; "we'll
loosen it when it feels safe" isn't a design.

---

## 4. Trust evaluation for pilot → general rollout

**The ask:** given a set of agents already running in a limited pilot, how
do you decide they're trustworthy enough to expand to the full
organization?

### The framework

- **Golden-set task success rate**, measured against a held-out,
  human-labeled set that doesn't change as the system is tuned (a moving
  target defeats the point of a benchmark) — set a threshold before rollout
  begins, not after seeing the pilot's number.
- **Shadow-mode period.** Before an agent's proposals affect anything real,
  run it in parallel against live input with its output logged but not
  acted on, and compare against what actually happened / what a human
  actually decided. This catches a class of failure a static golden set
  can't — genuine production input distribution, not a curated sample.
- **Human-override rate as an ongoing trust signal**, not just a rollout
  gate — trending it over time catches silent model or data drift after
  launch, not just before it.
- **Leakage rate at zero**, carried over from the read-only agent's design —
  this is a release-blocker at every rollout stage, not something that
  gets less strict as trust in the *functional* quality grows.
- **Incident and rollback plan defined before expansion, not during an
  incident.** A feature flag per team/segment means a bad rollout can be
  contained to the segment it started in and rolled back without a full
  system outage — expansion should be phased by segment for exactly this
  reason, not switched on globally at once.

### The judgment call this is actually testing

Functional accuracy (golden-set pass rate) and trustworthiness are
**different measurements** — a model can pass every golden-set task and
still have an override rate that says people don't trust its judgment on
the long tail of real input, and that gap is the actual rollout risk. The
phased, flagged, monitored rollout exists because the golden set alone
never tells you that.
