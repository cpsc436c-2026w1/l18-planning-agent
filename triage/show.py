"""Incident triage, shown step by step in the terminal: every tool call the model asks for, what came back, and what
app code does around it. For the lecture; the same code path as triage/agent.py.

    uv run python -m triage.show --incident rows_dropped_by_filter                 # stub model, offline
    uv run python -m triage.show --incident rows_dropped_by_filter --model gateway # live, course LLM gateway
    uv run python -m triage.show --incident rows_dropped_by_filter --replay        # a saved live run, no network

--replay reads output/triage-eval-gateway-v3.json (run 1 of the incident) and prints its tool calls with a short
pause, for when the network fails in class. --yes approves the fix without asking.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from grocery import config
from triage import world as W
from triage.agent import apply_fix, runbook, run_agent, verify

DIM, BOLD, RED, GREEN, BLUE, AMBER, END = "\033[2m", "\033[1m", "\033[31m", "\033[32m", "\033[34m", "\033[33m", "\033[0m"


def summary(name: str, result: dict) -> str:
    """One line a person can read for each tool result."""
    if "error" in result:
        return f"{RED}error: {result['error']}{END}"
    if name == "get_job_runs":
        return "; ".join(f"{r['Id']} {r['JobRunState']} {r['StartedOn'][11:16]}-{r['CompletedOn'][11:16]}"
                         + (f" {r['ErrorMessage'][:60]}" if r["ErrorMessage"] else "")
                         + ("" if "--eur_to_cad" in r["Arguments"] else " (no --eur_to_cad)")
                         for r in result["JobRuns"]) or "no runs"
    if name == "get_run_log":
        odd = [e["message"] for e in result["events"] if e["message"].startswith(("WARN", "ERROR"))]
        return " | ".join(m[:90] for m in odd) or f"{len(result['events'])} lines, no WARN or ERROR"
    if name == "list_raw_objects":
        return ", ".join(f"{o['Key'].split('/')[-1]} {o['Size'] // 1000} KB at {o['LastModified'][11:16]}"
                         for o in result["Contents"])
    if name == "count_rows":
        return ", ".join(f"{s} {n}" for s, n in result["rows_per_store"].items()) + f" (usual {result['usual_per_store']})"
    if name == "average_price":
        return ", ".join(f"{s} {v:.2f}" for s, v in result["avg_price_cad"].items()) + " CAD (usual " + \
            ", ".join(f"{v:.2f}" for v in result["seven_day_avg_cad"].values()) + ")"
    if name == "get_table_versions":
        return "; ".join(f"v{v['VersionId']} {v['UpdateTime'][:10]}: {', '.join(v['Columns'][4:5])}"
                         for v in result["TableVersions"])
    return json.dumps(result)[:100]


class ShownTools(W.TriageTools):
    def call(self, name, args):
        result = super().call(name, args)
        a = ", ".join(f"{k}={v}" for k, v in args.items())
        print(f"  {BLUE}tool call{END} {BOLD}{name}{END}({a})")
        print(f"  {DIM}result:{END}    {summary(name, result)}")
        return result


class ShownModel:
    def __init__(self, inner):
        self.inner, self.n = inner, 0

    def chat(self, messages, tools=None):
        self.n += 1
        t = time.monotonic()
        print(f"{AMBER}model call {self.n}{END} {DIM}(the model reads everything so far and asks for the next tool call){END}")
        reply = self.inner.chat(messages, tools)
        print(f"  {DIM}{time.monotonic() - t:.1f} s{END}")
        return reply

    @property
    def usage(self):
        return getattr(self.inner, "usage", [])


def replay(incident: str) -> dict:
    data = json.load(open(config.ROOT / "output" / "triage-eval-gateway-v3.json"))
    row = next(r for r in data["rows"] if r["incident"] == incident)
    run = row["runs"][0]
    print(f"{DIM}replaying a saved live run ({data['model']}, {data['at'][:10]}){END}")
    for i, step in enumerate(run["trace"], 1):
        time.sleep(0.8)
        a = ", ".join(f"{k}={v}" for k, v in step["args"].items())
        print(f"  {BLUE}tool call{END} {BOLD}{step['tool']}{END}({a})")
        print(f"  {DIM}result:{END}    {summary(step['tool'], step['result'])}")
    return {"cause": run["cause"], "evidence": run.get("evidence") or [], "model_calls": run["model_calls"],
            "tool_calls": run["tool_calls"], "stop_reason": run["stop_reason"]}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--incident", choices=list(W.INCIDENTS), default="rows_dropped_by_filter")
    ap.add_argument("--model", default="stub", help="stub, gateway or gateway:<model>")
    ap.add_argument("--replay", action="store_true")
    ap.add_argument("--yes", action="store_true")
    a = ap.parse_args()
    w = W.build(a.incident)
    tools = ShownTools(w)

    print(f"\n{RED}{BOLD}ALARM{END} {w.alarm}\n")
    print(f"{BOLD}1. Runbook (app code, no model){END}")
    rb = runbook(W.TriageTools(w))
    ok = rb["cause"] != "unknown" and verify(rb["cause"], w)
    print(f"  checks: {', '.join(rb['steps'])}  ->  {rb['cause']}" + (f"  {GREEN}confirmed by the data{END}" if ok else
          f"  {AMBER}not confirmed: hand over to the agent{END}"))
    if ok:
        cause, evidence = rb["cause"], ["runbook"]
    else:
        print(f"\n{BOLD}2. Agent (the model chooses each read-only tool call; app code runs it){END}")
        if a.replay:
            ag = replay(a.incident)
        else:
            from triage.stub import make
            ag = run_agent(w.alarm, ShownModel(make(a.model)), tools)
        print(f"  {BOLD}answer:{END} cause = {ag['cause']}")
        for e in ag.get("evidence") or []:
            print(f"    - {str(e)[:110]}")
        print(f"  {DIM}{ag['model_calls']} model calls, {ag['tool_calls']} tool calls; stopped by: {ag['stop_reason']}{END}")
        cause, evidence = ag["cause"] or "unknown", ag.get("evidence") or []

    print(f"\n{BOLD}3. App code checks the cause against the data{END}")
    if cause == "other" or not verify(cause, w):
        print(f"  {AMBER}'{cause}' is not confirmed by the data: a person gets the evidence; nothing is fixed{END}\n")
        return
    print(f"  {GREEN}'{cause}' confirmed{END}")
    fix = W.FIXES[cause]
    print(f"\n{BOLD}4. A person approves the fix{END}  ({fix})")

    def ask(f, params):
        if a.yes:
            print(f"  approved: {params}")
            return True
        return input(f"  approve {f} with {params}? [y/N] ").strip().lower() == "y"

    out = apply_fix(fix, w, ask)
    if out.get("declined"):
        print("  declined: nothing ran\n")
        return
    if "first" in out:
        print(f"\n{BOLD}5. Rerun, started once{END}")
        print(f"  first start: execution {out['first']['name']} -> {out['first'].get('job')}")
        print(f"  a retry with the same name: {out['retry']['note']}")
    print(f"\n{BOLD}6. The alarm's own check runs again{END}: " +
          (f"{GREEN}clear{END}" if w.alarm_clear() else f"{AMBER}still firing{END} (expected for {fix})") + "\n")


if __name__ == "__main__":
    sys.exit(main())
