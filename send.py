from typing import TypedDict, Annotated
import operator
from langgraph.graph import StateGraph, START, END
from langgraph.types import Send

class FanState(TypedDict):
    queries: list[str]
    hits: Annotated[list[str], operator.add]   # l branches
    summary: str                                # separate key, avoids the append gotcha

class SubState(TypedDict):
    query: str

def start(state: FanState) -> dict:
    return {}  # no-op entry node

def fan_out(state: FanState):
    return [Send("retrieve", {"query": q}) for q in state["queries"]]

def retrieve(state: SubState) -> dict:
    return {"hits": [f"doc_for::{state['query']}"]}

def compile_answer(state: FanState) -> dict:
    return {"summary": f"compiled {len(state['hits'])} hits"}

graph = StateGraph(FanState)
graph.add_node("start", start)
graph.add_node("retrieve", retrieve)
graph.add_node("compile_answer", compile_answer)

graph.add_edge(START, "start")
graph.add_conditional_edges("start", fan_out)
graph.add_edge("retrieve", "compile_answer")
graph.add_edge("compile_answer", END)

app = graph.compile()
result = app.invoke({"queries": ["python errors", "langgraph state"], "hits": [], "summary": ""})
print(result)
