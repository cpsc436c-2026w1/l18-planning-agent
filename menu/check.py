"""Two checks on the deployed pipeline, with a saved plan (no model call). Each sends one email.

    AWS_PROFILE=instructor uv run python -m menu.check duplicate   # one approved menu published twice: one email
    AWS_PROFILE=instructor uv run python -m menu.check bad         # a bad message and a good one: the good one is
                                                                   # sent at once, the bad one reaches the
                                                                   # dead-letter queue after 3 receives (about 3 min)
"""
from __future__ import annotations

import json
import sys
import time

import boto3

from grocery import config
from menu import names
from menu.publish import new_request_id, send_menu, topic_arn, wait_for_email


def saved_week() -> dict:
    return json.loads((config.OUTPUT_DIR / "orchestrator-gateway.json").read_text())["result"]


def count(sqs, url: str) -> int:
    a = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["ApproximateNumberOfMessages",
                                                               "ApproximateNumberOfMessagesNotVisible"])["Attributes"]
    return int(a["ApproximateNumberOfMessages"]) + int(a["ApproximateNumberOfMessagesNotVisible"])


def main(which: str) -> None:
    session = boto3.Session(region_name=names.REGION)
    sns, sqs, logs = session.client("sns"), session.client("sqs"), session.client("logs")
    dlq = sqs.get_queue_url(QueueName=names.DEAD_LETTER_QUEUE)["QueueUrl"]
    started = int(time.time() * 1000)

    if which == "duplicate":
        request_id = new_request_id()
        first = send_menu(saved_week(), "sam", request_id, lambda w: True)
        send_menu(saved_week(), "sam", request_id, lambda w: True)
        print(f"published twice with request_id {request_id}")
        print("record:", wait_for_email(request_id, first["approved_at"]))
        time.sleep(15)       # the function's log lines take a few seconds to reach CloudWatch Logs
        lines = logs.filter_log_events(logGroupName=f"/aws/lambda/{names.FUNCTION}", startTime=started,
                                       filterPattern=f'"{request_id}"')["events"]
        for line in lines:
            print("log:", line["message"].strip())
    else:
        before = count(sqs, dlq)
        t0 = time.time()
        sns.publish(TopicArn=topic_arn(session), Message="this is not a menu")
        good = send_menu(saved_week(), "sam", new_request_id(), lambda w: True)
        print(f"published a bad message, then a good one with request_id {good['request_id']}")
        print("good one:", wait_for_email(good["request_id"], good["approved_at"]))
        while count(sqs, dlq) == before and time.time() - t0 < 400:
            time.sleep(5)
        print(f"dead-letter queue: {before} -> {count(sqs, dlq)} messages, {time.time() - t0:.0f} s after the publish")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "duplicate")
