"""Two backends behind one interface. The router, planner, validator, compiler and merge do not know which one
they have; only these methods differ.

    catalog()               the catalog the planner reads
    dialect                 how the compiler names a table
    get_list(shopper)       key lookup in the operational store
    estimate(compiled)      Data scanned before the query runs, in bytes, or None when the engine has no dry run
    run(compiled)           rows, Data scanned, seconds

LocalBackend    lists in the DuckDB store of grocery/store.py; the lake as Parquet files read by a second DuckDB
                connection that holds only the lake, so a query cannot reach the lists.
AwsBackend      lists in DynamoDB (l18-grocery-lists, grocery/store.py DynamoStore); the lake in S3 as the same
                Parquet files, in Glue Data Catalog tables, queried with Athena in workgroup l18-storeplan, whose
                per-query Data scanned limit cancels a query that reads more.
"""
from __future__ import annotations

import time
from pathlib import Path

from grocery import config
from grocery.tools import GroceryTools
from storeplan import catalog as cat
from storeplan import fixtures as fx
from storeplan import plan as pl
from storeplan import lake as lk

SCAN_CAP_BYTES = 4_000_000      # offline cap on the estimated Data scanned of one query (ours)
ATHENA_USD_PER_TB = 5.50        # ca-central-1, AWS bulk price list published 2026-09-11 (receipt: price list)

AWS_NAMES = {"database": "l18_storeplan", "workgroup": "l18-storeplan", "prefix": "l18-storeplan",
             "tag": {"project": "436c-l18"}}


class LocalBackend:
    name = "local"
    dialect = pl.DUCKDB
    scan_cap = SCAN_CAP_BYTES

    def __init__(self, products_file: Path = config.DATA_FILE, lake: Path = lk.LAKE_DIR,
                 lists: dict | None = None, catalog: dict | None = None, extra: list[dict] | None = None):
        import duckdb

        from grocery.store import DuckDBStore

        self.lake = Path(lake)
        if not (self.lake / "prices_curated").exists():
            lk.build(products_file, self.lake, extra=extra)
        self.store = DuckDBStore(products_file)
        for p in (extra if extra is not None else [fx.INJECTED]):
            self.store.con.execute(
                "INSERT INTO products (code, name, brand, quantity, category, price_usd, price_eur, price_n, "
                "price_last, palm_oil, ingredients, search_text) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [p[k] for k in ("code", "name", "brand", "quantity", "category", "price_usd", "price_eur",
                                "price_n", "price_last", "palm_oil", "ingredients")]
                + [""])                          # no search text: the grocery search does not return it
        for shopper, codes in (lists if lists is not None else fx.LISTS).items():
            for i, code in enumerate(codes):
                self.store.put_list_item(shopper, f"{fx.FIXTURE_PREFIX}{i}", code)
        self.tools = GroceryTools(self.store)
        self.con = duckdb.connect()             # the lake only: no lists, no products
        for t in ("prices_raw", "prices_curated"):
            hive = "true" if t == "prices_curated" else "false"
            self.con.execute(f'CREATE VIEW "{t}" AS SELECT * FROM read_parquet('
                             f"'{self.lake}/{t}/**/*.parquet', hive_partitioning={hive})")
        self._catalog = catalog

    def catalog(self) -> dict:
        if self._catalog is None:
            self._catalog = cat.from_duckdb(self.lake)
        return self._catalog

    def get_list(self, shopper: str) -> list[dict]:
        return self.tools.get_list(shopper)["items"]

    def estimate(self, c: pl.Compiled) -> int:
        """Bytes of the column chunks the query reads, in the files of the partitions it reads: what a columnar
        engine reads for these columns. Row groups skipped by min/max statistics are not subtracted. Athena's
        DataScannedInBytes for the same queries came out about 4.3 KB higher per file read (why was not
        checked), so the cap here is set with room to spare."""
        import duckdb

        files = lk.files(c.table, self.lake, c.months)
        if not files:
            return 0
        names = ", ".join(f"'{f}'" for f in files)
        cols = ", ".join(f"'{x}'" for x in c.columns if x != "month") or "''"
        q = (f"SELECT coalesce(sum(total_compressed_size), 0) FROM parquet_metadata([{names}]) "
             f"WHERE path_in_schema IN ({cols})")
        return int(duckdb.connect().execute(q).fetchone()[0])

    def run(self, c: pl.Compiled) -> dict:
        t0 = time.monotonic()
        cur = self.con.execute(c.sql)
        names = [d[0] for d in cur.description]
        rows = [dict(zip(names, (_plain(v) for v in r))) for r in cur.fetchall()]
        return {"rows": rows, "state": "SUCCEEDED", "data_scanned_bytes": self.estimate(c),
                "data_scanned_is": "estimate", "seconds": round(time.monotonic() - t0, 3)}


class AwsBackend:
    """Needs AWS_PROFILE (the instructor profile) and the resources of `python -m storeplan.aws setup`."""
    name = "aws"
    scan_cap = None                 # Athena has no dry run: the workgroup's per-query limit is the cap

    def __init__(self, session=None):
        import boto3

        from grocery.store import DynamoStore

        self.session = session or boto3.Session(region_name=config.REGION)
        self.athena = self.session.client("athena", region_name=config.REGION)
        self.glue = self.session.client("glue", region_name=config.REGION)
        self.dialect = pl.athena(AWS_NAMES["database"])
        self.store = DynamoStore(self.session)
        self.tools = GroceryTools(self.store)
        self._catalog = None

    def catalog(self) -> dict:
        if self._catalog is None:
            self._catalog = cat.from_glue_with_meaning(self.glue, AWS_NAMES["database"])  # meaning from the data catalog
        return self._catalog

    def get_list(self, shopper: str) -> list[dict]:
        return self.tools.get_list(shopper)["items"]

    def estimate(self, c: pl.Compiled) -> None:
        return None

    def run(self, c: pl.Compiled) -> dict:
        t0 = time.monotonic()
        qid = self.athena.start_query_execution(
            QueryString=c.sql, WorkGroup=AWS_NAMES["workgroup"],
            QueryExecutionContext={"Database": AWS_NAMES["database"]})["QueryExecutionId"]
        while True:
            q = self.athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]
            state = q["Status"]["State"]
            if state in ("SUCCEEDED", "FAILED", "CANCELLED"):
                break
            time.sleep(0.5)
        scanned = int(q.get("Statistics", {}).get("DataScannedInBytes", 0))
        out = {"rows": [], "state": state, "data_scanned_bytes": scanned, "data_scanned_is": "DataScannedInBytes",
               "seconds": round(time.monotonic() - t0, 3), "query_execution_id": qid}
        if state != "SUCCEEDED":
            out["reason"] = q["Status"].get("StateChangeReason", "")[:300]
            return out
        out["rows"] = _athena_rows(self.athena, qid)
        return out


def _athena_rows(athena, qid: str) -> list[dict]:
    rows, names, types, token = [], None, None, None
    while True:
        kw = {"QueryExecutionId": qid, **({"NextToken": token} if token else {})}
        page = athena.get_query_results(**kw)
        if names is None:
            meta = page["ResultSet"]["ResultSetMetadata"]["ColumnInfo"]
            names, types = [m["Name"] for m in meta], [m["Type"] for m in meta]
            data = page["ResultSet"]["Rows"][1:]          # the first row of the first page is the header
        else:
            data = page["ResultSet"]["Rows"]
        for r in data:
            vals = [d.get("VarCharValue") for d in r["Data"]]
            rows.append({n: _typed(v, t) for n, v, t in zip(names, vals, types)})
        token = page.get("NextToken")
        if not token:
            return rows


def _typed(v, t: str):
    if v is None:
        return None
    if t in ("double", "float", "real", "decimal"):
        return float(v)
    if t in ("bigint", "integer", "int", "smallint", "tinyint"):
        return int(v)
    return v


def _plain(v):
    import datetime as dt

    return v.isoformat() if isinstance(v, dt.date) else v


def athena_usd(scanned_bytes: int) -> float:
    """Athena's charge for one query: Data scanned rounded up to the next MB, 10 MB at least (Athena pricing
    page; assumed, not checked here) at the ca-central-1 price."""
    mb = max(10, -(-scanned_bytes // 1_000_000))
    return mb * 1e6 / 1e12 * ATHENA_USD_PER_TB
