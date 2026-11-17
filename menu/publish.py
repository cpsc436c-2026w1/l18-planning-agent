"""The step after the plan: the shopper approves the week's menu, and only then it is published.

    plan (plain/orchestrator.py or with_strands/orchestrator.py)
      -> approve(week)                      a person says yes or no
      -> SNS topic l18-menu-ready           one message per approved plan, with its request_id
      -> SQS queue l18-menu-render -> Lambda l18-menu-email -> SES        (menu/fn/handler.py)

The agent's side ends at the publish call: it does not wait for the email and does not know who reads the
topic. `request_id` is made once, when the plan is made. A publish that is retried, or a message that SQS
delivers twice, carries the same id, and the email function sends one email per id.
"""
from __future__ import annotations

import json
import time
import uuid

import boto3

from menu import names

VERSION = 1


def new_request_id() -> str:
    return "menu-" + uuid.uuid4().hex[:16]


def menu_message(week: dict, shopper: str, request_id: str) -> dict:
    """What the topic carries: the checked parts of the plan. The names and prices are the catalogue's
    (the orchestrator read them there); the model's own text, `reply`, is left out."""
    return {"version": VERSION, "request_id": request_id, "shopper": shopper, "request": week["request"],
            "picks": [{"dish": p["dish"], "code": p["code"], "name": p["name"], "price_usd": p["price_usd"]}
                      for p in week["picks"]],
            "total_usd": week["total_usd"], "budget_usd": week["budget_usd"], "over_budget": week["over_budget"],
            "approved_at": round(time.time(), 3)}


def topic_arn(session=None) -> str:
    session = session or boto3.Session(region_name=names.REGION)
    account = session.client("sts").get_caller_identity()["Account"]
    return f"arn:aws:sns:{session.region_name}:{account}:{names.TOPIC}"


def send_menu(week: dict, shopper: str, request_id: str, approve, sns=None, arn: str | None = None) -> dict:
    """`approve(week) -> bool` is the gate. Nothing is published before it returns True."""
    if not approve(week):
        return {"published": False, "request_id": request_id, "reason": "not approved"}
    message = menu_message(week, shopper, request_id)
    sns = sns or boto3.client("sns", region_name=names.REGION)
    sent = sns.publish(TopicArn=arn or topic_arn(), Message=json.dumps(message, ensure_ascii=False),
                       MessageAttributes={"request_id": {"DataType": "String", "StringValue": request_id}})
    return {"published": True, "request_id": request_id, "sns_message_id": sent["MessageId"],
            "approved_at": message["approved_at"]}


def ask_to_email(week: dict) -> bool:
    answer = input(f"\nEmail this menu ({len(week['picks'])} dishes, ${week['total_usd']:.2f})? [y/N] ")
    return answer.strip().lower() == "y"


def wait_for_email(request_id: str, approved_at: float, seconds: float = 90, table=None) -> dict:
    """Reads the email function's record of this request_id until it says what happened to the email."""
    table = table or boto3.resource("dynamodb", region_name=names.REGION).Table(names.SENT_TABLE)
    deadline = time.time() + seconds
    while time.time() < deadline:
        item = table.get_item(Key={"request_id": request_id}, ConsistentRead=True).get("Item")
        if item and item["status"] != "sending":
            done_at = float(item.get("done_at", 0))
            return {"request_id": request_id, "status": item["status"],
                    "seconds_from_approval": round(done_at - approved_at, 2),
                    "ses_message_id": item.get("ses_message_id")}
        time.sleep(0.5)
    return {"request_id": request_id, "status": "no record yet", "waited_s": seconds}
