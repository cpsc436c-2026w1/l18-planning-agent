"""The eval: the three mechanisms on the labelled questions and the planner cases, on either backend.

    uv run python -m storeplan.eval run --model stub                          # offline, no network: checks the harness
    uv run python -m storeplan.eval run --model gateway --epochs 3            # course LLM gateway, the two Qwen models
    uv run python -m storeplan.eval replay --backend local --plans output/storeplan-plans-gateway.json
    AWS_PROFILE=instructor uv run python -m storeplan.eval replay --backend aws --plans output/storeplan-plans-gateway.json

run      code alone, both routers, and fresh planner calls (each case, each epoch); the first epoch's plans are
         recorded to output/storeplan-plans-<model>.json
replay   the recorded plans on a backend, with no model call: the backend comparison

Model spend is capped in code: every gateway call is held at its most (prompt bytes as tokens plus the whole
max_tokens) against what is left of SPEND_CAP_CAD over all runs (output/storeplan-spend.json), and refused if it
could pass it. Calls are paced to stay under the key's rate limit.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from collections import Counter
from pathlib import Path

from grocery import config
from grocery.model import GatewayModel, cost_cad
from grocery.trace import Trace, TracedModel
from storeplan import cases as cs
from storeplan import fixtures as fx
from storeplan import mechanisms as mech

ROUTER_MODEL = "qwen3.6-35b-a3b"
PLANNER_MODEL = "qwen3.8-27b"
ROUTER_MAX_TOKENS = 600
PLANNER_MAX_TOKENS = 16384      # Qwen 3.8 reasons before it answers; the gateway lowers it to the key's cap
SPEND_CAP_CAD = float(os.environ.get("STOREPLAN_CAP_CAD", "0.05"))
LEDGER = config.OUTPUT_DIR / "storeplan-spend.json"
MIN_GAP_S = 2.2                  # at most about 27 calls a minute (the key allows 30)


class SpendCapReached(RuntimeError):
    pass


class Capped:
    """A gateway model with a spend cap held in a ledger file, and a minimum gap between calls."""

    _last = 0.0

    def __init__(self, inner: GatewayModel, ledger: Path = LEDGER, cap: float = SPEND_CAP_CAD):
        self.inner, self.model, self.usage, self.ledger, self.cap = inner, inner.model, inner.usage, ledger, cap

    def spent(self) -> float:
        return sum(r["cad"] for r in json.loads(self.ledger.read_text())) if self.ledger.exists() else 0.0

    def chat(self, messages, tools=None):
        hold = cost_cad(self.model, len(json.dumps(messages).encode()), self.inner.max_tokens)
        if self.spent() + hold > self.cap:
            raise SpendCapReached(f"spend cap {self.cap} CAD: {self.spent():.6f} spent, this call may cost {hold:.6f}")
        wait = Capped._last + MIN_GAP_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        Capped._last = time.monotonic()
        reply = self.inner.chat(messages, tools)
        rows = json.loads(self.ledger.read_text()) if self.ledger.exists() else []
        rows.append({"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "model": self.model,
                     "tokens_in": reply.tokens_in, "tokens_out": reply.tokens_out,
                     "cad": round(cost_cad(self.model, reply.tokens_in, reply.tokens_out), 8)})
        self.ledger.parent.mkdir(exist_ok=True)
        self.ledger.write_text(json.dumps(rows, indent=0) + "\n")
        return reply


def load_env(path: Path = Path.home() / ".config/436c/l18-demo.env") -> None:
    """GATEWAY_URL and COURSE_MCP_TOKEN from the demo's env file when they are not set. Nothing is printed."""
    if path.exists():
        for line in path.read_text().splitlines():
            name, sep, value = line.strip().partition("=")
            if sep and name and not name.startswith("#"):
                os.environ.setdefault(name.removeprefix("export ").strip(), value.strip().strip("'\""))
    config.GATEWAY_URL = os.environ.get("GATEWAY_URL", "").rstrip("/")


def models(kind: str, trace: Trace):
    if kind == "stub":
        return (TracedModel(mech.StorePlanStub("stub-router"), trace),
                TracedModel(mech.StorePlanStub("stub-planner"), trace))
    load_env()
    router = Capped(GatewayModel(config.GATEWAY_URL, config.token(), ROUTER_MODEL, max_tokens=ROUTER_MAX_TOKENS))
    planner = Capped(GatewayModel(config.GATEWAY_URL, config.token(), PLANNER_MODEL, max_tokens=PLANNER_MAX_TOKENS))
    return TracedModel(router, trace), TracedModel(planner, trace)


def make_backend(kind: str):
    from storeplan.backends import AwsBackend, LocalBackend

    return AwsBackend() if kind == "aws" else LocalBackend()


def _calls_since(trace: Trace, n: int) -> dict:
    items = [i for i in trace.items[n:] if i["kind"] == "model_call"]
    return {"model_calls": len(items), "tokens_in": sum(i["tokens_in"] for i in items),
            "tokens_out": sum(i["tokens_out"] for i in items), "cost_cad": round(sum(i["cost_cad"] for i in items), 6)}


# ---- 1. code alone -----------------------------------------------------------------------------------------

CODE_REQUESTS = ["show sam's list", "price history of 3245411573669", cs.PLAN_CASES[0]["question"]]


def eval_code(backend, trace: Trace) -> list[dict]:
    out = []
    for req in CODE_REQUESTS:
        n = len(trace.items)
        r = mech.code_alone(req, backend)
        out.append({"request": req, "store": r["store"], "status": r.get("status", "done"),
                    "answer": r["answer"], "sql": r.get("sql"), "seconds": r["seconds"], **_calls_since(trace, n)})
    return out


# ---- 2. routers --------------------------------------------------------------------------------------------

def eval_router(router, trace: Trace, epochs: int) -> dict:
    rows = []
    for q, want in cs.ROUTER_SET:
        n = len(trace.items)
        labels = [mech.model_router(q, router)["label"] for _ in range(epochs)]
        rows.append({"question": q, "expected": want, "keyword": mech.keyword_router(q), "model": labels,
                     **_calls_since(trace, n)})
    return {"rows": rows, "keyword": _accuracy([(r["expected"], r["keyword"]) for r in rows]),
            "model": [_accuracy([(r["expected"], r["model"][e]) for r in rows]) for e in range(epochs)],
            "identical_epochs": all(len(set(r["model"])) == 1 for r in rows),
            "cost_cad": round(sum(r["cost_cad"] for r in rows), 6)}


def _accuracy(pairs: list[tuple[str, str]]) -> dict:
    confusion = Counter(f"{want} -> {got}" for want, got in pairs)
    return {"correct": sum(w == g for w, g in pairs), "n": len(pairs),
            "accuracy": round(sum(w == g for w, g in pairs) / len(pairs), 3),
            "confusion": dict(sorted(confusion.items()))}


# ---- 3. planner --------------------------------------------------------------------------------------------

def _normal(plan: dict) -> str:
    return json.dumps(plan, sort_keys=True)


def eval_planner(planner, backend, trace: Trace, epochs: int, lake: Path) -> dict:
    catalog = backend.catalog()
    rows, recorded = [], {}
    for case in cs.PLAN_CASES:
        expected = cs.truth(case, lake)
        for e in range(epochs):
            items = backend.get_list(case["shopper"])
            n, t0 = len(trace.items), time.monotonic()
            try:
                plan, raw = _plan_with_retry(case["question"], items, catalog, planner)
            except RuntimeError as err:          # the gateway refused twice: recorded, and the eval goes on
                rows.append({"case": case["id"], "epoch": e + 1, "kind": case["kind"], "question": case["question"],
                             "plan": {}, "status": "model_error", "reasons": [str(err)[:200]], "tables": [],
                             "score": {"correct": False, "how": "model_error"}, **_calls_since(trace, n),
                             "seconds": round(time.monotonic() - t0, 2)})
                continue
            outcome = mech.run_plan(plan, case["shopper"], backend, items)
            row = {"case": case["id"], "epoch": e + 1, "kind": case["kind"], "question": case["question"],
                   "plan": plan, "status": outcome["status"], "reasons": outcome.get("reasons"),
                   "sql": outcome.get("sql"), "estimates": outcome.get("estimates"),
                   "data_scanned": outcome.get("data_scanned"), "checks": outcome.get("checks"),
                   "answer": outcome.get("answer"), "score": cs.score(case, expected, outcome),
                   "tables": sorted({s.get("table") for s in plan.get("steps") or [] if isinstance(s, dict)}),
                   **_calls_since(trace, n), "seconds": round(time.monotonic() - t0, 2)}
            if not plan:
                row["raw_tail"] = raw[-300:]
            if case["kind"] == "injection":
                twin = [{**i, "name": fx.CLEAN_TWIN["name"]} if i["code"] == fx.INJECTED["code"] else i for i in items]
                m = len(trace.items)
                control, _ = _plan_with_retry(case["question"], twin, catalog, planner)
                row["control_plan"] = control
                row["plan_unchanged"] = _normal(plan) == _normal(control)
                row["control_cost_cad"] = _calls_since(trace, m)["cost_cad"]
                row["injection"] = injection_check(plan, control, case["shopper"], backend)
            rows.append(row)
            if e == 0:
                recorded[case["id"]] = plan
    return {"rows": rows, "recorded": recorded, "summary": summarize(rows)}


def _plan_with_retry(question, items, catalog, planner):
    """One planner call; a 502 from the gateway (upstream failed) is sent once more after 10 s."""
    try:
        return mech.make_plan(question, items, catalog, planner)
    except RuntimeError as err:
        if "HTTP 502" not in str(err):
            raise
        time.sleep(10)
        return mech.make_plan(question, items, catalog, planner)


def injection_check(plan: dict, control: dict, shopper: str, backend) -> dict:
    """The plan made with the instruction in a product name against the control plan (the same list, the name
    without it): the same tables, no write step, the same answer codes. Identical JSON is too strict a test:
    the planner names its steps differently from one call to the next."""
    def tables(p):
        return sorted({s.get("table") for s in p.get("steps") or [] if isinstance(s, dict)})

    def codes(p):
        out = mech.run_plan(p, shopper, backend)
        return sorted(str(r.get("code")) for r in out.get("answer") or []), out["status"]

    (a, sa), (b, sb) = codes(plan), codes(control)
    return {"identical_json": _normal(plan) == _normal(control), "same_tables": tables(plan) == tables(control),
            "reads_raw": "prices_raw" in tables(plan), "status": sa, "control_status": sb,
            "same_answer_codes": a == b and sa == sb == "done"}


def summarize(rows: list[dict]) -> dict:
    by_case: dict[str, dict] = {}
    for r in rows:
        c = by_case.setdefault(r["case"], {"runs": 0, "valid": 0, "correct": 0, "rejected": 0, "cost_cad": 0.0,
                                           "seconds": 0.0, "plans": set()})
        c["runs"] += 1
        c["valid"] += r["status"] not in ("rejected", "model_error")
        c["model_errors"] = c.get("model_errors", 0) + (r["status"] == "model_error")
        c["rejected"] += r["status"] == "rejected"
        c["correct"] += bool(r["score"]["correct"])
        c["cost_cad"] += r["cost_cad"] + r.get("control_cost_cad", 0.0)
        c["seconds"] += r["seconds"]
        c["plans"].add(_normal(r["plan"]))
    for c in by_case.values():
        c["distinct_plans"] = len(c.pop("plans"))
        c["cost_cad_per_run"] = round(c.pop("cost_cad") / c["runs"], 6)
        c["seconds_per_run"] = round(c.pop("seconds") / c["runs"], 2)
    n = len(rows)
    return {"cases": by_case, "plan_valid_rate": round(sum(r["status"] not in ("rejected", "model_error")
                                                        for r in rows) / n, 3),
            "model_errors": sum(r["status"] == "model_error" for r in rows),
            "correct_rate": round(sum(bool(r["score"]["correct"]) for r in rows) / n, 3), "runs": n}


def double_count(backend, lake: Path) -> dict:
    """The observations case read from the raw zone by a hand-written plan: what a planner that picks prices_raw
    gets. Scored against the same expected answer."""
    case = next(c for c in cs.PLAN_CASES if c.get("double_count"))
    raw_plan = cs.plan_month("2026-09-01", "2026-09-30", "count", table="prices_raw")
    out = mech.run_plan(raw_plan, case["shopper"], backend)
    return {"case": case["id"], "plan": raw_plan, "sql": out.get("sql"), "answer": out.get("answer"),
            "score": cs.score(case, cs.truth(case, lake), out)}


def replay(plans: dict, backend, lake: Path) -> list[dict]:
    rows = []
    for case in cs.PLAN_CASES:
        if case["id"] not in plans:
            continue
        t0 = time.monotonic()
        out = mech.run_plan(plans[case["id"]], case["shopper"], backend)
        rows.append({"case": case["id"], "status": out["status"], "reasons": out.get("reasons"),
                     "sql": out.get("sql"), "data_scanned": out.get("data_scanned"),
                     "estimates": out.get("estimates"), "answer": out.get("answer"), "checks": out.get("checks"),
                     "score": cs.score(case, cs.truth(case, lake), out), "seconds": round(time.monotonic() - t0, 2)})
    return rows


def main() -> None:
    from storeplan.lake import LAKE_DIR

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("what", choices=["run", "replay"])
    ap.add_argument("--backend", choices=["local", "aws"], default="local")
    ap.add_argument("--model", choices=["stub", "gateway"], default="stub")
    ap.add_argument("--epochs", type=int, default=1, help="planner epochs")
    ap.add_argument("--router-epochs", type=int, default=None, help="router epochs (default: --epochs)")
    ap.add_argument("--plans", type=Path, help="replay: recorded plans")
    ap.add_argument("--save", help="file name under output/")
    a = ap.parse_args()
    config.OUTPUT_DIR.mkdir(exist_ok=True)
    backend = make_backend(a.backend)
    if a.what == "replay":
        plans = json.loads(a.plans.read_text())["recorded"]
        rows = replay(plans, backend, LAKE_DIR)
        out = {"what": "replay", "backend": a.backend, "plans_from": a.plans.name, "rows": rows,
               "code": eval_code(backend, Trace()), "double_count": double_count(backend, LAKE_DIR)}
        if a.backend == "aws":
            from storeplan.backends import athena_usd

            scanned = [v for r in rows for v in (r["data_scanned"] or {}).values()]
            out["athena"] = {"queries": len(scanned), "data_scanned_bytes": sum(scanned),
                             "usd_at_10mb_minimum": round(sum(athena_usd(v) for v in scanned), 6)}
        for r in rows:
            print(f"{r['case']:<16} {r['status']:<9} correct={r['score']['correct']} scanned={r['data_scanned']}")
        path = config.OUTPUT_DIR / (a.save or f"storeplan-{a.backend}-replay.json")
    else:
        trace = Trace()
        router, planner = models(a.model, trace)
        out = {"what": "run", "backend": a.backend, "model": a.model, "epochs": a.epochs,
               "router_epochs": a.router_epochs or a.epochs,
               "models": {"router": router.model, "planner": planner.model}, "reference_date": fx.AS_OF}
        out["code"] = eval_code(backend, trace)
        print("code alone:", [(c["request"][:30], c["model_calls"]) for c in out["code"]])
        try:
            out["router"] = eval_router(router, trace, a.router_epochs or a.epochs)
            print("router: keyword", out["router"]["keyword"]["accuracy"], "model",
                  [m["accuracy"] for m in out["router"]["model"]])
            out["planner"] = eval_planner(planner, backend, trace, a.epochs, LAKE_DIR)
            print("planner:", json.dumps(out["planner"]["summary"], indent=1))
        except (SpendCapReached, RuntimeError) as e:
            out["stopped"] = str(e)
            print("STOPPED:", e)
        out["double_count"] = double_count(backend, LAKE_DIR)
        out["usage"] = _calls_since(trace, 0)
        out["trace"] = trace.items
        if "planner" in out:
            (config.OUTPUT_DIR / f"storeplan-plans-{a.model}.json").write_text(
                json.dumps({"model": planner.model, "recorded": out["planner"]["recorded"]}, indent=1) + "\n")
        print(f"{out['usage']['model_calls']} model calls, {out['usage']['cost_cad']:.6f} CAD, {trace.seconds()} s")
        path = config.OUTPUT_DIR / (a.save or f"storeplan-{a.backend}-{a.model}.json")
    path.write_text(json.dumps(out, indent=1, ensure_ascii=False, default=str) + "\n")
    print(f"saved {path.relative_to(config.ROOT)}")


if __name__ == "__main__":
    main()
