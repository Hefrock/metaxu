"""Adapters: attach Metaxu to a specific AI-stack boundary.

The core of Metaxu (event model, artifact, engines) is transport-neutral.
Each adapter observes one interception point and translates what it sees
into assurance events:

- :mod:`metaxu.adapters.mcp` — transparent proxy for MCP stdio servers
  (sees tool calls and retrieved data; blind to claims and answers).

Planned: OpenTelemetry bridge, CDS Hooks, LLM API gateway. No adapter is
privileged: full assurance comes from composing several observers into
one artifact via correlation IDs and `metaxu merge`.
"""

from .mcp import MCPProxy, run_proxy

__all__ = ["MCPProxy", "run_proxy"]
