from typing_extensions import TypedDict
from langgraph.types import interrupt, Command
from typing import Literal
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver
class WriteState(TypedDict):
    payload: dict
    decision: str
    result: str

def prepare_write(state: WriteState) -> dict:
    return {"payload": {"action": "delete_user","id": 42}}

def human_approval(state:WriteState) -> Command[Literal["execute", "cancelled"]]:
    decision = interrupt({
        "question": "Approve this write?",
        "payload": state["payload"]
    })

    if decision == "approve":
        return Command(update={"decision": "approved"}, goto="execute")
    return Command(update={"decision": "rejected"}, goto="cancelled")

def execute(state: WriteState) -> dict:
    return {"result": f"executed {state['payload']}"}

def cancelled_node(state: WriteState)-> dict:
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
graph = builder.compile(checkpointer=MemorySaver())

config = {"configurable": {"thread_id": "user-42-req-1"}}
result = graph.invoke({"payload": {}}, config= config)
print(result)

# later

final = graph.invoke(Command(resume="approve"), config=config)
print(final)