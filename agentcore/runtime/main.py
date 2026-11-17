"""AgentCore Runtime entrypoint: the two Strands agents of with_strands/, with their tools behind the Gateway.

Payloads (one Runtime session = one shopper conversation):
    {"mode": "plan", "prompt": "Plan dinners for a week under $60, no palm oil."}
    {"mode": "plan", "prompt": "...", "planner_model": "claude-sonnet-5-5"}    # the planner, for this call
    {"mode": "add", "prompt": "Which is cheaper, ...? Add the cheaper one to my list.", "shopper": "sam"}
    {"mode": "approve", "answer": "y"}       # answers the approval the "add" call returned, in the same session
    {"mode": "plan", "prompt": "...", "email": true}     # the reply asks for an approval of the menu
    {"mode": "approve_menu", "answer": "y"}  # a yes publishes the plan this session made to SNS (menu/publish.py)

The course token is read from SSM at the first call (COURSE_TOKEN_SSM_PARAM). It is not in the Runtime's
configuration and not in this repo. An agent that waits for an approval is kept in this process: AgentCore
routes every call of one session to the same microVM until the session ends (AgentCore docs, "Runtime session
lifecycle": context is preserved between invocations to the same session).
"""
import json
import os
import time
import uuid
from pathlib import Path

import boto3
from bedrock_agentcore.runtime import BedrockAgentCoreApp

app = BedrockAgentCoreApp()
REGION = os.environ.get("AWS_REGION", "ca-central-1")
WAITING: dict[str, tuple] = {}      # runtime session id -> the agent that waits for an approval, and its ids
MENUS: dict[str, tuple] = {}        # runtime session id -> the plan that waits for "email it?", its id, the shopper
_state: dict = {}


def setup() -> dict:
    """Once per microVM: the course token, the Gateway connection, the worker model."""
    if _state:
        return _state
    if not os.environ.get("COURSE_MCP_TOKEN"):
        ssm = boto3.client("ssm", region_name=REGION)
        os.environ["COURSE_MCP_TOKEN"] = ssm.get_parameter(
            Name=os.environ["COURSE_TOKEN_SSM_PARAM"], WithDecryption=True)["Parameter"]["Value"]
    import logging

    from grocery import config
    from with_strands import tracing
    from with_strands.gateway_tools import GatewayGrocery, open_gateway
    from with_strands.models import gateway_model

    logging.getLogger("strands").setLevel(logging.ERROR)
    tracing.setup()
    client = open_gateway(os.environ["TOOLS_GATEWAY_URL"], REGION)
    rows = [json.loads(line) for line in (Path(__file__).parent / "data" / "products.jsonl").read_text().splitlines()]
    _state.update(grocery=GatewayGrocery(client, sorted({r["category"] for r in rows})),
                  worker=gateway_model(config.WORKER_MODEL, 600), worker_model=config.WORKER_MODEL,
                  default_planner=config.PLANNER_MODEL, planners={})
    return _state


def planner_for(model_id: str):
    """The planner model of one invocation. Two keys: a Qwen planner uses the course token, like the workers.
    A provider model uses the instructor's demo key, read from its own SSM parameter and kept in this process."""
    from with_strands.models import gateway_model

    s = setup()
    if model_id not in s["planners"]:
        token = None
        if not model_id.startswith("qwen"):
            token = boto3.client("ssm", region_name=REGION).get_parameter(
                Name=os.environ["PLANNER_TOKEN_SSM_PARAM"], WithDecryption=True)["Parameter"]["Value"].strip()
        s["planners"][model_id] = gateway_model(model_id, token=token)
    return s["planners"][model_id]


@app.entrypoint
def invoke(payload, context):
    from with_strands import agent as bounded
    from with_strands import tracing
    from with_strands.orchestrator import plan_week
    from with_strands.run import usage_by_model, usage_from_trace

    t0 = time.time()
    s = setup()
    grocery = s["grocery"]
    session = getattr(context, "session_id", None) or "nosession"
    mode = str(payload.get("mode", "add"))
    planner_id = str(payload.get("planner_model") or s["default_planner"])
    planner = planner_for(planner_id)
    with tracing.run_span(f"grocery.{mode}", session=session) as trace_id:
        if mode == "plan":
            out = plan_week(str(payload["prompt"]), planner, s["worker"], grocery,
                            search_tool=grocery.tools["search_products"])
            if payload.get("email"):
                # The plan stays here. The approval call sends only an answer, so what is published is the plan
                # this session made, never a plan the caller sends back.
                from menu.publish import new_request_id

                request_id = new_request_id()
                MENUS[session] = (out, request_id, str(payload.get("shopper", "sam")))
                out = {**out, "status": "needs_menu_approval", "menu_request_id": request_id}
        elif mode == "approve_menu":
            if session not in MENUS:
                return {"status": "error", "error": "no menu is waiting in this session"}
            from menu.publish import send_menu

            week, request_id, shopper = MENUS.pop(session)
            yes = str(payload.get("answer", "n")).strip().lower() == "y"
            out = {"menu_email": send_menu(week, shopper, request_id, lambda w: yes, arn=os.environ["MENU_TOPIC_ARN"])}
            out["status"] = "menu_published" if out["menu_email"]["published"] else "menu_declined"
        elif mode == "add":
            shopper, request_id = str(payload.get("shopper", "sam")), uuid.uuid4().hex
            agent, cap = bounded.build(planner, grocery, shopper, request_id, tools=list(grocery.tools.values()))
            out = bounded.start(agent, cap, str(payload["prompt"]), shopper, request_id)
            if out["status"] == "needs_approval":
                WAITING[session] = (agent, cap, request_id, shopper, [a["id"] for a in out["approvals"]])
        elif mode == "approve":
            if session not in WAITING:
                return {"status": "error", "error": "no approval is waiting in this session"}
            agent, cap, request_id, shopper, pending = WAITING.pop(session)
            out = bounded.resume(agent, cap, request_id, {i: str(payload.get("answer", "n")) for i in pending})
            if out["status"] == "needs_approval":
                WAITING[session] = (agent, cap, request_id, shopper, [a["id"] for a in out["approvals"]])
            else:
                out["list"] = grocery.get_list(shopper)
        else:
            return {"status": "error", "error": f"unknown mode {mode}"}
    items = tracing.items()
    return {**out, "mode": mode, "models": {"planner": planner_id, "worker": s["worker_model"]}, "usage": usage_from_trace(items),
            "usage_by_model": usage_by_model(items), "trace_id": trace_id,
            "spans": len(items), "seconds_in_agent": round(time.time() - t0, 1), "runtime_session": session}


if __name__ == "__main__":
    app.run()
