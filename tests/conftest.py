"""Offline tests: a 12-product catalogue made up for the tests (tests/products.jsonl), the stub model, and moto
(a local stand-in for AWS) for DynamoDB. No network, no key, no account."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

FIXTURE = Path(__file__).resolve().parent / "products.jsonl"
OFFLINE = {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing",
           "AWS_DEFAULT_REGION": "ca-central-1", "AWS_REGION": "ca-central-1", "AWS_EC2_METADATA_DISABLED": "true"}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    for k, v in OFFLINE.items():
        monkeypatch.setenv(k, v)
    for k in ("AWS_PROFILE", "GATEWAY_URL", "COURSE_MCP_TOKEN"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture(params=["duckdb", "dynamodb"])
def store(request):
    """The same tests run on both stores."""
    from grocery.store import DuckDBStore, DynamoStore

    if request.param == "duckdb":
        yield DuckDBStore(FIXTURE)
        return
    from moto import mock_aws

    from grocery.load_dynamodb import load

    with mock_aws():
        load(path=FIXTURE)
        yield DynamoStore()


@pytest.fixture
def tools(store):
    from grocery.tools import GroceryTools

    return GroceryTools(store)


@pytest.fixture
def duck_tools():
    from grocery.store import DuckDBStore
    from grocery.tools import GroceryTools

    return GroceryTools(DuckDBStore(FIXTURE))
