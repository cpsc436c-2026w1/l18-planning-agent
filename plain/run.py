"""Command line for the two plain-Python versions. Prints the result and saves it, with its trace, under output/.

    uv run python -m plain.orchestrator --model stub
    uv run python -m plain.agent --model gateway --yes
"""
from __future__ import annotations

import argparse
import json

from grocery import config
from grocery.model import make_model, usage_total
from grocery.store import open_store
from grocery.tools import GroceryTools
from grocery.trace import Trace, TracedModel, TracedTools

REQUESTS = {
    "orchestrator": "Plan dinners for a week under $60, no palm oil.",
    "agent": "Which is cheaper, spaghetti or penne? Add the cheaper one to my list.",
}


def ask_shopper(shopper: str, product: dict) -> bool:
    answer = input(f"\nAdd {product['name']} ({product['brand']}, ${product['price_usd']:.2f}) "
                   f"to {shopper}'s list? [y/N] ")
    return answer.strip().lower() == "y"


def email_step(week: dict, shopper: str, yes: bool = False, no: bool = False, wait: bool = True) -> dict:
    """After the plan: ask, publish on a yes, then read what the email function recorded (menu/publish.py)."""
    try:
        from menu.publish import ask_to_email, new_request_id, send_menu, wait_for_email
    except ImportError:
        raise SystemExit("--email needs the course's SNS and SES setup, which is not in this repository")

    approve = (lambda w: True) if yes else (lambda w: False) if no else ask_to_email
    out = send_menu(week, shopper, new_request_id(), approve)
    if not out["published"]:
        print("menu not approved: nothing was published")
        return out
    print(f"menu published to SNS: request_id {out['request_id']}")
    if wait:
        out["email"] = wait_for_email(out["request_id"], out["approved_at"])
        print(f"email: {out['email']['status']}, {out['email'].get('seconds_from_approval', '?')} s after the approval")
        out["email"]["ses_message_id"] = "<id>"      # the saved run keeps our request_id, not AWS's message ids
    out["sns_message_id"] = "<id>"
    return out


def main(which: str) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", choices=["stub", "gateway"], default="stub")
    ap.add_argument("--request", default=REQUESTS[which])
    ap.add_argument("--shopper", default="sam")
    ap.add_argument("--yes", action="store_true", help="approve the write or the email without asking (saved runs)")
    ap.add_argument("--no", action="store_true", help="decline the write or the email without asking")
    ap.add_argument("--email", action="store_true",
                    help="orchestrator: ask to email the week's menu; a yes publishes it to SNS (needs AWS)")
    ap.add_argument("--save", default=None, help="file name under output/ (default: <which>-<model>.json)")
    a = ap.parse_args()

    trace = Trace()
    tools = TracedTools(GroceryTools(open_store()), trace)
    planner_name = config.PLANNER_MODEL if a.model == "gateway" else "stub-planner"
    worker_name = config.WORKER_MODEL if a.model == "gateway" else "stub-worker"
    planner = TracedModel(make_model(a.model, planner_name), trace)
    worker = TracedModel(make_model(a.model, worker_name, max_tokens=600), trace)

    if which == "orchestrator":
        from plain.orchestrator import plan_week

        result = plan_week(a.request, planner, worker, tools)
        used = usage_total(planner, worker)
        print(f"planned {result['dishes_planned']} dishes, kept {result['dishes_kept']}; "
              f"filters {result['filters']}")
        for p in result["picks"]:
            print(f"  {p['dish']:<28} {p['name'][:40]:<40} ${p['price_usd']:>5.2f}")
        print(f"total ${result['total_usd']:.2f} of ${result['budget_usd']:.0f}: "
              f"{'OVER BUDGET' if result['over_budget'] else 'within budget'}")
        print("\n" + result["reply"])
        models = {"planner": planner.model, "worker": worker.model}
        if a.email:
            result["menu_email"] = email_step(result, a.shopper, a.yes, a.no)
    else:
        from plain.agent import run_agent

        confirm = (lambda s, p: True) if a.yes else (lambda s, p: False) if a.no else ask_shopper
        result = run_agent(a.request, a.shopper, planner, tools, confirm)
        used = usage_total(planner)
        print(f"{result['status']} ({result['stop_reason']}): {result['model_calls']} model calls, "
              f"{result['tool_calls']} tool calls")
        print(result["reply"])
        print("list:", json.dumps(tools.inner.get_list(a.shopper), ensure_ascii=False))
        models = {"agent": planner.model}

    print(f"\n{used['model_calls']} model calls, {used['tokens_in']} + {used['tokens_out']} tokens, "
          f"{used['cost_cad']:.6f} CAD, {trace.seconds()} s, trace {trace.trace_id}")
    config.OUTPUT_DIR.mkdir(exist_ok=True)
    out = config.OUTPUT_DIR / (a.save or f"{which}-{a.model}.json")
    out.write_text(json.dumps({"which": which, "models": models, "result": result, "usage": used,
                               "seconds": trace.seconds(), "trace": trace.items}, indent=1, ensure_ascii=False))
    print(f"saved {out.relative_to(config.ROOT)}")
