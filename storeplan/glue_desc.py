"""Test drive: the lake tables' meaning kept in the Glue Data Catalog instead of storeplan/descriptions.py.

    AWS_PROFILE=instructor uv run python -m storeplan.glue_desc check      # copy, write, run checks a-d, tear down
    AWS_PROFILE=instructor uv run python -m storeplan.aws teardown-desc    # deletes the scratch database only

The live database l18_storeplan is only read (get_table, get_partitions). Everything written goes to the scratch
database l18_storeplan_desc (tagged project=436c-l18), whose tables point at the same S3 locations as the live ones:
deleting them is a Glue-only call and never touches S3.
"""
from __future__ import annotations

import copy
import difflib
import json
import sys

from storeplan import catalog as cat
from storeplan.backends import AWS_NAMES
from storeplan.descriptions import TABLES

LIVE = AWS_NAMES["database"]
SCRATCH = "l18_storeplan_desc"
LAKE = ("prices_raw", "prices_curated")
TABLE_INPUT = ("Name", "Description", "Owner", "LastAccessTime", "LastAnalyzedTime", "Retention",
               "StorageDescriptor", "PartitionKeys", "ViewOriginalText", "ViewExpandedText", "TableType",
               "Parameters", "TargetTable", "ViewDefinition", "FederatedTable")     # API_TableInput.html
PARTITION_INPUT = ("Values", "LastAccessTime", "StorageDescriptor", "Parameters", "LastAnalyzedTime")
MAX_DESCRIPTION, MAX_COMMENT, MAX_PARAM = 2048, 255, 512000


def _scratch_only(database: str) -> None:
    if database != SCRATCH:
        raise RuntimeError(f"refusing to write to {database}: only {SCRATCH} is written")


def table_input(t: dict) -> dict:
    """The TableInput part of a get_table result: an allow-list of the fields TableInput accepts."""
    return copy.deepcopy({k: t[k] for k in TABLE_INPUT if k in t})


def with_meaning(ti: dict, name: str) -> dict:
    """TableInput with descriptions.py's words for table `name` written into Description, Parameters, Comment."""
    d = TABLES[name]
    ti = copy.deepcopy(ti)
    ti["Description"] = d["holds"]
    params = dict(ti.get("Parameters", {}))
    params.update({k: d[k] for k in cat.MEANING_PARAMS if d.get(k)})
    if name in cat.PARTITIONS:
        params["partitions"] = json.dumps(cat.PARTITIONS[name])
    ti["Parameters"] = params
    for c in ti["StorageDescriptor"]["Columns"] + ti.get("PartitionKeys", []):
        c["Comment"] = d["columns"][c["Name"]]          # KeyError: a column with no words is a failure, not ""
    check_limits(ti)
    return ti


def check_limits(ti: dict) -> None:
    assert len(ti.get("Description", "")) <= MAX_DESCRIPTION, ("Description", len(ti["Description"]))
    for k, v in ti.get("Parameters", {}).items():
        assert len(k) <= 255 and len(v) <= MAX_PARAM, ("Parameters", k, len(v))
    for c in ti["StorageDescriptor"]["Columns"] + ti.get("PartitionKeys", []):
        assert len(c.get("Comment", "")) <= MAX_COMMENT, ("Comment", c["Name"], len(c["Comment"]))
        for k, v in c.get("Parameters", {}).items():
            assert len(k) <= 255 and len(v) <= MAX_PARAM, ("Column.Parameters", c["Name"], k)


def copy_tables(glue, tags: dict) -> dict:
    """Create the scratch database and copy the live tables (and prices_curated's partitions) into it."""
    try:
        glue.create_database(DatabaseInput={"Name": SCRATCH, "Description": "CPSC 436C L18 storeplan: catalog "
                                            "test drive (scratch, safe to delete)"}, Tags=tags)
    except glue.exceptions.AlreadyExistsException:
        pass
    counts = {}
    for name in LAKE:
        t = glue.get_table(DatabaseName=LIVE, Name=name)["Table"]
        _scratch_only(SCRATCH)
        glue.create_table(DatabaseName=SCRATCH, TableInput=table_input(t))
        parts = [p for page in glue.get_paginator("get_partitions").paginate(DatabaseName=LIVE, TableName=name)
                 for p in page["Partitions"]]
        if parts:
            glue.batch_create_partition(DatabaseName=SCRATCH, TableName=name, PartitionInputList=[
                {k: p[k] for k in PARTITION_INPUT if k in p} for p in parts])
        counts[name] = len(parts)
    return counts


def write_meaning(glue, database: str = SCRATCH) -> None:
    _scratch_only(database)
    for name in LAKE:
        t = glue.get_table(DatabaseName=database, Name=name)["Table"]
        glue.update_table(DatabaseName=database, TableInput=with_meaning(table_input(t), name))


def write_meaning_live(glue) -> None:
    """Write descriptions.py's words into the LIVE lake tables (l18_storeplan). Back up get_table first; the
    physical definition (storage, partitions) is copied as it is, only Description, Parameters and Comment change."""
    for name in LAKE:
        t = glue.get_table(DatabaseName=LIVE, Name=name)["Table"]
        glue.update_table(DatabaseName=LIVE, TableInput=with_meaning(table_input(t), name))

def teardown_desc(glue) -> None:
    """Glue-only: the scratch tables share the live S3 locations, so nothing under S3 is deleted."""
    for name in LAKE:
        try:
            glue.delete_table(DatabaseName=SCRATCH, Name=name)
        except glue.exceptions.EntityNotFoundException:
            pass
    try:
        glue.delete_database(Name=SCRATCH)
    except glue.exceptions.EntityNotFoundException:
        pass
    print(f"glue: scratch database {SCRATCH} deleted (S3 untouched)")


def _diff(a: str, b: str, la: str, lb: str) -> str:
    pa = json.dumps(json.loads(a), indent=1).splitlines()
    pb = json.dumps(json.loads(b), indent=1).splitlines()
    return "\n".join(difflib.unified_diff(pa, pb, la, lb, lineterm=""))


def _physical(t: dict) -> dict:
    sd = t["StorageDescriptor"]
    return {"Location": sd["Location"], "SerdeInfo": sd["SerdeInfo"], "InputFormat": sd.get("InputFormat"),
            "OutputFormat": sd.get("OutputFormat"),
            "Columns": [(c["Name"], c["Type"]) for c in sd["Columns"]],
            "PartitionKeys": [(c["Name"], c["Type"]) for c in t.get("PartitionKeys", [])],
            "TableType": t.get("TableType")}


def _live_fingerprint(glue) -> dict:
    out = {}
    for t in glue.get_tables(DatabaseName=LIVE)["TableList"]:
        out[t["Name"]] = {"Description": t.get("Description"), "UpdateTime": str(t["UpdateTime"]),
                          "VersionId": t.get("VersionId"), "Parameters": t.get("Parameters"),
                          "StorageDescriptor": t["StorageDescriptor"], "PartitionKeys": t.get("PartitionKeys")}
    return out


def check() -> int:
    from storeplan import aws

    s = aws.session()
    glue, athena = s.client("glue"), s.client("athena")
    acct = aws.names(s)["account"]
    results = {}
    before = _live_fingerprint(glue)
    print("live before:", json.dumps({k: {"Description": v["Description"], "UpdateTime": v["UpdateTime"],
                                          "VersionId": v["VersionId"]} for k, v in before.items()}))
    try:
        counts = copy_tables(glue, aws.TAGS)
        print(f"copied into {SCRATCH}: partitions {counts}")
        copied = {n: glue.get_table(DatabaseName=SCRATCH, Name=n)["Table"] for n in LAKE}
        write_meaning(glue)
        written = {n: glue.get_table(DatabaseName=SCRATCH, Name=n)["Table"] for n in LAKE}

        # (a) the planner's view: current reader on live == meaning reader on scratch, byte for byte; full dict too
        live_cat = cat.from_glue(glue, LIVE)
        new_cat = cat.from_glue_with_meaning(glue, SCRATCH)
        a, b = cat.for_prompt(live_cat), cat.for_prompt(new_cat)
        full_same = {k: v for k, v in live_cat.items() if k != "built_from"} == \
                    {k: v for k, v in new_cat.items() if k != "built_from"}
        results["a_prompt_bytes"] = a == b
        results["a_full_catalog"] = full_same
        print(f"(a) for_prompt equal: {a == b} ({len(a.encode())} vs {len(b.encode())} bytes); "
              f"full catalog equal (plan.py reads partitions): {full_same}")
        if a != b:
            print(_diff(a, b, "from_glue(l18_storeplan)", "from_glue_with_meaning(l18_storeplan_desc)"))

        # (b) idempotence: write again; TableInput projection and the prompt stay the same
        write_meaning(glue)
        again = {n: glue.get_table(DatabaseName=SCRATCH, Name=n)["Table"] for n in LAKE}
        same_ti = all(table_input(written[n]) == table_input(again[n]) for n in LAKE)
        same_prompt = cat.for_prompt(cat.from_glue_with_meaning(glue, SCRATCH)) == b
        results["b_idempotent"] = same_ti and same_prompt
        print(f"(b) write->read->write: TableInput identical {same_ti}; prompt identical {same_prompt}; "
              f"VersionId {[written[n].get('VersionId') for n in LAKE]} -> {[again[n].get('VersionId') for n in LAKE]}")

        # (c) UpdateTable with a changed description keeps the physical definition and the partitions
        name = "prices_curated"
        t0 = glue.get_table(DatabaseName=SCRATCH, Name=name)["Table"]
        n0 = len(glue.get_partitions(DatabaseName=SCRATCH, TableName=name)["Partitions"])
        ti = table_input(t0)
        ti["Description"] = t0["Description"] + " (changed)"
        glue.update_table(DatabaseName=SCRATCH, TableInput=ti)
        t1 = glue.get_table(DatabaseName=SCRATCH, Name=name)["Table"]
        n1 = len(glue.get_partitions(DatabaseName=SCRATCH, TableName=name)["Partitions"])
        keeps = _physical(t0) == _physical(t1) and t0["StorageDescriptor"] == t1["StorageDescriptor"] \
            and t0.get("PartitionKeys") == t1.get("PartitionKeys") and n0 == n1
        phys_vs_copy = all(_physical(copied[n]) == _physical(written[n]) for n in LAKE)
        results["c_update_keeps_storage"] = keeps and phys_vs_copy
        print(f"(c) UpdateTable(changed Description): StorageDescriptor and PartitionKeys identical {keeps}; "
              f"partitions {n0} -> {n1}; meaning write kept physical definition of both tables {phys_vs_copy}")
        # replace semantics, measured: an UpdateTable that leaves Description out drops it
        ti2 = table_input(t1)
        ti2.pop("Description")
        glue.update_table(DatabaseName=SCRATCH, TableInput=ti2)
        dropped = "Description" not in glue.get_table(DatabaseName=SCRATCH, Name=name)["Table"]
        results["c_replace_semantics_measured"] = dropped
        print(f"(c') UpdateTable without Description: Description gone {dropped} (replace semantics)")
        write_meaning(glue)

        # (d) Athena still queries the scratch tables, under the workgroup's Data scanned limit
        scratch_counts = {}
        for n in LAKE:
            q = aws.athena_sql(athena, f"SELECT count(*) FROM {n}", SCRATCH)
            st = q["Status"]["State"]
            scanned = q.get("Statistics", {}).get("DataScannedInBytes")
            rows = None
            if st == "SUCCEEDED":
                r = athena.get_query_results(QueryExecutionId=q["QueryExecutionId"])
                rows = r["ResultSet"]["Rows"][1]["Data"][0]["VarCharValue"]
            ok = st == "SUCCEEDED" and scanned is not None and scanned < aws.CUTOFF_BYTES
            results[f"d_athena_{n}"] = ok
            scratch_counts[n] = rows
            print(f"(d) {n}: {st} count={rows} DataScannedInBytes={scanned} (limit {aws.CUTOFF_BYTES:,}) "
                  f"{aws.mask(q['Status'].get('StateChangeReason', ''), acct)}")
        live_counts = {}
        for n in LAKE:
            q = aws.athena_sql(athena, f"SELECT count(*) FROM {n}", LIVE)
            live_counts[n] = athena.get_query_results(QueryExecutionId=q["QueryExecutionId"])[
                "ResultSet"]["Rows"][1]["Data"][0]["VarCharValue"] if q["Status"]["State"] == "SUCCEEDED" else None
        results["d_counts_match_live"] = live_counts == scratch_counts
        print(f"(d) counts on live l18_storeplan (read-only query): {live_counts}; match {live_counts == scratch_counts}")
        col_sql = "SELECT max(price_cad), count(*) FROM prices_curated WHERE month = '2026-10'"
        col = {}
        for db in (SCRATCH, LIVE):
            q = aws.athena_sql(athena, col_sql, db)
            val = athena.get_query_results(QueryExecutionId=q["QueryExecutionId"])["ResultSet"]["Rows"][1]["Data"] \
                if q["Status"]["State"] == "SUCCEEDED" else None
            col[db] = ([v.get("VarCharValue") for v in val] if val else None,
                       q.get("Statistics", {}).get("DataScannedInBytes"), q["Status"]["State"])
            print(f"(d') {db}: {col_sql} -> {col[db]}")
        results["d_column_read_matches_live"] = col[SCRATCH][0] is not None and col[SCRATCH][0] == col[LIVE][0]
    finally:
        teardown_desc(glue)
        after = _live_fingerprint(glue)
        results["live_unchanged"] = after == before
        if "live_counts" in locals():
            post = {}
            for n in LAKE:
                q = aws.athena_sql(athena, f"SELECT count(*) FROM {n}", LIVE)
                post[n] = athena.get_query_results(QueryExecutionId=q["QueryExecutionId"])["ResultSet"]["Rows"][1][
                    "Data"][0]["VarCharValue"] if q["Status"]["State"] == "SUCCEEDED" else None
            results["live_data_after_teardown"] = post == live_counts
            print(f"live counts after teardown: {post}; same as before teardown {post == live_counts}")
        print(f"live l18_storeplan unchanged (Description, UpdateTime, VersionId, Parameters, StorageDescriptor, "
              f"PartitionKeys): {after == before}")
        try:
            glue.get_database(Name=SCRATCH)
            results["scratch_gone"] = False
        except glue.exceptions.EntityNotFoundException:
            results["scratch_gone"] = True
        print("results:", json.dumps(results))
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit({"check": check}[sys.argv[1]]())
