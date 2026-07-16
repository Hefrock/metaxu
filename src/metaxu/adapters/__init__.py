"""Adapters: attach Metaxu to a specific AI-stack boundary.

The core of Metaxu (event model, artifact, engines) is transport-neutral.
Each adapter observes one interception point and translates what it sees
into assurance events:

- :mod:`metaxu.adapters.mcp` — transparent proxy for MCP stdio servers
  (sees tool calls and retrieved data; blind to claims and answers).
- :mod:`metaxu.adapters.otel` — OpenTelemetry exporter (assurance traces
  in existing observability tooling). Optional: ``pip install metaxu[otel]``;
  imported lazily so the core stays dependency-free.

Planned: OpenTelemetry importer, CDS Hooks, LLM API gateway. No adapter is
privileged: full assurance comes from composing several observers into
one artifact via correlation IDs and `metaxu merge`. Priority order in
``docs/adr/0002-adapter-strategy.md``.
"""

# Only the stdlib-only MCP proxy is imported eagerly; metaxu.adapters.otel
# pulls in the optional opentelemetry dependency and must be imported
# directly by callers that installed the extra.
from .mcp import MCPProxy, run_proxy

__all__ = ["MCPProxy", "run_proxy"]
