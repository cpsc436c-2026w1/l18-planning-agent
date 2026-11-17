"""The AWS side of storeplan: create, check and remove what the AWS backend needs. Sandbox account, ca-central-1.

    AWS_PROFILE=instructor uv run python -m storeplan.aws setup      # S3, Glue, Athena workgroup, list fixtures
    AWS_PROFILE=instructor uv run python -m storeplan.aws cutoff     # one query over the limit: Athena cancels it
    AWS_PROFILE=instructor uv run python -m storeplan.aws teardown   # removes all of it, and only it
    AWS_PROFILE=instructor uv run python -m storeplan.aws teardown-desc   # only the catalog test drive's scratch
                                                                          # Glue database (storeplan/glue_desc.py)

What setup creates, all tagged project=436c-l18 where the resource takes tags:
    S3        s3://436c-athena-<account>/l18-storeplan/lake/...   the Parquet files of data/lake/ (objects tagged)
              s3://436c-athena-<account>/l18-storeplan/athena-results/   query results
    Glue      database l18_storeplan (tagged); tables prices_raw, prices_curated (Glue tables take no tags)
    Athena    workgroup l18-storeplan: its result location, settings enforced, Data scanned limit per query 10 MB
    DynamoDB  fixture rows in the existing table l18-grocery-lists (request_id storeplan-fixture-*) and one
              fixture product in l18-grocery-products (code 9990001001): conditional puts, never an overwrite
None of these bills by the hour: S3 storage of about 31 MB, and Athena per query (Data scanned).
The account number is read from the profile and masked in everything this writes.
"""
from __future__ import annotations

import json
import sys
import time
from decimal import Decimal

import boto3

from grocery import config
from storeplan import catalog as cat
from storeplan import fixtures as fx
from storeplan import lake as lk
from storeplan.backends import AWS_NAMES

TAGS = AWS_NAMES["tag"]
CUTOFF_BYTES = 10_000_000          # the smallest per-query limit Athena accepts (Athena docs: 10 MB minimum)
DDL = {
    "prices_raw": """CREATE EXTERNAL TABLE IF NOT EXISTS prices_raw (
  obs_id string, product_code string, store string, observed_on date, price double, currency string, source string)
STORED AS PARQUET LOCATION '{loc}/prices_raw/' TBLPROPERTIES ('parquet.compression'='SNAPPY')""",
    "prices_curated": """CREATE EXTERNAL TABLE IF NOT EXISTS prices_curated (
  product_code string, store string, observed_on date, price_cad double)
PARTITIONED BY (month string)
STORED AS PARQUET LOCATION '{loc}/prices_curated/' TBLPROPERTIES ('parquet.compression'='SNAPPY')""",
}


def session():
    return boto3.Session(region_name=config.REGION)


def names(s) -> dict:
    acct = s.client("sts").get_caller_identity()["Account"]
    bucket = f"436c-athena-{acct}"
    root = f"s3://{bucket}/{AWS_NAMES['prefix']}"
    return {"account": acct, "bucket": bucket, "lake": f"{root}/lake", "results": f"{root}/athena-results/"}


def mask(text: str, acct: str) -> str:
    return text.replace(acct, "<account>")


def athena_sql(athena, sql: str, database: str | None = None) -> dict:
    kw = {"QueryString": sql, "WorkGroup": AWS_NAMES["workgroup"]}
    if database:
        kw["QueryExecutionContext"] = {"Database": database}
    qid = athena.start_query_execution(**kw)["QueryExecutionId"]
    while True:
        q = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]
        if q["Status"]["State"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
            return q
        time.sleep(0.5)


def setup() -> None:
    s = session()
    n = names(s)
    s3, athena, glue = s.client("s3"), s.client("athena"), s.client("glue")
    if not (lk.LAKE_DIR / "prices_curated").exists():
        lk.build()
    tagging = "&".join(f"{k}={v}" for k, v in TAGS.items())
    files = sorted(lk.LAKE_DIR.rglob("*.parquet"))
    for f in files:
        key = f"{AWS_NAMES['prefix']}/lake/{f.relative_to(lk.LAKE_DIR)}"
        s3.upload_file(str(f), n["bucket"], key, ExtraArgs={"Tagging": tagging})
    print(f"s3: {len(files)} Parquet files under s3://436c-athena-<account>/{AWS_NAMES['prefix']}/lake/")

    wg = AWS_NAMES["workgroup"]
    conf = {"ResultConfiguration": {"OutputLocation": n["results"]}, "EnforceWorkGroupConfiguration": True,
            "PublishCloudWatchMetricsEnabled": False, "BytesScannedCutoffPerQuery": CUTOFF_BYTES}
    existing = [w["Name"] for w in athena.list_work_groups()["WorkGroups"]]
    if wg not in existing:
        athena.create_work_group(Name=wg, Configuration=conf, Description="CPSC 436C L18 storeplan demo",
                                 Tags=[{"Key": k, "Value": v} for k, v in TAGS.items()])
    print(f"athena: workgroup {wg}, Data scanned limit {CUTOFF_BYTES:,} bytes per query")

    db = AWS_NAMES["database"]
    try:
        glue.create_database(DatabaseInput={"Name": db, "Description": "CPSC 436C L18 storeplan demo: price history"},
                             Tags=TAGS)
    except glue.exceptions.AlreadyExistsException:
        pass
    for t, ddl in DDL.items():
        q = athena_sql(athena, ddl.format(loc=n["lake"]), db)
        print(f"glue: table {t}: {q['Status']['State']} {mask(q['Status'].get('StateChangeReason', ''), n['account'])}")
    q = athena_sql(athena, "MSCK REPAIR TABLE prices_curated", db)
    print(f"glue: partitions of prices_curated: {q['Status']['State']}")

    seed(s)
    aws_cat = cat.from_glue(glue, db)
    path = config.OUTPUT_DIR / "storeplan-aws-catalog.json"
    path.write_text(json.dumps(aws_cat, indent=1) + "\n")
    diff = cat.same_columns(aws_cat, cat.load())
    print(f"catalog from Glue: {path.name}; differences from the DuckDB catalog: {diff or 'none'}")
    from storeplan import glue_desc
    glue_desc.write_meaning_live(glue)
    print("glue: table and column meaning written into the data catalog")


def seed(s) -> None:
    from boto3.dynamodb.conditions import Attr

    ddb = s.resource("dynamodb")
    lists, products = ddb.Table(config.LISTS_TABLE), ddb.Table(config.PRODUCTS_TABLE)
    put = 0
    for shopper, rid, code in fx.fixture_rows():
        try:
            lists.put_item(Item={"shopper": shopper, "request_id": rid, "code": code,
                                 "added_at": Decimal(str(time.time()))},
                           ConditionExpression=Attr("request_id").not_exists())
            put += 1
        except ddb.meta.client.exceptions.ConditionalCheckFailedException:
            pass
    p = fx.INJECTED
    item = {k: (Decimal(str(v)) if isinstance(v, float) else v) for k, v in p.items()}
    # no search_text: the grocery search (a Scan filtered on search_text) never returns the fixture product, so
    # the lecture's own search cannot show its instruction; get_product and get_list still find it by code
    try:
        products.put_item(Item=item, ConditionExpression=Attr("code").not_exists())
    except ddb.meta.client.exceptions.ConditionalCheckFailedException:
        products.update_item(Key={"code": p["code"]}, UpdateExpression="REMOVE search_text")
    print(f"dynamodb: {put} new fixture rows in {config.LISTS_TABLE}; fixture product {p['code']}")


def cutoff() -> None:
    """One query that reads the two large columns of prices_raw (about 29 MB): over the 10 MB limit."""
    s = session()
    acct = names(s)["account"]
    sql = "SELECT count(DISTINCT obs_id), count(DISTINCT source) FROM prices_raw"
    t0 = time.monotonic()
    q = athena_sql(s.client("athena"), sql, AWS_NAMES["database"])
    out = {"sql": sql, "state": q["Status"]["State"],
           "reason": mask(q["Status"].get("StateChangeReason", ""), acct),
           "data_scanned_bytes": q.get("Statistics", {}).get("DataScannedInBytes"),
           "seconds": round(time.monotonic() - t0, 1), "limit_bytes": CUTOFF_BYTES}
    (config.OUTPUT_DIR / "storeplan-aws-cutoff.json").write_text(json.dumps(out, indent=1) + "\n")
    print(json.dumps(out, indent=1))


def teardown() -> None:
    from boto3.dynamodb.conditions import Key

    s = session()
    n = names(s)
    ddb = s.resource("dynamodb")
    lists = ddb.Table(config.LISTS_TABLE)
    gone = 0
    for shopper in fx.LISTS:
        items = lists.query(KeyConditionExpression=Key("shopper").eq(shopper)
                            & Key("request_id").begins_with(fx.FIXTURE_PREFIX))["Items"]
        for it in items:
            lists.delete_item(Key={"shopper": shopper, "request_id": it["request_id"]})
            gone += 1
    ddb.Table(config.PRODUCTS_TABLE).delete_item(Key={"code": fx.INJECTED["code"]})
    print(f"dynamodb: {gone} fixture rows and fixture product {fx.INJECTED['code']} deleted")
    glue = s.client("glue")
    for t in DDL:
        try:
            glue.delete_table(DatabaseName=AWS_NAMES["database"], Name=t)
        except glue.exceptions.EntityNotFoundException:
            pass
    try:
        glue.delete_database(Name=AWS_NAMES["database"])
    except glue.exceptions.EntityNotFoundException:
        pass
    print(f"glue: database {AWS_NAMES['database']} and its tables deleted")
    athena = s.client("athena")
    if AWS_NAMES["workgroup"] in [w["Name"] for w in athena.list_work_groups()["WorkGroups"]]:
        athena.delete_work_group(WorkGroup=AWS_NAMES["workgroup"], RecursiveDeleteOption=True)
    print(f"athena: workgroup {AWS_NAMES['workgroup']} deleted")
    b = s.resource("s3").Bucket(n["bucket"])
    deleted = b.objects.filter(Prefix=f"{AWS_NAMES['prefix']}/").delete()
    print(f"s3: {sum(len(d.get('Deleted', [])) for d in deleted)} objects under {AWS_NAMES['prefix']}/ deleted")


if __name__ == "__main__":
    def teardown_desc() -> None:
        from storeplan import glue_desc

        glue_desc.teardown_desc(session().client("glue"))

    {"setup": setup, "cutoff": cutoff, "teardown": teardown, "teardown-desc": teardown_desc}[sys.argv[1]]()
