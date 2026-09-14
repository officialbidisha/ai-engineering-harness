# ai-engineering-harness

Small, runnable LangGraph exercises exploring the same handful of primitives at
increasing levels of production-readiness: `Command`, `interrupt()`, and `Send`
(dynamic fan-out).

## Contents

- **`command.py`, `retry_mechanism_with_command.py`** — `Command` for atomic
  state-update-plus-routing, and a bounded retry loop pattern.
- **`interrupt_command.py` → `approval_service.py`** — the same
  human-in-the-loop approval gate, first as a single in-process script
  (`MemorySaver`), then wrapped in a FastAPI service with a durable
  (`SqliteSaver`) checkpointer so the pause/resume can span separate HTTP
  requests.
- **`scratch_1_.py`, `send.py` → `send_tavily.py` / `send_tavily_truncate.py`**
  — dynamic fan-out with `Send`, from a stub two-branch example up to a real
  parallel web-search-and-synthesize graph (Tavily + OpenAI), hardened with:
  - per-branch failure isolation (a failed search doesn't crash the whole run)
  - a bounded fan-out width with two alternative strategies — reject
    (`send_tavily.py`) vs. truncate-with-a-note (`send_tavily_truncate.py`)
  - untrusted-content fencing around retrieved web content before it enters
    the model's context (indirect prompt-injection mitigation)
  - structured output with citation validation (claimed sources are checked
    against what was actually retrieved)
  - async node functions + `ainvoke` for genuine concurrent I/O across
    branches
- **`COMMAND_PATTERNS_REVISION.md`** — condensed reference notes on `Command`.
- **`diagrams/`** — a Mermaid diagram (+ rendered PNG) of the retry pattern.

## Running

Each script is self-contained. The `send_tavily*.py` files call the Tavily
and OpenAI APIs and expect `TAVILY_API_KEY` / `OPENAI_API_KEY` in the
environment (a `.env` file works via `python-dotenv`).
