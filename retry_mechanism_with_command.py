
from typing import Literal
from typing_extensions import TypedDict, Annotated
from langgraph.types import Command
from langgraph.graph import StateGraph, START, END

class AgentState(TypedDict):
    attempts: int
    tool_call:str
    tool_result: str
    error: str
    status: str
    max_retries:int


# state["max_retries"] = 3

def agent(state: AgentState)-> Command[Literal["execute_tool", "fail", "__end__"]]:
    if state["status"] =="success":
        return Command(update = {"tool_call": ""}, goto= END)  # type: ignore[return-value]
    if  state["attempts"] <= state["max_retries"]:
        return Command(update = {"tool_call": "execute_tool", "attempts": state["attempts"]+1}, goto = "execute_tool")
    else:
       return Command(update={}, goto ="fail")

def execute_tool(state: AgentState) -> dict:
    """Execute the tool """
    if (state["attempts"] == 2):
         return {"status":"success"}
    else:
        return {"status":"failed"}

def fail(state: AgentState) -> dict:
    """Report that retries were exhausted"""
    print(f"Giving up after {state['attempts']} attempts")
    return {"error": "max retries exceeded"}

builder = StateGraph(AgentState)
builder.add_node("agent", agent)
builder.add_node("execute_tool", execute_tool)
builder.add_node("fail", fail)

builder.add_edge(START, "agent")
builder.add_edge("execute_tool", "agent")
builder.add_edge("fail", END)

graph = builder.compile()
print(graph.get_graph().draw_mermaid())

if __name__ == "__main__":
    result = graph.invoke({
        "attempts": 0,
        "tool_call": "",
        "tool_result": "",
        "error": "",
        "status": "",
        "max_retries": 3,
    })
    print(result)