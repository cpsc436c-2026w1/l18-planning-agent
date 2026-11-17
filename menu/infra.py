"""The messaging and state of the weekly-menu email: topic, queue, dead-letter queue, subscription, table.
Safe to run again. The Lambda function, its role and its event source mapping are in agentcore/05-menu-email.sh.

    AWS_PROFILE=instructor uv run python -m menu.infra
"""
from __future__ import annotations

import json

import boto3

from menu import names


def ensure(session=None, visibility_timeout: int = names.VISIBILITY_TIMEOUT_S) -> dict:
    session = session or boto3.Session(region_name=names.REGION)
    sns, sqs, ddb = session.client("sns"), session.client("sqs"), session.client("dynamodb")

    topic_arn = sns.create_topic(Name=names.TOPIC)["TopicArn"]

    dlq_url = sqs.create_queue(QueueName=names.DEAD_LETTER_QUEUE,
                               Attributes={"MessageRetentionPeriod": str(14 * 86400)})["QueueUrl"]
    dlq_arn = sqs.get_queue_attributes(QueueUrl=dlq_url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    queue_arn = dlq_arn.removesuffix(names.DEAD_LETTER_QUEUE) + names.QUEUE
    attributes = {
        "VisibilityTimeout": str(visibility_timeout),
        # after MAX_RECEIVES receives without a delete, SQS moves the message to the dead-letter queue
        "RedrivePolicy": json.dumps({"deadLetterTargetArn": dlq_arn, "maxReceiveCount": names.MAX_RECEIVES}),
        # only this topic may send to the queue
        "Policy": json.dumps({"Version": "2012-10-17", "Statement": [{
            "Effect": "Allow", "Principal": {"Service": "sns.amazonaws.com"}, "Action": "sqs:SendMessage",
            "Resource": queue_arn, "Condition": {"ArnEquals": {"aws:SourceArn": topic_arn}}}]}),
    }
    queue_url = sqs.create_queue(QueueName=names.QUEUE)["QueueUrl"]
    sqs.set_queue_attributes(QueueUrl=queue_url, Attributes=attributes)

    # raw message delivery: the queue gets the published JSON itself, not SNS's envelope around it
    subscription_arn = sns.subscribe(TopicArn=topic_arn, Protocol="sqs", Endpoint=queue_arn,
                                     Attributes={"RawMessageDelivery": "true"},
                                     ReturnSubscriptionArn=True)["SubscriptionArn"]

    if names.SENT_TABLE not in ddb.list_tables()["TableNames"]:
        ddb.create_table(TableName=names.SENT_TABLE, BillingMode="PAY_PER_REQUEST",
                         AttributeDefinitions=[{"AttributeName": "request_id", "AttributeType": "S"}],
                         KeySchema=[{"AttributeName": "request_id", "KeyType": "HASH"}])
        ddb.get_waiter("table_exists").wait(TableName=names.SENT_TABLE)
        ddb.update_time_to_live(TableName=names.SENT_TABLE,
                                TimeToLiveSpecification={"Enabled": True, "AttributeName": "expires_at"})
    return {"topic_arn": topic_arn, "queue_url": queue_url, "queue_arn": queue_arn, "dlq_url": dlq_url,
            "dlq_arn": dlq_arn, "subscription_arn": subscription_arn, "table": names.SENT_TABLE}


if __name__ == "__main__":
    made = ensure()
    account = made["queue_arn"].split(":")[4]
    for name, value in made.items():
        print(f"{name}: {value.replace(account, '<account>')}")
