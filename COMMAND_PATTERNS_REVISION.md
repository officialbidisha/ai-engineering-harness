# LangGraph `Command` — Patterns Reference

Condensed from the full deep-dive session. Companion runnable scripts in this
folder: `command.py` (routing), `retry_mechanism_with_command.py` (loops),
`interrupt_command.py` (HITL basics), `approval_service.py` (prod-shaped HITL
service).

## The one idea everything else follows from

> State mutation and routing are usually the *same decision*, made by the
> *same code*, with the *same information*. `Command` lets a node express
> both atomically in one return value, instead of splitting it into a state
> write plus a separate router function that has to stay in sync by hand.

```python
Command(update=dict, goto=str | list[str], graph=Command.PARENT, resume=Any)
```

## Must-know patterns

**1. State update + routing together**
```python
def check_amount(state) -> Command[Literal["manual_review", "auto_approve"]]:
    if state["amount"] > 10_000:
        return Command(update={"status": "needs_review"}, goto="manual_review")
    return Command(update={"status": "approved"}, goto="auto_approve")
```
Cleaner than `add_conditional_edges` when the branch condition *is* the
node's own business logic — no separate router function re-deriving it.

**2. Loops need a monotonic counter + a check**
`agent → execute_tool → agent` cycles are just ordinary edges; LangGraph
doesn't special-case cycles. Without a field that moves toward a stop
condition (`attempts`) and a node that checks it, you get infinite
execution (backstopped only by `recursion_limit`, not a design strategy).

**3. `interrupt()` + `Command(resume=...)` — human-in-the-loop**
- `interrupt(payload)` freezes the graph, checkpoints full state under
  `thread_id`, and surfaces `payload` as `result["__interrupt__"]`.
- `graph.invoke(Command(resume=value), config={"configurable": {"thread_id": ...}})`
  reloads that checkpoint and makes the *same* `interrupt()` call return
  `value` instead of pausing.
- Requires a checkpointer (`MemorySaver` for dev, `SqliteSaver`/`PostgresSaver`
  for anything that must survive a restart).

**4. THE gotcha: interrupted nodes restart from the top on resume**
Everything *before* `interrupt()` in that node re-runs on every resume.
```python
# BAD — runs twice: once on first call, again on resume
def human_approval(state):
    send_slack_notification(...)      # ⚠️ non-idempotent, re-executed on resume
    decision = interrupt({...})
```
Fix: separate think → interrupt → act into different nodes. Nothing
non-idempotent may sit before or inside the `interrupt()`-containing node.

**5. Approval gate shape (production HITL)**
```
prepare (pure, no side effects) → human_approval (interrupt only) → execute (side effect, runs once)
```
This is `approval_service.py`. The `resume` value in real systems comes from
an actual HTTP request body / button click / Slack payload — not a hardcoded
string. Two separate endpoints: one that starts the graph and returns the
interrupt payload, one that receives the decision and resumes.

**6. Static edge + Command trap (classic bug)**
```python
builder.add_edge("router", "A")          # ⚠️ still fires unconditionally
def router(state) -> Command[Literal["B"]]:
    return Command(goto="B")             # BOTH A and B execute — no error, just silently wrong
```
`Command(goto=...)` **adds** a dynamic edge; it does not cancel a
statically declared one. Rule: a node that ever returns `Command(goto=...)`
must have zero `add_edge` calls registering its outgoing edges.

**7. Fan-out: `goto=[...]` vs `Send`**
- `Command(goto=["A", "B"])` — fixed node names, same state, scheduled for
  the same superstep, no ordering guarantee.
- `Command(goto=[Send("worker", {...}), Send("worker", {...})])` — same
  node invoked N times with **different, per-item** input. Needed for
  "map over a dynamically-sized list" (map-reduce).
- Either way: if parallel branches write the same state key, you need a
  reducer (`Annotated[list, operator.add]`) or you get `InvalidUpdateError`.

**8. `Command.PARENT` — escaping a subgraph**
Inside a subgraph, plain `goto="x"` can only reach that subgraph's own
nodes. `Command(goto="parent_node", graph=Command.PARENT)` routes to a node
in whichever graph is hosting this one — used for specialist → supervisor
escalation, or child security-check → parent human-review handoff.

## Comparison table (compressed)

| | `add_edge` | `add_conditional_edges` | `Command(goto=)` | `Command(update=,goto=)` | `Send` | `interrupt`+`resume` |
|---|---|---|---|---|---|---|
| Routing decided by | builder (static) | router fn | node itself | node itself | node itself | resuming caller |
| Changes state? | no | no | no | yes | yes | yes |
| Parallel? | only if multi-edge | if router returns list | if goto is list | if goto is list | yes, natively | n/a |
| Typical use | fixed pipeline | pure branch, no state | pure dispatch | classify-and-act | map-reduce | approval gates |

## Decision tree

```
Need to decide WHERE to go, based on something just computed?
├─ No  → add_edge / add_conditional_edges
└─ Yes → Also need to write state as part of it?
         ├─ No  → Command(goto=...)
         └─ Yes → Per-item dynamic fan-out (map over a list)?
                  ├─ Yes → Command(goto=[Send(...), ...])
                  └─ No  → Need to pause for a human/external event?
                           ├─ Yes → interrupt() + Command(resume=...)
                           └─ No  → Destination outside this subgraph?
                                    ├─ Yes → Command(goto=..., graph=Command.PARENT)
                                    └─ No  → Command(update=..., goto=...)
```

## Self-test questions

1. Why does `Command` exist when conditional edges already support dynamic
   routing?
2. A node returns `Command(goto="B")` but a static `add_edge` to `A` also
   exists from that node. What actually runs?
3. Exactly what re-executes when a graph resumes after `interrupt()`?
4. When do you need `Send` instead of `Command(goto=[...])`?
5. Two parallel branches write the same non-`Annotated` state key — what
   happens at runtime?
6. Why should specialists in a supervisor architecture hand back to the
   supervisor instead of calling each other directly?
7. How do you stop an LLM-invoked tool from routing straight into a
   destructive action with no human check?

## Debugging scenarios

- **"Both branches always run"** → leftover `add_edge` alongside a
  `Command(goto=...)` node (Pattern 6 above).
- **"User got charged twice"** → non-idempotent side effect placed before
  or inside the `interrupt()`-containing node (Pattern 4 above).
- **`InvalidUpdateError` only under real load, never in single-item tests**
  → missing reducer on a field multiple `Send`-spawned workers all write to
  (Pattern 7 above).

## One-line whiteboard summary

`Command` is the wire that lets one function emit both "what should state
look like now" and "where do we go from here" from a single evaluation,
instead of forcing you to write that decision twice and hope the two
versions never disagree.
