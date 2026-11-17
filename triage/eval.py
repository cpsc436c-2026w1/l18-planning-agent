"""Score incident triage on every planted incident, three ways:

    runbook          code only (no model call); counted correct only when its cause is the planted one
    agent            the bounded agent alone, N runs per incident
    runbook+agent    the runbook first; the agent only when the data does not confirm the runbook's cause

The agent sees the same alarm and the same data with or without the runbook before it (the runbook's reads
change nothing but the Data scanned counter), so runbook+agent reuses the agent's runs instead of new calls.
Expected causes are fixed in triage/world.py before any live run; the cause is an enum and code scores it.

    uv run python -m triage.eval --model stub
    uv run python -m triage.eval --model gateway --runs 3      (about 8 x 3 agent runs, at most 10 calls each)
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone

from grocery import config
from grocery.model import usage_total
from triage import world as W
from triage.agent import triage
from triage.stub import make


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default="stub")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--out", default=None)
    ap.add_argument("--incidents", nargs="*", default=None, help="only these incidents")
    a = ap.parse_args()
    model = make(a.model)
    rows = []
    for inc in (a.incidents or list(W.INCIDENTS)):
        rb = triage(inc, None)
        agent_runs = []
        for _ in range(a.runs):
            trace: list = []
            try:
                r = triage(inc, model, use_runbook=False, trace=trace)
            except RuntimeError as e:                           # the runbook path never calls the model                           # a refused or cut call: counted, not hidden
                r = {"cause": "error", "verified": False, "agent": {"stop_reason": str(e)[:160],
                     "model_calls": 0, "tool_calls": 0, "tools_used": []}, "fix": {}, "alarm_clear_after": False}
            ag = r.get("agent") or {}
            agent_runs.append({"cause": r["cause"], "verified": r["verified"],
                               "escalated_with_evidence": (r.get("fix") or {}).get("evidence_points_at_the_problem"),
                               "evidence": ag.get("evidence"),
                               "stop_reason": r["agent"]["stop_reason"], "model_calls": r["agent"]["model_calls"],
                               "tool_calls": r["agent"]["tool_calls"], "tools_used": r["agent"]["tools_used"],
                               "proposed_fix": r["agent"].get("fix"),
                               "fix_applied": (r.get("fix") or {}).get("applied"),
                               "alarm_clear_after": r.get("alarm_clear_after"), "trace": trace})
        expected = W.INCIDENTS[inc]["cause"]
        for x in agent_runs:                       # "other" counts only with evidence that points at the problem
            x["right"] = x["cause"] == expected and (expected != "other" or bool(x["escalated_with_evidence"]))
        rb_ok = rb["runbook"]["cause"] == expected
        rows.append({"incident": inc, "expected": expected, "known_to_runbook": inc in W.RUNBOOK_KNOWN,
                     "runbook_cause": rb["runbook"]["cause"], "runbook_correct": rb_ok,
                     "runbook_confirmed": rb["runbook"]["verified"],
                     "agent_correct": sum(x["right"] for x in agent_runs),
                     "agent_wrong_but_refused": sum(x["cause"] != expected and not x["verified"] for x in agent_runs),
                     "agent_wrong_and_confirmed": sum(x["cause"] != expected and x["verified"] for x in agent_runs),
                     "hybrid_correct": a.runs if rb["runbook"]["verified"] and rb_ok
                     else sum(x["right"] for x in agent_runs),
                     "hybrid_model_calls": 0 if rb["runbook"]["verified"]
                     else sum(x["model_calls"] for x in agent_runs),
                     "runs": agent_runs})
        print(f"{inc:24} runbook {rb['runbook']['cause']:23} agent "
              f"{Counter(x['cause'] for x in agent_runs).most_common()}")
    usage = usage_total(model) if hasattr(model, "usage") and a.model.startswith("gateway") else {}
    out = {"model": a.model if a.model.startswith("stub") else getattr(model, "model", a.model),
           "runs_per_incident": a.runs, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "usage": usage, "rows": rows}
    path = a.out or config.ROOT / "output" / f"triage-eval-{a.model.replace(':', '-')}.json"
    with open(path, "w") as f:
        json.dump(out, f, indent=1)
    print(f"saved {path}; usage {usage}")


if __name__ == "__main__":
    main()
