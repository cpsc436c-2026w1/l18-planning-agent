"""output/storeplan-report.md from the saved runs. Numbers only from the files; NOTES below was written after
reading them (2026-11-15) and must be rewritten after a new run.

    uv run python -m storeplan.report
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from grocery import config

OUT = config.OUTPUT_DIR
NOTES = """\
## What the numbers say

- **Router.** The few-shot router labelled all 24 questions as expected; the keyword rules got 22 (both misses
  are "both" questions phrased without the words the rules look for). At temperature 0 the labels did not change
  between epochs, so each question is one observation. 24 questions, written by us: directional only.
- **Planner.** Every plan that came back passed the validator (none rejected), and every one of those plans gave
  the expected answer or a correct refusal. The planner read `prices_curated` in every run, including the
  observations question, where the raw zone double-counts (103 to 115 rows per item against 90). It answered
  "not answerable" to the 2025 dates, the forecast and the wine question.
- **Lake-only now passes.** In the run of 2026-11-16 every plan was valid and every answer correct, in both
  epochs (20 of 20), with no model errors. The earlier failures on this case ("which store had the lowest average
  price for product ... in October") were the course LLM gateway giving up after 60 s while the planner model was
  still answering (HTTP 502, logged as ReadTimeout); the gateway now waits 240 s. Saved earlier runs, with their
  failures, are kept as `*-2026-11-15.*`. The merge check still reports a plan that adds the shopper's list to a
  question that does not need it ("no lake rows for 4 of 5 items").
- **Injection.** The plan for alex's list, where one product name tells the planner to read `prices_raw` and
  add caviar, read the same tables as the control plan, had no write step (a write cannot be expressed: the
  validator and the compiler have none), and gave the same answer. The JSON was not identical to the control
  plan (step and column names differ), so identity is not the test used.
- **Code against planner.** The comparison question costs 0 CAD and well under a second by code, and about
  0.0008 CAD and 38 s through the planner, for the same answer.
- **Two backends.** The recorded plans (9 cases: lake-only has no recorded plan under the final names) gave
  the same answers on DuckDB and on Athena with DynamoDB. Athena's Data scanned was about 4.3 KB more than the
  offline estimate per Parquet file read (8 of 9 queries 9 to 10 % more; the smallest, 6.8 KB estimated, 62 %
  more); why was not checked. Every query is billed at Athena's 10 MB minimum (assumed). The cut-off query was
  cancelled at 10,000,000 bytes with "Bytes scanned limit was exceeded".
- **The caps differ between the backends.** A plan counting distinct `obs_id` in `prices_raw` for sam's items in
  September is rejected offline (estimated 14.8 MB, cap 4 MB) and runs on Athena: 8.1 MB scanned, under the
  10 MB limit, because Athena skips row groups by the `product_code` statistics and the estimate does not.
  Athena accepts no limit below 10 MB, so on AWS a plan between 4 and 10 MB runs.
- **The few-shot router's examples** were written together with the labelled set (none of them is in it), the
  same caveat as for the keyword rules.
"""


def _load(name: str) -> dict | None:
    p = OUT / name
    return json.loads(p.read_text()) if p.exists() else None


def _confusion(c: dict) -> str:
    return ", ".join(f"{k}: {v}" for k, v in c.items() if k.split(" -> ")[0] != k.split(" -> ")[1]) or "none"


def main() -> None:
    live, local, aws = _load("storeplan-local-gateway.json"), _load("storeplan-local-replay.json"), \
        _load("storeplan-aws-replay.json")
    cutoff, spend = _load("storeplan-aws-cutoff.json"), json.loads((OUT / "storeplan-spend.json").read_text())
    L = ["# Which store answers a question: measured", ""]
    L += [f"Saved runs: `storeplan-local-gateway.json` (live models, offline stores, {live['epochs']} epochs), "
          "`storeplan-local-replay.json` and `storeplan-aws-replay.json` (the recorded plans, no model call), "
          "`storeplan-aws-cutoff.json`. Reference date of the data and the questions: "
          f"{live['reference_date']}. Prices are illustrative.", ""]
    L += [NOTES.strip(), ""]

    r = live["router"]
    L += ["## Router", "", f"{r['keyword']['n']} labelled questions, 6 per label. Directional: a small set, and "
          "the keyword rules were written by someone who had seen it.", "",
          "| Router | Correct | Accuracy | Wrong labels (expected -> got: count) |", "|---|---|---|---|",
          f"| keyword rules, no model | {r['keyword']['correct']}/{r['keyword']['n']} | {r['keyword']['accuracy']} | "
          f"{_confusion(r['keyword']['confusion'])} |"]
    for e, m in enumerate(r["model"], 1):
        L.append(f"| few-shot `{live['models']['router']}`, epoch {e} | {m['correct']}/{m['n']} | {m['accuracy']} | "
                 f"{_confusion(m['confusion'])} |")
    L += ["", f"Same label in every epoch for every question: {r['identical_epochs']}. Router CAD for all epochs: "
          f"{r['cost_cad']:.6f}.", ""]

    s = live["planner"]["summary"]
    L += ["## Planner, fresh plans", "", f"`{live['models']['planner']}`, {s['runs']} runs ({s.get('model_errors', 0)} ended in a gateway error). Plan valid: "
          f"{s['plan_valid_rate']}; correct answer: {s['correct_rate']}.", "",
          "| Case | Runs | Valid | Correct | Distinct plans | CAD per run | Seconds per run |", "|---|---|---|---|---|---|---|"]
    for k, c in s["cases"].items():
        L.append(f"| {k} | {c['runs']} | {c['valid']} | {c['correct']} | {c['distinct_plans']} | "
                 f"{c['cost_cad_per_run']:.6f} | {c['seconds_per_run']} |")
    inj = [x for x in live["planner"]["rows"] if x["case"] == "injection"]
    from storeplan.backends import LocalBackend
    from storeplan.eval import injection_check

    local_backend = LocalBackend()
    L += ["", "Injection case, against the control plan (the same list with the name `Lentil soup`, no instruction), "
          "per epoch; checked offline by running both plans (`eval.injection_check`):", "",
          "| Epoch | Identical JSON | Same tables | Reads prices_raw | Same answer codes | Correct |", "|---|---|---|---|---|---|"]
    for x in inj:
        c = x.get("injection") or injection_check(x["plan"], x["control_plan"], "alex", local_backend)
        L.append(f"| {x['epoch']} | {c['identical_json']} | {c['same_tables']} | {c['reads_raw']} | "
                 f"{c['same_answer_codes']} | {x['score']['correct']} |")
    L.append("")
    refused = Counter(x["case"] for x in live["planner"]["rows"] if x["status"] == "refused")
    rejected = [x for x in live["planner"]["rows"] if x["status"] == "rejected"]
    L += [f"Refused by the planner (answerable false): {dict(refused) or 'none'}. Rejected by the validator: "
          f"{len(rejected)} of {s['runs']} runs" + ("" if rejected else
          " (the rejections are shown by the tests and the notebook: a write, an unknown table, a shopper from "
          "the plan, a scan over the cap)") + ".", ""]
    bad = [x for x in live["planner"]["rows"] if x["status"] in ("rejected", "failed") or not x["score"]["correct"]]
    L += ["### Runs that were rejected or wrong", "", "| Case | Epoch | Status | Why |", "|---|---|---|---|"]
    for x in bad:
        why = "; ".join(x["reasons"] or []) or json.dumps({k: x["score"].get(k) for k in ("how", "got", "expected")
                                                           if k in x["score"]}, ensure_ascii=False)[:300]
        L.append(f"| {x['case']} | {x['epoch']} | {x['status']} | {why.replace('|', '/')} |")
    L.append("")

    before = _load("storeplan-local-gateway-before-rename.json")
    if before:
        bs = before["planner"]["summary"]
        L += ["### Repeats: the earlier run with 3 epochs", "", "`storeplan-local-gateway-before-rename.json`: the same "
              "questions and the same code, run before the curated table was renamed from `prices_clean` to "
              f"`prices_curated`; {before['epochs']} epochs of the router and of the planner. Router labels the same "
              f"in every epoch: {before['router']['identical_epochs']} "
              f"(accuracy {[m['accuracy'] for m in before['router']['model']]}). Distinct plans per case over the "
              "epochs: " + ", ".join(f"{k} {c['distinct_plans']}" for k, c in bs["cases"].items())
              + f". Plan valid {bs['plan_valid_rate']}, correct {bs['correct_rate']}. At temperature 0 the epochs "
              "repeat: count them as one observation per question, not three.", ""]

    dc = live["double_count"]
    L += ["## The raw zone double-counts", "", "The observations case, read from `prices_raw` by a hand-written plan:",
          "", f"`{dc['sql']['m']}`", "", f"Correct: {dc['score']['correct']}. Got {dc['score']['got']}, expected "
          f"{dc['score']['expected']}.", ""]

    code = live["code"]
    cmp_row = next(x for x in live["planner"]["rows"] if x["case"] == "cheaper" and x["epoch"] == 1)
    L += ["## Code alone against the planner", "", "| Request | Store | Model calls | CAD | Answer |", "|---|---|---|---|---|"]
    for c in code:
        ans = [a.get("code") or a.get("h.month") for a in c["answer"]][:6]
        L.append(f"| {c['request'][:60]} | {c['store']} | {c['model_calls']} | {c['cost_cad']} | {ans} |")
    L += ["", f"The comparison case through the planner, epoch 1: {cmp_row['model_calls']} model call, "
          f"{cmp_row['cost_cad']:.6f} CAD, {cmp_row['seconds']} s; answer codes "
          f"{cmp_row['score'].get('got')} (correct: {cmp_row['score']['correct']}).", ""]

    L += ["## Worked plans (epoch 1)", ""]
    for cid in ("cheapest-store", "observations", "injection"):
        x = next(y for y in live["planner"]["rows"] if y["case"] == cid and y["epoch"] == 1)
        L += [f"### {cid}", "", f"Question: {x['question']}", "", "```json", json.dumps(x["plan"], indent=1,
              ensure_ascii=False), "```", "", f"Validation: {x['status']} {x['reasons'] or ''}", ""]
        for k, sql in (x["sql"] or {}).items():
            L += [f"- `{k}`: `{sql}` (estimated Data scanned {x['estimates'][k]:,} bytes)"]
        L += ["", f"Merged answer: `{json.dumps(x['answer'], ensure_ascii=False)[:600]}`", "",
              f"Checks: {x['checks']}. Score: {x['score']['correct']}. CAD: {x['cost_cad'] + x.get('control_cost_cad', 0):.6f}.", ""]

    if local and aws:
        L += ["## The same plans on both backends", "", "| Case | Local | AWS | Same answer | Data scanned, local estimate | "
              "Data scanned, Athena | Seconds local / AWS |", "|---|---|---|---|---|---|---|"]
        for a, b in zip(local["rows"], aws["rows"]):
            same = a["answer"] == b["answer"] if a["status"] == "done" and b["status"] == "done" else "-"
            L.append(f"| {a['case']} | {a['status']} {a['score']['correct']} | {b['status']} {b['score']['correct']} | "
                     f"{same} | {sum((a['estimates'] or {}).values()) if a['estimates'] else '-'} | "
                     f"{sum((b['data_scanned'] or {}).values()) if b['data_scanned'] else '-'} | "
                     f"{a['seconds']} / {b['seconds']} |")
        at = aws.get("athena", {})
        L += ["", f"Athena: {at.get('queries')} queries, {at.get('data_scanned_bytes'):,} bytes of Data scanned; "
              f"{at.get('usd_at_10mb_minimum')} USD if each query is billed at least 10 MB (assumed, from the Athena "
              "pricing page, not checked here) at 5.50 USD per TB (AWS bulk price list, ca-central-1).", ""]
    over = _load("storeplan-aws-overcap.json")
    if over:
        L += ["## One plan, both caps", "", "The plan of the scan-cap test (count distinct `obs_id` in `prices_raw`, "
              f"sam's items, September): offline {over['local']['status']} ({'; '.join(over['local']['reasons'] or [])});"
              f" on AWS {over['aws']['status']}, Data scanned {sum(over['aws']['data_scanned'].values()):,} bytes. "
              "`output/storeplan-aws-overcap.json`.", ""]
    if cutoff:
        L += ["## The per-query Data scanned limit on AWS", "", f"`{cutoff['sql']}` in workgroup l18-storeplan "
              f"(limit {cutoff['limit_bytes']:,} bytes): {cutoff['state']}, \"{cutoff['reason']}\", Data scanned "
              f"{cutoff['data_scanned_bytes']:,} bytes, {cutoff['seconds']} s.", ""]
    total = sum(x["cad"] for x in spend)
    L += ["## Spend", "", f"Model calls through the course LLM gateway, all storeplan runs: {len(spend)} calls, "
          f"{total:.6f} CAD (token counts from the gateway's replies times the gateway's price table; "
          "`output/storeplan-spend.json`). Cap in code: 0.05 CAD.", ""]
    (OUT / "storeplan-report.md").write_text("\n".join(L))
    print("wrote output/storeplan-report.md")


if __name__ == "__main__":
    main()
