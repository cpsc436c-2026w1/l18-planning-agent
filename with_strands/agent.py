"""The bounded agent with Strands: the same request as plain/agent.py.

What Strands gives: the loop, and an interrupt that stops the agent before a tool call and resumes it with the
caller's answer. What stays ours (with_strands/hooks.py): CallCap (6 tool calls, 8 model calls, a deadline)
and WriteGuard (the shopper named in the request, the request key, the approval before add_to_list).

Run:  uv run python -m with_strands.run agent
"""
from __future__ import annotations

import uuid

from strands import Agent

from grocery.tools import GroceryTools
from plain.agent import AGENT_PROMPT, DEADLINE_S, MAX_MODEL_CALLS, MAX_TOOL_CALLS
from with_strands.hooks import CallCap, WriteGuard
from with_strands.tools import make_tools


def build(model, grocery: GroceryTools, shopper: str, request_id: str, tools: list | None = None):
    """`tools`: the local Strands tools by default; on AgentCore, the Gateway's MCP tools (agentcore/runtime)."""
    cap = CallCap(model_calls=MAX_MODEL_CALLS, tool_calls=MAX_TOOL_CALLS, seconds=DEADLINE_S)
    agent = Agent(name="grocery_agent", model=model, system_prompt=AGENT_PROMPT, callback_handler=None,
                  tools=tools or list(make_tools(grocery).values()),
                  hooks=[cap, WriteGuard(grocery, shopper, request_id)])
    return agent, cap


def start(agent: Agent, cap: CallCap, request: str, shopper: str, request_id: str) -> dict:
    """First invocation. Returns status 'needs_approval' with the interrupts, or the finished run."""
    return _report(agent(f"Shopper {shopper}: {request}"), cap, request_id)


def resume(agent: Agent, cap: CallCap, request_id: str, answers: dict[str, str]) -> dict:
    """Second invocation, with the shopper's answer for each interrupt id."""
    responses = [{"interruptResponse": {"interruptId": i, "response": a}} for i, a in answers.items()]
    return _report(agent(responses), cap, request_id)


def _report(result, cap: CallCap, request_id: str) -> dict:
    run = {"request_id": request_id, "model_calls": cap.model_calls, "tool_calls": cap.tool_calls,
           "stopped": cap.stopped, "stop_reason": result.stop_reason}
    if result.stop_reason == "interrupt":
        return {**run, "status": "needs_approval",
                "approvals": [{"id": i.id, "name": i.name, **i.reason} for i in result.interrupts]}
    return {**run, "status": "stopped" if cap.stopped else "done", "reply": str(result).strip()}


def run_agent(request: str, shopper: str, model, grocery: GroceryTools, confirm, request_id: str | None = None):
    """The whole request on the laptop: start, ask the shopper for each interrupt, resume."""
    request_id = request_id or uuid.uuid4().hex
    agent, cap = build(model, grocery, shopper, request_id)
    run = start(agent, cap, request, shopper, request_id)
    asked = []
    while run["status"] == "needs_approval":
        answers = {a["id"]: ("y" if confirm(shopper, a) else "n") for a in run["approvals"]}
        asked += run["approvals"]
        run = resume(agent, cap, request_id, answers)
    return {"request": request, "shopper": shopper, **run, "approvals_asked": asked}
