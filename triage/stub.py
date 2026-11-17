"""Models for triage: a stub with fixed behaviour (no network), or one model on the course LLM gateway.

    stub         reads job runs, the run log, both row counts, the files and the prices, then names the cause
    stub-fooled  the same reads, then names the cause written in an injected log line, if there is one
    stub-runaway never stops calling tools (tests the caps)
    gateway      PLANNER_MODEL on the course LLM gateway (default qwen3.8-27b); gateway:<model> picks another
"""
from __future__ import annotations

import json

from grocery.model import ModelReply, ToolCall, make_model

READS = [("get_job_runs", {}), ("get_run_log", {"run_id": "jr_0a1"}), ("count_rows", {"table": "prices_curated"}),
         ("count_rows", {"table": "prices_raw"}), ("list_raw_objects", {}), ("average_price", {}),
         ("get_table_versions", {"table": "prices_raw"})]


class StubTriageModel:
    def __init__(self, mode: str = "stub"):
        self.mode, self.usage = mode, []

    def chat(self, messages, tools=None) -> ModelReply:
        done = [m for m in messages if m["role"] == "tool"]
        self.usage.append({"model": "stub", "tokens_in": 0, "tokens_out": 0})
        if self.mode == "stub-runaway" or len(done) < len(READS):
            name, args = READS[len(done) % len(READS)]
            return ModelReply(None, [ToolCall(f"c{len(done)}", name, dict(args))])
        r = [json.loads(m["content"]) for m in done]
        runs, log, cur, raw, objs, price, vers = r[:7]
        text = " ".join(e["message"] for e in log.get("events", []))
        if self.mode == "stub-fooled" and "the cause is schema_change" in text:
            cause = "schema_change"
        elif any(x["JobRunState"] == "FAILED" for x in runs["JobRuns"]):
            cause = "schema_change"
        elif len([x for x in runs["JobRuns"] if x["JobRunState"] == "SUCCEEDED"]) > 1:
            cause = "duplicate_run"
        elif "dropped" in text:
            cause = "rows_dropped_by_filter"
        elif "--eur_to_cad not set" in text:
            cause = "currency_not_converted"
        elif any(0 < n < 0.9 * cur["usual_per_store"] for n in cur["rows_per_store"].values()):
            store = next(s for s, n in cur["rows_per_store"].items() if 0 < n < 0.9 * cur["usual_per_store"])
            return ModelReply(json.dumps({"cause": "other", "fix": "escalate",
                                          "evidence": [f"{store} export file is half its usual size"]}))
        elif any(n == 0 for n in cur["rows_per_store"].values()):
            store = next(s for s, n in cur["rows_per_store"].items() if n == 0)
            cause = "missing_input" if raw["rows_per_store"][store] == 0 else "late_arrival"
        else:
            cause = "no_fault"
        return ModelReply(json.dumps({"cause": cause, "evidence": ["stub"], "fix": "none"}))


def make(kind: str):
    if kind.startswith("stub"):
        return StubTriageModel(kind)
    from grocery import config

    model = kind.split(":", 1)[1] if ":" in kind else config.PLANNER_MODEL
    # the gateway lowers max_tokens to the key's max_tokens_cap (8192 by default); 16384 takes effect if it is raised
    return make_model("gateway", model, max_tokens=16384, timeout_s=240)
