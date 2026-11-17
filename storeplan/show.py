"""One question to the planner over the data catalog, shown step by step in the terminal. For the lecture.

What it shows, in order:
  1. app code reads the Glue Data Catalog (GetTables): each table's Description and each column's Comment
  2. what the planner call receives: the question, the catalog text, the items on the shopper's list
  3. the plan the model returns (JSON, not SQL)
  4. app code checks the plan, compiles each lake step to SQL, and (offline) estimates its Data scanned
  5. app code runs the steps: a DynamoDB lookup by key and Athena queries (Data scanned as Athena reports it)
  6. app code merges the parts by product code and prints the answer

    AWS_PROFILE=instructor uv run python -m storeplan.show --backend aws --model gateway     # all live
    uv run python -m storeplan.show --backend local --model gateway                          # DuckDB, live model
    uv run python -m storeplan.show --backend local --model stub                             # offline
    --question "..." picks another question; --shopper alex uses the list with the injected product name.
"""
from __future__ import annotations

import argparse
import json
import textwrap
import time

from grocery import config
from grocery.model import GatewayModel
from storeplan import catalog as cat
from storeplan import mechanisms as mech
from storeplan.backends import AWS_NAMES, AwsBackend, LocalBackend

DIM, BOLD, BLUE, AMBER, GREEN, RED, END = "\033[2m", "\033[1m", "\033[34m", "\033[33m", "\033[32m", "\033[31m", "\033[0m"
DEFAULT_Q = "For each item on sam's list, which store had the lowest average price from 2026-08-01 to 2026-10-31?"


def step(n: int, text: str) -> None:
    print(f"\n{BOLD}{n}. {text}{END}")


def show_catalog(catalog: dict, backend_name: str) -> None:
    where = (f"Glue Data Catalog, database {AWS_NAMES['database']} (glue.get_tables)" if backend_name == "aws"
             else "the local catalog file built from DuckDB (catalog.json), same fields")
    print(f"  {DIM}source: {where}{END}")
    for t in catalog["tables"].values():
        print(f"  {BLUE}{t['name']}{END} ({t.get('store_type')}, {t.get('access')})")
        print(textwrap.fill(f"holds: {t.get('holds', '')}", 110, initial_indent="    ", subsequent_indent="    "))
        print(textwrap.fill(f"use for: {t.get('use_for', '')}", 110, initial_indent="    ", subsequent_indent="    "))
        cols = ", ".join(f"{c['name']} ({c.get('about', c.get('type', ''))[:40]})" for c in t.get("columns", [])[:7])
        print(textwrap.fill(f"columns: {cols}", 110, initial_indent="    ", subsequent_indent="    "))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backend", choices=["local", "aws"], default="local")
    ap.add_argument("--model", choices=["stub", "gateway"], default="stub")
    ap.add_argument("--question", default=DEFAULT_Q)
    ap.add_argument("--shopper", default="sam")
    a = ap.parse_args()

    backend = AwsBackend() if a.backend == "aws" else LocalBackend()
    if a.model == "stub":
        planner = mech.StorePlanStub("stub-planner")
    else:   # the gateway lowers max_tokens to the key's cap; Qwen 3.8 reasons in its output budget
        planner = GatewayModel(config.GATEWAY_URL, config.token(), "qwen3.8-27b", max_tokens=16384, timeout_s=280)

    print(f"\n{BOLD}Question:{END} {a.question}   {DIM}(shopper: {a.shopper}){END}")

    step(1, "App code reads the data catalog")
    t = time.monotonic()
    catalog = backend.catalog()
    show_catalog(catalog, backend.name)
    print(f"  {DIM}{time.monotonic() - t:.1f} s{END}")

    step(2, "App code reads the shopper's list by key (DynamoDB GetItem/Query)" if a.backend == "aws"
         else "App code reads the shopper's list by key")
    items = backend.get_list(a.shopper)
    for i in items:
        print(f"  {i['code']}  {i['name'][:70]}")

    step(3, "One planner call: question + catalog text + list items in, a plan out")
    payload = cat.for_prompt(catalog)
    print(f"  {DIM}the planner receives {len(payload):,} characters of catalog text and {len(items)} items{END}")
    t = time.monotonic()
    plan, _raw = mech.make_plan(a.question, items, catalog, planner)
    print(f"  {DIM}{time.monotonic() - t:.1f} s{END}")
    print(textwrap.indent(json.dumps(plan, indent=1)[:2500], "  "))

    step(4, "App code checks the plan, compiles each lake step and runs it")
    t = time.monotonic()
    out = mech.run_plan(plan, a.shopper, backend, items)
    for k, sql in (out.get("sql") or {}).items():
        print(f"  {BLUE}step {k} as SQL:{END}")
        print(textwrap.indent(sql, "    "))
        est = (out.get("estimates") or {}).get(k)
        if est is not None:
            print(f"    {DIM}estimated Data scanned before running: {est:,} bytes{END}")
        if k in (out.get("data_scanned") or {}):
            print(f"    {DIM}Data scanned ({'Athena DataScannedInBytes' if a.backend == 'aws' else 'DuckDB file bytes'}):"
                  f" {out['data_scanned'][k]:,} bytes, {(out.get('lake_rows') or {}).get(k, 0)} rows{END}")
    print(f"  {DIM}{time.monotonic() - t:.1f} s{END}")

    step(5, "Result")
    status = out["status"]
    colour = GREEN if status == "done" else AMBER if status == "refused" else RED
    print(f"  status: {colour}{status}{END}")
    for r in out.get("reasons", []):
        print(f"  - {r}")
    if out.get("reason"):
        print(f"  reason: {out['reason']}")
    for row in (out.get("answer") or [])[:10]:
        print("  " + "  ".join(f"{k}={v}" for k, v in row.items()))
    for c in out.get("checks") or []:
        print(f"  {DIM}check: {c}{END}")
    print()


if __name__ == "__main__":
    main()
