"""Command line for the two Strands versions. Prints the result and saves it, with its trace, under output/.

    uv run python -m with_strands.run orchestrator
    uv run python -m with_strands.run agent --yes

Both use your course token (KB_MCP_TOKEN in .env, see the README) and the course LLM gateway.
"""
from __future__ import annotations

import argparse
import json
import logging
import time

from grocery import config
from grocery.model import cost_cad
from grocery.store import open_store
from grocery.tools import GroceryTools
from plain.run import REQUESTS, ask_shopper
from with_strands import tracing
from with_strands.models import gateway_model


def usage_from_trace(items: list[dict]) -> dict:
    """Tokens and cost, added up from the model-call spans of the run's trace."""
    calls = [i for i in items if i["span"] == "chat" and i["tokens_in"] is not None]
    return {"model_calls": len(calls), "tokens_in": sum(c["tokens_in"] for c in calls),
            "tokens_out": sum(c["tokens_out"] for c in calls),
            "cost_cad": round(sum(cost_cad(c["model"], c["tokens_in"], c["tokens_out"]) for c in calls), 6)}


def usage_by_model(items: list[dict]) -> dict:
    """The same sums, one entry per model: who made how many calls, and what they cost."""
    out: dict[str, dict] = {}
    for c in (i for i in items if i["span"] == "chat" and i["tokens_in"] is not None):
        m = out.setdefault(c["model"], {"model_calls": 0, "tokens_in": 0, "tokens_out": 0, "cost_cad": 0.0})
        m["model_calls"] += 1
        m["tokens_in"] += c["tokens_in"]
        m["tokens_out"] += c["tokens_out"]
        m["cost_cad"] = round(m["cost_cad"] + cost_cad(c["model"], c["tokens_in"], c["tokens_out"]), 6)
    return out


def main() -> None:
    # Qwen sends its reasoning with each reply; Strands drops it from the next request and warns every time
    logging.getLogger("strands").setLevel(logging.ERROR)
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("which", choices=["orchestrator", "agent"])
    ap.add_argument("--request", default=None)
    ap.add_argument("--shopper", default="sam")
    ap.add_argument("--yes", action="store_true", help="agent: approve the write without asking (saved runs)")
    ap.add_argument("--no", action="store_true", help="agent: decline the write without asking")
    ap.add_argument("--email", action="store_true",
                    help="orchestrator: ask to email the week's menu; a yes publishes it to SNS (needs AWS)")
    ap.add_argument("--save", default=None)
    a = ap.parse_args()
    request = a.request or REQUESTS[a.which]
    grocery = GroceryTools(open_store())
    planner = gateway_model(config.PLANNER_MODEL)
    worker = gateway_model(config.WORKER_MODEL, max_tokens=600)
    t0 = time.monotonic()

    with tracing.run_span(f"grocery.{a.which}", request=request) as trace_id:
        if a.which == "orchestrator":
            from with_strands.orchestrator import plan_week

            result = plan_week(request, planner, worker, grocery)
            print(f"model asked for {result['dishes_asked']} dishes, kept {result['dishes_kept']}")
            for p in result["picks"]:
                print(f"  {p['dish'][:44]:<44} {p['name'][:32]:<32} ${p['price_usd']:>5.2f}")
            print(f"total ${result['total_usd']:.2f} of ${result['budget_usd']:.0f}: "
                  f"{'OVER BUDGET' if result['over_budget'] else 'within budget'}")
            models = {"planner": config.PLANNER_MODEL, "worker": config.WORKER_MODEL}
            if a.email:
                from plain.run import email_step

                print("\n" + result.get("reply", ""))
                result["menu_email"] = email_step(result, a.shopper, a.yes, a.no)
        else:
            from with_strands.agent import run_agent

            confirm = (lambda s, p: True) if a.yes else (lambda s, p: False) if a.no else ask_shopper
            result = run_agent(request, a.shopper, planner, grocery, confirm)
            print(f"{result['status']} ({result['stop_reason']}): {result['model_calls']} model calls, "
                  f"{result['tool_calls']} tool calls; approvals asked: {len(result['approvals_asked'])}")
            print("list:", json.dumps(grocery.get_list(a.shopper), ensure_ascii=False))
            models = {"agent": config.PLANNER_MODEL}
        if "menu_email" not in result:
            print("\n" + result.get("reply", ""))

    seconds = round(time.monotonic() - t0, 1)
    items = tracing.items()
    used = usage_from_trace(items)
    print(f"\n{used['model_calls']} model calls, {used['tokens_in']} + {used['tokens_out']} tokens, "
          f"{used['cost_cad']:.6f} CAD, {seconds} s, trace {trace_id} ({len(items)} spans)")
    config.OUTPUT_DIR.mkdir(exist_ok=True)
    out = config.OUTPUT_DIR / (a.save or f"strands-{a.which}-gateway.json")
    out.write_text(json.dumps({"which": a.which, "sdk": "strands-agents 1.64.0", "models": models,
                               "result": result, "usage": used, "seconds": seconds, "trace": items},
                              indent=1, ensure_ascii=False, default=str))
    print(f"saved {out.relative_to(config.ROOT)}")


if __name__ == "__main__":
    main()
