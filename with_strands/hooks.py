"""What stays ours when an SDK runs the loop: the caps, the budget, the approval rule and the idempotency key.

Strands calls a hook at fixed points of its agent loop (strands.hooks, Strands Agents 1.64.0). A hook is app
code: the model does not see it and a prompt cannot switch it off.

  CallCap       before every model call and every tool call: count it, and cancel it past the cap
  WeekBudget    around every dish_worker call: at most 7 dishes, prices added up against the budget
  WriteGuard    before add_to_list: set the shopper named in the request and the request key, and interrupt the agent
                until the shopper approves (docs: "Interrupts", BeforeToolCallEvent)
"""
from __future__ import annotations

import hashlib
import time

from strands.hooks import (AfterToolCallEvent, BeforeInvocationEvent, BeforeModelCallEvent, BeforeToolCallEvent,
                           HookProvider, HookRegistry)

from grocery.model import parse_json
from grocery.tools import GroceryTools


def tool_name(event) -> str:
    """'add_to_list' for the local tool and for the AgentCore Gateway's 'grocery-tools___add_to_list'."""
    return event.tool_use["name"].split("___")[-1]


class CallCap(HookProvider):
    """At most `model_calls` model calls, `tool_calls` tool calls and `seconds` seconds.

    per_invocation=True starts the count again on every invocation: right for a worker that is called once per
    dish. The bounded agent keeps one count for the whole request, because an approval resumes the same request
    with a second invocation.
    """

    def __init__(self, model_calls: int, tool_calls: int, seconds: float = 120, per_invocation: bool = False):
        self.max_model, self.max_tool, self.seconds, self.per_invocation = model_calls, tool_calls, seconds, per_invocation
        self.stopped = ""
        self._reset()

    def _reset(self) -> None:
        self.model_calls, self.tool_ids, self.deadline = 0, set(), time.monotonic() + self.seconds

    def register_hooks(self, registry: HookRegistry, **kwargs) -> None:
        registry.add_callback(BeforeInvocationEvent, self.start)
        registry.add_callback(BeforeModelCallEvent, self.before_model)
        registry.add_callback(BeforeToolCallEvent, self.before_tool)

    def start(self, event: BeforeInvocationEvent) -> None:
        if self.per_invocation:
            self._reset()

    def before_model(self, event: BeforeModelCallEvent) -> None:
        over = (f"cap of {self.max_model} model calls" if self.model_calls >= self.max_model else
                f"deadline of {self.seconds:.0f} s" if time.monotonic() > self.deadline else "")
        if over:
            self.stopped = over
            event.cancel = f"Stopped by the app: {over}."     # the loop ends without this model call
        else:
            self.model_calls += 1

    def before_tool(self, event: BeforeToolCallEvent) -> None:
        call_id = event.tool_use["toolUseId"]
        if call_id in self.tool_ids:                # the same call again, resumed after an approval
            return
        if len(self.tool_ids) >= self.max_tool:
            # this call does not run; the model gets the refusal as the tool result and may still reply.
            # A model that keeps asking is ended by the cap on model calls.
            self.stopped = f"cap of {self.max_tool} tool calls"
            event.cancel_tool = f"Stopped by the app: {self.stopped}. Reply now with what you have."
        else:
            self.tool_ids.add(call_id)

    @property
    def tool_calls(self) -> int:
        return len(self.tool_ids)


class WeekBudget(HookProvider):
    """The orchestrator's two checks. The dishes and the total are kept here, in app code, not in the model's text."""

    def __init__(self, max_dishes: int = 7, budget_usd: float = 60.0):
        self.max_dishes, self.budget_usd = max_dishes, budget_usd
        self.picks: list[dict] = []
        self.asked = 0          # dish_worker calls the model asked for
        self.total_usd = 0.0

    def register_hooks(self, registry: HookRegistry, **kwargs) -> None:
        registry.add_callback(BeforeToolCallEvent, self.before_worker)
        registry.add_callback(AfterToolCallEvent, self.after_worker)

    def before_worker(self, event: BeforeToolCallEvent) -> None:
        if tool_name(event) != "dish_worker":
            return
        self.asked += 1
        if len(self.picks) >= self.max_dishes:      # the plan is cut here, whatever the model planned
            event.cancel_tool = f"The plan is capped at {self.max_dishes} dishes. Do not plan more dishes."
        elif self.total_usd > self.budget_usd:      # no more spending once the budget is passed
            event.cancel_tool = (f"The total is ${self.total_usd:.2f}, over the ${self.budget_usd:.0f} budget. "
                                 "Stop and report it.")

    def after_worker(self, event: AfterToolCallEvent) -> None:
        if tool_name(event) != "dish_worker" or event.cancel_message or isinstance(event.result, Exception):
            return
        if event.result.get("status") == "error":
            return
        pick = parse_json(" ".join(c.get("text", "") for c in event.result.get("content", [])))
        if "price_usd" not in pick:
            return
        self.picks.append(pick)
        self.total_usd = round(self.total_usd + float(pick["price_usd"]), 2)

    @property
    def over_budget(self) -> bool:
        return self.total_usd > self.budget_usd


class WriteGuard(HookProvider):
    """Before add_to_list runs: the shopper is the one named in the request, the write has its key, and a person approves."""

    def __init__(self, grocery: GroceryTools, shopper: str, request_id: str):
        self.grocery, self.shopper, self.request_id = grocery, shopper, request_id

    def register_hooks(self, registry: HookRegistry, **kwargs) -> None:
        registry.add_callback(BeforeToolCallEvent, self.before_tool)

    def before_tool(self, event: BeforeToolCallEvent) -> None:
        args = event.tool_use["input"]
        if "shopper" in args:
            args["shopper"] = self.shopper                  # the shopper named in the request, whatever the model wrote
        if tool_name(event) != "add_to_list":
            return                                          # read tools run without a question
        product = self.grocery.get_product(str(args.get("code", "")))
        if "error" in product:
            event.cancel_tool = product["error"]
            return
        args["shopper"] = self.shopper
        args["request_id"] = hashlib.sha256(
            f"{self.request_id}:{self.shopper}:{product['code']}".encode()).hexdigest()[:16]
        # The agent stops here and returns the interrupt to the caller. When the caller invokes the agent again
        # with the shopper's answer, this hook runs again and interrupt() returns that answer.
        answer = event.interrupt("grocery-approval", reason={
            "shopper": self.shopper, "code": product["code"], "name": product["name"],
            "brand": product["brand"], "price_usd": product["price_usd"]})
        if str(answer).strip().lower() not in ("y", "yes"):
            event.cancel_tool = "The shopper declined. Nothing was added; do not ask again."
