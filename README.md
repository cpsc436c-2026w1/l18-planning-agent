# CPSC 436C L18: planning and bounded agents, the code from the lecture

This repository holds the demos from L18. Each one runs on your laptop. You can run it with no key at all, using a stub
model, or with your course token through the course LLM gateway. To reuse code in your project, start with the files under "What to borrow" below and keep their app-code checks.
The file [`llms.txt`](llms.txt) describes the repository for a coding assistant: paste its URL
(https://raw.githubusercontent.com/cpsc436c-2026w1/l18-planning-agent/main/llms.txt) into the assistant's chat.

| Demo | What you give it | What it prints | Command (from the repository root) |
|---|---|---|---|
| Planner over the data catalog | a question about a shopper's list and price history | the catalog the planner reads, the plan it returns, the SQL app code compiles, the Data scanned by each query, the answer | `uv run python -m storeplan.show --model gateway` |
| Triage of a failed pipeline night | an alarm on a nightly job | every tool call the model asks for and its result, the cause it names, the code check, the approval (given by `--yes`), the rerun, the recheck | `uv run python -m triage.show --model gateway --yes` |
| Grocery planner (orchestrator-workers) | "Plan dinners for a week under $60, no palm oil." | the planner's dishes, one worker pick for each dish, the total checked in app code | `uv run python -m plain.orchestrator --model gateway` |
| Grocery agent (bounded agent) | "Which is cheaper, spaghetti or penne? Add the cheaper one to my list." | a question before it writes to the list, then its reply and the number of model and tool calls | `uv run python -m plain.agent --model gateway` |

Replace `--model gateway` with `--model stub` to run without a token and without network. For triage, `--replay`
prints a saved live run.

## Set up

1. Install [uv](https://docs.astral.sh/uv/getting-started/installation/), a Python package manager.
2. On your laptop, clone this repository: `git clone https://github.com/cpsc436c-2026w1/l18-planning-agent.git`.
   Then move into it with `cd l18-planning-agent`. Run every command below from this folder.
3. Build the small price lake the catalog demo queries (DuckDB writes Parquet files under `data/lake/`):
   `uv run python -m storeplan.lake`. You should see two lines, one for `prices_raw` and one for `prices_curated`.
4. For the live runs, put your course token in a file named `.env` in the repository root, one line:
   `KB_MCP_TOKEN=<your token>`. The token is in the issue "Your course MCP tokens" in your team repository. The
   file is git-ignored. In our runs, one live demo used well under one cent of the course budget.
5. Check that everything runs offline: `uv run pytest -q` should end with `92 passed`.

## What runs where

| Part | On your laptop | With the AWS backend (`--backend aws`) |
|---|---|---|
| Stores | DuckDB for the product table, the shopping lists and the price lake | DynamoDB, Athena over S3, the Glue Data Catalog, in an AWS account set up with `storeplan/aws.py` |
| Model calls | the course LLM gateway with your token, or the stub | the same gateway |
| Hosting | your terminal | the lecture showed the same agents on Amazon Bedrock AgentCore Runtime; that deployment is not in this repository |

`storeplan/aws.py` creates the AWS side in an account you control, if your project has one: an S3 prefix, a Glue
database with the two lake tables and their descriptions, an Athena workgroup with a 10 MB limit per query.

## What to borrow for your project

- **A planner that reads a catalog and returns a plan, not SQL.** `storeplan/mechanisms.py` (`make_plan`,
  `run_plan`) and `storeplan/plan.py` (`validate`, `compile_step`, `merge`). App code checks the plan against the
  catalog, compiles each step, checks each step's estimated Data scanned against a limit, and merges the parts. The model never writes SQL.
- **Catalog text from Glue.** `storeplan/catalog.py` (`from_glue_with_meaning`, `for_prompt`) reads each table's
  description and each column's comment and turns them into the planner's input.
- **A bounded agent.** `plain/agent.py`: limits that app code checks before every call, approval before the write,
  a request key (the same identifier on every retry of one request) so a retried write applies once.
- **Runbook first, agent second.** `triage/agent.py`: the runbook is app code that checks the causes of past incidents. Code checks the causes it knows, the agent reads only when
  code cannot settle the alarm, and app code confirms the named cause before any fix.
- **Narrow tools.** `triage/world.py` (`TriageTools`, `TOOL_SPECS`): read-only tools that each read one thing,
  return a small result and refuse anything outside their scope. `grocery/tools.py`: three read tools and one write
  tool that runs only after approval. No tool runs free SQL.

## Data

`data/products.jsonl` and the price lake built from it are derived from Open Food Facts and Open Prices, under the
Open Database License (ODbL) 1.0: see [`data/DATA-LICENSE.md`](data/DATA-LICENSE.md). The prices in the lake are
illustrative, not observed prices, and the stores are not real shops.
