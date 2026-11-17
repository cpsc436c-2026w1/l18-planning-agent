"""Lambda l18-menu-email: one approved menu in, one email out.

Called by Lambda's SQS event source mapping with a batch of messages from the queue l18-menu-render. For each:
  1. read and check the message      a message that cannot be read is reported as failed
  2. render the email                Jinja2: menu.html.j2 and menu.txt.j2
  3. claim the request_id            conditional put in the table l18-menu-sent; a second claim is refused
  4. send                            SES, from and to one verified address (SES sandbox)
  5. record the result               status sent, with SES's message id

The reply names the messages that failed (partial batch response). Lambda deletes the others from the queue.
A failed message becomes visible again and is retried; after 3 receives SQS moves it to the dead-letter queue.

Once per request_id: SQS standard queues deliver at least once, so the same message can arrive twice. The
claim in step 3 comes before the send, and a failed send gives the claim back. A crash between steps 3 and 4
leaves the claim without an email: this design sends at most one email per request_id.

Environment: MENU_SENT_TABLE; MENU_ADDRESS_PARAM (the SSM parameter that holds the address) or MENU_EMAIL_TO;
MENU_SEND=off renders and records without calling SES.
"""
from __future__ import annotations

import json
import os
import time

import boto3
from botocore.exceptions import ClientError

from menu.fn.render import render

REGION = os.environ.get("AWS_REGION", "ca-central-1")
KEEP_DAYS = 30
_cache: dict = {}


class BadMessage(ValueError):
    """The message can never be sent, however often it is retried."""


def address() -> str:
    """Read once per execution environment. Never logged and never stored in the table."""
    if "address" not in _cache:
        _cache["address"] = os.environ.get("MENU_EMAIL_TO") or boto3.client("ssm", region_name=REGION).get_parameter(
            Name=os.environ["MENU_ADDRESS_PARAM"])["Parameter"]["Value"].strip()
    return _cache["address"]


def table():
    return boto3.resource("dynamodb", region_name=REGION).Table(os.environ.get("MENU_SENT_TABLE", "l18-menu-sent"))


def read_menu(body: str) -> dict:
    try:
        menu = json.loads(body)
    except ValueError as e:
        raise BadMessage(f"not JSON: {e}") from None
    if not isinstance(menu, dict) or menu.get("version") != 1:
        raise BadMessage("not a version 1 menu message")
    request_id, picks = menu.get("request_id"), menu.get("picks")
    if not isinstance(request_id, str) or not request_id:
        raise BadMessage("no request_id")
    if not isinstance(picks, list) or not picks or not all(
            isinstance(p, dict) and isinstance(p.get("dish"), str) and isinstance(p.get("price_usd"), (int, float))
            and "name" in p and "code" in p for p in picks):
        raise BadMessage("picks must be a list of {dish, code, name, price_usd}")
    for key in ("total_usd", "budget_usd"):
        if not isinstance(menu.get(key), (int, float)):
            raise BadMessage(f"{key} must be a number")
    # the total in the email is the sum of the prices in the email
    if abs(sum(p["price_usd"] for p in picks) - menu["total_usd"]) > 0.005:
        raise BadMessage("total_usd is not the sum of the picks")
    menu["over_budget"] = menu["total_usd"] > menu["budget_usd"]
    menu.setdefault("shopper", "unknown")
    menu.setdefault("request", "")
    return menu


def claim(request_id: str) -> bool:
    """True for the first caller with this request_id, False for every later one."""
    now = time.time()
    try:
        table().put_item(Item={"request_id": request_id, "status": "sending", "claimed_at": str(round(now, 3)),
                               "expires_at": int(now) + KEEP_DAYS * 86400},
                         ConditionExpression="attribute_not_exists(request_id)")
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def record(request_id: str, status: str, ses_message_id: str | None = None) -> None:
    table().update_item(Key={"request_id": request_id},
                        UpdateExpression="SET #s = :s, done_at = :t, ses_message_id = :m",
                        ExpressionAttributeNames={"#s": "status"},
                        ExpressionAttributeValues={":s": status, ":t": str(round(time.time(), 3)),
                                                   ":m": ses_message_id or "none"})


def send(email: dict) -> str:
    to = address()
    out = boto3.client("sesv2", region_name=REGION).send_email(
        FromEmailAddress=to, Destination={"ToAddresses": [to]},
        Content={"Simple": {"Subject": {"Data": email["subject"], "Charset": "UTF-8"},
                            "Body": {"Html": {"Data": email["html"], "Charset": "UTF-8"},
                                     "Text": {"Data": email["text"], "Charset": "UTF-8"}}}})
    return out["MessageId"]


def process(body: str) -> str:
    menu = read_menu(body)
    email = render(menu)
    request_id = menu["request_id"]
    if not claim(request_id):
        return f"{request_id}: duplicate, nothing sent"
    if os.environ.get("MENU_SEND", "ses") == "off":
        record(request_id, "rendered, not sent")
        return f"{request_id}: rendered {len(email['html'])} bytes of HTML, sending is off"
    try:
        ses_message_id = send(email)
    except Exception:
        table().delete_item(Key={"request_id": request_id})      # give the claim back: the retry may send
        raise
    record(request_id, "sent", ses_message_id)
    return f"{request_id}: sent, SES message {ses_message_id}"


def handler(event, context=None):
    failures = []
    for message in event.get("Records", []):
        try:
            print(process(message["body"]))
        except Exception as e:      # one failed message must not fail the others in its batch
            print(f"failed {message['messageId']}: {type(e).__name__}: {str(e)[:200]}")
            failures.append({"itemIdentifier": message["messageId"]})
    return {"batchItemFailures": failures}
