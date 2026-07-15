"""Transparent MCP assurance proxy.

Sits between any MCP client (agent host) and any MCP server speaking the
stdio transport, forwarding JSON-RPC messages byte-for-byte while
recording an assurance session on the side. Adoption is a config change,
not a code change — in the client's MCP configuration, replace::

    "command": "my-fhir-server", "args": ["--port", "..."]

with::

    "command": "metaxu",
    "args": ["mcp-proxy", "--out", "artifacts/", "--", "my-fhir-server", "--port", "..."]

What the proxy records per session (one artifact per server process):

* every ``tools/call`` — name, arguments, result summary, errors, timing;
* provenance for every successful tool result and ``resources/read``
  (source system, content hash, retrieval time), with optional snapshots
  so ``metaxu verify`` can later detect source drift;
* client/server identity and protocol version (from ``initialize``) into
  the reproducibility block;
* declarative policy evaluation, driven by a tag map
  (``--tags tags.json``: ``{"get_allergies": ["allergy_check"]}``) so
  institution policies match tool calls without knowing tool names.

What a transparent proxy **cannot** attest: claims, evidence links, and
the final answer never cross the MCP wire, so those artifact sections are
empty and the trust dimensions derived from them score accordingly (the
rationale strings say why). The ``missing_answer`` safety check is
disabled for proxy sessions since answers are structurally out of view.
Full assurance still requires SDK instrumentation in the agent; the proxy
is the zero-effort floor, not the ceiling.

Forwarding is failure-proof by construction: recording errors are caught
and reported to stderr, never allowed to drop or mutate a message.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from typing import Any, BinaryIO

from .events import utcnow
from .policy import PolicyEngine
from .provenance import ProvenanceRecord
from .replay import save_snapshot
from .safety import DEFAULT_CHECKS, SafetyEngine, check_missing_answer
from .session import AssuranceSession


class MCPProxy:
    """Observes an MCP stdio conversation and builds an assurance session.

    The message handlers are pure with respect to I/O (they only mutate
    the session), so they can be unit-tested without subprocesses; the
    pumping loop lives in :func:`run_proxy`.
    """

    def __init__(
        self,
        server_command: list[str],
        question: str | None = None,
        policy_engine: PolicyEngine | None = None,
        tag_map: dict[str, list[str]] | None = None,
        snapshot_dir: str | None = None,
    ):
        self.server_command = server_command
        self.server_label = "mcp:" + (server_command[0] if server_command else "unknown")
        self.tag_map = tag_map or {}
        self.snapshot_dir = snapshot_dir
        # Answers never cross the MCP wire; flagging their absence on
        # every proxy artifact would be noise, not signal.
        safety = SafetyEngine([c for c in DEFAULT_CHECKS if c is not check_missing_answer])
        self.session = AssuranceSession(
            question=question or f"MCP session: {' '.join(server_command)}",
            policy_engine=policy_engine,
            safety_engine=safety,
            metadata={
                "dev.metaxu/observer": "mcp-proxy",
                "dev.metaxu/server_command": server_command,
                "dev.metaxu/started_at": utcnow(),
            },
        )
        self._pending: dict[Any, dict[str, Any]] = {}
        self._lock = threading.Lock()

    # -- message handlers (called with parsed JSON-RPC objects) -----------

    def observe_client_message(self, msg: Any) -> None:
        """A message travelling client -> server."""
        with self._lock:
            for item in _as_messages(msg):
                self._client_message(item)

    def observe_server_message(self, msg: Any) -> None:
        """A message travelling server -> client."""
        with self._lock:
            for item in _as_messages(msg):
                self._server_message(item)

    def _client_message(self, msg: dict[str, Any]) -> None:
        method = msg.get("method")
        msg_id = msg.get("id")
        params = msg.get("params") or {}
        if method is None or msg_id is None:
            return  # response or notification; nothing to track
        if method == "initialize":
            client_info = params.get("clientInfo") or {}
            self.session.reproducibility["mcp_client"] = client_info
            self.session.reproducibility["mcp_protocol_version"] = params.get(
                "protocolVersion"
            )
            self._pending[msg_id] = {"kind": "initialize"}
        elif method == "tools/call":
            self._pending[msg_id] = {
                "kind": "tool",
                "name": params.get("name", "unknown"),
                "arguments": params.get("arguments") or {},
                "start": time.perf_counter(),
            }
        elif method == "resources/read":
            self._pending[msg_id] = {
                "kind": "resource",
                "uri": params.get("uri", "unknown"),
                "start": time.perf_counter(),
            }

    def _server_message(self, msg: dict[str, Any]) -> None:
        msg_id = msg.get("id")
        if msg_id is None or msg_id not in self._pending:
            return
        call = self._pending.pop(msg_id)
        if call["kind"] == "initialize":
            result = msg.get("result") or {}
            server_info = result.get("serverInfo") or {}
            self.session.reproducibility["mcp_server"] = server_info
            if server_info.get("name"):
                self.server_label = "mcp:" + server_info["name"]
            return

        duration_ms = (time.perf_counter() - call["start"]) * 1000
        result = msg.get("result")
        error = _extract_error(msg)

        if call["kind"] == "tool":
            name = call["name"]
            tags = self.tag_map.get(name, [])
            tool_event = self.session.record_tool_call(
                name=name,
                arguments=call["arguments"],
                result=None if error else _content_text(result),
                error=error,
                tags=tags,
                duration_ms=duration_ms,
                version=self.session.reproducibility.get("mcp_server", {}).get("version"),
            )
            if not error and result is not None:
                self._record_provenance(
                    resource_type="MCPToolResult",
                    resource_id=f"{name}#{msg_id}",
                    content=result,
                    tags=tags,
                    parent_id=tool_event.id,
                )
        elif call["kind"] == "resource" and not error and result is not None:
            self._record_provenance(
                resource_type="MCPResource",
                resource_id=call["uri"],
                content=result,
                tags=self.tag_map.get(call["uri"], []),
                parent_id=None,
            )

    def _record_provenance(
        self,
        resource_type: str,
        resource_id: str,
        content: Any,
        tags: list[str],
        parent_id: str | None,
    ) -> None:
        record = ProvenanceRecord.for_resource(
            source_system=self.server_label,
            resource_type=resource_type,
            resource_id=resource_id,
            content=content,
        )
        self.session.record_retrieval(record, tags=tags, parent_id=parent_id)
        if self.snapshot_dir:
            save_snapshot(self.snapshot_dir, record, content)

    # -- finalization ------------------------------------------------------

    def finalize(self):
        with self._lock:
            for call in self._pending.values():
                if call["kind"] == "tool":
                    self.session.record_tool_call(
                        name=call["name"],
                        arguments=call["arguments"],
                        error="no response before session ended",
                        tags=self.tag_map.get(call["name"], []),
                    )
            self._pending.clear()
            return self.session.finalize()


def _as_messages(msg: Any) -> list[dict[str, Any]]:
    """Normalize a JSON-RPC payload (single or batch) to a list of dicts."""
    if isinstance(msg, dict):
        return [msg]
    if isinstance(msg, list):
        return [m for m in msg if isinstance(m, dict)]
    return []


def _extract_error(msg: dict[str, Any]) -> str | None:
    """JSON-RPC error object, or MCP tool-level isError result."""
    if "error" in msg and msg["error"] is not None:
        err = msg["error"]
        return f"jsonrpc {err.get('code')}: {err.get('message')}"
    result = msg.get("result")
    if isinstance(result, dict) and result.get("isError"):
        return "tool error: " + (_content_text(result) or "(no detail)")
    return None


def _content_text(result: Any) -> str | None:
    """Concatenated text parts of an MCP tool result, for the trace summary."""
    if not isinstance(result, dict):
        return str(result) if result is not None else None
    parts = [
        item.get("text", "")
        for item in result.get("content", [])
        if isinstance(item, dict) and item.get("type") == "text"
    ]
    if parts:
        return "\n".join(parts)
    return json.dumps(result, default=str)


def _pump(
    source: BinaryIO,
    sink: BinaryIO,
    observe,
) -> None:
    """Forward newline-delimited messages, observing each; never drop bytes."""
    for line in iter(source.readline, b""):
        stripped = line.strip()
        if stripped:
            try:
                observe(json.loads(stripped))
            except Exception as exc:  # noqa: BLE001 — recording must never block traffic
                print(f"metaxu-mcp-proxy: recording error (ignored): {exc}", file=sys.stderr)
        try:
            sink.write(line)
            sink.flush()
        except (BrokenPipeError, ValueError):
            break
    try:
        sink.close()
    except Exception:  # noqa: BLE001
        pass


def run_proxy(
    server_command: list[str],
    out_dir: str,
    question: str | None = None,
    policy_file: str | None = None,
    tags_file: str | None = None,
    snapshots: bool = True,
) -> str:
    """Spawn the real server, proxy stdio, and write the artifact on exit.

    Returns the path of the written artifact.
    """
    import os
    import subprocess

    os.makedirs(out_dir, exist_ok=True)
    policy_engine = PolicyEngine.from_file(policy_file) if policy_file else None
    tag_map: dict[str, list[str]] = {}
    if tags_file:
        with open(tags_file, encoding="utf-8") as f:
            tag_map = json.load(f)

    proxy = MCPProxy(
        server_command=server_command,
        question=question,
        policy_engine=policy_engine,
        tag_map=tag_map,
        snapshot_dir=os.path.join(out_dir, "snapshots") if snapshots else None,
    )

    child = subprocess.Popen(
        server_command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=None,  # child inherits our stderr
    )
    try:
        to_server = threading.Thread(
            target=_pump,
            args=(sys.stdin.buffer, child.stdin, proxy.observe_client_message),
            daemon=True,
        )
        to_client = threading.Thread(
            target=_pump,
            args=(child.stdout, sys.stdout.buffer, proxy.observe_server_message),
            daemon=True,
        )
        to_server.start()
        to_client.start()
        to_client.join()  # server closing stdout ends the session
        child.wait()
    finally:
        artifact = proxy.finalize()
        path = os.path.join(out_dir, f"{artifact.id}.json")
        artifact.save(path)
        print(f"metaxu-mcp-proxy: assurance artifact written to {path}", file=sys.stderr)
    return path
