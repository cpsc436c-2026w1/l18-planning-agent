"""Orchestrator-workers with Strands: the same request as plain/orchestrator.py.

What Strands gives: the loop, the tool calls, a worker agent used as a tool, hooks and traces.
What stays ours (with_strands/hooks.py): the cap of 7 dishes and the 60 USD budget, in the WeekBudget hook.

Different from the plain version: here the planner model drives a loop and calls the worker once per dish as a
tool, so it makes a model call per step; in the plain version app code runs the workers after one planner call.

Run:  uv run python -m with_strands.run orchestrator
"""
from __future__ import annotations

import json

from strands import Agent, tool
from strands.tools.executors import SequentialToolExecutor

from grocery.model import parse_json
from grocery.tools import GroceryTools
from with_strands.hooks import CallCap, WeekBudget
from with_strands.tools import make_tools

MAX_DISHES = 7
BUDGET_USD = 60.0

ORCHESTRATOR_PROMPT = """You are the orchestrator of a grocery app. Split the shopper's request into dinner
dishes, one per day, built on products the shop sells. For each dish call dish_worker once. When every dish has
a product, or a tool result tells you to stop, write the week's list: one line per dish with the product and its
price, then say whether the total is within the budget. Use only names and prices that the tool results hold."""

WORKER_PROMPT = """You are the worker for one dish of a grocery app. Call search_products once with the search
words and the filters you were given; search for the main product only. Pick the product that fits the dish;
when several fit, the cheapest.
Reply with JSON only: {"code": "<code>"}, or {"code": null} when none fits."""


def build(planner_model, worker_model, grocery: GroceryTools, search_tool=None):
    """`search_tool`: the local Strands tool by default; on AgentCore, the Gateway's MCP tool."""
    search_tool = search_tool or make_tools(grocery)["search_products"]

    @tool
    def dish_worker(dish: str, search: str, no_palm_oil: bool = False) -> str:
        """Finds one product for one dinner dish and returns its code, name and price_usd.

        Args:
            dish: the dish, for example "pasta with tomato sauce"
            search: one or two words for the dish's main product, for example "pasta"
            no_palm_oil: true when the shopper asked for no palm oil
        """
        # The worker is an agent of its own: its own model, prompt, tool and cap. A new Agent per dish, because
        # one Agent object runs one request at a time (in 1.64.0 worker.as_tool() refused a second call of the
        # same batch with "already processing a request").
        worker = Agent(name="dish_worker", model=worker_model, system_prompt=WORKER_PROMPT,
                       tools=[search_tool], callback_handler=None,
                       hooks=[CallCap(model_calls=4, tool_calls=2)])
        filters = {"no_palm_oil": True} if no_palm_oil else {}
        reply = worker(f"Dish: {dish}. Search words: {search}. Filters: {json.dumps(filters)}")
        # the worker names a code; the name and the price are read from the product table, not from its reply
        product = grocery.get_product(str(parse_json(str(reply)).get("code")))
        if "error" in product or (no_palm_oil and product["palm_oil"] != "no"):
            return json.dumps({"dish": dish, "code": None, "name": "nothing found", "price_usd": 0.0})
        return json.dumps({"dish": dish, "code": product["code"], "name": product["name"],
                           "price_usd": product["price_usd"], "palm_oil": product["palm_oil"]})

    week = WeekBudget(MAX_DISHES, BUDGET_USD)
    cap = CallCap(model_calls=MAX_DISHES + 5, tool_calls=2 * MAX_DISHES, seconds=300)
    prompt = ORCHESTRATOR_PROMPT + "\nThe shop sells: " + ", ".join(grocery.categories()) + "."
    orchestrator = Agent(
        name="orchestrator", model=planner_model, system_prompt=prompt, callback_handler=None,
        tools=[dish_worker],                            # an agent as a tool (Strands docs, "Agents as tools")
        tool_executor=SequentialToolExecutor(),         # one worker at a time, in the order of the plan
        hooks=[week, cap])
    return orchestrator, week, cap


def plan_week(request: str, planner_model, worker_model, grocery: GroceryTools, search_tool=None) -> dict:
    orchestrator, week, cap = build(planner_model, worker_model, grocery, search_tool)
    result = orchestrator(request)
    return {"request": request, "dishes_asked": week.asked, "dishes_kept": len(week.picks), "picks": week.picks,
            "total_usd": week.total_usd, "budget_usd": week.budget_usd, "over_budget": week.over_budget,
            "stopped": cap.stopped, "stop_reason": result.stop_reason, "reply": str(result).strip()}
