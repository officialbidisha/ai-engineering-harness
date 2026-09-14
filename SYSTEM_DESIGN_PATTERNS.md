# Agentic system design patterns

Four recurring shapes that come up when you move the primitives in this repo
(`Command`, `interrupt()`, `Send`, reducers, checkpointing) from a single
script into a real multi-agent system operating on business data. Each one
follows the same delivery framework: understand the problem → requirements
(functional / non-functional) → core entities → API surface → high-level
design (one pass per functional requirement) → deep dives on the hard parts
→ additional considerations (trade-offs, rollout, what's deliberately out
of scope).

---

## 1. A read-only briefing agent (retrieval, no write path)

### Understanding the problem

An agent that helps someone prepare for a meeting by pulling relevant
context from a CRM-like record store and a team chat tool, and compiling
it into a short, cited brief. The record store and chat tool are treated
as existing systems with query APIs — out of scope to design; the system
under design is the retrieval-and-synthesis layer sitting in front of them.

### Functional requirements

**Core:**
1. A user can request a brief for a given account/deal and receive a
   synthesized, cited result within a few seconds.
2. The brief surfaces only information the requesting user is already
   authorized to see — no exceptions, no "usually."

**Below the line (out of scope):**
- Building the CRM or chat tool themselves.
- Any write-back to the source systems (that's design 3).
- Multi-turn conversational refinement of the brief — v1 is single-shot,
  request in, brief out.

### Non-functional requirements

- **Latency:** interactive, pre-meeting use — an end-to-end budget of a
  few seconds for retrieval + synthesis, not minutes.
- **Scale:** tens of thousands of users, each requesting a handful of
  briefs a day, sharply bursty around business hours rather than uniform
  — the traffic shape looks nothing like a steady-state API.
- **Consistency:** retrieval can tolerate staleness measured in minutes
  (a deal's fields drift slowly); **access control cannot tolerate any
  staleness or approximation** — this is a hard requirement, not a tuning
  knob, and it's the one place in this design where "eventually correct"
  isn't good enough.
- **Availability:** a single failing retrieval source should degrade the
  brief (fewer sections, a note about what's missing), not fail the whole
  request.

### The set up

**Planning the approach:** this is a fan-out retrieval-and-synthesis
pipeline sitting behind an authorization boundary. Because nothing in the
system writes anywhere, there's no approval-gate machinery to design —
the two hard problems are retrieval quality across heterogeneous sources,
and making the access-control guarantee structural rather than a filter
someone can forget to apply.

**Core entities:**
- `BriefRequest` — requesting user, target account/deal id, timestamp.
- `RetrievedChunk` — source, content, relevance score, ACL scope.
- `Brief` — compiled text, `citations[]`, generated-at timestamp.
- `AccessGrant` — the (user, account/deal, scope) relationship consulted
  before any retrieval happens, not after.

**API / system interface:**
```
POST /briefs
  body: { accountId, requestingUserId }
  returns: { briefId, status: "pending" }        // async — see NFR latency budget

GET /briefs/{briefId}
  returns: { status, brief: { text, citations[] } }
```

### High-level design

**1. A user requests a brief and receives a synthesized, cited result.**
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
into a shared `hits` channel through an additive reducer. Each branch
fails in isolation: if chat search times out, the brief still compiles
from the other two sources with a note about what's missing, rather than
the whole request failing because one of three sources was slow. This is
also where the "no HITL gate" design decision belongs — everything
downstream of this system is a read, and an approval gate exists to catch
consequential, hard-to-reverse actions. A read that only a human ever
looks at isn't one; reserving the gate for design 3, where it's load-
bearing, is the more precise call than applying it reflexively everywhere.

**2. The brief surfaces only authorized information.**
Access is scoped to records the requesting user already has rights to —
territory, ownership, whatever the source system's ACL model is — and
that scope is folded into the retrieval **query itself**, before anything
is fetched. See Deep Dive 2 for why this has to be a pre-filter and not a
post-filter, and why that distinction is a leak, not just an inefficiency.

### Deep dives

**1. How do we make retrieval both fast and correct across heterogeneous
sources?**
- **Chunking:** a recursive splitter (paragraph → line → word boundary),
  not fixed-size — a chunk cut mid-table or mid-heading embeds worse and
  reads worse once it lands in the compiled brief. Chunk size is a
  measured choice (recall@k on a golden query set), not a fixed default
  picked once and forgotten.
- **Hybrid retrieval:** dense embeddings catch "what's the renewal risk"
  matching a passage about "SOC2 concerns" — semantic, not lexical. They
  are bad at exact tokens: a deal ID, a ticket number. Sparse (BM25) is
  the mirror image — exact on rare tokens, blind to synonymy. Fuse the
  two ranked lists by **rank**, not raw score — Reciprocal Rank Fusion —
  because BM25 scores and cosine similarities live on incomparable
  scales, and RRF rewards a document both retrievers agree on over one
  retriever's confident outlier.
- **Reranking:** a cheap bi-encoder pass gets a candidate set into scope
  (optimizes recall — don't lose the right passage); an optional
  cross-encoder rerank on the shortlist buys precision on the results
  that actually make the cut — worth the added latency when the source
  pool is large and heterogeneous, skippable when it's already small and
  ACL-narrowed to a handful of records.
- **Grounding discipline:** every claim in the compiled brief carries a
  citation back to a specific retrieved chunk. Check claimed sources
  against what was actually retrieved and strip or flag anything that
  doesn't match — a fabricated citation is a real failure mode, not a
  hypothetical one, and the model's self-reported grounding can't be
  trusted on its own.

**2. How do we guarantee zero ACL leakage rather than "usually correct"?**
Post-filtering (search everything, drop unauthorized results after
ranking) is a real leak, not just an inefficiency, for three separate
reasons: restricted content still enters whatever reads the candidate set
downstream — a reranker, a summarizer — before the filter ever runs;
a caller who normally gets five results and suddenly gets two has just
learned that three restricted things exist, which is an oracle for
enumerating what's hidden; and it relies on application code remembering
to filter correctly every single time, which is a bug waiting to happen.
Pre-filtering — folding authorization into the retrieval query itself, or
better, into the access token the retrieval service was issued — makes
leakage impossible by construction rather than prevented by a
conditional somewhere in application code.

**3. How do we keep retrieval fast under bursty load without serving
stale writes?**
Cache retrieval results on a short TTL (minutes, not hours) — deal state
changes, and a stale field in a brief costs more than an extra cache
miss. Eviction policy matters more than it looks: plain LRU means one
person running a broad scan (pulling every record in a territory for a
review) evicts everyone else's warm working set entirely; a scan-
resistant policy — LFU with decay, or an admission filter — avoids that
specific failure mode where a single cold, one-off access pattern
destroys a hot working set that has nothing to do with it.

### Additional considerations

- **Eval:** retrieval and generation are evaluated **separately** — 
  recall@k answers whether the right context was even found (a ceiling
  nothing downstream can fix); precision@k and a groundedness/citation
  check on the compiled brief answer whether it was used well. Track
  **leakage rate** (fraction of requests where retrieval touched
  something the caller shouldn't see) as a release-blocking metric with
  a target of zero — not a quality metric to trend over time and improve
  gradually.
- **Why no approval gate:** covered in High-Level Design step 1 — worth
  restating as an explicit design decision, not an omission.

---

## 2. A model-routing and governance layer

### Understanding the problem

A layer that decides which model handles which task across a portfolio
of agents, with cost and audit control. This sits in front of every
model call the other agents make — it's cross-cutting infrastructure, not
an agent itself.

### Functional requirements

**Core:**
1. Every agent task is routed to a model appropriate to its risk tier.
2. Every model call is logged with enough detail to audit cost, tier,
   and outcome after the fact.

**Below the line (out of scope):**
- Building the agents that call through this layer.
- Fine-tuning or training custom models — routing happens across
  existing providers/tiers only.

### Non-functional requirements

- **Cost:** must bound spend from any single task or tenant — a
  denial-of-wallet attack or a runaway loop should not translate into an
  unbounded bill.
- **Latency:** the routing decision itself must be near-zero overhead —
  a lookup plus a rate-limit check, not another model call in the
  critical path.
- **Availability:** a provider outage on one tier should degrade
  gracefully (fallback to another tier), not cascade-fail every agent
  that happens to be routed there.

### The set up

**Planning the approach:** this is a decision-and-enforcement layer, not
a new agent, so the design centers on the tiering policy, budget
enforcement, and audit logging — not on orchestration complexity.

**Core entities:**
- `Task` — type, declared risk tier, tenant/team.
- `ModelTier` — name, candidate providers, cost ceiling, fallback tier.
- `Budget` — tenant, tier, remaining tokens, refill rate.
- `CallLog` — task id, tier used, tokens, cost, latency, outcome.

**API / system interface:**
```
POST /route
  body: { taskType, tenantId, estimatedTokens }
  returns: { model, tier, requestId }

POST /calls/{requestId}/complete
  body: { actualTokens, latencyMs, outcome }
  // writes the audit log entry, reconciles the budget
```

### High-level design

**1. Every task is routed to a model tier appropriate to its risk.**
```
request → classify task risk tier (public-facing copy vs. deal-sensitive analysis vs. write-triggering)
        → route to the model tier mapped to that risk tier
        → execute
        → on provider failure/rate-limit: fall back to the next tier down, logged as a degradation, not silently
```
The **risk-tier → model-tier mapping** is the actual design artifact
here, not the routing code itself — e.g. public marketing copy can go to
a smaller, cheaper model with lenient review; anything touching
customer-specific sensitive data, or feeding a write-capable downstream
agent, goes to the strongest available model with tighter output
constraints (structured output, stricter groundedness checks).

**2. Every call is auditable after the fact.**
Every routed call writes a `CallLog` entry — task, tier, model, tokens,
cost, latency, outcome — regardless of success or failure, so cost and
quality can be reconstructed per-task after the fact rather than only
observed live.

### Deep dives

**1. How do we prevent denial-of-wallet?**
A per-request rate limiter is the wrong unit for LLM traffic — one
request can be 200 tokens, another 200,000. **Token bucket with a
variable cost** is the right primitive: `cost = estimated_tokens / 1000`
instead of `cost = 1`, so the budget is a genuine token budget, not a
request-count budget a single expensive call can blow through unnoticed.
Layer a **hard per-tenant/per-task ceiling** on top of the bucket so one
workflow can't exhaust the shared budget for everyone else.

**2. Fail open or fail closed when the budget/routing service itself is
down?**
Decided deliberately, and **per tier** — if the budget service is
unavailable, does a request proceed anyway (risking an unbounded bill)
or get rejected (degrading availability to protect spend)? Fail closed
on the expensive tier, fail open on the cheap one: the cost of a
short availability hit on cheap traffic is lower than the cost of an
unbounded bill on expensive traffic.

**3. How do we detect a provider degradation and route around it before
it fully fails?**
Track latency/error rate per provider and fall back to the tier's backup
provider proactively, rather than waiting for hard failures — the same
principle as a circuit breaker, applied at the routing layer instead of
inside each individual agent.

**4. How do we know the tiering policy itself is still correct over
time?**
Track cost and latency **per tier**, not in aggregate — an aggregate
number hides whether the expensive tier is being over-used for tasks
that didn't need it. Alert on tier-migration drift (tasks classified
into a higher tier than their historical baseline) as an early signal of
either a classifier regression or a genuine shift in what's being asked
of the system.

### Additional considerations

- **Trade-off:** a stricter tiering policy costs latency and money on the
  low end (routing conservatively) or risk on the high end (routing
  aggressively to save cost). State this explicitly as a deliberate,
  revisitable policy choice — not a fixed property of the system that
  was solved once.

---

## 3. A write agent, gated (the hard one)

### Understanding the problem

An agent that updates a record in a system of record — e.g. a deal's
status field — based on a conversation in a team chat tool. The moment
this agent writes, the risk posture changes completely relative to
design 1: everything from the read-only case still applies, plus an
entire additional layer that exists only because the action is
consequential and not trivially reversible.

### Functional requirements

**Core:**
1. A proposed record change is generated from a conversational trigger,
   previewed to a human, and applied only after explicit approval.
2. Every applied change is captured in an audit trail sufficient to
   answer "who approved what, when, from what source" — indefinitely,
   not just for a retention window.

**Below the line (out of scope):**
- Auto-approval / no-human-in-the-loop paths for v1 — that's a later,
  graduated phase (see Additional Considerations), not a day-one
  requirement.
- Building the record system itself — treated as an existing system with
  a write API.

### Non-functional requirements

- **Exactly-once effect:** an approved write must apply once, even under
  network retries, a duplicated trigger, or a human double-clicking
  approve.
- **Durability across a long pause:** an approval decision can take
  hours; the pending state must survive a process crash and resume
  correctly, not just survive within one process's lifetime.
- **Consistency of the audit trail specifically:** it must never fork —
  two different answers to "did this happen" depending on which replica
  answers is not an acceptable failure mode, even though most of the
  rest of the system can tolerate looser consistency.

### The set up

**Planning the approach:** this is the one design in the set where the
naive version — an agent with a write tool — is actually unsafe. The
whole design exists to interpose a durable pause, a human decision, and
a deterministic executor between "the model wants to write" and "the
write happens."

**Core entities:**
- `ChangeProposal` — source message, target record, field, proposed
  value, justification, citation.
- `ApprovalDecision` — proposal id, approver, decision, timestamp.
- `ExecutedChange` — proposal id, applied value, applied-at, verified
  (bool — did the read-back confirm it landed).
- `AuditEntry` — immutable, append-only, correlating proposal + decision
  + execution into one record.

**API / system interface:**
```
POST /proposals                          // triggered internally by the reasoning agent
  body: { sourceMessageId, targetRecordId, field, proposedValue, justification }
  returns: { proposalId, status: "pending_approval" }

POST /proposals/{proposalId}/decision
  body: { approverId, decision: "approve" | "reject" }
  // on approve, triggers the deterministic executor

GET /proposals/{proposalId}
  returns: full status including the correlated audit trail
```

### High-level design

**1. A proposed change is generated and held for approval.**
```
trigger (chat message) → reasoning agent proposes a change
                          (field, new value, justification, citation to the source message)
                       → interrupt(): pause, surface a preview of the exact diff to a human
```
`interrupt()` pauses execution and checkpoints exactly what's pending —
the proposed diff, the source citation — so the approval can happen
asynchronously, potentially hours later, without holding a process open.

**2. On approval, the change is applied exactly once by a separate,
deterministic executor.**
```
on approval: deterministic executor performs the write (not the reasoning agent itself)
           → read-back verification: re-fetch the record, confirm the write landed as intended
```
**Reasoning and execution are architecturally separate nodes.** The LLM
that proposed the change never holds write credentials — a second,
deterministic (non-LLM) executor performs the actual API call, with
exactly the previously-approved payload and nothing else. This is also
the actual defense against a prompt-injection payload hidden in the
source chat message: such a payload can at most influence what gets
*proposed* for review; it cannot reach the write path on its own, because
the executor only ever executes what a human explicitly approved,
verbatim.

**3. Every change is auditable.**
```
→ append to an immutable audit trail (who approved, what changed, when, source citation)
```
See Deep Dive 4 for why this specific component has a stricter
consistency requirement than everything else in the design.

### Deep dives

**1. How do we pause for an approval that might take hours, without
holding a process open?**
`interrupt()` plus a durable checkpointer — the paused state (the
proposal, its inputs) is persisted, and the process can shut down
entirely; resuming later reloads that checkpoint and continues from
there rather than requiring a live, blocked process the whole time.

**2. What's the replay gotcha, and how do we design around it?**
On resume, the node that called `interrupt()` re-runs **from its own
top**, not from the middle — so any side effect placed *before* the
`interrupt()` call in that same node executes twice: once leading into
the pause, once again on replay. The fix is structural: put `interrupt()`
as early as possible in the node, before any side-effecting code, so
there's nothing upstream of it left to duplicate.

**3. How do we guarantee exactly-once application under retries or
duplicate triggers?**
Make the write itself idempotent, independent of the interrupt
mechanics. Derive an idempotency key from the *intent* of the write (who,
what field, what value, what source event) rather than from the delivery
mechanism, so two independently-triggered attempts at the same real
change collapse to one. The check-and-claim step must itself be a single
atomic operation (`INSERT ... ON CONFLICT DO NOTHING` or equivalent) —
a naive check-then-write is itself a race between two concurrent
attempts, and would just relocate the exact bug it's meant to fix.

**4. Why must the audit trail specifically be the one strongly
consistent component, when the rest of the system tolerates staleness
fine?**
Everything upstream of it (retrieval, drafting) can tolerate staleness —
a slightly-behind cache is cheap. The record of "did this write happen,
and was it approved" cannot fork: two different answers to that question
depending on which replica you ask defeats the entire purpose of having
an audit trail. In practice this means backing it with a single
linearizable store, even while the rest of the system is built for
availability over strict consistency — the consistency requirement is
scoped to the smallest component that actually needs it, not applied
uniformly across the whole system.

### Additional considerations

- **Graduated trust rollout:** full human approval on every write during
  pilot, moving to sampled/spot-check approval or threshold-based
  auto-approval only once the human-override rate has been low and
  stable for a defined observation window. State the threshold and the
  window explicitly — "we'll loosen it when it feels safe" isn't a
  design, it's a deferral.
- **Eval:** the human-override rate (how often a human rejects or edits
  the proposed change) is a leading trust signal here that a purely
  read-only agent doesn't get — it surfaces model unreliability before
  it ever shows up in a downstream metric.

---

## 4. Trust evaluation for pilot → general rollout

### Understanding the problem

Given a set of agents already running in a limited pilot — including
designs 1 through 3 above — how do you decide they're trustworthy enough
to expand to the full organization? This is a process and measurement
design, not a service with its own API — but it follows the same
discipline: define what "trustworthy enough" means measurably, before
the rollout decision has to be made under pressure.

### Functional requirements

**Core:**
1. There is a quantitative, pre-committed bar an agent must clear before
   expansion — not a subjective "it feels ready" call made after seeing
   the pilot's numbers.
2. Expansion happens in reversible, bounded increments, with a defined
   incident/rollback path, not as a single global switch-flip.

**Below the line (out of scope):**
- The agents' own functional design (covered in 1–3).
- Automating the rollback mechanism itself — assumed to exist as
  standard feature-flag infrastructure.

### Non-functional requirements

- **Auditability of the decision itself:** the rollout decision must be
  traceable to specific, recorded metrics — not made and then
  rationalized after the fact.
- **Bounded blast radius:** a bad expansion must be containable to the
  segment it started in.

### The set up

**Core entities:**
- `GoldenTask` — a held-out, human-labeled task, fixed once the
  evaluation period starts (a moving target defeats the point of a
  benchmark).
- `ShadowRun` — the agent's output on live input, logged but not acted
  on, compared against what a human actually decided.
- `OverrideEvent` — a human rejecting or editing an agent's proposal,
  tracked over time, not just at a single pilot checkpoint.
- `RolloutSegment` — a team or territory behind its own feature flag,
  the unit of both expansion and rollback.

### High-level design

**The rollout pipeline, as a sequence of gates rather than a single
decision:**
1. **Golden-set task success rate** measured against the fixed,
   held-out set, checked against a threshold set *before* rollout
   begins.
2. **Shadow-mode period** — before the agent's proposals affect anything
   real, run it in parallel against live input with output logged but
   not acted on. This catches a class of failure a static golden set
   can't: genuine production input distribution, not a curated sample.
3. **Human-override rate**, tracked as an ongoing signal, not just a
   one-time gate — trending it after launch catches silent model or data
   drift that a pre-launch check can't.
4. **Leakage rate held at zero**, carried over from design 1 — a
   release-blocker at every rollout stage, not something that relaxes as
   trust in functional quality grows.
5. **Phased, flagged expansion by segment**, with an incident/rollback
   plan defined *before* expansion, not improvised during an incident —
   a bad rollout is contained to the segment it started in and rolled
   back without a full system outage.

### Deep dives

**1. Why isn't golden-set accuracy enough on its own?**
A model can pass every golden-set task and still have an override rate
that says people don't trust its judgment on the long tail of real
input — the golden set, by construction, never contains that tail. This
is the actual gap the rest of the framework exists to close.

**2. Why shadow mode instead of just a bigger golden set?**
A golden set is static and curated; shadow mode measures the agent
against the real, messy, live input distribution — including inputs
nobody thought to label — without any risk, since nothing is acted on
yet.

**3. Why segment the rollout instead of a single global flag?**
Bounding blast radius: if the override rate spikes or the golden-set
performance doesn't hold in production, the damage and the rollback are
both scoped to one segment, not the whole organization.

### Additional considerations

- **The judgment call this framework is actually testing:** functional
  accuracy and trustworthiness are different measurements, and treating
  a high golden-set score as sufficient evidence for full rollout is the
  most common mistake this framework is designed to prevent.
