"""Orchestrator-workers in plain Python: "Plan dinners for a week under $60, no palm oil."

The model decides what the parts are; app code runs and checks every part (L17, "Pattern: orchestrator-workers"):
  1. planner call    the dishes, which depend on the request            (PLANNER_MODEL, the stronger model)
  2. app code        caps the plan at MAX_DISHES
  3. worker calls    one per dish: search with the filter, then one model call picks a product  (WORKER_MODEL)
  4. app code        adds up the prices against the budget
  5. merge call      the week's list, in words                          (WORKER_MODEL)

Run:  uv run python -m plain.orchestrator --model stub
      uv run python -m plain.orchestrator --model gateway      (needs your course token, KB_MCP_TOKEN in .env)
"""
from __future__ import annotations

import json, os

from grocery.model import parse_json
from grocery.tools import GroceryTools

MAX_DISHES = 7        # one dinner per day (ours)
BUDGET_USD = float(os.environ.get("BUDGET_USD", "60"))   # the week's budget in the lecture's request (ours)

PLANNER_PROMPT = """You are the planner of a grocery app. Split the shopper's request into dinner dishes.
Reply with JSON only: {"dishes": [{"dish": "<name>", "search": "<one or two words for its main product>"}],
"filters": {"no_palm_oil": <true or false>}}. One dish per day of the request, built on products the shop sells."""

WORKER_PROMPT = """You are the worker for one dish of a grocery app. From the products in the user message, pick
the one that fits the dish best; when several fit, the cheapest. Reply with JSON only: {"code": "<code>"},
or {"code": null} when none fits."""

MERGE_PROMPT = """You are the merger of a grocery app. Write the week's shopping list from the JSON in the user
message: one line per dish with the product and its price, then the total and whether it is within the budget.
Use only the names and numbers in the JSON."""


def plan_week(request: str, planner, worker, tools: GroceryTools) -> dict:
    # 1. Planner call: one model call returns the dishes. Nothing has been searched yet.
    system = PLANNER_PROMPT + "\nThe shop sells: " + ", ".join(tools.categories()) + "."
    reply = planner.chat([{"role": "system", "content": system}, {"role": "user", "content": request}])
    plan = parse_json(reply.content)
    planned = [{"dish": str(d.get("dish") or d["search"]), "search": str(d["search"])}
               for d in plan.get("dishes") or [] if isinstance(d, dict) and d.get("search")]

    # 2. App code checks the plan before any worker starts: at most MAX_DISHES, whatever the model returned.
    #    (AI Engineering ch. 6, "Planning overview": validate a plan before it runs; a simple heuristic drops
    #    plans with more than X steps.)
    dishes = planned[:MAX_DISHES]
    filters = {"no_palm_oil": bool((plan.get("filters") or {}).get("no_palm_oil"))}

    # 3. One worker per dish. The number of workers depends on the plan, not on this code.
    picks = [run_worker(dish, filters, worker, tools) for dish in dishes]

    # 4. App code adds up the prices. The prices come from the product table, never from a model's reply.
    total = round(sum(p["price_usd"] for p in picks), 2)
    over_budget = total > BUDGET_USD

    # 5. Merge call: one model call turns the checked parts into the week's list. Writing up checked numbers
    #    is a small job, so it goes to the worker model.
    week = {"picks": picks, "total_usd": total, "budget_usd": BUDGET_USD, "over_budget": over_budget}
    merged = worker.chat([{"role": "system", "content": MERGE_PROMPT},
                          {"role": "user", "content": json.dumps(week)}])
    return {"request": request, "dishes_planned": len(planned), "dishes_kept": len(dishes), "filters": filters,
            **week, "reply": (merged.content or "").strip()}


def run_worker(dish: dict, filters: dict, worker, tools: GroceryTools) -> dict:
    """One dish: app code searches with the filter, one model call picks, app code looks the pick up."""
    found = tools.search_products(dish["search"], filters)
    reply = worker.chat([{"role": "system", "content": WORKER_PROMPT},
                         {"role": "user", "content": json.dumps({"dish": dish["dish"], **found})}])
    code = parse_json(reply.content).get("code")
    # the pick must be one of the products this worker was given; anything else counts as "none found"
    product = next((p for p in found["products"] if p["code"] == code), None)
    if product is None:
        return {"dish": dish["dish"], "code": None, "name": "nothing found", "price_usd": 0.0}
    return {"dish": dish["dish"], "code": product["code"], "name": product["name"],
            "price_usd": product["price_usd"], "palm_oil": product["palm_oil"]}


if __name__ == "__main__":
    from plain.run import main

    main("orchestrator")
