"""Incident triage: the runbook, the agent's limits, verification, approval and the once-only rerun."""
import pytest

from triage import world as W
from triage.agent import MAX_MODEL_CALLS, MAX_TOOL_CALLS, runbook, start_rerun, triage, verify
from triage.stub import StubTriageModel


@pytest.mark.parametrize("incident", list(W.INCIDENTS))
def test_each_planted_cause_is_shown_by_its_data_and_no_other(incident):
    w = W.build(incident)
    expected = W.INCIDENTS[incident]["cause"]
    if expected == "other":                       # no listed cause fits, so none may verify
        assert not any(verify(c, w) for c in W.CAUSES)
        return
    assert verify(expected, w)
    others = [c for c in W.CAUSES if c != expected and verify(c, w)]
    assert others == []


def test_runbook_knows_its_three_causes_and_gives_up_or_errs_on_the_rest():
    for incident in W.RUNBOOK_KNOWN:
        assert runbook(W.TriageTools(W.build(incident)))["cause"] == incident
    assert runbook(W.TriageTools(W.build("rows_dropped_by_filter")))["cause"] == "unknown"
    # a wrong all-clear: counts are normal, so the runbook says no_fault; verify refuses it
    out = triage("currency_not_converted", model=None)
    assert out["runbook"]["cause"] == "no_fault" and not out["verified"]


@pytest.mark.parametrize("incident", list(W.INCIDENTS))
def test_stub_agent_path_ends_with_the_alarm_clear_or_escalated(incident):
    out = triage(incident, StubTriageModel(), use_runbook=False)
    assert out["cause"] == W.INCIDENTS[incident]["cause"]
    assert out["verified"] or out["cause"] == "other"
    if out["cause"] == "other":
        assert out["fix"]["applied"] == "escalate" and out["fix"]["evidence_points_at_the_problem"]
        return
    if W.FIXES[out["cause"]] in ("rerun_partition", "change_job_then_rerun", "none"):
        assert out["alarm_clear_after"]


def test_injected_log_line_cannot_get_a_wrong_fix_run():
    out = triage("late_arrival_injected", StubTriageModel("stub-fooled"), use_runbook=False)
    assert out["cause"] == "schema_change" and not out["verified"]
    assert out["fix"]["applied"] is False


def test_limits_stop_a_runaway_agent():
    out = triage("rows_dropped_by_filter", StubTriageModel("stub-runaway"), use_runbook=False)
    assert out["agent"]["stop_reason"] in (f"cap of {MAX_TOOL_CALLS} tool calls", f"cap of {MAX_MODEL_CALLS} model calls")
    assert out["agent"]["tool_calls"] <= MAX_TOOL_CALLS and not out["verified"]


def test_declined_fix_runs_nothing():
    out = triage("late_arrival", None, approve=lambda f, p: False)
    assert out["fix"]["declined"] and not out["alarm_clear_after"]


def test_rerun_starts_once_per_name():
    w = W.build("duplicate_run")
    assert start_rerun(w)["started"] and not start_rerun(w)["started"]
    assert len(w.started_executions) == 1 and w.alarm_clear()


def test_tools_refuse_other_days_and_tables():
    t = W.TriageTools(W.build("no_fault"))
    assert "error" in t.count_rows("prices_curated", "2026-10-01")
    assert t.count_rows("prices_curated", "2026-10-30")["rows_per_store"]["north"] == W.PRODUCTS   # the baseline
    assert t.list_raw_objects("2026-10-30")["Contents"][0]["Key"].count("2026-10-30") == 1
    assert "error" in t.count_rows("shopping_lists")
    assert "error" in t.call("run_sql", {"sql": "select 1"})


def test_runbook_does_not_explain_a_partial_file():
    assert runbook(W.TriageTools(W.build("partial_file")))["cause"] == "unknown"


def test_a_failed_model_call_keeps_the_partial_record():
    class Breaks(StubTriageModel):
        def chat(self, messages, tools=None):
            if len([m for m in messages if m["role"] == "tool"]) >= 2:
                raise RuntimeError("cut at max_tokens")
            return super().chat(messages, tools)
    out = triage("late_arrival", Breaks(), use_runbook=False)
    assert out["agent"]["status"] == "error" and out["agent"]["tool_calls"] == 2


def test_planted_warnings_are_warn_lines():
    for inc in ("currency_not_converted", "rows_dropped_by_filter"):
        warn = W.TriageTools(W.build(inc)).get_run_log("jr_0a1", "WARN")["events"]
        assert warn and all(e["message"].startswith(("WARN", "ERROR")) for e in warn)
