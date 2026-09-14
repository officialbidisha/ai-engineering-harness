import sys
import os

EXPECTED_VENV = os.path.expanduser("~/Desktop/ResolveFlow/.venv")
if sys.prefix != EXPECTED_VENV:
    sys.exit(
        f"Wrong interpreter: running from {sys.prefix}, expected {EXPECTED_VENV}.\n"
        f"Run with: {EXPECTED_VENV}/bin/python3 {sys.argv[0]}"
    )

import operator
from typing import TypedDict, Annotated
from langgraph.graph import StateGraph, START, END
class State(TypedDict):
    log: Annotated[list[str], operator.add]


def node_a(state:State):
    return {"log": ["node_a"]}

def node_b(state:State):
    return {"log": ["node_b"]}

builder = StateGraph(State)
builder.add_node("node_a", node_a)
builder.add_edge(START, "node_a")
builder.add_node("node_b", node_b)
builder.add_edge(START, "node_b")
builder.add_edge("node_a", END)
builder.add_edge("node_b", END)
graph = builder.compile()
print(graph.invoke({"log": []}))
