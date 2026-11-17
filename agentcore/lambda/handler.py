"""The tools Lambda behind the AgentCore Gateway: the four grocery tools over DynamoDB, next to the pilot's two
lake tools (list_tables, run_query), which stay as they were.

The Gateway speaks MCP to the agent (tools/list, tools/call). This function does not: it receives the tool's
arguments as `event` and the tool's name, prefixed with the target's name, in the client context
(AgentCore docs, "Lambda function input format").
"""
import os
import time

import boto3

from grocery.store import DynamoStore
from grocery.tools import GroceryTools

REGION = os.environ.get("AWS_REGION", "ca-central-1")
GROCERY_TOOLS = ("search_products", "get_product", "get_list", "add_to_list")
_grocery = None


def grocery() -> GroceryTools:
    global _grocery
    if _grocery is None:        # once per execution environment, reused by warm invocations
        _grocery = GroceryTools(DynamoStore(region=REGION))
    return _grocery


# ---- the pilot's lake tools, unchanged ----------------------------------------------------------------------

ATHENA_OUTPUT = os.environ.get("ATHENA_OUTPUT", "")
WORKGROUP = os.environ.get("ATHENA_WORKGROUP", "primary")
MAX_ROWS = 50


def list_tables(database: str | None = None) -> dict:
    glue = boto3.client("glue", region_name=REGION)
    if not database:
        return {"databases": [d["Name"] for d in glue.get_databases()["DatabaseList"]]}
    out = []
    for t in glue.get_tables(DatabaseName=database)["TableList"]:
        cols = [c["Name"] + ":" + c.get("Type", "?") for c in t.get("StorageDescriptor", {}).get("Columns", [])]
        out.append({"table": t["Name"], "columns": cols, "partitions": [c["Name"] for c in t.get("PartitionKeys", [])]})
    return {"database": database, "tables": out}


def run_query(sql: str, database: str = "l10_rides") -> dict:
    athena = boto3.client("athena", region_name=REGION)
    if not sql.strip().lower().startswith(("select", "with", "show", "describe")):
        return {"error": "read-only: query must start with SELECT, WITH, SHOW or DESCRIBE"}
    qid = athena.start_query_execution(QueryString=sql, QueryExecutionContext={"Database": database},
                                       ResultConfiguration={"OutputLocation": ATHENA_OUTPUT},
                                       WorkGroup=WORKGROUP)["QueryExecutionId"]
    for _ in range(50):
        st = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]
        if st["State"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        time.sleep(0.5)
    if st["State"] != "SUCCEEDED":
        return {"error": st.get("StateChangeReason", st["State"]), "query_id": qid}
    res = athena.get_query_results(QueryExecutionId=qid, MaxResults=MAX_ROWS + 1)
    rows = [[c.get("VarCharValue") for c in r["Data"]] for r in res["ResultSet"]["Rows"]]
    stats = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"].get("Statistics", {})
    return {"columns": rows[0], "rows": rows[1:], "scanned_bytes": stats.get("DataScannedInBytes"), "query_id": qid}


LAKE_TOOLS = {"list_tables": list_tables, "run_query": run_query}


def lambda_handler(event, context):
    full = context.client_context.custom.get("bedrockAgentCoreToolName", "") if context.client_context else ""
    name = full.split("___", 1)[1] if "___" in full else full
    try:
        if name in GROCERY_TOOLS:
            return grocery().call(name, dict(event or {}))
        if name in LAKE_TOOLS:
            return LAKE_TOOLS[name](**(event or {}))
        return {"error": f"unknown tool {full!r}"}
    except Exception as e:      # the reason goes back to the agent as the tool result
        return {"error": f"{type(e).__name__}: {e}"}
