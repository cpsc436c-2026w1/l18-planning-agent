"""Create the two DynamoDB tables and load the products file. Safe to run again: it overwrites the same items.

    AWS_PROFILE=<your sandbox profile> uv run python -m grocery.load_dynamodb

Tables (on-demand capacity: no charge for throughput while nobody calls them; storage of about 0.3 MB is charged):
    $GROCERY_PREFIX-products   key: code
    $GROCERY_PREFIX-lists      key: shopper, request_id   (request_id is the idempotency key of a write)
"""
from __future__ import annotations

import sys
from decimal import Decimal

import boto3

from grocery import config
from grocery.store import read_products


def ensure_table(ddb, name: str, keys: list[tuple[str, str]]) -> None:
    client = ddb.meta.client
    try:
        client.describe_table(TableName=name)
        return
    except client.exceptions.ResourceNotFoundException:
        pass
    ddb.create_table(TableName=name, BillingMode="PAY_PER_REQUEST",
                     KeySchema=[{"AttributeName": k, "KeyType": t} for k, t in keys],
                     AttributeDefinitions=[{"AttributeName": k, "AttributeType": "S"} for k, _ in keys],
                     Tags=[{"Key": "demo", "Value": "l18-planning-agent"}])
    client.get_waiter("table_exists").wait(TableName=name)


def load(session=None, path=config.DATA_FILE) -> int:
    ddb = (session or boto3.Session(region_name=config.REGION)).resource("dynamodb", region_name=config.REGION)
    ensure_table(ddb, config.PRODUCTS_TABLE, [("code", "HASH")])
    ensure_table(ddb, config.LISTS_TABLE, [("shopper", "HASH"), ("request_id", "RANGE")])
    rows = read_products(path)
    with ddb.Table(config.PRODUCTS_TABLE).batch_writer() as batch:
        for r in rows:
            batch.put_item(Item={k: (Decimal(str(v)) if isinstance(v, float) else v) for k, v in r.items()
                                 if v is not None})
        # one item that is not a product: the list of categories, for the planner's prompt
        batch.put_item(Item={"code": "#categories", "categories": sorted({r["category"] for r in rows})})
    return len(rows)


if __name__ == "__main__":
    n = load()
    print(f"loaded {n} products into {config.PRODUCTS_TABLE}; lists table {config.LISTS_TABLE} is ready")
    sys.exit(0)
