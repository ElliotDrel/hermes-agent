"""Adapter-owned session tool overlays, bound around foreground construction and execution."""
from contextlib import nullcontext


def platform_tool_scope(turn):
    adapter = turn._runner._delivery_adapter_for(turn._ctx.source)
    factory = getattr(adapter, 'session_tool_scope', None)
    return factory(turn) if callable(factory) else nullcontext()
