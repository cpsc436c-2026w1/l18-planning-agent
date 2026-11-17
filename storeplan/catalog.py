"""The catalog the planner reads: one entry per table, with its store type, columns, what it holds, its access
pattern and its partition columns.

The words come from storeplan/descriptions.py (written once). The columns and types come from the store:
    offline   DuckDB reads the Parquet files of the lake (DESCRIBE)          -> storeplan/catalog.json
    AWS       the Glue Data Catalog table definitions (glue:GetTable)        -> output/storeplan-aws-catalog.json
Both are built when the data is built, never at question time.

    uv run python -m storeplan.catalog           # rewrites storeplan/catalog.json from data/lake/
"""
from __future__ import annotations

import json
from pathlib import Path

from storeplan import fixtures as fx
from storeplan.descriptions import TABLES

CATALOG_FILE = Path(__file__).resolve().parent / "catalog.json"
PARTITIONS = {"prices_curated": [{"column": "month", "from": "observed_on", "format": "YYYY-MM"}]}
TYPES = {"VARCHAR": "string", "DATE": "date", "DOUBLE": "double", "BIGINT": "bigint", "INTEGER": "int"}


def _entry(name: str, columns: list[tuple[str, str]]) -> dict:
    d = TABLES[name]
    entry = {"name": name, "store_type": d["store_type"], "holds": d["holds"], "use_for": d["use_for"],
             "access": d["access"],
             "columns": [{"name": c, "type": t, "about": d["columns"].get(c, "")} for c, t in columns],
             "partition_columns": [p["column"] for p in PARTITIONS.get(name, [])]}
    if d.get("key"):
        entry["key"] = d["key"]
    if name in PARTITIONS:
        entry["partitions"] = PARTITIONS[name]
    return entry


def _operational() -> dict:
    return _entry("shopping_lists", [("shopper", "string"), ("code", "string"), ("name", "string"),
                                     ("quantity", "int")])


def _wrap(tables: list[dict], built_from: str) -> dict:
    return {"built_from": built_from, "as_of": fx.AS_OF, "first_day": fx.FIRST_DAY,
            "tables": {t["name"]: t for t in tables}}


def from_duckdb(lake: Path) -> dict:
    import duckdb

    con = duckdb.connect()
    tables = [_operational()]
    for name in ("prices_raw", "prices_curated"):
        hive = name in PARTITIONS
        rows = con.execute(f"DESCRIBE SELECT * FROM read_parquet('{lake}/{name}/**/*.parquet', "
                           f"hive_partitioning={str(hive).lower()})").fetchall()
        tables.append(_entry(name, [(r[0], TYPES.get(r[1], r[1].lower())) for r in rows]))
    return _wrap(tables, "duckdb")


def from_glue(glue, database: str) -> dict:
    tables = [_operational()]
    for name in ("prices_raw", "prices_curated"):
        t = glue.get_table(DatabaseName=database, Name=name)["Table"]
        cols = [(c["Name"], c["Type"]) for c in t["StorageDescriptor"]["Columns"]]
        cols += [(c["Name"], c["Type"]) for c in t.get("PartitionKeys", [])]
        tables.append(_entry(name, cols))
    return _wrap(tables, "glue")


MEANING_PARAMS = ("store_type", "access", "use_for", "key")     # table Parameters that carry meaning
# how a partition value is derived (column, from, format) lives in the table Parameter "partitions" as JSON:
# Glue rejects Column.Parameters on partition keys (UpdateTable: "Parameters not supported for partition columns")


def _entry_from_glue(t: dict) -> dict:
    """One lake table, meaning and all, from a Glue table definition (glue:GetTable). No descriptions.py."""
    p = t.get("Parameters", {})
    cols = t["StorageDescriptor"]["Columns"] + t.get("PartitionKeys", [])
    entry = {"name": t["Name"], "store_type": p["store_type"], "holds": t["Description"], "use_for": p["use_for"],
             "access": p["access"],
             "columns": [{"name": c["Name"], "type": c["Type"], "about": c.get("Comment", "")} for c in cols],
             "partition_columns": [c["Name"] for c in t.get("PartitionKeys", [])]}
    if p.get("key"):
        entry["key"] = p["key"]
    if p.get("partitions"):
        entry["partitions"] = json.loads(p["partitions"])
    return entry


def from_glue_with_meaning(glue, database: str) -> dict:
    """Like from_glue, but the lake tables' meaning comes from the Glue Data Catalog itself: table Description
    (holds), table Parameters (store_type, access, use_for, key), column Comment (about), and the table
    Parameter partitions (JSON: column, from, format). A lake table without them fails here (KeyError), never falls back."""
    # shopping_lists still comes from descriptions.py: it is a DynamoDB table, not in Glue, and DynamoDB tables
    # have no description field to hold its meaning
    tables = [_operational()]
    for name in ("prices_raw", "prices_curated"):
        tables.append(_entry_from_glue(glue.get_table(DatabaseName=database, Name=name)["Table"]))
    return _wrap(tables, "glue")


def load(path: Path = CATALOG_FILE) -> dict:
    return json.loads(Path(path).read_text())


def same_columns(a: dict, b: dict) -> list[str]:
    """Differences in tables, columns and types between two catalogs (empty: the planner sees the same thing)."""
    out = []
    for name in sorted(set(a["tables"]) | set(b["tables"])):
        ca = [(c["name"], c["type"]) for c in a["tables"].get(name, {}).get("columns", [])]
        cb = [(c["name"], c["type"]) for c in b["tables"].get(name, {}).get("columns", [])]
        if sorted(ca) != sorted(cb):
            out.append(f"{name}: {ca} != {cb}")
    return out


def for_prompt(catalog: dict) -> str:
    """The catalog as the planner sees it: compact JSON, the build details left out."""
    keep = ("name", "store_type", "holds", "use_for", "access", "key", "columns", "partition_columns")
    tables = [{k: t[k] for k in keep if k in t} for t in catalog["tables"].values()]
    return json.dumps({"data_from": catalog["first_day"], "data_to": catalog["as_of"], "tables": tables})


if __name__ == "__main__":
    from storeplan.lake import LAKE_DIR

    CATALOG_FILE.write_text(json.dumps(from_duckdb(LAKE_DIR), indent=1) + "\n")
    print(f"wrote {CATALOG_FILE.name}")
