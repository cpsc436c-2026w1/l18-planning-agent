# The grocery agents on Amazon Bedrock AgentCore

This folder holds the code that the lecture's hosted version runs. Read it next to the laptop versions in
`plain/` and `with_strands/`.

| File | What it does |
|---|---|
| `runtime/main.py` | The AgentCore Runtime entry point. It runs the two Strands agents from `with_strands/`. A `plan` call runs the dinner planner. An `add` call runs the bounded agent; when that agent asks for approval, it waits in the session until an `approve` call answers. |
| `runtime/requirements.txt` | The packages in the Runtime's code bundle. |
| `lambda/handler.py` | The four grocery tools as one Lambda function, over the DynamoDB tables. AgentCore Gateway calls it for every tool call. |
| `lambda/tools.json` | The tool definitions that the Gateway offers the agent. |
| `../with_strands/gateway_tools.py` | Calls the Gateway's tools from inside the Runtime. |
| `../menu/` | Optional: after a person approves, it emails the week's menu through Amazon SNS. |

Model calls go to the course LLM gateway. The Runtime reads the course token from AWS Systems Manager Parameter Store
at its first call.

Deploying this code needs AWS resources that this repository does not create: the DynamoDB tables, the Lambda
function, a Gateway with that function as its target, a Runtime with an IAM role, and the token parameter. To
create them in your own account, follow the [`agent-agentcore`](https://github.com/cpsc436c-2026w1/recipes/tree/main/agent-agentcore)
recipe. It builds the same structure step by step, with requirements, tests and clean-up.
