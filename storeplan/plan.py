"""A plan, checked and compiled by app code. The model never writes SQL.

The planner returns a structured plan (JSON). Each step names a store, a table from the catalog, and what to
read from it:

    {"answerable": true,
     "steps": [
       {"id": "list", "store": "operational", "table": "shopping_lists", "lookup": "by_key"},
       {"id": "then", "store": "lake", "table": "prices_curated",
        "filters": [{"column": "observed_on", "op": "=", "value": "2026-09-15"},
                    {"column": "product_code", "op": "in", "value": "$list.code"}],
        "group_by": ["product_code"], "aggregates": [{"fn": "avg", "column": "price_cad", "as": "price"}]},
       {"id": "now", ...}],
     "result": {"per_item": {"pick": "min", "column": "then.price"},           (optional: one row per item)
                "derive": [{"as": "change", "op": "-", "left": "now.price", "right": "then.price"}],
                "keep": [{"column": "change", "op": "<", "value": 0}],
                "sort": {"column": "change", "order": "asc"}, "limit": 1,
                "answer": ["code", "name", "change"]}}
    {"answerable": false, "reason": "..."}

validate() checks a plan against the catalog: known tables and columns, read steps only, no shopper from the
model, typed filter values. compile_step() turns a lake step into one SELECT for a dialect, so "read-only" and
"only catalog tables" are properties of the compiler: it has no other statement to emit, and it takes the
table name from the catalog. merge() joins the parts by product code and checks that every item on the list
appears once.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

FILTER_OPS = {"=", "!=", "<", "<=", ">", ">=", "in", "between"}
AGG_FNS = {"min", "max", "avg", "sum", "count", "count_distinct"}
DERIVE_OPS = {"-", "+", "/", "abs_diff", "pct_change"}
STEP_KEYS = {"operational": {"id", "store", "table", "lookup"},
             "lake": {"id", "store", "table", "filters", "group_by", "aggregates", "columns"}}
WRITE_WORDS = {"write", "put", "insert", "update", "delete", "add", "remove", "upsert", "drop", "create"}
MAX_STEPS = 4
MAX_ROWS = 5000                     # a lake step without aggregates returns at most this many rows (ours)
IDENT = re.compile(r"^[a-z][a-z0-9_]{0,30}$")
LIST_REF = "$list.code"             # the only value app code fills in: the codes the list lookup returned


class PlanRejected(Exception):
    def __init__(self, reasons: list[str]):
        super().__init__("; ".join(reasons))
        self.reasons = reasons


@dataclass
class Compiled:
    step: str
    table: str
    sql: str
    columns: list[str]                       # catalog columns the query reads
    months: list[str] | None = None          # partitions it reads (None: all)
    out: list[str] = field(default_factory=list)


@dataclass
class Dialect:
    """What differs between the engines: how a table is named. Everything else is the SQL both accept."""
    name: str
    database: str | None = None

    def table(self, name: str) -> str:
        return f'"{self.database}"."{name}"' if self.database else f'"{name}"'


DUCKDB = Dialect("duckdb")


def athena(database: str) -> Dialect:
    return Dialect("athena", database)


# ---- validation -------------------------------------------------------------------------------------------

def validate(plan: dict, catalog: dict) -> list[str]:
    """Every reason the plan cannot run (empty: it can). Nothing runs before this returns empty."""
    if not isinstance(plan, dict) or not plan:
        return ["the reply held no plan"]
    if plan.get("answerable") is False:
        return []
    steps = plan.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        return [f"a plan has 1 to {MAX_STEPS} steps"]
    tables, why, ids = catalog["tables"], [], []
    for s in steps:
        if not isinstance(s, dict):
            why.append("a step is not an object")
            continue
        sid, store, table = s.get("id"), s.get("store"), s.get("table")
        if not all(isinstance(x, str) for x in (sid, store, table)):
            why.append(f"step {sid!r}: id, store and table are text")
            continue
        if not isinstance(sid, str) or not IDENT.match(sid) or sid in ids:
            why.append(f"step id {sid!r}: lower-case words, unique")
        ids.append(sid)
        words = {str(v).lower() for k, v in s.items() if k in ("action", "lookup", "op", "operation")}
        if words & WRITE_WORDS:
            why.append(f"step {sid}: writes are not allowed ({', '.join(sorted(words & WRITE_WORDS))})")
            continue
        if "shopper" in s or "key_value" in s or "value" in s:
            why.append(f"step {sid}: the shopper is set by app code from the request, not by the plan")
            continue
        if table not in tables:
            why.append(f"step {sid}: table {table!r} is not in the catalog")
            continue
        entry = tables[table]
        if store != entry["store_type"]:
            why.append(f"step {sid}: table {table} is in the {entry['store_type']} store, not {store!r}")
            continue
        extra = set(s) - STEP_KEYS[store]
        if extra:
            why.append(f"step {sid}: unknown fields {sorted(extra)}")
        if store == "operational":
            if s.get("lookup") != "by_key":
                why.append(f"step {sid}: the operational store answers key lookups only (lookup: by_key)")
        else:
            why += _check_lake_step(s, entry, has_list=any(
                isinstance(x, dict) and x.get("store") == "operational" for x in steps))
    if sum(1 for s in steps if isinstance(s, dict) and s.get("store") == "operational") > 1:
        why.append("one list lookup per plan")
    if not why:
        why += _check_result(plan.get("result") or {}, steps, tables)
    return why


def _check_lake_step(s: dict, entry: dict, has_list: bool) -> list[str]:
    sid, why = s["id"], []
    cols = {c["name"]: c["type"] for c in entry["columns"]}
    filters, group_by = s.get("filters") or [], s.get("group_by") or []
    aggs, columns = s.get("aggregates") or [], s.get("columns") or []
    if not isinstance(filters, list) or not isinstance(group_by, list) or not isinstance(aggs, list):
        return [f"step {sid}: filters, group_by and aggregates are lists"]
    if not aggs and not columns:
        why.append(f"step {sid}: name aggregates or columns")
    if aggs and columns:
        why.append(f"step {sid}: aggregates and plain columns cannot be mixed; use group_by")
    names = [*group_by, *columns, *(a.get("column") for a in aggs if isinstance(a, dict)),
             *(a.get("as") for a in aggs if isinstance(a, dict)), *(f.get("column") for f in filters if isinstance(f, dict))]
    if not isinstance(columns, list) or any(not isinstance(c, str) for c in names):
        return [f"step {sid}: column names and aliases are text"]
    for c in [*group_by, *columns]:
        if c not in cols:
            why.append(f"step {sid}: column {c!r} is not in {entry['name']}")
    if aggs and "product_code" not in group_by and has_list:
        why.append(f"step {sid}: group by product_code to join with the list")
    for a in aggs:
        if not isinstance(a, dict) or a.get("fn") not in AGG_FNS:
            why.append(f"step {sid}: aggregate {a!r}: fn is one of {sorted(AGG_FNS)}")
            continue
        if a.get("column") not in cols:
            why.append(f"step {sid}: column {a.get('column')!r} is not in {entry['name']}")
        if not isinstance(a.get("as"), str) or not IDENT.match(a["as"]):
            why.append(f"step {sid}: aggregate needs a name in 'as'")
    for f in filters:
        why += [f"step {sid}: {w}" for w in _check_filter(f, cols, has_list)]
    return why


def _check_filter(f, cols: dict, has_list: bool) -> list[str]:
    if not isinstance(f, dict) or f.get("column") not in cols:
        return [f"filter {f!r}: column is not in the table"]
    op, value, kind = f.get("op"), f.get("value"), cols[f["column"]]
    if op not in FILTER_OPS:
        return [f"filter op {op!r} is not one of {sorted(FILTER_OPS)}"]
    if value == LIST_REF:
        if op != "in" or not has_list or f["column"] != "product_code":
            return [f"{LIST_REF} needs column product_code, op 'in' and a list lookup step"]
        return []
    values = value if op in ("in", "between") else [value]
    if not isinstance(values, list) or (op == "between" and len(values) != 2) or not 1 <= len(values) <= 50:
        return [f"filter on {f['column']}: {op} takes a list of values"]
    for v in values:
        if _literal(v, kind) is None:
            return [f"filter on {f['column']}: {v!r} is not a {kind}"]
    return []


def _check_result(r: dict, steps: list[dict], tables: dict) -> list[str]:
    if not isinstance(r, dict):
        return ["result is an object"]
    names, numeric = _available_columns(steps, tables)
    why = []
    per_item = r.get("per_item")
    if per_item is not None:
        if not isinstance(per_item, dict) or per_item.get("pick") not in ("min", "max") \
                or not isinstance(per_item.get("column"), str) or per_item.get("column") not in names:
            why.append(f"per_item {per_item!r}: pick min or max of a known column")
    for d in r.get("derive") or []:
        if not isinstance(d, dict) or not all(isinstance(d.get(k), str) for k in ("op", "left", "right", "as")) \
                or d.get("op") not in DERIVE_OPS or d.get("left") not in numeric \
                or d.get("right") not in numeric or not IDENT.match(str(d.get("as", ""))):
            why.append(f"derive {d!r}: op in {sorted(DERIVE_OPS)}, left and right known number columns, a name")
            continue
        names.add(d["as"])
        numeric.add(d["as"])
    for k in r.get("keep") or []:
        if not isinstance(k, dict) or not isinstance(k.get("column"), str) or not isinstance(k.get("op"), str) \
                or k.get("column") not in names or k.get("op") not in FILTER_OPS - {"in", "between"}:
            why.append(f"keep {k!r}: a known column, a comparison and a value")
    sort = r.get("sort")
    if sort is not None and (not isinstance(sort, dict) or not isinstance(sort.get("column"), str)
                             or sort.get("column") not in names
                             or sort.get("order", "asc") not in ("asc", "desc")):
        why.append(f"sort {sort!r}: a known column, asc or desc")
    if r.get("limit") is not None and (not isinstance(r["limit"], int) or not 1 <= r["limit"] <= 100):
        why.append("limit is a whole number from 1 to 100")
    answer = r.get("answer")
    if not isinstance(answer, list) or not answer or any(not isinstance(a, str) or a not in names for a in answer):
        why.append(f"answer {answer!r}: a list of known columns ({', '.join(sorted(names))})")
    return why


def _available_columns(steps: list[dict], tables: dict) -> tuple[set[str], set[str]]:
    """Every column name the result spec may use, and the ones that hold numbers."""
    names: set[str] = set()
    numeric: set[str] = set()
    for s in steps:
        if s["store"] == "operational":
            names |= {"code", "name", "quantity"} | {f"{s['id']}.{c}" for c in ("code", "name", "quantity")}
            numeric |= {"quantity", f"{s['id']}.quantity"}
        else:
            kinds = {c["name"]: c["type"] for c in tables[s["table"]]["columns"]}
            plain = (s.get("group_by") or []) + (s.get("columns") or [])
            names |= {f"{s['id']}.{c}" for c in plain}
            numeric |= {f"{s['id']}.{c}" for c in plain if kinds.get(c) in ("double", "bigint", "int")}
            for a in s.get("aggregates") or []:
                names.add(f"{s['id']}.{a['as']}")
                if a["fn"] in ("count", "count_distinct", "sum", "avg") or kinds.get(a["column"]) in ("double", "bigint", "int"):
                    numeric.add(f"{s['id']}.{a['as']}")
    return names, numeric


def _literal(v, kind: str) -> str | None:
    """The SQL literal for a value of a catalog type, or None when the value is not of that type."""
    if kind == "date":
        try:
            return f"DATE '{dt.date.fromisoformat(str(v)).isoformat()}'"
        except ValueError:
            return None
    if kind in ("double", "bigint", "int"):
        return repr(float(v)) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    if isinstance(v, (str, int)) and not isinstance(v, bool) and len(str(v)) <= 64:
        return "'" + str(v).replace("'", "''") + "'"
    return None


# ---- compiling ---------------------------------------------------------------------------------------------

def compile_step(s: dict, catalog: dict, dialect: Dialect, list_codes: list[str] | None = None) -> Compiled:
    """One validated lake step -> one SELECT. Table and column names come from the catalog, values are typed
    literals, and the partition filter is added from the date filter. There is no other statement to emit."""
    entry = catalog["tables"][s["table"]]
    cols = {c["name"]: c["type"] for c in entry["columns"]}
    group_by, aggs = s.get("group_by") or [], s.get("aggregates") or []
    select = [f'"{c}"' for c in group_by] + [_agg(a) for a in aggs]
    if not aggs:
        select = [f'"{c}"' for c in s["columns"]]
    where = [_where(f, cols, list_codes) for f in s.get("filters") or []]
    months = _months(s, entry)
    if months is not None:
        part = entry["partitions"][0]["column"]
        where.append(f'"{part}" IN (' + ", ".join(f"'{m}'" for m in months) + ")")
    sql = f"SELECT {', '.join(select)} FROM {dialect.table(s['table'])}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    if aggs and group_by:
        sql += " GROUP BY " + ", ".join(f'"{c}"' for c in group_by)
    if not aggs:
        sql += f" LIMIT {MAX_ROWS}"
    read = sorted({*group_by, *(a["column"] for a in aggs), *(s.get("columns") or []),
                   *(f["column"] for f in s.get("filters") or [])})
    out = group_by + [a["as"] for a in aggs] if aggs else list(s["columns"])
    return Compiled(s["id"], s["table"], sql, read, months, out)


def _agg(a: dict) -> str:
    if a["fn"] == "count_distinct":
        return f'count(DISTINCT "{a["column"]}") AS "{a["as"]}"'
    return f'{a["fn"]}("{a["column"]}") AS "{a["as"]}"'


def _where(f: dict, cols: dict, list_codes: list[str] | None) -> str:
    col, op, kind = f'"{f["column"]}"', f["op"], cols[f["column"]]
    if f["value"] == LIST_REF:
        codes = [c for c in (list_codes or []) if re.fullmatch(r"[0-9A-Za-z_-]{1,32}", c)] or ["-"]
        return f"{col} IN (" + ", ".join(f"'{c}'" for c in codes) + ")"
    if op == "in":
        return f"{col} IN (" + ", ".join(_literal(v, kind) for v in f["value"]) + ")"
    if op == "between":
        lo, hi = (_literal(v, kind) for v in f["value"])
        return f"{col} BETWEEN {lo} AND {hi}"
    return f"{col} {'<>' if op == '!=' else op} {_literal(f['value'], kind)}"


def _months(s: dict, entry: dict) -> list[str] | None:
    """The month partitions a date filter can touch; None when the filter does not bound both ends."""
    if not entry.get("partitions"):
        return None
    src = entry["partitions"][0]["from"]
    lo = hi = None
    for f in s.get("filters") or []:
        if f.get("column") != src:
            continue
        v = f["value"]
        if f["op"] == "=":
            lo = hi = v
        elif f["op"] == "between":
            lo, hi = v
        elif f["op"] == "in":
            lo, hi = min(v), max(v)
        elif f["op"] in (">", ">="):
            lo = v
        elif f["op"] in ("<", "<="):
            hi = v
    if lo is None or hi is None:
        return None
    y, m = int(str(lo)[:4]), int(str(lo)[5:7])
    out = []
    while (y, m) <= (int(str(hi)[:4]), int(str(hi)[5:7])) and len(out) < 24:
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


# ---- merging -----------------------------------------------------------------------------------------------

def merge(plan: dict, items: list[dict] | None, results: dict[str, list[dict]]) -> dict:
    """Join each lake step to the list by product code, apply the result spec, and check the join.

    Returns {"rows", "answer", "checks"}; checks lists every way the merged rows break "each item once".
    """
    steps = plan["steps"]
    lake = [s for s in steps if s["store"] == "lake"]
    op = next((s for s in steps if s["store"] == "operational"), None)
    checks: list[str] = []
    if op is not None:
        rows = []
        for it in items or []:
            base = {"code": it["code"], "name": it["name"], "quantity": it.get("quantity", 1),
                    **{f"{op['id']}.{k}": it.get(k) for k in ("code", "name", "quantity")}}
            combos = [base]
            for s in lake:
                hits = [r for r in results[s["id"]] if str(r.get("product_code")) == it["code"]]
                if not hits:
                    hits = [{}]
                combos = [{**c, **{f"{s['id']}.{k}": v for k, v in h.items()}} for c in combos for h in hits]
            rows += combos
    else:
        rows = [{f"{lake[0]['id']}.{k}": v for k, v in r.items()} for r in results[lake[0]["id"]]]
    r = plan.get("result") or {}
    if r.get("per_item") and op is not None:
        pick, col = r["per_item"]["pick"], r["per_item"]["column"]
        best: dict[str, dict] = {}
        for row in rows:
            v, cur = row.get(col), best.get(row["code"])
            if cur is None or (v is not None and (cur.get(col) is None or
                                                  (v < cur[col] if pick == "min" else v > cur[col]))):
                best[row["code"]] = row
        rows = list(best.values())
    if op is not None:
        seen: dict[str, int] = {}
        for row in rows:
            seen[row["code"]] = seen.get(row["code"], 0) + 1
        for it in items or []:
            n = seen.get(it["code"], 0)
            if n != 1:
                checks.append(f"item {it['code']} appears {n} times in the merged rows")
        prefixes = tuple(f"{s['id']}." for s in lake)
        missing = [row["code"] for row in rows
                   if not any(k.startswith(prefixes) and v is not None for k, v in row.items())]
        if missing:
            checks.append(f"no lake rows for {len(missing)} of {len(rows)} items")
    for d in r.get("derive") or []:
        for row in rows:
            row[d["as"]] = _derive(d["op"], row.get(d["left"]), row.get(d["right"]))
    for k in r.get("keep") or []:
        rows = [row for row in rows if _compare(row.get(k["column"]), k["op"], k["value"])]
    if r.get("sort"):
        col, desc = r["sort"]["column"], r["sort"].get("order", "asc") == "desc"
        rows = sorted((row for row in rows if row.get(col) is not None), key=lambda x: x[col], reverse=desc) \
            + [row for row in rows if row.get(col) is None]
    if r.get("limit"):
        rows = rows[:r["limit"]]
    answer = [{a: row.get(a) for a in r.get("answer") or []} for row in rows]
    return {"rows": rows, "answer": answer, "checks": checks}


def _derive(op: str, a, b):
    if a is None or b is None:
        return None
    if op == "-":
        return round(a - b, 4)
    if op == "+":
        return round(a + b, 4)
    if op == "abs_diff":
        return round(abs(a - b), 4)
    if op == "/":
        return round(a / b, 4) if b else None
    return round((a - b) / b * 100, 2) if b else None      # pct_change


def _compare(v, op: str, value) -> bool:
    if v is None:
        return False
    try:
        return {"=": v == value, "!=": v != value, "<": v < value, "<=": v <= value,
                ">": v > value, ">=": v >= value}[op]
    except TypeError:
        return False
