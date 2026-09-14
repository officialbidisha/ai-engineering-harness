#!/usr/bin/env python3
from typing import Literal
from typing_extensions import TypedDict
from langgraph.types import Command
from langgraph.graph import StateGraph, START, END

class State(TypedDict):
    amount: int
    status: str

def check_amount(state: State,) -> Command[Literal["manual_review", "auto_approve"]]:
    amount = state["amount"]

    if amount > 10_000:
        return Command(
            update = {"status":"needs_review"},
            goto = "manual_review"
        )
    
    return Command(
        update = {"status": "approved"},
        goto = "auto_approve"
    )

def manual_review(state:State):
    print("Sending for manual review")
    return {"status":"under_manual_review"}

def auto_approve(state: State):
    print("Automatially approved")
    return {"status": "completed"}

builder = StateGraph(State)
builder.add_node("check_amount", check_amount)
builder.add_node("manual_review", manual_review)
builder.add_node("auto_approve", auto_approve)

builder.add_edge(START, "check_amount")

builder.add_edge("manual_review", END)
builder.add_edge("auto_approve", END)

graph = builder.compile()

res = graph.invoke({
    "amount": 20_000,
    "status": "new"
})
print(res)