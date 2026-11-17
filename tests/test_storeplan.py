"""storeplan offline: the validator, the compiler, the merge check, the three mechanisms on the stub. No network."""
from __future__ import annotations

import copy
import json

import pytest

from grocery import config
from grocery.model import ModelReply
from grocery.trace import Trace, TracedModel
from storeplan import cases as cs
from storeplan import eval as ev
from storeplan import fixtures as fx
from storeplan import mechanisms as mech
from storeplan import plan as pl


@pytest.fixture(scope="session")
def lake(tmp_path_factory):
    from storeplan.lake import build

    path = tmp_path_factory.mktemp("lake")
    build(config.DATA_FILE, path)
    return path


@pytest.fixture
def backend(lake):
    from storeplan.backends import LocalBackend

    return LocalBackend(lake=lake)


GOOD = cs.plan_cheaper("2026-09-15", "2026-10-31")


def test_the_hand_written_plan_is_valid(backend):
    assert pl.validate(GOOD, backend.catalog()) == []


def test_a_write_is_rejected(backend):
    for step in ({**cs.LIST_STEP, "lookup": "put"}, {**cs.LIST_STEP, "action": "delete"}):
        bad = copy.deepcopy(GOOD)
        bad["steps"][0] = step
        reasons = pl.validate(bad, backend.catalog())
        assert any("writes are not allowed" in r for r in reasons), reasons


def test_an_unknown_table_is_rejected(backend):
    for table in ("orders", "lists"):
        bad = copy.deepcopy(GOOD)
        bad["steps"][1]["table"] = table
        assert any("not in the catalog" in r for r in pl.validate(bad, backend.catalog()))
    bad = copy.deepcopy(GOOD)
    bad["steps"][1]["table"] = "shopping_lists"            # an operational table read as if it were the lake
    assert any("operational store" in r for r in pl.validate(bad, backend.catalog()))


def test_an_unknown_column_is_rejected(backend):
    bad = copy.deepcopy(GOOD)
    bad["steps"][1]["aggregates"][0]["column"] = "password"
    assert any("'password' is not in prices_curated" in r for r in pl.validate(bad, backend.catalog()))


def test_a_shopper_taken_from_the_model_is_rejected(backend):
    bad = copy.deepcopy(GOOD)
    bad["steps"][0]["shopper"] = "alex"
    reasons = pl.validate(bad, backend.catalog())
    assert any("set by app code" in r for r in reasons)
    out = mech.run_plan(bad, "sam", backend)
    assert out["status"] == "rejected" and "answer" not in out


def test_the_list_read_is_the_requests_shopper(backend):
    out = mech.run_plan(GOOD, "alex", backend)
    sql = " ".join(out["sql"].values())
    assert all(c in sql for c in fx.LISTS["alex"]) and not any(c in sql for c in fx.LISTS["sam"])


def test_a_scan_over_the_cap_is_rejected_before_it_runs(backend, monkeypatch):
    bad = cs.plan_month("2026-09-01", "2026-09-30", "count_distinct", table="prices_raw")
    bad["steps"][1]["aggregates"][0]["column"] = "obs_id"
    assert pl.validate(bad, backend.catalog()) == []        # the plan is well formed ...
    ran = []
    monkeypatch.setattr(backend, "run", lambda c: ran.append(c))
    out = mech.run_plan(bad, "sam", backend)
    assert out["status"] == "rejected" and "over the cap" in out["reasons"][0]   # ... and too big to read
    assert ran == []


def test_the_compiler_emits_one_read_only_select_on_catalog_tables(backend):
    import duckdb

    for s in GOOD["steps"][1:]:
        c = pl.compile_step(s, backend.catalog(), pl.DUCKDB, ["1", "2"])
        parsed = json.loads(duckdb.connect().execute("SELECT json_serialize_sql(?)", [c.sql]).fetchone()[0])
        assert parsed["error"] is False and len(parsed["statements"]) == 1
        assert c.months == ["2026-09"] or c.months == ["2026-10"]          # the partition filter is added
        assert duckdb.connect().get_table_names(c.sql) == {"prices_curated"}
    athena = pl.compile_step(GOOD["steps"][1], backend.catalog(), pl.athena("l18_storeplan"), ["1"])
    assert 'FROM "l18_storeplan"."prices_curated"' in athena.sql


def test_a_quote_in_a_value_stays_a_value(backend):
    s = cs.plan_store_lowest("x' OR '1'='1", "2026-10-01", "2026-10-31")["steps"][0]
    c = pl.compile_step(s, backend.catalog(), pl.DUCKDB)
    assert "'x'' OR ''1''=''1'" in c.sql and backend.run(c)["rows"] == []


def test_the_merge_check_finds_an_item_that_appears_more_than_once(backend):
    p = cs.plan_cheapest_store("2026-08-01", "2026-10-31")
    del p["result"]["per_item"]                             # grouped by store, and nothing picks one row per item
    out = mech.run_plan(p, "sam", backend)
    assert out["status"] == "done" and any("appears 3 times" in c for c in out["checks"])
    assert mech.run_plan(cs.plan_cheapest_store("2026-08-01", "2026-10-31"), "sam", backend)["checks"] == []


def test_the_merge_check_finds_items_without_lake_rows(backend):
    p = cs.plan_store_lowest("3245411573669", "2026-10-01", "2026-10-31")
    p["steps"].insert(0, cs.LIST_STEP)                     # a lake-only question joined to the list for no reason
    p["steps"][1]["group_by"] = ["product_code", "store"]
    p["result"] = {"per_item": {"pick": "min", "column": "prices.price"}, "answer": ["prices.store"]}
    out = mech.run_plan(p, "sam", backend)
    assert "no lake rows for 4 of 5 items" in out["checks"]


def test_code_alone_makes_no_model_call(backend):
    trace = Trace()
    rows = ev.eval_code(backend, trace)
    assert [r["model_calls"] for r in rows] == [0, 0, 0] and trace.items == []
    assert rows[0]["store"] == "operational" and len(rows[0]["answer"]) == len(fx.LISTS["sam"])
    assert mech.code_alone("Which items on my list are cheapest?", backend) is None   # not a known request


def test_the_router_harness_runs_on_stub_labels():
    trace = Trace()
    router = TracedModel(mech.StorePlanStub("stub-router"), trace)
    out = ev.eval_router(router, trace, epochs=2)
    assert len(out["rows"]) == len(cs.ROUTER_SET) >= 20 and len(trace.items) == 2 * len(cs.ROUTER_SET)
    assert out["model"][0] == out["keyword"] and out["identical_epochs"]   # the stub router is the keyword rules
    assert _label_counts() == {label: 6 for label in cs.LABELS}


def _label_counts():
    from collections import Counter

    return dict(Counter(label for _, label in cs.ROUTER_SET))


def test_few_shot_examples_are_not_in_the_labelled_set():
    assert not {q for q, _ in cs.FEW_SHOT} & {q for q, _ in cs.ROUTER_SET}


def test_the_planner_eval_on_the_stub_scores_every_case(backend, lake):
    trace = Trace()
    planner = TracedModel(mech.StorePlanStub("stub-planner"), trace)
    out = ev.eval_planner(planner, backend, trace, epochs=1, lake=lake)
    assert out["summary"]["correct_rate"] == 1.0 and out["summary"]["plan_valid_rate"] == 1.0
    inj = next(r for r in out["rows"] if r["case"] == "injection")
    assert inj["plan_unchanged"] and "prices_raw" not in inj["tables"]


def test_the_raw_zone_double_counts(backend, lake):
    dc = ev.double_count(backend, lake)
    assert dc["score"]["correct"] is False
    assert all(dc["score"]["got"][c][0] > dc["score"]["expected"][c] for c in dc["score"]["expected"])


def test_malformed_plans_are_rejected_and_never_run(backend, monkeypatch):
    no_id = copy.deepcopy(GOOD); del no_id["steps"][1]["id"]
    list_table = copy.deepcopy(GOOD); list_table["steps"][1]["table"] = ["prices_curated"]
    dict_column = copy.deepcopy(GOOD); dict_column["steps"][1]["group_by"] = [{"x": 1}]
    list_on_date = copy.deepcopy(GOOD)
    list_on_date["steps"][1]["filters"] = [{"column": "observed_on", "op": "in", "value": "$list.code"}]
    text_derive = cs.plan_cheapest_store("2026-08-01", "2026-10-31")
    text_derive["result"]["derive"] = [{"as": "d", "op": "-", "left": "prices.store", "right": "prices.price"}]
    monkeypatch.setattr(backend, "run", lambda c: pytest.fail("ran"))
    for bad in (no_id, list_table, dict_column, list_on_date, text_derive, {"steps": "all"}, [1, 2]):
        out = mech.run_plan(bad, "sam", backend)
        assert out["status"] == "rejected", (bad, out)


def test_a_bad_plan_from_the_planner_never_runs(backend, monkeypatch):
    bad = copy.deepcopy(GOOD)
    bad["steps"][1]["table"] = "lists"
    plan, _ = mech.make_plan("q", [], backend.catalog(), mech.StorePlanStub(plan=bad))
    monkeypatch.setattr(backend, "run", lambda c: pytest.fail("ran"))
    assert mech.run_plan(plan, "sam", backend)["status"] == "rejected"


def test_the_spend_cap_refuses_a_call_that_could_pass_it(tmp_path):
    class Inner:
        model, max_tokens, usage = "qwen3.8-27b", 6000, []

        def chat(self, messages, tools=None):
            return ModelReply("{}", tokens_in=10, tokens_out=10, model=self.model)

    capped = ev.Capped(Inner(), ledger=tmp_path / "spend.json", cap=0.0005)
    with pytest.raises(ev.SpendCapReached):
        capped.chat([{"role": "user", "content": "x"}])
    assert not (tmp_path / "spend.json").exists()


def test_athena_rows_and_price():
    from storeplan.backends import _athena_rows, athena_usd

    class Fake:
        def get_query_results(self, **kw):
            return {"ResultSet": {"ResultSetMetadata": {"ColumnInfo": [{"Name": "product_code", "Type": "varchar"},
                                                                         {"Name": "price", "Type": "double"}]},
                                  "Rows": [{"Data": [{"VarCharValue": "product_code"}, {"VarCharValue": "price"}]},
                                           {"Data": [{"VarCharValue": "1001"}, {"VarCharValue": "2.5"}]},
                                           {"Data": [{"VarCharValue": "1002"}, {}]}]}}

    assert _athena_rows(Fake(), "q") == [{"product_code": "1001", "price": 2.5}, {"product_code": "1002", "price": None}]
    assert athena_usd(1_000) == athena_usd(10_000_000) == pytest.approx(10e6 / 1e12 * 5.50)


def _glue_table(name: str) -> dict:
    """A get_table result shaped like the live l18_storeplan tables (Athena DDL, no meaning in it)."""
    from storeplan import catalog as cat

    cols = {"prices_raw": [("obs_id", "string"), ("product_code", "string"), ("store", "string"),
                           ("observed_on", "date"), ("price", "double"), ("currency", "string"),
                           ("source", "string")],
            "prices_curated": [("product_code", "string"), ("store", "string"), ("observed_on", "date"),
                               ("price_cad", "double")]}[name]
    return {"Name": name, "DatabaseName": "l18_storeplan", "CreateTime": 0, "UpdateTime": 0, "VersionId": "0",
            "CatalogId": "000000000000", "IsRegisteredWithLakeFormation": False, "TableType": "EXTERNAL_TABLE",
            "Parameters": {"EXTERNAL": "TRUE", "parquet.compression": "SNAPPY"},
            "StorageDescriptor": {"Columns": [{"Name": c, "Type": t} for c, t in cols],
                                  "Location": f"s3://bucket/{name}/", "SerdeInfo": {"SerializationLibrary": "x"}},
            "PartitionKeys": [{"Name": p["column"], "Type": "string"} for p in cat.PARTITIONS.get(name, [])]}


class _StubGlue:
    def __init__(self, tables: dict):
        self.tables = tables

    def get_table(self, DatabaseName, Name):
        return {"Table": copy.deepcopy(self.tables[Name])}


def test_meaning_written_to_glue_reads_back_as_the_same_catalog():
    from storeplan import catalog as cat
    from storeplan import glue_desc as gd

    live = _StubGlue({n: _glue_table(n) for n in gd.LAKE})
    written = {}
    for n in gd.LAKE:
        ti = gd.with_meaning(gd.table_input(live.tables[n]), n)
        assert not {"CreateTime", "DatabaseName", "CatalogId", "VersionId"} & set(ti)
        assert gd.with_meaning(ti, n) == ti                       # writing twice changes nothing
        written[n] = ti
    old, new = cat.from_glue(live, "l18_storeplan"), cat.from_glue_with_meaning(_StubGlue(written), "x")
    assert cat.for_prompt(old) == cat.for_prompt(new)
    assert {k: v for k, v in old.items() if k != "built_from"} == {k: v for k, v in new.items() if k != "built_from"}


def test_meaning_reader_does_not_fall_back_to_descriptions():
    from storeplan import catalog as cat
    from storeplan import glue_desc as gd

    with pytest.raises(KeyError):
        cat.from_glue_with_meaning(_StubGlue({n: _glue_table(n) for n in gd.LAKE}), "x")
    with pytest.raises(RuntimeError):
        gd.write_meaning(None, "l18_storeplan")
