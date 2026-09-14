"""
Variant of send_tavily.py using TRUNCATE-WITH-A-NOTE instead of reject.

Key structural point: a router function used with add_conditional_edges
(fan_out) can ONLY return routing info (Send objects) -- it cannot also
write to state. So the truncation + "what got dropped" bookkeeping has
to happen in an actual NODE that runs before fan_out, not inside fan_out
itself. That's why `start` -- previously a no-op -- now does real work.
"""

import os
from typing import TypedDict, Annotated
import operator
import requests

from dotenv import load_dotenv
from tavily import TavilyClient
from openai import OpenAI
from langgraph.graph import StateGraph, START, END
from langgraph.types import Send
from tavily.errors import (
    TimeoutError as TavilyTimeoutError,
    UsageLimitExceededError,
    BadRequestError,
    ForbiddenError,
    InvalidAPIKeyError,
)

load_dotenv("/Users/bidishadas/Desktop/ResolveFlow/.env")

tavily = TavilyClient(api_key=os.environ["TAVILY_API_KEY"])
openai_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
MAX_QUERIES = 5


class FanState(TypedDict):
    queries: list[str]
    dropped_queries: list[str]                   # NEW -- only `start` ever writes this
    hits: Annotated[list[dict], operator.add]
    summary: str
    answer: str


class SubState(TypedDict):
    query: str


def start(state: FanState) -> dict:
    """Truncate to MAX_QUERIES and record what got dropped, before fan_out runs."""
    queries = state["queries"]
    kept = queries[:MAX_QUERIES]
    dropped = queries[MAX_QUERIES:]
    return {"queries": kept, "dropped_queries": dropped}


def fan_out(state: FanState):
    # No length guard needed here -- `start` already guaranteed
    # len(state["queries"]) <= MAX_QUERIES by the time this runs.
    return [Send("retrieve", {"query": q}) for q in state["queries"]]


def retrieve(state: SubState) -> dict:
    try:
        result = tavily.search(query=state["query"], max_results=2, include_raw_content=False)
        hits = [
            {"query": state["query"], "title": r["title"], "content": r["content"]}
            for r in result["results"]
        ]
    except (UsageLimitExceededError, BadRequestError, ForbiddenError,
             InvalidAPIKeyError, TavilyTimeoutError,
             requests.exceptions.RequestException) as e:
        hits = [{"query": state["query"], "error": str(e)}]
    return {"hits": hits}


def compile_answer(state: FanState) -> dict:
    good_hits = [h for h in state["hits"] if "content" in h]
    failed_queries = [h["query"] for h in state["hits"] if "error" in h]
    dropped = state["dropped_queries"]

    context = "\n\n".join(
        f"[{h['query']}] {h['title']}: {h['content']}" for h in good_hits
    )

    if not good_hits:
        return {
            "summary": f"compiled 0 hits ({len(failed_queries)} failed, {len(dropped)} dropped)",
            "answer": "Could not retrieve any results — all queries failed.",
        }

    response = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "Answer using only the provided context. Be concise."},
            {"role": "user", "content": f"Context:\n{context}\n\nQuestion: Summarize what LangGraph's Send API and conditional edges are for."},
        ],
    )
    answer = response.choices[0].message.content

    notes = []
    if failed_queries:
        notes.append(f"no results for: {', '.join(failed_queries)}")
    if dropped:
        notes.append(f"dropped (exceeded {MAX_QUERIES}-query limit): {', '.join(dropped)}")
    if notes:
        answer += "\n\n(Note: " + "; ".join(notes) + ")"

    return {
        "summary": f"compiled {len(good_hits)} hits ({len(failed_queries)} failed, {len(dropped)} dropped)",
        "answer": answer,
    }


graph = StateGraph(FanState)
graph.add_node("start", start)
graph.add_node("retrieve", retrieve)
graph.add_node("compile_answer", compile_answer)

graph.add_edge(START, "start")
graph.add_conditional_edges("start", fan_out)
graph.add_edge("retrieve", "compile_answer")
graph.add_edge("compile_answer", END)

app = graph.compile()

if __name__ == "__main__":
    result = app.invoke({
        "queries": [
            "LangGraph Send API",
            "LangGraph conditional edges",
            "LangGraph checkpointer",
            "LangGraph interrupt",
            "LangGraph reducers",
            "LangGraph subgraphs",        # 6th query -- exceeds MAX_QUERIES=5, gets dropped
            "LangGraph streaming modes",  # 7th query -- also dropped
        ],
        "dropped_queries": [],
        "hits": [],
        "summary": "",
        "answer": "",
    })

    for hit in result["hits"]:
        if "error" in hit:
            print(f"{hit['query']}::ERROR::{hit['error']}")
        else:
            print(f"{hit['query']}::{hit['title']}")
    print(result["summary"])
    print("\n--- ANSWER ---")
    print(result["answer"])
