"""One trace per run: every model call and every tool call becomes one item under the run's trace id.

The plain-Python versions get their trace from two wrappers, so plain/orchestrator.py and plain/agent.py hold
no tracing code. The Strands version gets its trace from OpenTelemetry spans (with_strands/tracing.py).
"""
from __future__ import annotations

import json
import time
import uuid

from grocery.model import cost_cad

TOOL_NAMES = ("search_products", "get_product", "get_list", "add_to_list")


class Trace:
    def __init__(self):
        self.trace_id = uuid.uuid4().hex
        self.t0 = time.monotonic()
        self.items: list[dict] = []

    def add(self, kind: str, name: str, started: float, **detail) -> None:
        self.items.append({"trace_id": self.trace_id, "step": len(self.items) + 1, "kind": kind, "name": name,
                           "start_ms": round((started - self.t0) * 1000), "ms": round((time.monotonic() - started) * 1000),
                           **detail})

    def seconds(self) -> float:
        return round(time.monotonic() - self.t0, 1)


class TracedModel:
    def __init__(self, inner, trace: Trace):
        self.inner, self.trace, self.model, self.usage = inner, trace, inner.model, inner.usage

    def chat(self, messages, tools=None):
        started = time.monotonic()
        reply = self.inner.chat(messages, tools)
        self.trace.add("model_call", reply.model, started, tokens_in=reply.tokens_in, tokens_out=reply.tokens_out,
                       cost_cad=round(cost_cad(reply.model, reply.tokens_in, reply.tokens_out), 6),
                       asked_for=[c.name for c in reply.tool_calls] or "reply")
        return reply


class TracedTools:
    """Wraps GroceryTools: each of the four tools, called directly or through call(), leaves one item."""

    def __init__(self, inner, trace: Trace):
        self.inner, self.trace = inner, trace

    def call(self, name: str, args: dict) -> dict:
        return getattr(self, name)(**args) if name in TOOL_NAMES else self.inner.call(name, args)

    def __getattr__(self, name):
        tool = getattr(self.inner, name)
        if name not in TOOL_NAMES:
            return tool

        def traced(*args, **kwargs):
            started = time.monotonic()
            try:
                result = tool(*args, **kwargs)
            except TypeError as e:
                result = {"error": f"wrong arguments for {name}: {e}"}
            self.trace.add("tool_call", name, started, arguments=json.dumps([*args, kwargs], default=str)[:200],
                           result=json.dumps(result, ensure_ascii=False, default=str)[:200])
            return result

        return traced
