"""Where the products and the shoppers' lists live: DuckDB on the laptop, DynamoDB on AWS.

Both stores have the same four methods, so the tools in grocery/tools.py do not know which one they have:
    candidates(words, no_palm_oil, max_price_usd)   products whose text holds any of the words
    product(code)                                   one product, or None
    list_items(shopper)                             the rows of one shopper's list
    put_list_item(shopper, request_id, code)        one conditional write; returns (row, created)

The write is idempotent: the key of a list row is (shopper, request_id). A second write with the same request_id
is refused by the store itself (a primary key in DuckDB, a condition expression in DynamoDB), and the stored row
comes back in place of a second row (Agentic AI Lens AGENTREL06-BP04: deterministic idempotency keys and a
conditional write).
"""
from __future__ import annotations

import json
import time
from decimal import Decimal
from pathlib import Path

from grocery import config

SEARCH_FIELDS = ("name", "brand", "category")


def read_products(path: Path = config.DATA_FILE) -> list[dict]:
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    for r in rows:
        r["search_text"] = " ".join(str(r.get(f) or "") for f in SEARCH_FIELDS).lower()
    return rows


class DuckDBStore:
    """An in-memory DuckDB with the products file loaded and an empty lists table."""

    def __init__(self, path: Path = config.DATA_FILE):
        import duckdb          # imported here: the Lambda and the Runtime use DynamoStore and do not ship DuckDB

        self.duplicate_key = duckdb.ConstraintException
        self.con = duckdb.connect(":memory:")
        self.con.execute(f"CREATE TABLE products AS SELECT *, lower(concat_ws(' ', name, brand, category)) "
                         f"AS search_text FROM read_json_auto('{path}', columns={{'code': 'VARCHAR', "
                         "'name': 'VARCHAR', 'brand': 'VARCHAR', 'quantity': 'VARCHAR', 'category': 'VARCHAR', "
                         "'price_usd': 'DOUBLE', 'price_eur': 'DOUBLE', 'price_n': 'INTEGER', "
                         "'price_last': 'VARCHAR', 'palm_oil': 'VARCHAR', 'ingredients': 'VARCHAR'})")
        self.con.execute("CREATE TABLE lists (shopper VARCHAR, request_id VARCHAR, code VARCHAR, "
                         "added_at DOUBLE, PRIMARY KEY (shopper, request_id))")

    def _rows(self, sql: str, params: list) -> list[dict]:
        cur = self.con.execute(sql, params)
        names = [d[0] for d in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]

    def candidates(self, words: list[str], no_palm_oil: bool = False, max_price_usd: float | None = None):
        if not words:
            return []
        where = "(" + " OR ".join("search_text LIKE ?" for _ in words) + ")"
        params: list = [f"%{w}%" for w in words]
        if no_palm_oil:
            where += " AND palm_oil = 'no'"
        if max_price_usd is not None:
            where += " AND price_usd <= ?"
            params.append(max_price_usd)
        return self._rows(f"SELECT * FROM products WHERE {where}", params)

    def categories(self) -> list[str]:
        return [r["category"] for r in self._rows("SELECT DISTINCT category FROM products ORDER BY 1", [])]

    def product(self, code: str) -> dict | None:
        rows = self._rows("SELECT * FROM products WHERE code = ?", [code])
        return rows[0] if rows else None

    def list_items(self, shopper: str) -> list[dict]:
        return self._rows("SELECT * FROM lists WHERE shopper = ? ORDER BY added_at", [shopper])

    def put_list_item(self, shopper: str, request_id: str, code: str) -> tuple[dict, bool]:
        row = {"shopper": shopper, "request_id": request_id, "code": code, "added_at": time.time()}
        try:
            self.con.execute("INSERT INTO lists VALUES (?, ?, ?, ?)", list(row.values()))
            return row, True
        except self.duplicate_key:  # this request_id was written before: return what was stored
            stored = self._rows("SELECT * FROM lists WHERE shopper = ? AND request_id = ?", [shopper, request_id])
            return stored[0], False


class DynamoStore:
    """Two DynamoDB tables: products (key: code) and lists (key: shopper, request_id).

    The search is a Scan with a filter, as in the naive grocery app of L17: fine for a few hundred items in one
    1 MB page, and not a search index (L17, "Search").
    """

    def __init__(self, session=None, region: str = config.REGION):
        import boto3

        self.ddb = (session or boto3.Session(region_name=region)).resource("dynamodb", region_name=region)
        self.products = self.ddb.Table(config.PRODUCTS_TABLE)
        self.lists = self.ddb.Table(config.LISTS_TABLE)

    def candidates(self, words: list[str], no_palm_oil: bool = False, max_price_usd: float | None = None):
        from boto3.dynamodb.conditions import Attr

        if not words:
            return []
        cond = Attr("search_text").contains(words[0])
        for w in words[1:]:
            cond = cond | Attr("search_text").contains(w)
        if no_palm_oil:
            cond = cond & Attr("palm_oil").eq("no")
        if max_price_usd is not None:
            cond = cond & Attr("price_usd").lte(Decimal(str(max_price_usd)))
        rows, kwargs = [], {"FilterExpression": cond}
        while True:
            page = self.products.scan(**kwargs)
            rows += page["Items"]
            if "LastEvaluatedKey" not in page:
                return [_plain(r) for r in rows]
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def categories(self) -> list[str]:
        item = self.products.get_item(Key={"code": "#categories"}).get("Item") or {}
        return sorted(item.get("categories", []))

    def product(self, code: str) -> dict | None:
        item = self.products.get_item(Key={"code": code}).get("Item")
        return _plain(item) if item else None

    def list_items(self, shopper: str) -> list[dict]:
        from boto3.dynamodb.conditions import Key

        # a strongly consistent read: the shopper sees the row that was just written
        items = self.lists.query(KeyConditionExpression=Key("shopper").eq(shopper), ConsistentRead=True)["Items"]
        return sorted((_plain(i) for i in items), key=lambda r: r["added_at"])

    def put_list_item(self, shopper: str, request_id: str, code: str) -> tuple[dict, bool]:
        from botocore.exceptions import ClientError

        row = {"shopper": shopper, "request_id": request_id, "code": code, "added_at": Decimal(str(time.time()))}
        try:
            # the condition makes the write happen once: it fails when an item with this key already exists
            self.lists.put_item(Item=row, ConditionExpression="attribute_not_exists(request_id)")
            return _plain(row), True
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            stored = self.lists.get_item(Key={"shopper": shopper, "request_id": request_id},
                                         ConsistentRead=True)["Item"]
            return _plain(stored), False


def _plain(item: dict) -> dict:
    """DynamoDB numbers come back as Decimal; the tools return floats and ints."""
    return {k: (float(v) if isinstance(v, Decimal) else v) for k, v in item.items()}


def open_store(kind: str | None = None):
    import os

    kind = kind or os.environ.get("GROCERY_STORE", "duckdb")
    return DynamoStore() if kind == "dynamodb" else DuckDBStore()
