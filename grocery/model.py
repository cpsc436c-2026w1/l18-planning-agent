"""Model calls: the course LLM gateway for real runs, a stub with fixed behaviour for offline runs and tests.

Both have one method, `chat(messages, tools=None) -> ModelReply`, and both keep a list of what each call used
(`usage`), so app code can add up tokens and cost without asking the model.

The gateway speaks the OpenAI chat format: a POST to {GATEWAY_URL}/chat/completions with the course MCP token as
the bearer key. It checks the key, the per-key budget and the rate limit before it forwards the call, and answers
429 when one of them is used up. That budget lives outside this app, so a loop here cannot remove it
(Agentic AI Lens AGENTCOST07-BP01; AI Engineering ch. 10, "Gateway").
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import httpx

# Canadian dollars per million tokens (input, output): a copy of the course gateway's price table
# (ops/llm-gateway-course/overlay.py, read 2026-11-13; provider prices are USD list prices x 1.45).
PRICES_CAD = {"qwen3.8-27b": (0.10, 0.20), "qwen3.6-35b-a3b": (0.05, 0.10),
              "claude-haiku-5-5": (0.145, 0.725), "claude-sonnet-5-5": (2.90, 14.50),
              "gpt-6-luna": (0.145, 0.725), "gpt-6.1-sol": (2.90, 14.50)}


def sampling(model: str) -> dict:
    """temperature 0 for the Qwen models. The provider models on the gateway take only their default
    temperature (a request with temperature 0 came back 502 on 2026-11-13), so nothing is sent for them."""
    return {"temperature": 0} if model.startswith("qwen") else {}


def cost_cad(model: str, tokens_in: int, tokens_out: int) -> float:
    p_in, p_out = PRICES_CAD.get(model, (0.0, 0.0))
    return (tokens_in * p_in + tokens_out * p_out) / 1e6


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class ModelReply:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    model: str = ""


def parse_json(text: str | None) -> dict:
    """The JSON object in a model's reply. Models wrap it in ```json fences or add a sentence; take the braces."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return {}
    try:
        found = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return {}
    return found if isinstance(found, dict) else {}


def assistant_message(reply: ModelReply) -> dict:
    """A model reply with tool calls, in the form the next request sends back."""
    return {"role": "assistant", "content": reply.content,
            "tool_calls": [{"id": c.id, "type": "function",
                            "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
                           for c in reply.tool_calls]}


class GatewayModel:
    """One model on the course LLM gateway. Needs GATEWAY_URL and COURSE_MCP_TOKEN."""

    def __init__(self, base_url: str, token: str, model: str, max_tokens: int = 3000, timeout_s: float = 120.0):
        if not base_url or not token:
            raise SystemExit("set KB_MCP_TOKEN to your course token first (README, \"Run it\")")
        self.model, self.max_tokens = model, max_tokens
        self.usage: list[dict] = []
        self.http = httpx.Client(base_url=base_url, timeout=timeout_s, headers={"Authorization": f"Bearer {token}"})

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> ModelReply:
        body = {"model": self.model, "messages": messages, "max_tokens": self.max_tokens, **sampling(self.model)}
        if tools:
            body["tools"] = tools
        r = self.http.post("/chat/completions", json=body)
        if r.status_code >= 400:
            # 401/403: the key. 429: rate_limit_exceeded or budget_exhausted. Not retried: the run ends here.
            raise RuntimeError(f"course LLM gateway refused the call: HTTP {r.status_code} {r.text[:200]}")
        data = r.json()
        msg = data["choices"][0]["message"]
        if data["choices"][0].get("finish_reason") == "length":
            # Qwen 3.8 reasons before it answers, and the reasoning counts as output tokens
            raise RuntimeError(f"{self.model}: the reply was cut at max_tokens={self.max_tokens}")
        calls = [ToolCall(c["id"], c["function"]["name"], _args(c["function"].get("arguments")))
                 for c in msg.get("tool_calls") or []]
        used = data.get("usage") or {}
        reply = ModelReply(msg.get("content"), calls, used.get("prompt_tokens", 0),
                           used.get("completion_tokens", 0), self.model)
        self.usage.append({"model": self.model, "tokens_in": reply.tokens_in, "tokens_out": reply.tokens_out})
        return reply


def _args(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        found = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return found if isinstance(found, dict) else {}


def usage_total(*models) -> dict:
    """Calls, tokens and cost of every model call these models made."""
    rows = [u for m in {id(m): m for m in models}.values() for u in m.usage]
    return {"model_calls": len(rows), "tokens_in": sum(u["tokens_in"] for u in rows),
            "tokens_out": sum(u["tokens_out"] for u in rows),
            "cost_cad": round(sum(cost_cad(u["model"], u["tokens_in"], u["tokens_out"]) for u in rows), 6)}


# ---- the stub: no network, no key, no cost ------------------------------------------------------------------

STUB_DISHES = [("pasta with tomato sauce", "pasta"), ("tuna rice bowl", "tuna"), ("lentil soup", "soup"),
               ("pizza night", "pizza"), ("gnocchi with pesto", "gnocchi"), ("chicken wraps", "wraps"),
               ("ravioli", "ravioli"), ("noodle stir-fry", "noodles"), ("quiche and salad", "quiche")]


class StubModel:
    """Stands in for the LLM. It reads which call it is from the first words of the system prompt.

    planner: returns `dishes` dishes (9 by default: two more than the cap, so the cut is visible).
    worker:  picks the cheapest product of the search result it was given.
    merger:  lists the picks.
    agent:   searches the two products the request names, then asks to add the cheaper one, then replies.
             With runaway=True it asks for another search on every call and never replies.
    Token counts are estimates (characters / 4), not a tokenizer.
    """

    def __init__(self, model: str = "stub", dishes: int = 9, runaway: bool = False):
        self.model, self.dishes, self.runaway = model, dishes, runaway
        self.usage: list[dict] = []

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> ModelReply:
        system, user = messages[0]["content"], messages[1]["content"]
        role = system.split()[3].strip(".,:")      # "You are the planner ..." -> "planner"
        reply = getattr(self, f"_{role}")(messages, user)
        reply.model = self.model
        reply.tokens_in = sum(len(json.dumps(m)) for m in messages) // 4
        reply.tokens_out = len(reply.content or json.dumps([c.arguments for c in reply.tool_calls])) // 4
        self.usage.append({"model": self.model, "tokens_in": reply.tokens_in, "tokens_out": reply.tokens_out})
        return reply

    def _planner(self, messages, user) -> ModelReply:
        plan = {"dishes": [{"dish": d, "search": s} for d, s in STUB_DISHES[:self.dishes]],
                "filters": {"no_palm_oil": "no palm oil" in user.lower()}}
        return ModelReply(json.dumps(plan))

    def _worker(self, messages, user) -> ModelReply:
        products = parse_json(user).get("products") or []
        best = min(products, key=lambda p: p["price_usd"], default=None)
        return ModelReply(json.dumps({"code": best["code"] if best else None}))

    def _merger(self, messages, user) -> ModelReply:
        week = parse_json(user)
        lines = [f"- {p['dish']}: {p['name']} (${p['price_usd']:.2f})" for p in week.get("picks", []) if p.get("code")]
        verdict = "over" if week.get("over_budget") else "within"
        return ModelReply("\n".join(lines) + f"\nTotal ${week.get('total_usd', 0):.2f}, {verdict} the "
                          f"${week.get('budget_usd', 0):.0f} budget.")

    def _agent(self, messages, user) -> ModelReply:
        n = len(self.usage) + 1
        results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        names = re.search(r"cheaper, (.+?) or (.+?)\?", user)
        wanted = [names.group(1), names.group(2)] if names else ["pasta", "rice"]
        if self.runaway:
            return ModelReply(None, [ToolCall(f"call-{n}", "search_products", {"query": wanted[0]})])
        if len(results) < 2:
            return ModelReply(None, [ToolCall(f"call-{n}", "search_products", {"query": wanted[len(results)]})])
        if len(results) == 2:
            firsts = [r["products"][0] for r in results if r.get("products")]
            if not firsts:
                return ModelReply("I found neither product.")
            best = min(firsts, key=lambda p: p["price_usd"])
            return ModelReply(None, [ToolCall(f"call-{n}", "add_to_list", {"shopper": "?", "code": best["code"]})])
        last = results[-1]
        if last.get("added") or last.get("duplicate_request"):
            return ModelReply(f"I added {last['name']} (${last['price_usd']:.2f}) to your list.")
        return ModelReply("Nothing was added to your list.")


def make_model(kind: str, model: str, **kw):
    """kind 'gateway': the course gateway with the environment's URL and token. kind 'stub': the stub."""
    from grocery import config

    if kind == "stub":
        return StubModel(model=model)
    return GatewayModel(config.GATEWAY_URL, config.token(), model, **kw)
