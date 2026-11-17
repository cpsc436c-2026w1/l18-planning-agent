"""One trace per run, from the spans Strands emits (OpenTelemetry; Strands docs, "Traces").

Strands opens a span for every agent invocation, every loop cycle, every model call and every tool call. Here
the spans are kept in memory, the whole run sits under one parent span, and the run's trace is saved as JSON
lines next to the result: the same job the trace items do in the plain-Python version.
"""
from __future__ import annotations

from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from strands.telemetry import StrandsTelemetry

from grocery.model import cost_cad

_exporter: InMemorySpanExporter | None = None


def setup() -> InMemorySpanExporter:
    global _exporter
    if _exporter is None:
        provider = TracerProvider()
        _exporter = InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(_exporter))
        StrandsTelemetry(tracer_provider=provider)      # Strands' spans go to this provider
        trace.set_tracer_provider(provider)
    return _exporter


@contextmanager
def run_span(name: str, **attributes):
    """The parent span of one run: every agent, model and tool span below it carries the same trace id."""
    exporter = setup()
    exporter.clear()
    with trace.get_tracer("grocery").start_as_current_span(name, attributes=attributes) as span:
        yield format(span.get_span_context().trace_id, "032x")


def items() -> list[dict]:
    """The finished spans of the run, oldest first, as small dicts."""
    spans = sorted(setup().get_finished_spans(), key=lambda s: s.start_time)
    out = []
    for s in spans:
        a = dict(s.attributes or {})
        tokens = (a.get("gen_ai.usage.input_tokens"), a.get("gen_ai.usage.output_tokens"))
        cost = (round(cost_cad(a.get("gen_ai.request.model", ""), *tokens), 6)
                if s.name == "chat" and None not in tokens else None)
        out.append({"trace_id": format(s.context.trace_id, "032x"), "span": s.name,
                    "ms": round((s.end_time - s.start_time) / 1e6),
                    "model": a.get("gen_ai.request.model"), "tool": a.get("gen_ai.tool.name"),
                    "tokens_in": a.get("gen_ai.usage.input_tokens"),
                    "tokens_out": a.get("gen_ai.usage.output_tokens"), "cost_cad": cost})
    return out
