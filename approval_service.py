"""
Production-shaped example: a FastAPI service that pauses a LangGraph run
for human approval, and resumes it later from a separate HTTP request.

Run the server:
    .venv/bin/uvicorn approval_service:app --reload

Then, in another terminal:
    curl -X POST localhost:8000/requests
    # -> {"thread_id": "...", "needs_approval": {...}}

    curl -X POST localhost:8000/requests/<thread_id>/decision \
         -H "Content-Type: application/json" \
         -d '{"decision": "approve"}'
    # -> final graph state

At the bottom of this file, `if __name__ == "__main__"` also runs the same
flow in-process (no server needed) using FastAPI's TestClient, so you can
just do `.venv/bin/python approval_service.py` to see it work end to end.
"""

import sqlite3
from typing import Literal

from fastapi import FastAPI
from typing_extensions import TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt, Command


class WriteState(TypedDict):
    payload: dict
    decision: str
    result: str


def prepare_write(state: WriteState) -> dict:
    # pure — this WILL re-run on every resume, so no side effects here
    return {"payload": {"action": "delete_user", "id": 42}}


def human_approval(state: WriteState) -> Command[Literal["execute", "cancelled"]]:
    decision = interrupt({
        "question": "Approve this write?",
        "payload": state["payload"],
    })
    if decision == "approve":
        return Command(update={"decision": "approved"}, goto="execute")
    return Command(update={"decision": "rejected"}, goto="cancelled")


def execute(state: WriteState) -> dict:
    # the actual side effect — only ever reached AFTER approval
    return {"result": f"executed {state['payload']}"}


def cancelled_node(state: WriteState) -> dict:
    return {"result": "cancelled by human"}


builder = StateGraph(WriteState)
builder.add_node("prepare_write", prepare_write)
builder.add_node("human_approval", human_approval)
builder.add_node("execute", execute)
builder.add_node("cancelled", cancelled_node)
builder.add_edge(START, "prepare_write")
builder.add_edge("prepare_write", "human_approval")
builder.add_edge("execute", END)
builder.add_edge("cancelled", END)

# Durable checkpointer backed by a real file, so paused runs survive a
# server restart -- unlike MemorySaver, which only lives in process memory.
conn = sqlite3.connect("checkpoints.db", check_same_thread=False)
checkpointer = SqliteSaver(conn)
graph = builder.compile(checkpointer=checkpointer)

app = FastAPI()


@app.post("/requests")
def create_request():
    """Something (a user action, a scheduled job, ...) wants to make a write."""
    import uuid
    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    result = graph.invoke({"payload": {}}, config=config) #Invoking point
    interrupt_payload = result["__interrupt__"][0].value

    # in a real service: persist thread_id in your own DB/ticket system here,
    # so whatever approves it later knows which thread to resume.
    return {"thread_id": thread_id, "needs_approval": interrupt_payload}


@app.post("/requests/{thread_id}/decision")
def submit_decision(thread_id: str, body: dict):
    """A human approved/rejected -- this is where `resume`'s value comes from."""
    config = {"configurable": {"thread_id": thread_id}}
    # Getting the value from human response
    final = graph.invoke(Command(resume=body["decision"]), config=config)
    return final


if __name__ == "__main__":
    from fastapi.testclient import TestClient

    client = TestClient(app)

    print("--- creating request (pauses for approval) ---")
    created = client.post("/requests").json()
    print(created)

    thread_id = created["thread_id"]

    print("\n--- submitting human decision ---")
    final = client.post(
        f"/requests/{thread_id}/decision",
        json={"decision": "approve"},
    ).json()
    print(final)
